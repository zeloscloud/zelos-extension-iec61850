"""MMS acquisition into the station tree: one thread per configured server.

Rows land in the IEC 61850 model, beside the GOOSE control blocks:

    <LD>/<LN>/<DO>          one row per report or read that carried the data
                            object: its attributes as fields (`stVal`, `mag_f`,
                            `q`, `t`), then `src`, `rcb`, `rpt_sqnum`, `reason`
    <LD>/LLN0/RP|BR/<rcb>   one row per report received

A row holds only what its PDU carried: a data-change report leaves the
attributes it did not include empty, nothing is filled from an earlier value.
Row time is receive time; the IED's own `t` stays a field.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

import zelos_sdk

from . import mms
from .capture import ConfigError
from .mms import Member, MmsError, Report, ServerConfig, Session

logger = logging.getLogger(__name__)

#: Inclusion reasons as one bitmask, the values other 61850 stacks use.
REASON_BITS = {
    "data_change": 1,
    "quality_change": 2,
    "data_update": 4,
    "integrity": 8,
    "general_interrogation": 16,
    "application_trigger": 32,
}

_D = zelos_sdk.DataType
_TYPES = {
    "bool": _D.Boolean,
    "int": _D.Int64,
    "uint": _D.UInt64,
    "float32": _D.Float32,
    "float64": _D.Float64,
    "bits": _D.UInt32,  # Quality, Dbpos: the GOOSE decoder's type for them
    "bytes": _D.Binary,
    "str": _D.String,
    "time": _D.Int64,  # ns since the epoch, as the GOOSE `t`
}
#: After a data object's attributes: where the row came from.
_PROVENANCE = (
    ("src", _D.String),  # report | read
    ("rcb", _D.String),
    ("rpt_sqnum", _D.UInt32),
    ("reason", _D.UInt8),
)
_RCB_FIELDS = (
    ("rptID", _D.String),
    ("sqNum", _D.UInt32),
    ("timeOfEntry", _D.Int64),
    ("entryID", _D.String),
    ("confRev", _D.UInt32),
    ("bufOvfl", _D.Boolean),
    ("datSet", _D.String),
    ("n_included", _D.UInt32),
    ("reasons", _D.UInt8),
)
#: Read once per connection when the server has them: who the device is.
_IDENTITY = ("LLN0.NamPlt[DC]", "LPHD1.PhyNam[DC]")
_STOP_JOIN_S = 5.0


def parse_servers(config: dict) -> list[ServerConfig]:
    """The config's `servers`, validated. Raises ConfigError naming the entry."""
    servers: list[ServerConfig] = []
    for i, entry in enumerate(config.get("servers") or [], 1):
        where = f"Server {i}"
        try:
            server = ServerConfig(
                host=str(entry.get("host") or ""),
                port=int(entry.get("port") or mms.DEFAULT_PORT),
                label=str(entry.get("name") or "").strip(),
                reports=[r.strip() for r in entry.get("reports") or [] if r.strip()],
                reads=[
                    (r.get("ref", ""), r.get("interval_s", 1.0)) for r in entry.get("reads") or []
                ],
            )
        except (MmsError, TypeError, ValueError) as exc:
            raise ConfigError(f"{where}: {exc}") from exc
        if not server.reports and not server.reads:
            raise ConfigError(
                f"{where} ({server.host}): nothing to acquire. Add a report control block or "
                "a read; the 'Browse Server' action lists what the server has."
            )
        if any(s.label == server.label for s in servers):
            raise ConfigError(f"{where}: a server named {server.label!r} is already configured.")
        servers.append(server)
    return servers


def _split(leaf: str) -> tuple[str, str]:
    """``LD/LN.DO.DA...`` as its event (``LD/LN/DO``) and field (``DA_...``)."""
    ld, _, rest = leaf.partition("/")
    ln, do, *attrs = rest.split(".")
    return f"{ld}/{ln}/{do}", "_".join(attrs) or "value"


class Tree:
    """The station's events on one trace source, shared by its servers."""

    def __init__(self, source: Any) -> None:
        self.source = source
        self._lock = threading.Lock()
        self._owners: dict[str, str] = {}  # logical device -> server label
        self._fields: dict[tuple[str, str], tuple[str, str]] = {}  # (leaf, fc) -> (event, field)
        self._schemas: dict[str, tuple] = {}

    def register(self, label: str, members: Iterable[Member], rcbs: Iterable[str]) -> None:
        """Declare the data object events `members` fill and the `rcbs` events.

        A logical device belongs to the first server that brings it; a second
        server with the same name is refused, never merged.
        """
        events: dict[str, dict[str, Any]] = {}
        placed: dict[tuple[str, str], tuple[str, str]] = {}
        with self._lock:
            for member in members:
                ld = member.reference.partition("/")[0]
                if self._owners.setdefault(ld, label) != label:
                    raise MmsError(
                        f"Logical device {ld} is served by both {self._owners[ld]} and {label}. "
                        "Names must be unique in one station; remove one of the servers."
                    )
                for leaf, kind in member.leaves:
                    if (leaf, member.fc) in placed:  # a data set and a read may overlap
                        continue
                    event, field = _split(leaf)
                    fields = events.setdefault(event, {})
                    if field in fields or field in dict(_PROVENANCE):
                        field = f"{field}_{member.fc}"  # the same attribute under two FCs
                    fields[field] = _TYPES[kind]
                    placed[leaf, member.fc] = (event, field)
            self._fields.update(placed)
            for event, fields in events.items():
                self._declare(event, (*fields.items(), *_PROVENANCE))
            for rcb in rcbs:
                self._declare(rcb.replace("$", "/"), _RCB_FIELDS)

    def _declare(self, event: str, fields: tuple) -> None:
        known = self._schemas.get(event)
        if known is None:
            meta = [zelos_sdk.TraceEventFieldMetadata(name, dtype) for name, dtype in fields]
            self.source.add_event(event, meta)
            self._schemas[event] = fields
        elif known != fields:
            raise MmsError(
                f"{event} changed shape since the extension started (the IED's model or the "
                "data sets differ). Restart the extension to pick the new model up."
            )

    def log(self, time_ns: int, fc: str, values: mms.Values, extra: dict[str, Any]) -> int:
        """One row per data object among `values`; the rows written."""
        rows: dict[str, dict[str, Any]] = {}
        for leaf, value in values:
            event, field = self._fields[leaf, fc]
            if value is not None:
                rows.setdefault(event, {})[field] = value
        for event, row in rows.items():
            self.source.log_at(time_ns, event, {**row, **extra})
        return len(rows)

    def log_report(self, report: Report) -> int:
        """The report's row and its data objects' rows; the rows written."""
        rows: dict[str, dict[str, Any]] = {}
        reasons: dict[str, int] = {}
        for entry in report.entries:
            if entry.error:
                logger.debug("%s: %s: %s", report.rcb, entry.reference, entry.error)
                continue
            reason = sum(REASON_BITS.get(r, 0) for r in entry.reasons)
            for leaf, value in entry.values:
                event, field = self._fields[leaf, entry.fc]
                if value is not None:
                    rows.setdefault(event, {})[field] = value
                    reasons[event] = reasons.get(event, 0) | reason
        extra: dict[str, Any] = {"src": "report", "rcb": report.rcb}
        if report.seq_num is not None:
            extra["rpt_sqnum"] = report.seq_num
        for event, row in rows.items():
            self.source.log_at(report.rx_ns, event, {**row, **extra, "reason": reasons[event]})
        block = {
            "rptID": report.rpt_id,
            "sqNum": report.seq_num,
            "timeOfEntry": report.time_ns,
            "entryID": report.entry_id.hex() if report.entry_id is not None else None,
            "confRev": report.conf_rev,
            "bufOvfl": report.buf_ovfl,
            "datSet": report.data_set,
            "n_included": len(report.entries),
            "reasons": sum(
                REASON_BITS.get(r, 0) for r in {r for e in report.entries for r in e.reasons}
            ),
        }
        self.source.log_at(
            report.rx_ns,
            report.rcb.replace("$", "/"),
            {k: v for k, v in block.items() if v is not None},
        )
        return len(rows) + 1


class MmsStation:
    """One server's acquisition thread: reports and polled reads into a `Tree`."""

    def __init__(
        self,
        config: ServerConfig,
        tree: Tree,
        *,
        on_fatal: Callable[[], None] | None = None,
        **loop: Any,
    ) -> None:
        self.config = config
        self.tree = tree
        self.fatal: MmsError | None = None
        self._on_fatal = on_fatal
        self._loop = loop  # backoff, timeout, heartbeat overrides
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "stopped"
        self._blocks: list[str] = []
        self._counts = {"connects": 0, "reports": 0, "reads": 0, "rows": 0}
        self._last_error = ""
        self._last_rx = 0.0

    @property
    def name(self) -> str:
        return self.config.label

    def start(self) -> None:
        self._state = "connecting"
        self._thread = threading.Thread(target=self._run, name=f"mms-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(_STOP_JOIN_S)
            if self._thread.is_alive():
                logger.warning("%s: MMS thread still closing after %gs", self.name, _STOP_JOIN_S)
        if self.fatal is None:
            self._state = "stopped"

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "host": self.config.host,
            "port": self.config.port,
            "state": self._state,
            "report_blocks": list(self._blocks),
            **self._counts,
            "last_rx_age_s": round(time.monotonic() - self._last_rx, 3) if self._last_rx else None,
            "last_error": self._last_error,
        }

    # --- the acquisition thread -------------------------------------------------

    def _run(self) -> None:
        try:
            mms.run_with_reconnect(
                self.config,
                self._on_report,
                self._stop,
                on_connect=self._on_connect,
                on_read=self._on_read,
                on_lost=self._on_lost,
                **self._loop,
            )
        except MmsError as exc:
            self._fail(exc)
        except Exception as exc:  # a bug must stop the extension legibly, not a silent thread
            logger.exception("%s: MMS acquisition failed", self.name)
            self._fail(MmsError(f"{self.name}: MMS acquisition failed: {exc}"))

    def _fail(self, exc: MmsError) -> None:
        self.fatal, self._state, self._last_error = exc, "failed", str(exc)
        if self._on_fatal is not None:
            self._on_fatal()

    def _on_connect(self, session: Session) -> None:
        blocks = session.blocks
        polled = [session.member(ref) for ref, _ in self.config.reads]
        lds = {m.reference.partition("/")[0] for ms in (*blocks.values(), polled) for m in ms}
        identity = [
            found
            for ld in sorted(lds)
            for ref in _IDENTITY
            if (found := _identity(session, f"{ld}/{ref}"))
        ]
        members = [m for ms in blocks.values() for m in ms] + polled + [m for m, _ in identity]
        self.tree.register(self.name, members, blocks)
        now = time.time_ns()
        for member, values in identity:
            self._counts["rows"] += self.tree.log(now, member.fc, values, {"src": "read"})
        self._blocks = list(blocks)
        self._counts["connects"] += 1
        self._state, self._last_error = "connected", ""
        logger.info(
            "%s: acquiring %d report block%s, %d read%s",
            self.name,
            len(blocks),
            "s"[: len(blocks) != 1],
            len(polled),
            "s"[: len(polled) != 1],
        )

    def _on_report(self, report: Report) -> None:
        self._counts["reports"] += 1
        self._counts["rows"] += self.tree.log_report(report)
        self._last_rx = time.monotonic()

    def _on_read(self, member: Member, values: mms.Values, rx_ns: int) -> None:
        self._counts["reads"] += 1
        self._counts["rows"] += self.tree.log(rx_ns, member.fc, values, {"src": "read"})
        self._last_rx = time.monotonic()

    def _on_lost(self, error: MmsError) -> None:
        self._state, self._last_error = "reconnecting", str(error)


def _identity(session: Session, ref: str) -> tuple[Member, mms.Values] | None:
    """`ref` read once, or None when the server has no such object."""
    try:
        return session.read_values(ref)
    except MmsError as exc:
        if exc.retry:
            raise
        return None
