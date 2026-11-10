"""The one seam onto open61850's MMS client.

Association, discovery, reads, data sets and report control blocks for one
IEC 61850 server, plus a reconnecting report loop. Nothing else in the
extension imports `open61850`: values leave as plain Python, references as
``(reference, fc)`` pairs such as ``("IED1LD0/MMXU1.TotW.mag.f", "MX")``, and
failures as :class:`MmsError`.

open61850 delivers reports on its receive thread. `Session` decodes them there
and hands them over a queue, so consumers run on their own thread. The queue is
unbounded: blocking the receive thread would also stall the responses a
consumer may be waiting on, and reports are event-rate traffic.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from open61850.data import ArrayData, BitStringData, BoolData, StructureData, TimestampData
from open61850.mms import (
    AssociationError,
    DataAccessError,
    InformationReport,
    MmsClient,
    MmsConnectionError,
    MmsTimeout,
    ObjectName,
    OptFlds,
    ReasonCode,
    Reference,
    ServerModel,
    ServiceError,
    TrgOps,
    decode_report,
    is_report,
)
from open61850.mms import MmsError as _LibError
from open61850.mms import Report as _LibReport
from open61850.mms import discover as _discover
from open61850.mms import rcb as _rcb
from open61850.mms.types import ArrayType, MmsType, PrimitiveType, StructureType

logger = logging.getLogger(__name__)

DEFAULT_PORT = 102
#: Connect and per-request timeout, seconds.
DEFAULT_TIMEOUT_S = 5.0
#: Integrity report period, seconds; open61850's own default.
INTEGRITY_PERIOD_S = 2.0
#: How often `run_with_reconnect` reads back its blocks' RptEna, seconds.
HEARTBEAT_S = 5.0
#: Report queue poll: the latency of a stop or a lost connection being noticed.
POLL_S = 0.2
#: Per-request bound on the release writes in `Session.close`.
RELEASE_TIMEOUT_S = 1.0

TRG_OPS = TrgOps(data_change=True, quality_change=True, integrity=True, general_interrogation=True)
OPT_FLDS = OptFlds(
    sequence_number=True,
    report_time_stamp=True,
    reason_for_inclusion=True,
    data_set_name=True,
    buffer_overflow=True,
    entry_id=True,
    conf_revision=True,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_T = TypeVar("_T")

#: ``(reference, fc)``: ``("IED1LD0/MMXU1.TotW.mag.f", "MX")``.
Ref = tuple[str, str]
#: ``(leaf reference, value)`` pairs of one member, in the member's leaf order.
Values = tuple[tuple[str, Any], ...]

# MMS primitive -> leaf kind; a float's width and a bit-string's length refine it.
_KINDS = {
    "boolean": "bool",
    "integer": "int",
    "unsigned": "uint",
    "bcd": "uint",
    "octet-string": "bytes",
    "visible-string": "str",
    "mms-string": "str",
    "utc-time": "time",
    "binary-time": "time",
}


class MmsError(RuntimeError):
    """An MMS operation failed; the message says what to check.

    `retry` marks a failure a reconnect may cure (link down, timeout, a block
    held by another association) as opposed to a configuration error.
    """

    def __init__(self, message: str, *, retry: bool = False) -> None:
        super().__init__(message)
        self.retry = retry


@dataclass
class ServerConfig:
    """One IEC 61850 server. Validated on construction."""

    host: str
    port: int = DEFAULT_PORT
    label: str = ""  # defaults to the host, dots as underscores
    reports: list[str] = field(default_factory=list)  # LD/LLN0$BR$name or LD/LLN0.name
    reads: list[tuple[str, float]] = field(default_factory=list)  # (reference, interval s)
    integrity_period: float = INTEGRITY_PERIOD_S  # seconds, 0 for none

    def __post_init__(self) -> None:
        self.host = self.host.strip()
        if not self.host:
            raise MmsError("An MMS server needs a 'host'.")
        self.label = self.label or self.host.replace(".", "_")
        for ref in self.reports:
            _rcb_names(ref)
        self.reads = [(ref, float(interval)) for ref, interval in self.reads]
        for ref, interval in self.reads:
            _parse(ref)
            if interval <= 0:
                raise MmsError(f"Read interval for {ref} must be positive, got {interval:g}.")
        if self.integrity_period < 0:
            raise MmsError(f"integrity_period must be 0 or more, got {self.integrity_period:g}.")


@dataclass(frozen=True)
class Member:
    """A data set member or a read target with its leaves, typed by the server.

    A leaf is ``(reference, kind)``: the member's own reference when it is a
    data attribute, ``<member>.<component>...`` (array elements by index) when
    it is a structure. Kinds: bool, int, uint, float32, float64, bits (a
    bit-string of up to 32 bits, as its integer), bytes, str, time (ns).
    """

    reference: str
    fc: str
    leaves: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ReportEntry:
    """One data set member in a report."""

    reference: str
    fc: str
    value: Any  # plain Python; None when the server sent an access error
    reasons: tuple[str, ...]  # ReasonCode fields set: data_change, integrity, ...
    error: str | None = None
    values: Values = ()  # the member's leaves; empty on an access error


@dataclass(frozen=True)
class Report:
    """One decoded report. Optional fields are None when OptFlds leaves them out."""

    rcb: str  # LD/LLN0$BR$name
    rpt_id: str
    rx_ns: int  # when it arrived, ns since the epoch
    seq_num: int | None
    time_ns: int | None  # TimeOfEntry
    data_set: str | None
    conf_rev: int | None
    buf_ovfl: bool | None
    entry_id: bytes | None
    entries: tuple[ReportEntry, ...]


@dataclass(frozen=True)
class _Block:
    """An enabled RCB and its data set, for naming report entries."""

    name: ObjectName
    members: tuple[Member, ...]
    types: tuple[MmsType, ...]

    @property
    def text(self) -> str:
        return f"{self.name.domain}/{self.name.item}"

    def convert(self, report: _LibReport, rx_ns: int) -> Report:
        entries = []
        for e in report.entries:
            # OptFlds data-reference is off: the inclusion index is the data set position.
            member = self.members[e.index]
            error = str(e.value) if isinstance(e.value, DataAccessError) else None
            value = None if error else _plain(e.value)
            values = () if error else _values(member, self.types[e.index], e.value)
            entries.append(
                ReportEntry(member.reference, member.fc, value, _reasons(e.reason), error, values)
            )
        return Report(
            rcb=self.text,
            rpt_id=report.rpt_id,
            rx_ns=rx_ns,
            seq_num=report.seq_num,
            time_ns=_ns(report.time_of_entry) if report.time_of_entry else None,
            data_set=report.data_set,
            conf_rev=report.conf_rev,
            buf_ovfl=report.buf_ovfl,
            entry_id=report.entry_id,
            entries=tuple(entries),
        )


class Session:
    """One association with one server, driven from one thread."""

    def __init__(self, config: ServerConfig, *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.config = config
        self.timeout = timeout
        self._client: MmsClient | None = None
        self._model = ServerModel()  # logical devices discovered so far, for FC lookup
        # By RptID. The receive thread reads it, so a block goes in before it is enabled.
        self._blocks: dict[str, _Block] = {}
        self._queue: queue.Queue[Report] = queue.Queue()
        self._types: dict[Ref, tuple[Member, MmsType]] = {}

    def __enter__(self) -> Session:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def _where(self) -> str:
        return f"{self.config.label} ({self.config.host}:{self.config.port})"

    def connect(self) -> None:
        """Open TCP, COTP and the MMS association; blocks up to `timeout`."""
        if self._client is not None:
            return
        c = self.config
        try:
            self._client = MmsClient.connect(
                c.host, c.port, timeout=self.timeout, on_information_report=self._on_message
            )
        except (_LibError, OSError) as exc:
            raise self._error(exc, "connecting") from exc
        logger.info("%s: connected", self._where)

    def discover(self) -> dict[str, list[Ref]]:
        """Every logical device with its data attribute references, in server order."""
        model = self._call("discovering the data model", _discover)
        self._model = model
        return {
            ld: [_ref(r) for node in device.logical_nodes.values() for r in node.references()]
            for ld, device in model.logical_devices.items()
        }

    def read(self, ref: str) -> tuple[Any, str]:
        """One value as plain Python, with the FC read. A `ref` without FC
        (``LD/LN.DO.DA``) has it looked up in its logical device's model."""
        reference = self._resolve(ref)
        data = self._call(f"reading {ref}", lambda c: c.read(reference))
        return _plain(data), reference.fc or ""

    def member(self, ref: str) -> Member:
        """`ref` with its leaves, from the server's type for it (one request, kept)."""
        return self._typed(_ref(self._resolve(ref)))[0]

    def read_values(self, ref: str) -> tuple[Member, Values]:
        """One read of `ref`, a data object or attribute, as its leaves."""
        member, mms_type = self._typed(_ref(self._resolve(ref)))
        name = f"{member.reference}[{member.fc}]"
        data = self._call(f"reading {ref}", lambda c: c.read(name))
        try:
            return member, _values(member, mms_type, data)
        except ValueError as exc:
            raise MmsError(f"{self._where}: {ref} does not match its type: {exc}.") from None

    @property
    def blocks(self) -> dict[str, tuple[Member, ...]]:
        """Every enabled block's data set members, by the block's MMS reference."""
        return {b.text: b.members for b in self._blocks.values()}

    def dataset_members(self, ref: str) -> list[Ref]:
        """A data set's members in order; `ref` is ``LD/LLN0.name`` or ``LD/LLN0$name``."""
        return self._call(
            f"reading data set {ref}", lambda c: [_ref(n) for n in c.get_data_set_members(ref)]
        )

    def enable_reports(
        self, refs: Iterable[str], *, integrity_period: float = INTEGRITY_PERIOD_S
    ) -> list[str]:
        """Reserve and enable report control blocks; their MMS references, in order.

        Triggers: data change, quality change, integrity every `integrity_period`
        seconds (0 for none) and general interrogation, with a GI once enabled
        so the first report carries every member. Blocks enabled before a
        failure stay enabled until `close()`.
        """
        return [self._enable(ref, integrity_period) for ref in refs]

    def reports(self, stop: threading.Event | None = None) -> Iterator[Report]:
        """Reports in arrival order, on the calling thread, until `stop` is set
        or the session closes. Raises MmsError when the connection is lost."""
        while self._client is not None and not (stop is not None and stop.is_set()):
            report = self._next(POLL_S)
            if report is not None:
                yield report

    def close(self) -> None:
        """Disable and release the blocks this session enabled, then drop the
        association. Best effort and bounded: each release write waits at most
        `RELEASE_TIMEOUT_S`, and a dead link stops them (the server frees a lost
        client's blocks itself)."""
        client, self._client = self._client, None
        if client is None:
            return
        blocks, self._blocks = list(self._blocks.values()), {}
        client.request_timeout = min(client.request_timeout, RELEASE_TIMEOUT_S)
        for block in blocks:
            if not client.is_connected:
                break
            try:
                _rcb.disable(client, block.name)
            except (MmsConnectionError, MmsTimeout, OSError) as exc:
                logger.info("%s: %s not released: %s", self._where, block.text, exc)
                break
            except _LibError as exc:
                logger.warning("%s: %s not released: %s", self._where, block.text, exc)
        client.close()
        logger.info("%s: closed", self._where)

    # --- internals --------------------------------------------------------------

    def _enable(self, ref: str, integrity_period: float) -> str:
        found = []
        for name in _rcb_names(ref):
            status = self._call(f"reading {ref}", lambda c, n=name: _rcb.read_status(c, n))
            if status.rpt_ena is not None:  # an absent block reads as all-None, not an error
                found.append(status)
        if not found:
            raise MmsError(
                f"{self._where}: no report control block {ref}. Instances are often "
                "numbered (urcbMeas01); discover() lists the logical nodes."
            )
        if len(found) > 1:
            raise MmsError(
                f"{self._where}: {ref} names both a BRCB and a URCB. "
                "Write LD/LLN0$BR$name or LD/LLN0$RP$name."
            )
        status = found[0]
        block_name = status.rcb
        text = f"{block_name.domain}/{block_name.item}"
        assert self._client is not None
        if not (status.free or status.available_to(self._client.local_address)):
            raise MmsError(
                f"{self._where}: {text} is in use ({status.describe()}). Free it on the "
                "IED or use another instance of the block.",
                retry=True,
            )
        if not status.dat_set:
            raise MmsError(f"{self._where}: {text} has no data set (DatSet is empty).")
        typed = [self._typed(m) for m in self.dataset_members(status.dat_set)]
        members = tuple(m for m, _ in typed)
        rpt_id = status.rpt_id or text  # 8-1: an empty RptID reports as the block's reference
        if (other := self._blocks.get(rpt_id)) is not None:
            raise MmsError(
                f"{self._where}: {text} and {other.text} share RptID {rpt_id!r}, so their "
                "reports cannot be told apart. Enable one of them."
            )
        self._blocks[rpt_id] = _Block(block_name, members, tuple(t for _, t in typed))
        settings = _rcb.RcbSettings(
            trg_ops=TRG_OPS, opt_flds=OPT_FLDS, intg_pd_ms=round(integrity_period * 1000)
        )
        try:
            self._call(f"enabling {text}", lambda c: _rcb.enable(c, block_name, settings))
        except MmsError:
            # A partial reservation lapses with the association (a BRCB after ResvTms).
            del self._blocks[rpt_id]
            raise
        logger.info(
            "%s: enabled %s (RptID %s, %d members)", self._where, text, rpt_id, len(members)
        )
        return text

    def _typed(self, ref: Ref) -> tuple[Member, MmsType]:
        if ref not in self._types:
            reference, fc = ref
            name = f"{reference}[{fc}]"
            mms_type = self._call(f"reading the type of {name}", lambda c: c.get_type(name))
            leaves = tuple((".".join((reference, *path)), kind) for path, kind in _leaves(mms_type))
            self._types[ref] = Member(reference, fc, leaves), mms_type
        return self._types[ref]

    def _resolve(self, ref: str) -> Reference:
        """`ref` with its FC, looked up in the model when it has none."""
        parsed = _parse(ref)
        if parsed.fc is not None:
            return parsed
        if parsed.ld not in self._model.logical_devices:
            found = self._call(f"discovering {parsed.ld}", lambda c: _discover(c, [parsed.ld]))
            self._model.logical_devices.update(found.logical_devices)
        try:
            return self._model.resolve(parsed)
        except KeyError:
            raise MmsError(
                f"{self._where}: no {ref} in the server's model; discover() lists the references."
            ) from None
        except ValueError as exc:  # under several FCs
            raise MmsError(f"{self._where}: {exc}. Name one, as {ref}[ST].") from None

    def _on_message(self, message: InformationReport) -> None:
        # open61850's receive thread. Never raise: an exception here ends that
        # thread without closing the client, which then reads as connected.
        try:
            if not is_report(message):
                return
            report = decode_report(message)
            block = self._blocks.get(report.rpt_id)
            if block is None:
                logger.debug("%s: report for unknown RptID %r", self._where, report.rpt_id)
                return
            self._queue.put(block.convert(report, time.time_ns()))
        except Exception:
            logger.exception("%s: dropped a report that does not decode", self._where)

    def _next(self, timeout: float) -> Report | None:
        """The next report, or None after `timeout`. Raises MmsError once the
        connection has ended and every report it carried has been taken."""
        try:
            return self._queue.get(timeout=timeout) if timeout > 0 else self._queue.get_nowait()
        except queue.Empty:
            pass
        client = self._client
        if client is None:
            raise MmsError(f"{self._where}: session closed.")
        if client.is_connected:
            return None
        try:
            return self._queue.get_nowait()  # queued before the close was seen
        except queue.Empty:
            raise MmsError(
                f"{self._where}: connection lost ({client.wait_closed(0)}).", retry=True
            ) from None

    def _check(self) -> None:
        """Read back every enabled block's RptEna. open61850 sets no TCP keepalive,
        so a server gone without closing (power cut, cable) only shows as a
        request timing out; a block found disabled counts as lost too."""
        blocks = list(self._blocks.values())
        if not blocks:
            return
        names = [ObjectName(f"{b.name.item}$RptEna", b.name.domain) for b in blocks]
        values = self._call("checking report control blocks", lambda c: c.read_many(names))
        for block, value in zip(blocks, values, strict=True):
            if value != BoolData(True):
                raise MmsError(f"{self._where}: {block.text} is no longer enabled.", retry=True)

    def _call(self, doing: str, fn: Callable[[MmsClient], _T]) -> _T:
        if self._client is None:
            raise MmsError(f"{self._where}: not connected ({doing}).")
        try:
            return fn(self._client)
        except (_LibError, OSError, ValueError) as exc:
            raise self._error(exc, doing) from exc

    def _error(self, exc: BaseException, doing: str) -> MmsError:
        where = self._where
        if isinstance(exc, AssociationError):
            return MmsError(
                f"{where} refused the MMS association: {exc}. Check that it is an IEC 61850 "
                "server with a client connection free.",
                retry=True,
            )
        if isinstance(exc, MmsTimeout):
            return MmsError(
                f"{where}: no answer {doing} within {self.timeout:g}s. Check that the IED "
                "is up and reachable from this machine.",
                retry=True,
            )
        if isinstance(exc, MmsConnectionError | OSError):
            return MmsError(
                f"{where}: {doing}: {exc}. Check that the IED is up and reachable from this "
                f"machine, MMS is enabled on it, and nothing blocks TCP {self.config.port}.",
                retry=True,
            )
        if isinstance(exc, _rcb.RcbError):
            if exc.cause.code == 2:  # temporarily-unavailable: another client holds it
                return MmsError(f"{where}: {doing}: {exc}. Another client holds it.", retry=True)
            return MmsError(
                f"{where}: {doing}: the IED refused {exc.attribute} ({exc.cause}). Its "
                "report settings may be fixed by its configuration (SCL ReportSettings)."
            )
        if isinstance(exc, DataAccessError | ServiceError):
            return MmsError(
                f"{where}: {doing}: {exc}. Check the reference and its FC; discover() lists them."
            )
        return MmsError(f"{where}: {doing}: {exc}")


def run_with_reconnect(
    config: ServerConfig,
    on_report: Callable[[Report], None],
    stop_event: threading.Event,
    backoff: Sequence[float] = (1, 2, 5, 10),
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    heartbeat: float = HEARTBEAT_S,
    on_connect: Callable[[Session], None] | None = None,
    on_read: Callable[[Member, Values, int], None] | None = None,
    on_lost: Callable[[MmsError], None] | None = None,
) -> None:
    """Connect, enable ``config.reports`` and call `on_report` with each report,
    on this thread, until `stop_event` is set.

    `on_connect` runs once the blocks are enabled, before any report is
    delivered. ``config.reads`` are then read every interval, the first at
    once, each going to `on_read` with its receive time. `on_lost` hears the
    error behind every reconnect.

    A lost connection (closed, a request timing out, or a block the `heartbeat`
    read-back finds disabled) is retried after the next `backoff` delay, the
    last repeating, and the blocks enabled again; the delays start over once
    they are. A configuration error raises MmsError, as does nothing a retry
    would fix. An exception from `on_report` ends the loop.
    """
    attempt = 0
    while not stop_event.is_set():
        session = Session(config, timeout=timeout)
        error: MmsError | None = None
        try:
            session.connect()
            session.enable_reports(config.reports, integrity_period=config.integrity_period)
            polls = [[0.0, ref, interval] for ref, interval in config.reads]
            for _, ref, _ in polls:
                session.member(ref)  # a bad reference fails before anything is emitted
            if on_connect is not None:
                on_connect(session)
            attempt = 0
            due = time.monotonic() + heartbeat
            while not stop_event.is_set():
                # Wake for the next read due: a read interval may be shorter than POLL_S.
                wait = min([POLL_S, *(p[0] - time.monotonic() for p in polls)])
                report = session._next(max(wait, 0.0))
                if report is not None:
                    on_report(report)
                now = time.monotonic()
                for poll in polls:
                    if now >= poll[0]:
                        member, values = session.read_values(poll[1])
                        if on_read is not None:
                            on_read(member, values, time.time_ns())
                        poll[0] = now + poll[2]
                if now >= due:
                    session._check()
                    due = time.monotonic() + heartbeat
        except MmsError as exc:
            if not exc.retry:
                raise
            error = exc
        finally:
            session.close()
        if error is None:
            return
        if on_lost is not None:
            on_lost(error)
        delay = backoff[min(attempt, len(backoff) - 1)]
        attempt += 1
        logger.warning("%s Reconnecting in %gs.", error, delay)
        stop_event.wait(delay)


def browse(host: str, port: int = DEFAULT_PORT, *, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """What a server offers, read over one short association: each logical
    device's data sets (with their member count) and report control blocks
    (data set, whether enabled, who holds it). Nothing is written."""
    out: dict[str, dict[str, list]] = {}
    with Session(ServerConfig(host, port), timeout=timeout) as session:
        model = session._call("discovering the data model", _discover)
        for node in model.logical_nodes():
            device = out.setdefault(node.ld, {"data_sets": [], "report_blocks": []})
            for name in node.data_sets:
                ref = f"{node.ld}/{node.name}${name}"
                device["data_sets"].append(
                    {"ref": ref, "members": len(session.dataset_members(ref))}
                )
            for block in (*node.control_blocks("BR"), *node.control_blocks("RP")):
                status = session._call(
                    f"reading {block.name}", lambda c, b=block: _rcb.read_status(c, b.object_name)
                )
                device["report_blocks"].append(
                    {
                        "ref": f"{block.ld}/{block.object_name.item}",
                        "buffered": block.fc == "BR",
                        "data_set": status.dat_set or "",
                        "enabled": bool(status.rpt_ena),
                        "free": status.free,
                        "state": status.describe(),
                    }
                )
    return out


def _rcb_names(ref: str) -> list[ObjectName]:
    """The MMS names `ref` may mean: one, or the BR and RP readings of ``LD/LN.name``."""
    parsed = _parse(ref)
    if len(parsed.path) == 1 and parsed.fc in ("BR", "RP"):
        return [parsed.object_name]
    if len(parsed.path) == 1 and parsed.fc is None:
        return [parsed.with_fc(fc).object_name for fc in ("BR", "RP")]
    raise MmsError(
        f"{ref!r} is not a report control block reference. Write LD/LLN0$BR$name or LD/LLN0.name."
    )


def _parse(ref: str) -> Reference:
    try:
        return Reference.parse(ref)
    except ValueError as exc:
        raise MmsError(
            f"{ref!r} is not a reference ({exc}). Write LD/LN.DO.DA, LD/LN.DO.DA[FC] or "
            "LD/LN$FC$DO$DA."
        ) from None


def _ref(name: ObjectName | Reference) -> Ref:
    r = name if isinstance(name, Reference) else Reference.from_object_name(name)
    return r.name, r.fc or ""


def _reasons(reason: ReasonCode | None) -> tuple[str, ...]:
    return tuple(k for k, v in asdict(reason).items() if v) if reason is not None else ()


def _ns(t: datetime) -> int:
    return (t - _EPOCH) // timedelta(microseconds=1) * 1000


def _kind(mms_type: PrimitiveType) -> str:
    size = mms_type.size
    if mms_type.kind == "float":
        return "float64" if size == 64 else "float32"
    if mms_type.kind == "bit-string":
        return "bits" if size is not None and abs(size) <= 32 else "bytes"
    return _KINDS.get(mms_type.kind, "str")


def _leaves(mms_type: MmsType, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str]]:
    """Every primitive below `mms_type` as ``(path, kind)``, in wire order."""
    if isinstance(mms_type, StructureType):
        return [leaf for name, t in mms_type.components for leaf in _leaves(t, (*path, name))]
    if isinstance(mms_type, ArrayType):
        element = mms_type.element
        return [leaf for i in range(mms_type.count) for leaf in _leaves(element, (*path, str(i)))]
    if not isinstance(mms_type, PrimitiveType):
        raise ValueError(f"{'.'.join(path) or 'it'} has a named type ({mms_type})")
    return [(path, _kind(mms_type))]


def _flatten(data: Any, mms_type: MmsType) -> list[Any]:
    """`data`'s leaves as plain values, in `_leaves` order. A shape other than
    the type's raises ValueError: positions would name the wrong leaves."""
    if isinstance(mms_type, StructureType):
        parts, types = getattr(data, "members", None), [t for _, t in mms_type.components]
    elif isinstance(mms_type, ArrayType):
        parts, types = getattr(data, "elements", None), [mms_type.element] * mms_type.count
    elif isinstance(data, StructureData | ArrayData) or not isinstance(mms_type, PrimitiveType):
        raise ValueError("a structure where the type has a primitive")
    else:
        return [_leaf(data, _kind(mms_type))]
    if parts is None or len(parts) != len(types):
        raise ValueError(f"{type(data).__name__} where the type has {len(types)} components")
    return [v for part, t in zip(parts, types, strict=True) for v in _flatten(part, t)]


def _leaf(data: Any, kind: str) -> Any:
    if kind == "bits":
        return int.from_bytes(data.value, "big") >> data.unused_bits
    if kind == "time":
        return _ns(data.value)
    return str(data.value) if kind == "str" else data.value


def _values(member: Member, mms_type: MmsType, data: Any) -> Values:
    flat = _flatten(data, mms_type)
    return tuple((ref, v) for (ref, _), v in zip(member.leaves, flat, strict=True))


def _plain(data: Any) -> Any:
    """An open61850 value in the GOOSE decoder's shapes: bit-strings of up to
    32 bits as their integer (Dbpos, Quality), longer ones as bytes; times as
    ns since the epoch; structures and arrays as lists."""
    if isinstance(data, BitStringData):
        bits = len(data.value) * 8 - data.unused_bits
        return int.from_bytes(data.value, "big") >> data.unused_bits if bits <= 32 else data.value
    if isinstance(data, TimestampData):
        return _ns(data.value)
    if isinstance(data, StructureData):
        return [_plain(m) for m in data.members]
    if isinstance(data, ArrayData):
        return [_plain(e) for e in data.elements]
    return data.value  # bool, int, float, str, bytes
