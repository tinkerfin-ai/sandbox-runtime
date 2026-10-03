"""Verify owned container cleanup when the image test receives a signal."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
from pathlib import Path


def verify(script: Path, image: str, environment_key: str, prefix: str) -> None:
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        environment = {**os.environ, environment_key: "1"}
        process = subprocess.Popen(
            ["bash", str(script), image],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        signalled = False
        cleaned = False
        try:
            for line in process.stdout:
                print(line, end="", flush=True)
                if line.strip() == f"{prefix} signal-ready":
                    process.send_signal(stop_signal)
                    signalled = True
                if line.startswith(f"{prefix} cleanup:") and "remaining=[]" in line:
                    cleaned = True
            exit_code = process.wait(timeout=120)
            assert signalled, "image test did not reach its controlled wait"
            assert cleaned, "image test did not verify owned cleanup"
            assert exit_code == 128 + stop_signal, (stop_signal, exit_code)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=120)
            process.stdout.close()
        print(f"{prefix} {stop_signal.name}: cleanup verified", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("execd_image", nargs="?")
    parser.add_argument("--runtime-image")
    arguments = parser.parse_args()
    if not arguments.execd_image and not arguments.runtime_image:
        parser.error("provide an execd image or --runtime-image")
    if arguments.execd_image:
        verify(
            Path(__file__).with_name("image.sh"),
            arguments.execd_image,
            "EXECD_TEST_SIGNAL_CHECK",
            "execd integration",
        )
    if arguments.runtime_image:
        tests = Path(__file__).resolve().parents[2] / "tests"
        verify(
            tests / "runtime-smoke.sh",
            arguments.runtime_image,
            "RUNTIME_TEST_SIGNAL_CHECK",
            "runtime smoke",
        )
        verify(
            tests / "workspace-runtime.sh",
            arguments.runtime_image,
            "WORKSPACE_TEST_SIGNAL_CHECK",
            "workspace runtime",
        )


if __name__ == "__main__":
    main()
