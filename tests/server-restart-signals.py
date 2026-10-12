"""Verify interrupted daemon-restart tests remove every owned Docker resource."""

from __future__ import annotations

import signal
import subprocess
import sys
from collections import deque
from pathlib import Path


def verify_signal(image: str, interruption: signal.Signals) -> None:
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("server-restart.py")), image],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    interrupted = False
    cleaned = False
    tail: deque[str] = deque(maxlen=30)
    try:
        assert process.stdout is not None
        for line in process.stdout:
            tail.append(line.strip())
            if "isolated daemon ready" in line:
                process.send_signal(interruption)
                interrupted = True
            if "server restart cleanup:" in line and "remaining=[]" in line:
                cleaned = True
        code = process.wait()
        assert interrupted and cleaned and code == 128 + interruption, "\n".join(tail)
        print(f"restart test {interruption.name}: cleanup verified", flush=True)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()


if __name__ == "__main__":
    for interruption in (signal.SIGINT, signal.SIGTERM):
        verify_signal(sys.argv[1], interruption)
