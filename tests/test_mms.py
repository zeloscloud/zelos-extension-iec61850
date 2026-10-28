"""The MMS seam against open61850's own server, in process on loopback.

`data/mms.cid`: logical device IED1LD0 with a float measurand and a boolean
status, a data set of both, and one URCB and one BRCB on it.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from open61850.server import IedModel, MmsServer

from zelos_extension_iec61850.mms import Report, ServerConfig, Session, run_with_reconnect

SCL = Path(__file__).parent / "data" / "mms.cid"
LD = "IED1LD0"
WATT = f"{LD}/MMDC1.Watt.mag.f"
IND = f"{LD}/GGIO1.Ind1.stVal"
URCB = f"{LD}/LLN0$RP$urcbMeas01"
BRCB = f"{LD}/LLN0$BR$brcbMeas01"
GI = ("general_interrogation",)


def serve(port: int = 0) -> MmsServer:
    """A fresh model each time: a restart is an IED reboot, not a reused state."""
    server = MmsServer(IedModel.from_scl(SCL), "127.0.0.1", port)
    server.start()
    return server


@pytest.fixture
def server() -> Iterator[MmsServer]:
    server = serve()
    yield server
    server.stop()


@pytest.fixture
def session(server: MmsServer) -> Iterator[Session]:
    with Session(ServerConfig("127.0.0.1", server.port), timeout=2) as s:
        yield s


def next_report(session: Session, timeout: float = 2.0) -> Report:
    stop = threading.Event()
    timer = threading.Timer(timeout, stop.set)
    timer.start()
    try:
        report = next(session.reports(stop), None)
    finally:
        timer.cancel()
    assert report is not None, f"no report within {timeout}s"
    return report


def entries(report: Report) -> list[tuple]:
    return [(e.reference, e.fc, e.value, e.reasons) for e in report.entries]


@pytest.fixture
def loop(
    server: MmsServer, caplog: pytest.LogCaptureFixture
) -> Iterator[tuple[queue.Queue[Report], Callable]]:
    """`run_with_reconnect` on a thread, stopped and joined (bounded) at the end."""
    caplog.set_level(logging.INFO, logger="zelos_extension_iec61850.mms")  # for `connects`
    got: queue.Queue[Report] = queue.Queue()
    stop = threading.Event()
    threads: list[threading.Thread] = []

    def start(config: ServerConfig, **kwargs) -> None:
        thread = threading.Thread(
            target=run_with_reconnect, args=(config, got.put, stop), kwargs=kwargs, daemon=True
        )
        thread.start()
        threads.append(thread)

    yield got, start
    stop.set()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive(), "run_with_reconnect did not stop within 3s"


def connects(caplog: pytest.LogCaptureFixture) -> int:
    return sum(r.getMessage().endswith(": connected") for r in caplog.records)


def test_discover_lists_the_device_and_references_with_fc(session: Session) -> None:
    model = session.discover()
    assert list(model) == [LD]
    assert {(WATT, "MX"), (IND, "ST")} <= set(model[LD])


def test_read_returns_the_float_and_its_fc(server: MmsServer, session: Session) -> None:
    server.model.set(f"{WATT}[MX]", 12.5)
    assert session.read(WATT) == (12.5, "MX")  # FC looked up
    assert session.read(f"{WATT}[MX]") == (12.5, "MX")


def test_dataset_members_in_order(session: Session) -> None:
    assert session.dataset_members(f"{LD}/LLN0.dsMeas") == [(WATT, "MX"), (IND, "ST")]


@pytest.mark.parametrize(
    ("ref", "rcb"), [(URCB, URCB), (f"{LD}/LLN0.brcbMeas01", BRCB)], ids=["urcb", "brcb"]
)
def test_report_names_member_value_and_reason(
    server: MmsServer, session: Session, caplog: pytest.LogCaptureFixture, ref: str, rcb: str
) -> None:
    assert session.enable_reports([ref], integrity_period=0) == [rcb]
    gi = next_report(session)
    assert gi.rcb == rcb
    assert entries(gi) == [(WATT, "MX", 0.0, GI), (IND, "ST", False, GI)]

    server.model.set(f"{IND}[ST]", True)
    assert entries(next_report(session)) == [(IND, "ST", True, ("data_change",))]

    session.close()  # releases without complaint
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_reconnects_after_a_server_restart(
    server: MmsServer, loop, caplog: pytest.LogCaptureFixture
) -> None:
    got, start = loop
    port = server.port
    config = ServerConfig("127.0.0.1", port, reports=[URCB, BRCB], integrity_period=0)
    start(config, backoff=(0.2,), timeout=1)
    gis = [got.get(timeout=3) for _ in range(2)]
    assert {r.rcb for r in gis} == {URCB, BRCB}

    server.stop()
    restarted = serve(port)
    try:
        gis = [got.get(timeout=3) for _ in range(2)]  # both blocks enabled again
        assert {r.rcb for r in gis} == {URCB, BRCB}
        assert all(e.reasons == GI for r in gis for e in r.entries)

        restarted.model.set(f"{WATT}[MX]", 7.5)
        changes = [got.get(timeout=2) for _ in range(2)]
        assert {r.rcb for r in changes} == {URCB, BRCB}
        assert all(entries(r) == [(WATT, "MX", 7.5, ("data_change",))] for r in changes)
        assert connects(caplog) == 2
    finally:
        restarted.stop()


def test_a_silent_server_is_noticed_and_reconnected(
    server: MmsServer, loop, caplog: pytest.LogCaptureFixture
) -> None:
    """open61850 sets no keepalive: a server that stops answering only shows
    through the heartbeat read timing out. Its stale association still holds
    the block until the server drops it, so the re-enable retries meanwhile."""
    hang = threading.Event()

    def stall_once(_connection, name):
        if hang.is_set() and name.item.endswith("$RptEna"):
            hang.clear()
            time.sleep(1.0)  # past the client's timeout
        return None

    server.read_hooks.append(stall_once)
    got, start = loop
    config = ServerConfig("127.0.0.1", server.port, reports=[URCB], integrity_period=0)
    start(config, backoff=(0.1,), timeout=0.3, heartbeat=0.2)
    assert entries(got.get(timeout=3))[0][3] == GI

    hang.set()
    assert entries(got.get(timeout=4))[0][3] == GI  # enabled again on a new association
    assert connects(caplog) >= 2
