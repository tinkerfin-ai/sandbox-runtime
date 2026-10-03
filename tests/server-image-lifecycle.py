"""Verify real Docker test cleanup after signals sent to the owning shell PID."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


def verify_signal(image: str, interrupt: signal.Signals) -> None:
    """Interrupt after the container announces readiness, without a timing probe."""
    with tempfile.TemporaryDirectory(prefix="tinkerfin-server-signal-") as directory:
        tests = Path(directory) / "tests"
        tests.mkdir()
        runner = tests / "server-image.sh"
        shutil.copyfile(Path(__file__).with_name("server-image.sh"), runner)
        (tests / "server-contract.py").write_text(
            'import signal\nprint("CONTAINER_READY", flush=True)\nsignal.pause()\n'
        )
        process = subprocess.Popen(
            ["bash", str(runner), image],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        run_id = ""
        try:
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                if line.startswith("server image integration run: "):
                    run_id = line.strip().rsplit(" ", 1)[1]
                if line.strip() == "CONTAINER_READY":
                    break
            else:
                raise AssertionError(
                    "The controlled container did not announce readiness"
                )
            assert run_id
            os.kill(process.pid, interrupt)
            output, _ = process.communicate()
            print(output, end="", flush=True)
            assert process.returncode == 128 + interrupt, process.returncode
            assert f"cleanup: {run_id}; remaining=[]" in output
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
            if run_id:
                owned = subprocess.check_output(
                    [
                        "docker",
                        "ps",
                        "--all",
                        "--quiet",
                        "--filter",
                        f"label=io.tinkerfin.server-test={run_id}",
                    ],
                    text=True,
                ).split()
                for container in owned:
                    subprocess.run(
                        ["docker", "rm", "--force", "--volumes", container], check=True
                    )
                assert not owned, (
                    f"The shell failed to reclaim owned containers: {owned}"
                )
        print(f"{interrupt.name}: cleanup passed, exit={process.returncode}")


if __name__ == "__main__":
    for requested_signal in (signal.SIGINT, signal.SIGTERM):
        verify_signal(sys.argv[1], requested_signal)
