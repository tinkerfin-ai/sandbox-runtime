"""Verify workspace Docker-owner cleanup after explicitly synchronized signals."""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
from pathlib import Path
from typing import cast
from uuid import UUID

OWNER_LABEL = "io.tinkerfin.workspace-restart-test"


def interrupt_owner(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def owned_containers(identity: str) -> list[str]:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label={OWNER_LABEL}={identity}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.split()


def verify(image: str, execd_image: str | None, interrupt: signal.Signals) -> None:
    """Signal only the newly started owner after its readiness receipt arrives."""
    arguments = [
        sys.executable,
        str(Path(__file__).with_name("workspace-restart.py")),
        image,
    ]
    if execd_image is not None:
        arguments.append(execd_image)
    arguments.append("--wait-for-signal")
    process = subprocess.Popen(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    identity = ""
    expected_names: set[str] = set()
    try:
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            payload: object = json.loads(line)
            assert isinstance(payload, dict)
            record = cast(dict[str, object], payload)
            if "containers" in record:
                run_id = record["run_id"]
                assert isinstance(run_id, str) and UUID(hex=run_id).hex == run_id
                identity = run_id
                names = record["containers"]
                assert isinstance(names, list)
                for name in cast(list[object], names):
                    assert isinstance(name, str)
                    expected_names.add(name)
            if record.get("signal_ready") is True:
                assert record["run_id"] == identity and len(expected_names) == 2
                break
        else:
            raise AssertionError("Workspace owner exited before signal readiness")
        process.send_signal(interrupt)
        output, _error = process.communicate()
        print(output, end="", flush=True)
        assert process.returncode == 128 + interrupt, process.returncode
        cleanup: dict[str, str | list[str]] = {"cleanup": identity, "remaining": []}
        assert any(json.loads(line) == cleanup for line in output.splitlines())
        assert owned_containers(identity) == []
        print(
            json.dumps(
                {"signal": interrupt.name, "result": "passed", "run_id": identity}
            ),
            flush=True,
        )
    finally:
        previous = {
            signum: signal.signal(signum, signal.SIG_IGN)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.communicate()
            else:
                process.wait()
                if process.stdout is not None:
                    process.stdout.close()
            if identity:
                for container in owned_containers(identity):
                    result = subprocess.run(
                        ["docker", "inspect", "--format", "{{.Name}}", container],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    assert result.stdout.strip().removeprefix("/") in expected_names
                    subprocess.run(
                        ["docker", "rm", "--force", "--volumes", container],
                        check=True,
                    )
                assert owned_containers(identity) == []
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("execd_image", nargs="?")
    arguments = parser.parse_args()
    for interrupt in (signal.SIGINT, signal.SIGTERM):
        signal.signal(interrupt, interrupt_owner)
    for interrupt in (signal.SIGINT, signal.SIGTERM):
        verify(arguments.image, arguments.execd_image, interrupt)


if __name__ == "__main__":
    main()
