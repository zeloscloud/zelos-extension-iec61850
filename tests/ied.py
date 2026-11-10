"""A scripted IED for functional tests: open61850's server on an SCL file.

Every `--period` seconds it steps the values named on the command line (and the
`t` beside each, when the data object has one) and appends the step to `--log`
as JSON lines: the truth a recorded trace is compared against.

    python tests/ied.py tests/data/mms.cid --port 10102 --log truth.jsonl \\
        --float 'IED1LD0/MMDC1.Watt.mag.f[MX]' --dbpos 'IED1LD0/XSWI1.Pos.stVal[ST]'
"""

from __future__ import annotations

import argparse
import json
import signal
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from open61850.server import IedModel, MmsServer


def _value(kind: str, step: int, index: int) -> float | bool | int:
    if kind == "float":
        return step * 1.5 + index * 1000
    if kind == "bool":
        return step % 2 == 1
    return 1 + step % 2  # Dbpos: off (1), on (2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scl", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--period", type=float, default=0.2)
    parser.add_argument("--steps", type=int, default=0, help="0: until stopped")
    parser.add_argument("--delay", type=float, default=0.0, help="before the first step")
    for kind in ("float", "bool", "dbpos"):
        parser.add_argument(f"--{kind}", action="append", default=[], metavar="REF[FC]")
    args = parser.parse_args()

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    model = IedModel.from_scl(args.scl)
    server = MmsServer(model, args.host, args.port)
    server.start()
    print(f"listening {server.port}", flush=True)
    targets = [(kind, ref) for kind in ("float", "bool", "dbpos") for ref in getattr(args, kind)]
    try:
        with args.log.open("a") as log:
            stop.wait(args.delay)
            step = 0
            while not stop.is_set() and (not args.steps or step < args.steps):
                step += 1
                for index, (kind, ref) in enumerate(targets):
                    value = _value(kind, step, index)
                    name, _, fc = ref.partition("[")
                    stamp = f"{'.'.join(name.split('.')[:2])}.t[{fc}"  # the data object's own t
                    with model.batch():  # one report entry for value and time
                        model.set(ref, value)
                        if model.node(stamp) is not None:
                            model.set(stamp, datetime.now(UTC))
                    record = {"step": step, "t_ns": time.time_ns(), "ref": name, "value": value}
                    log.write(json.dumps(record) + "\n")
                log.flush()
                stop.wait(args.period)
            print(f"done {step}", flush=True)
            stop.wait()  # keep serving until stopped
    finally:
        server.stop()


if __name__ == "__main__":
    main()
