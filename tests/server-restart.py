"""Exercise daemon reboot and pause using only a disposable, isolated Docker daemon."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from collections import deque
from threading import Event, Thread
from uuid import uuid4

OWNER = "io.tinkerfin.server-restart-test"
SOCKET = "unix:///run/restart-test/docker.sock"
DIND_IMAGE = "docker:27.2.0-dind"


def docker(*arguments: str, input: str | None = None) -> str:
    result = subprocess.run(
        ["docker", *arguments], input=input, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout.strip()


class RestartTest:
    """Own exact outer resource names and every attached Docker CLI process."""

    def __init__(self, image: str) -> None:
        self.run_id = uuid4().hex
        self.image = image
        self.daemon = f"tinkerfin-server-restart-{self.run_id}"
        self.volumes = [
            f"{self.daemon}-{suffix}" for suffix in ("data", "socket", "metadata")
        ]
        self.containers = [self.daemon]
        self.attachments: list[subprocess.Popen[str]] = []
        self.readers: list[Thread] = []
        self.ids = {role: str(uuid4()) for role in ("automatic", "manual", "paused")}

    def inner(self, *arguments: str) -> str:
        return docker("exec", self.daemon, "docker", "--host", SOCKET, *arguments)

    def start_daemon(self) -> None:
        ready = Event()
        found = []
        tail: deque[str] = deque(maxlen=20)
        process = subprocess.Popen(
            ["docker", "start", "--attach", self.daemon],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.attachments.append(process)

        def collect() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    tail.append(line.strip())
                    if "API listen on /run/restart-test/docker.sock" in line:
                        found.append(True)
                        ready.set()
            finally:
                process.stdout.close()
                ready.set()

        reader = Thread(target=collect, name="server-restart-daemon-log")
        self.readers.append(reader)
        reader.start()
        ready.wait()
        if not found:
            raise RuntimeError("Test daemon did not become ready: " + "\n".join(tail))

    def workload_ready(self, role: str, incarnation: int) -> None:
        process = subprocess.Popen(
            [
                "docker",
                "exec",
                self.daemon,
                "docker",
                "--host",
                SOCKET,
                "logs",
                "--follow",
                self.ids[role],
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if line.strip() == f"READY:{incarnation}":
                    return
            raise AssertionError(f"Workload {role} exited before announcing readiness")
        finally:
            process.terminate()
            process.communicate()

    def control(self, phase: str) -> None:
        name = f"{self.daemon}-{phase}"
        self.containers.append(name)
        docker(
            "create",
            "--name",
            name,
            "--network",
            "none",
            "--interactive",
            "--label",
            f"{OWNER}={self.run_id}",
            "--mount",
            f"type=volume,source={self.volumes[1]},target=/run/restart-test",
            "--mount",
            f"type=volume,source={self.volumes[2]},target=/data",
            "--env",
            f"DOCKER_HOST={SOCKET}",
            "--env",
            "HOME=/data",
            "--entrypoint",
            "python",
            self.image,
            "-",
            phase,
            json.dumps(self.ids),
        )
        source = """
import json, sys
from fastapi import HTTPException
from opensandbox_server.config import AppConfig, DockerConfig, RuntimeConfig
from opensandbox_server.services.docker import DockerSandboxService
phase, identities = sys.argv[1], json.loads(sys.argv[2])
service = DockerSandboxService(config=AppConfig(
    runtime=RuntimeConfig(type="docker", execd_image="test-owned-execd"),
    docker=DockerConfig(network_mode="none"),
))
if phase == "pause":
    service.pause_sandbox(identities["paused"])
    assert service.get_sandbox(identities["paused"]).status.state == "Paused"
elif phase == "observe":
    actual = {key: service.get_sandbox(value).status.state for key,value in identities.items()}
    assert actual["automatic"] == "Running", actual
    assert actual["manual"] in {"Terminated", "Failed"}, actual
    assert actual["paused"] == "Paused", actual
elif phase == "resume":
    service.resume_sandbox(identities["paused"])
    assert service.get_sandbox(identities["paused"]).status.state == "Running"
    try:
        service.resume_sandbox(identities["manual"])
    except HTTPException as error:
        assert error.status_code == 409
    else:
        raise AssertionError("A manually stopped sandbox was resumed")
service.docker_client.close()
print(phase + ": passed")
"""
        print(
            docker("start", "--attach", "--interactive", name, input=source), flush=True
        )

    def run(self) -> None:
        # Images are borrowed. Refuse Docker's implicit pull so this run never
        # creates an untracked host image reference during interruption.
        for reference in (self.image, DIND_IMAGE):
            docker("image", "inspect", reference)
        print(
            json.dumps(
                {
                    "integration_run": self.run_id,
                    "daemon": self.daemon,
                    "volumes": self.volumes,
                    "inner_containers": self.ids,
                }
            ),
            flush=True,
        )
        for volume in self.volumes:
            docker("volume", "create", "--label", f"{OWNER}={self.run_id}", volume)
        docker(
            "create",
            "--pull",
            "never",
            "--name",
            self.daemon,
            "--privileged",
            "--tmpfs",
            "/run",
            "--network",
            "none",
            "--label",
            f"{OWNER}={self.run_id}",
            "--env",
            "DOCKER_TLS_CERTDIR=",
            "--mount",
            f"type=volume,source={self.volumes[0]},target=/var/lib/docker",
            "--mount",
            f"type=volume,source={self.volumes[1]},target=/run/restart-test",
            DIND_IMAGE,
            "dockerd",
            "--host",
            SOCKET,
            "--storage-driver",
            "vfs",
            "--iptables=false",
            "--bridge=none",
        )
        self.start_daemon()
        print("isolated daemon ready; loading its workload image", flush=True)
        saving = subprocess.Popen(
            ["docker", "save", DIND_IMAGE], stdout=subprocess.PIPE
        )
        loading: subprocess.Popen[bytes] | None = None
        try:
            loading = subprocess.Popen(
                [
                    "docker",
                    "exec",
                    "--interactive",
                    self.daemon,
                    "docker",
                    "--host",
                    SOCKET,
                    "load",
                ],
                stdin=saving.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert saving.stdout is not None
            saving.stdout.close()
            output, errors = loading.communicate()
            if loading.returncode or saving.wait():
                raise RuntimeError(errors.decode() or output.decode())
        finally:
            for process in (loading, saving):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait()
        command = (
            "n=0; test ! -f /incarnation || n=$(cat /incarnation); n=$((n+1)); "
            'printf "%s" "$n" > /incarnation; '
            "test -f /preserved || printf original > /preserved; "
            'echo "READY:$n"; exec sleep infinity'
        )
        originals = {}
        for role, identity in self.ids.items():
            originals[role] = self.inner(
                "create",
                "--name",
                identity,
                "--network",
                "none",
                "--restart",
                "unless-stopped",
                "--label",
                f"opensandbox.io/id={identity}",
                "--label",
                "opensandbox.io/manual-cleanup=true",
                "--label",
                f"{OWNER}={self.run_id}",
                "--entrypoint",
                "/bin/sh",
                DIND_IMAGE,
                "-c",
                command,
            )
            self.inner("start", identity)
            self.workload_ready(role, 1)
        self.inner("stop", self.ids["manual"])
        self.control("pause")
        docker("stop", self.daemon)
        self.start_daemon()
        self.workload_ready("automatic", 2)
        self.control("observe")
        self.control("resume")
        self.workload_ready("paused", 2)
        for role in ("automatic", "paused"):
            identity = self.ids[role]
            assert (
                self.inner("inspect", "--format", "{{.Id}}", identity)
                == originals[role]
            )
            assert self.inner("exec", identity, "cat", "/preserved") == "original"
        assert (
            self.inner("inspect", "--format", "{{.State.Running}}", self.ids["manual"])
            == "false"
        )
        print(
            "daemon reboot: identity/files preserved; manual stop and pause respected",
            flush=True,
        )

    def close(self) -> None:
        failures: list[Exception] = []
        for name in reversed(self.containers):
            try:
                inspection = subprocess.run(
                    ["docker", "inspect", name],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if inspection.returncode:
                    continue
                info = json.loads(inspection.stdout)[0]
                assert info["Config"]["Labels"].get(OWNER) == self.run_id
                docker("rm", "--force", "--volumes", info["Id"])
            except Exception as error:  # noqa: BLE001 - settle every owned resource before reporting errors
                failures.append(error)
        for process in self.attachments:
            if process.poll() is None:
                process.terminate()
            process.wait()
        for reader in self.readers:
            reader.join()
        for name in self.volumes:
            try:
                inspection = subprocess.run(
                    ["docker", "volume", "inspect", name],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if inspection.returncode:
                    continue
                info = json.loads(inspection.stdout)[0]
                assert info["Labels"].get(OWNER) == self.run_id
                docker("volume", "rm", name)
            except Exception as error:  # noqa: BLE001 - settle every owned resource before reporting errors
                failures.append(error)
        if failures:
            raise RuntimeError(
                "Test resource cleanup failed: "
                + "; ".join(f"{type(error).__name__}: {error}" for error in failures)
            ) from failures[0]
        assert not docker(
            "ps", "--all", "--quiet", "--filter", f"label={OWNER}={self.run_id}"
        )
        assert not docker(
            "volume", "ls", "--quiet", "--filter", f"label={OWNER}={self.run_id}"
        )
        print(f"server restart cleanup: {self.run_id}; remaining=[]", flush=True)


def main() -> None:
    """Run the integration and retain resource ownership on interruption."""
    owner = RestartTest(sys.argv[1])

    def interrupt(number: int, _frame: object) -> None:
        raise SystemExit(128 + number)

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        owner.run()
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        owner.close()


if __name__ == "__main__":
    main()
