"""Functional: the CLI as a process against a scripted IED as another, to a .trz.

The IED logs every value it sets; the recorded trace must carry the same
sequence. Same-stack (both ends are open61850): it proves the path from the
socket to the trace file, not wire interoperability.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pyarrow as pa
import zelos_sdk

REPO = Path(__file__).resolve().parents[1]
LD = "IED1LD0"
STEPS = 8


def _column(path: Path, field: str, *more: str) -> list[tuple]:
    """Rows of `<event>.<field>` (and sibling fields), in time order."""
    event = field.rsplit(".", 1)[0]
    fields = [field, *(f"{event}.{m}" for m in more)]
    with zelos_sdk.TraceReader(str(path)) as reader:
        window = reader.time_range()
        result = reader.query(
            [s.id for s in reader.list_data_segments()],
            [f"*/IEC61850/{f}" for f in fields],
            window.start,
            window.end,
        )
    table = pa.ipc.open_stream(result.to_arrow()).read_all()
    names = [next(c for c in table.column_names if c.endswith(f)) for f in fields]
    return list(zip(*(table.column(n).to_pylist() for n in names), strict=True))


def test_reported_and_read_values_match_the_ied_log(tmp_path: Path) -> None:
    truth_path, trace = tmp_path / "truth.jsonl", tmp_path / "out.trz"
    watt, pos, ind = f"{LD}/MMDC1.Watt.mag.f", f"{LD}/XSWI1.Pos.stVal", f"{LD}/GGIO1.Ind1.stVal"
    # A dead agent port: rows go to the file only, never to an agent on this machine.
    env = {**os.environ, "ZELOS_AGENT_URL": "http://127.0.0.1:9", "PYTHONUNBUFFERED": "1"}
    ied = subprocess.Popen(
        [sys.executable, "tests/ied.py", "tests/data/mms.cid", "--log", str(truth_path)]
        + ["--steps", str(STEPS), "--period", "0.15", "--delay", "1.5"]
        + ["--float", f"{watt}[MX]", "--dbpos", f"{pos}[ST]", "--bool", f"{ind}[ST]"],
        cwd=REPO,
        stdout=subprocess.PIPE,
        text=True,
    )
    cli = None
    try:
        port = ied.stdout.readline().split()[1]
        cli = subprocess.Popen(
            [sys.executable, "main.py", "mms", "127.0.0.1", "--port", port]
            + ["--report", f"{LD}/LLN0$RP$urcbObj01", "--read", f"{ind}[ST]=0.05"]
            + ["--file", str(trace)],
            cwd=REPO,
            env=env,
        )
        assert ied.stdout.readline().split() == ["done", str(STEPS)]
        time.sleep(0.5)  # the last report and read land
        cli.send_signal(signal.SIGINT)
        assert cli.wait(15) == 0
    finally:
        for process in (cli, ied):
            if process is not None and process.poll() is None:
                process.kill()

    truth: dict[str, list] = {}
    for line in truth_path.read_text().splitlines():
        record = json.loads(line)
        truth.setdefault(record["ref"], []).append(record["value"])

    # Reports: the general interrogation, then every change in order, none missed.
    rows = _column(trace, f"{LD}/MMDC1/Watt.mag_f", "reason", "rpt_sqnum", "t", "src")
    # Integrity reports (reason 8) repeat the current value in between.
    assert rows[0][1] == 16 and {r[4] for r in rows} == {"report"}
    changes = [r for r in rows if r[1] == 1]
    assert [r[0] for r in changes] == truth[watt]
    assert all(abs(r[3] - time.time_ns()) < 60e9 for r in changes)  # the IED's own t, as ns
    positions = _column(trace, f"{LD}/XSWI1/Pos.stVal", "reason")
    assert [r[0] for r in positions if r[1] == 1] == truth[pos]

    # One block row per report, sequence numbers without a gap.
    numbers = [r[0] for r in _column(trace, f"{LD}/LLN0/RP/urcbObj01.sqNum")]
    assert numbers == list(range(len(numbers))) and len(numbers) >= STEPS + 1

    # Polled reads at 50 ms see every 150 ms step, in order.
    reads = [r[0] for r in _column(trace, f"{LD}/GGIO1/Ind1.stVal", "src") if r[1] == "read"]
    seen = [v for i, v in enumerate(reads) if i == 0 or v != reads[i - 1]]
    assert seen == [False, *truth[ind]]
