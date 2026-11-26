"""MMS rows in the station tree, against open61850's own server in process.

Same-stack: client and server share one codec, so these pin the mapping from
reports and reads to rows, not wire interoperability.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
import zelos_sdk
from open61850.server import MmsServer

from zelos_extension_iec61850.capture import ConfigError
from zelos_extension_iec61850.mms import ServerConfig
from zelos_extension_iec61850.station import MmsStation, Tree, parse_servers

from .test_mms import IND, LD, URCB, serve

OBJ = f"{LD}/LLN0$RP$urcbObj01"
POS = f"{LD}/XSWI1/Pos"
PROVENANCE = ["src", "rcb", "rpt_sqnum", "reason"]


class Recorder:
    """A trace source that keeps what it is given."""

    def __init__(self) -> None:
        self.schemas: dict[str, list[str]] = {}
        self.rows: list[tuple[str, dict[str, Any]]] = []

    def add_event(self, name: str, fields: list) -> None:
        self.schemas[name] = [f.name for f in fields]

    def log_at(self, time_ns: int, name: str, row: dict[str, Any]) -> None:
        assert set(row) <= set(self.schemas[name]), f"{name}: undeclared field in {row}"
        self.rows.append((name, row))

    def of(self, name: str) -> list[dict[str, Any]]:
        return [row for event, row in self.rows if event == name]

    def wait(self, name: str, count: int, timeout: float = 3.0) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while len(self.of(name)) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(self.of(name)) >= count, f"{name}: {self.of(name)}"
        return self.of(name)


@pytest.fixture
def server() -> Iterator[MmsServer]:
    server = serve()
    yield server
    server.stop()


@pytest.fixture
def station() -> Iterator[Callable[..., MmsStation]]:
    started: list[MmsStation] = []

    def start(tree: Tree, port: int, **config: Any) -> MmsStation:
        fatal = threading.Event()
        s = MmsStation(
            ServerConfig("127.0.0.1", port, integrity_period=0, **config),
            tree,
            on_fatal=fatal.set,
            timeout=2,
            backoff=(0.2,),
        )
        s.fatal_event = fatal
        s.start()
        started.append(s)
        return s

    yield start
    for s in started:
        s.stop()


def test_a_report_becomes_one_row_per_data_object_and_one_for_the_block(server, station) -> None:
    out = Recorder()
    s = station(Tree(out), server.port, reports=[OBJ], reads=[(f"{IND}[ST]", 0.1)])

    gi = out.wait(POS, 1)[0]
    assert out.schemas[POS] == ["stVal", "q", "t", *PROVENANCE]
    assert gi == {
        "stVal": 0,
        "q": 0,
        "t": 0,
        "src": "report",
        "rcb": OBJ,
        "rpt_sqnum": 0,
        "reason": 16,
    }

    server.model.set(f"{LD}/XSWI1.Pos.stVal[ST]", 2)
    change = out.wait(POS, 2)[1]
    assert (change["stVal"], change["reason"], change["rpt_sqnum"]) == (2, 1, 1)
    assert len(out.of(f"{LD}/MMDC1/Watt")) == 1  # not in the second report: no row

    block = out.wait(f"{LD}/LLN0/RP/urcbObj01", 2)
    assert [(b["sqNum"], b["n_included"], b["reasons"]) for b in block] == [(0, 2, 16), (1, 1, 1)]
    assert block[0]["datSet"] == f"{LD}/LLN0$dsObj"

    assert out.wait(f"{LD}/GGIO1/Ind1", 2)[0] == {"stVal": False, "src": "read"}
    assert out.of(f"{LD}/LLN0/NamPlt") == [{"vendor": "", "swRev": "", "src": "read"}]
    status = s.status()
    assert (status["state"], status["report_blocks"], status["connects"]) == ("connected", [OBJ], 1)


def test_a_row_holds_only_what_the_report_carried(server, station) -> None:
    out = Recorder()
    station(Tree(out), server.port, reports=[URCB])
    out.wait(f"{LD}/MMDC1/Watt", 1)
    assert out.schemas[f"{LD}/MMDC1/Watt"] == [
        "mag_f",
        *PROVENANCE,
    ]  # the data set's attribute only

    server.model.set(f"{IND}[ST]", True)
    assert out.wait(f"{LD}/GGIO1/Ind1", 2)[1]["stVal"] is True
    assert len(out.of(f"{LD}/MMDC1/Watt")) == 1


def test_a_block_the_server_lacks_stops_the_station_with_the_reason(server, station) -> None:
    s = station(Tree(Recorder()), server.port, reports=[f"{LD}/LLN0$RP$nope01"])
    assert s.fatal_event.wait(3)
    assert "no report control block" in str(s.fatal)
    assert s.status()["state"] == "failed"


def test_two_servers_with_the_same_logical_device_are_refused(server, station) -> None:
    other = serve()
    try:
        tree = Tree(out := Recorder())
        station(tree, server.port, label="a", reports=[OBJ])
        out.wait(POS, 1)
        second = station(tree, other.port, label="b", reports=[OBJ])
        assert second.fatal_event.wait(3)
        assert "served by both a and b" in str(second.fatal)
    finally:
        other.stop()


def test_rows_reach_a_trace_file_through_the_sdk(server, station, tmp_path) -> None:
    """The declared types take the values: nothing is refused on the way to disk."""
    namespace = zelos_sdk.TraceNamespace("station-test")
    path = tmp_path / "station.trz"
    with zelos_sdk.TraceWriter(str(path), namespace=namespace):
        source = zelos_sdk.TraceSource("IEC61850", namespace=namespace)
        s = station(Tree(source), server.port, reports=[OBJ], reads=[(f"{IND}[ST]", 0.1)])
        deadline = time.monotonic() + 3
        while s.status()["rows"] < 6 and time.monotonic() < deadline:
            time.sleep(0.02)
        s.stop()
        source.flush()
        namespace.drain()
    with zelos_sdk.TraceReader(str(path)) as reader:
        events = {e.name for src in reader.list_fields() for e in src.events}
    assert {POS, f"{LD}/MMDC1/Watt", f"{LD}/LLN0/RP/urcbObj01", f"{LD}/GGIO1/Ind1"} <= events


@pytest.mark.parametrize(
    ("servers", "message"),
    [
        ([{"host": ""}], "needs a 'host'"),
        ([{"host": "10.0.0.1"}], "nothing to acquire"),
        ([{"host": "10.0.0.1", "reports": ["LD0/MMXU1.TotW.mag.f"]}], "not a report control block"),
        ([{"host": "h", "reports": ["L/LLN0$RP$a"]}] * 2, "already configured"),
    ],
)
def test_bad_server_config_is_refused_by_entry(servers: list, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_servers({"servers": servers})
