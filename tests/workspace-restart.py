"""Exercise real workspace restarts using exclusively owned Docker resources.

The runtime and execd images must already be cached. Startup follows the
supervisor's explicit ready record, while an owned thread continuously drains
each Docker attachment. No polling or elapsed-time threshold establishes success.
Use --startup-only to reproduce the original lifetime-marker failure without
depending on the recovery protocol. Use --wait-for-signal to verify owner cleanup.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import secrets
import shlex
import signal
import socket
import stat
import subprocess
import tarfile
import tempfile
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import cast
from urllib.parse import urlencode
from uuid import uuid4

EXECD_IMAGE = (
    "ghcr.io/tinkerfin-ai/opensandbox-execd@sha256:"
    "50fcc0386fb893e56eee047c87b3bae40ed2451629606feeaa605ab283df06ce"
)
OWNER_LABEL = "io.tinkerfin.workspace-restart-test"
PYTHON = "/opt/sandbox-runtime/venv/bin/python"
RUNTIME = Path("/opt/sandbox-runtime/workspaces")
REGISTRY = Path("/var/lib/tinkerfin-workspaces")
PHASE_SCRIPT = "/opt/opensandbox/workspace-restart.py"
PHASE_STATE = Path("/tmp/workspace-restart-state.json")


def run(
    arguments: list[str],
    *,
    content: str | None = None,
    input_file: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Join the child on success, failure, and interruption before returning."""
    with ExitStack() as resources:
        source = (
            resources.enter_context(input_file.open("rb"))
            if input_file is not None
            else subprocess.PIPE
            if content is not None
            else subprocess.DEVNULL
        )
        process = subprocess.Popen(
            arguments,
            stdin=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            output, error = process.communicate(content)
        except BaseException:
            process.kill()
            process.communicate()
            raise
    result = subprocess.CompletedProcess(arguments, process.returncode, output, error)
    if check and result.returncode:
        raise RuntimeError(
            f"{arguments[0]} exited with {result.returncode}: "
            f"{result.stderr[-8000:]}{result.stdout[-8000:]}"
        )
    return result


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("Expected a JSON object")
    entries = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in entries):
        raise TypeError("Expected string JSON keys")
    return cast(dict[str, object], value)


def text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("Expected a JSON string")
    return value


def strings(value: object) -> dict[str, str]:
    return {key: text(item) for key, item in mapping(value).items()}


class Attachment:
    """Own one Docker attachment, its bounded readiness signal, and log drainer."""

    def __init__(self, container: str, log: Path) -> None:
        self.log = log
        self.process = subprocess.Popen(
            ["docker", "start", "--attach", container],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        reader: Thread | None = None
        try:
            self.ready: Queue[bool] = Queue(maxsize=1)
            self.failure: BaseException | None = None
            reader = Thread(target=self._drain, name="workspace-restart-logs")
            self.reader = reader
            reader.start()
        except BaseException:
            try:
                self.process.kill()
                self.process.wait()
                if reader is not None and reader.ident is not None:
                    reader.join()
            finally:
                if self.process.stdout is not None:
                    self.process.stdout.close()
            raise

    def _drain(self) -> None:
        announced = False
        try:
            assert self.process.stdout is not None
            with self.log.open("w") as target:
                for line in self.process.stdout:
                    target.write(line)
                    target.flush()
                    if not announced:
                        try:
                            payload: object = json.loads(line)
                        except ValueError:
                            continue
                        if payload == {"ready": True}:
                            announced = True
                            self.ready.put(True)
        except (OSError, ValueError) as error:
            self.failure = error
        finally:
            if not announced:
                self.ready.put(False)

    def wait_ready(self) -> None:
        """Report failure before the owner stops its container and joins this CLI."""
        if not self.ready.get():
            if self.failure is not None:
                raise RuntimeError("Workspace log collection failed") from self.failure
            raise RuntimeError(
                "Workspace supervisor exited before ready:\n"
                + self.log.read_text()[-16000:]
            )

    def join(self) -> None:
        """Join after Docker has stopped the container and closed its attachment."""
        self.process.wait()
        self.reader.join()
        assert self.process.stdout is not None
        self.process.stdout.close()
        if self.failure is not None:
            raise RuntimeError("Workspace log collection failed") from self.failure

    def close(self) -> None:
        """Reap the attachment after owned-container cleanup, even on interruption."""
        if self.process.poll() is None:
            self.process.kill()
        self.join()


class DockerRun:
    """Own exact container names and labels; image caches remain borrowed."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.identity = uuid4().hex
        self.source = f"tinkerfin-workspace-source-{self.identity}"
        self.parent = f"tinkerfin-workspace-restart-{self.identity}"
        self.guards: list[str] = []
        self.unrelated = directory / "unrelated"
        self.attachments: list[Attachment] = []

    def prepare(self, image: str, execd_image: str) -> None:
        """Create the two owned containers using cached images and Server options."""
        for reference in (image, execd_image):
            result = run(["docker", "image", "inspect", reference], check=False)
            if result.returncode:
                raise RuntimeError(f"Required image is not cached: {reference}")
        label = f"{OWNER_LABEL}={self.identity}"
        self.unrelated.mkdir(mode=0o700)
        (self.unrelated / "sentinel").write_text(self.identity)
        run(
            [
                "docker",
                "create",
                "--pull",
                "never",
                "--name",
                self.source,
                "--label",
                label,
                "--network",
                "none",
                execd_image,
            ]
        )
        tooling = self.directory / "opensandbox"
        tooling.mkdir()
        for source, destination in (
            ("/execd", "execd"),
            ("/bootstrap.sh", "bootstrap.sh"),
            ("/usr/local/bin/bwrap", "bwrap"),
            ("/opt/opensandbox/opensandbox-session-gate", "opensandbox-session-gate"),
        ):
            run(["docker", "cp", f"{self.source}:{source}", str(tooling / destination)])
        run(
            [
                "docker",
                "create",
                "--pull",
                "never",
                "--name",
                self.parent,
                "--label",
                label,
                "--network",
                "none",
                "--mount",
                f"type=bind,source={self.unrelated},target=/unrelated-workspace-restart",
                "--cap-add",
                "NET_ADMIN",
                "--cap-add",
                "SYS_ADMIN",
                "--security-opt",
                "no-new-privileges:true",
                "--security-opt",
                "seccomp=unconfined",
                "--security-opt",
                "apparmor=unconfined",
                "--tmpfs",
                "/var/lib/execd/isolation",
                "--env",
                f"EXECD_ACCESS_TOKEN={secrets.token_hex(32)}",
                "--env",
                "EXECD=/opt/opensandbox/execd",
                "--env",
                f"EXECD_ISOLATION_CONFIG={RUNTIME}/isolation.toml",
                "--env",
                "TINKERFIN_WORKSPACES=1",
                "--env",
                "TINKERFIN_CONTROL_HOST=control.example",
                "--entrypoint",
                "/opt/opensandbox/bootstrap.sh",
                image,
                "/opt/sandbox-runtime/bin/entrypoint.sh",
                "sleep",
                "infinity",
            ]
        )
        archive = self.directory / "tooling.tar"
        with tarfile.open(archive, "w") as bundle:
            directory = tarfile.TarInfo("opt/opensandbox")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            bundle.addfile(directory)
            for source, destination in (
                (tooling / "execd", "opt/opensandbox/execd"),
                (tooling / "bootstrap.sh", "opt/opensandbox/bootstrap.sh"),
                (tooling / "bwrap", "usr/local/bin/bwrap"),
                (
                    tooling / "opensandbox-session-gate",
                    "opt/opensandbox/opensandbox-session-gate",
                ),
                (Path(__file__).resolve(), PHASE_SCRIPT.lstrip("/")),
            ):
                member = bundle.gettarinfo(str(source), arcname=destination)
                member.uid = member.gid = 0
                member.uname = member.gname = "root"
                with source.open("rb") as content:
                    bundle.addfile(member, content)
        run(["docker", "cp", "-", f"{self.parent}:/"], input_file=archive)

    def verify_mount_guards(self, image: str) -> None:
        """Reject shared registry mounts before touching their owned sentinel data."""
        for index, target in enumerate(
            ("/var/lib/tinkerfin-workspaces", "/var/lib/tinkerfin-workspaces/records")
        ):
            source = self.directory / f"mount-{index}"
            source.mkdir(mode=0o700)
            (source / "sentinel").write_text(self.identity)
            before = manifest(source, directories=(".",))
            name = f"tinkerfin-workspace-mount-{index}-{self.identity}"
            self.guards.append(name)
            run(
                [
                    "docker",
                    "create",
                    "--pull",
                    "never",
                    "--name",
                    name,
                    "--label",
                    f"{OWNER_LABEL}={self.identity}",
                    "--network",
                    "none",
                    "--mount",
                    f"type=bind,source={source},target={target}",
                    "--env",
                    "TINKERFIN_WORKSPACES=1",
                    "--env",
                    "TINKERFIN_CONTROL_HOST=control.example",
                    "--entrypoint",
                    "/opt/sandbox-runtime/bin/entrypoint.sh",
                    image,
                    "sleep",
                    "infinity",
                ]
            )
            result = run(["docker", "start", "--attach", name], check=False)
            assert result.returncode != 0 and (
                "External mounts must not overlap workspace storage"
                in result.stdout + result.stderr
            ), result
            assert manifest(source, directories=(".",)) == before
            print(
                json.dumps(
                    {
                        "mount_guard": target,
                        "container": name,
                        "result": "rejected_without_changes",
                    }
                ),
                flush=True,
            )

    def start(self) -> Attachment:
        attachment = Attachment(
            self.parent, self.directory / f"boot-{len(self.attachments)}.log"
        )
        self.attachments.append(attachment)
        attachment.wait_ready()
        print(json.dumps({"ready": True, "boot": len(self.attachments)}), flush=True)
        return attachment

    def stop(self, attachment: Attachment, *, kill: bool = False) -> None:
        if kill:
            run(["docker", "kill", "--signal", "KILL", self.parent])
        else:
            run(["docker", "stop", "--time", "-1", self.parent])
        attachment.join()
        result = run(
            ["docker", "inspect", "--format", "{{.State.Running}}", self.parent]
        )
        assert result.stdout.strip() == "false"

    def phase(self, phase: str, cycle: str) -> None:
        result = run(
            [
                "docker",
                "exec",
                self.parent,
                PYTHON,
                "-u",
                "-I",
                "-S",
                PHASE_SCRIPT,
                "--phase",
                phase,
                "--cycle",
                cycle,
            ]
        )
        print(result.stdout, end="", flush=True)

    def close(self) -> None:
        """Delete only verified owned containers, then verify no labeled residue."""
        failures: list[Exception] = []
        for name in (self.parent, self.source, *self.guards):
            found = run(["docker", "container", "inspect", name], check=False)
            if found.returncode:
                if (
                    "No such container" in found.stderr
                    or "No such object" in found.stderr
                ):
                    continue
                failures.append(RuntimeError(found.stderr))
                continue
            payload: object = json.loads(found.stdout)
            assert isinstance(payload, list)
            containers = cast(list[object], payload)
            assert len(containers) == 1
            container = mapping(containers[0])
            labels = mapping(mapping(container["Config"])["Labels"])
            if labels.get(OWNER_LABEL) != self.identity:
                failures.append(
                    RuntimeError(f"Refusing to remove a borrowed container: {name}")
                )
                continue
            try:
                run(["docker", "rm", "--force", "--volumes", name])
            except (OSError, RuntimeError) as error:
                failures.append(error)
        for attachment in self.attachments:
            try:
                attachment.close()
            except (OSError, RuntimeError) as error:
                failures.append(error)
        remaining = run(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label={OWNER_LABEL}={self.identity}",
            ]
        ).stdout.split()
        if remaining:
            failures.append(RuntimeError(f"Owned resources remain: {remaining}"))
        if failures:
            details = "\n".join(str(failure) for failure in failures)
            raise RuntimeError(
                f"Workspace integration cleanup failed:\n{details}"
            ) from failures[0]
        print(json.dumps({"cleanup": self.identity, "remaining": []}), flush=True)


def native(
    method: str, path: str, body: dict[str, object] | None = None
) -> tuple[int, str]:
    connection = http.client.HTTPConnection("127.0.0.1", 44772, timeout=60)
    try:
        connection.request(
            method,
            path,
            json.dumps(body) if body is not None else None,
            headers={
                "Content-Type": "application/json",
                "X-EXECD-ACCESS-TOKEN": os.environ["EXECD_ACCESS_TOKEN"],
            },
        )
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


def namespace() -> str:
    status, content = native("GET", "/v1/isolated/capabilities")
    assert status == 200, content
    payload = mapping(json.loads(content))
    assert (
        payload["available"] is True and payload["namespace_exit_confirmation"] is True
    )
    return text(payload["session_namespace"])


def registry(
    project: str,
    operation: str,
    arguments: dict[str, object],
    *,
    rejected: bool = False,
) -> object:
    result = run(
        [PYTHON, "-I", "-S", str(RUNTIME / "registry.py")],
        content=json.dumps(
            {"project": project, "operation": operation, "arguments": arguments}
        ),
        check=False,
    )
    payload: object = json.loads(result.stdout)
    if rejected:
        assert (
            result.returncode == 1
            and mapping(mapping(payload)["error"])["reason"] == "stale"
        ), result
    else:
        assert result.returncode == 0, result
    return payload


def network(
    operation: str, owner: dict[str, str] | None = None, *, rejected: bool = False
) -> dict[str, object]:
    arguments = [PYTHON, "-I", "-S", str(RUNTIME / "network.py"), operation]
    if owner is not None:
        arguments.extend((owner["session_id"], owner["session_namespace"]))
    result = run(arguments, check=False)
    payload = mapping(json.loads(result.stdout))
    if rejected:
        assert result.returncode == 1 and "error" in payload
    else:
        assert result.returncode == 0, result.stderr
    return payload


@dataclass(frozen=True, slots=True)
class Session:
    """Keep the exact remote identity and request needed to test delayed replay."""

    label: str
    incarnation: str
    session_id: str
    session_namespace: str
    token: str
    request: dict[str, object]
    environment: dict[str, str]
    marker: str

    @property
    def project(self) -> str:
        return hashlib.sha256(self.label.encode()).hexdigest()

    @property
    def root(self) -> Path:
        return REGISTRY / "projects" / self.project / self.incarnation

    @property
    def owner(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "session_namespace": self.session_namespace,
        }

    @property
    def arguments(self) -> dict[str, object]:
        return {"incarnation": self.incarnation, **self.owner}


def command(session: Session, code: str) -> str:
    status, content = native(
        "POST",
        f"/v1/isolated/session/{session.session_id}/run",
        {"code": code, "envs": session.environment},
    )
    assert status == 200, content
    output: list[str] = []
    terminal = False
    for line in content.splitlines():
        if not line.strip():
            continue
        assert not terminal, "Isolated command emitted data after its terminal event"
        event = mapping(json.loads(line))
        if event["type"] == "error":
            raise AssertionError(f"Isolated command failed: {event}")
        if event["type"] == "stdout":
            output.append(text(event["text"]))
        if event["type"] == "execution_complete":
            terminal = True
    assert terminal, (
        f"Isolated command ended without terminal confirmation: {content[-16000:]}"
    )
    return "".join(output)


def python(session: Session, code: str) -> str:
    return command(session, "python -c " + shlex.quote(code))


def new_session(label: str) -> Session:
    project = hashlib.sha256(label.encode()).hexdigest()
    prepared = mapping(registry(project, "prepare", {}))
    incarnation = text(prepared["incarnation"])
    owner = {"session_id": str(uuid4()), "session_namespace": namespace()}
    arguments: dict[str, object] = {"incarnation": incarnation, **owner}
    registry(project, "reserve", arguments)
    token = text(network("grant", owner)["token"])
    request = mapping(registry(project, "session_request", arguments))
    status, content = native("POST", "/v1/isolated/session", request)
    assert status == 201, content
    session = Session(
        label,
        incarnation,
        owner["session_id"],
        owner["session_namespace"],
        token,
        request,
        {},
        f"workspace-descendant-{uuid4().hex}",
    )
    initialized = command(
        session, f"{PYTHON} -I -S {RUNTIME}/initialize.py {session.session_id} {token}"
    )
    return Session(
        label,
        incarnation,
        session.session_id,
        session.session_namespace,
        token,
        request,
        strings(json.loads(initialized)),
        session.marker,
    )


def marker_live(marker: str) -> bool:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, ProcessLookupError):
            continue
        if marker.encode() in arguments:
            return True
    return False


def leave_descendant(session: Session) -> None:
    code = """import os, signal
reader, writer = os.pipe()
child = os.fork()
if child == 0:
    os.close(reader)
    os.setsid()
    if os.fork() != 0:
        os._exit(0)
    null = os.open('/dev/null', os.O_RDWR)
    for descriptor in (0, 1, 2):
        os.dup2(null, descriptor)
    os.close(null)
    os.write(writer, b'ready')
    os.close(writer)
    while True:
        signal.pause()
os.close(writer)
assert os.read(reader, 5) == b'ready'
os.close(reader)
os.waitpid(child, 0)
print('descendant-ready')
"""
    result = command(session, f"python -c {shlex.quote(code)} {session.marker}")
    assert "descendant-ready" in result and marker_live(session.marker)


def manifest(
    root: Path,
    *,
    directories: tuple[str, ...] = ("files", "home", "cache", "dependencies"),
) -> dict[str, str]:
    """Hash every persisted file and symlink, including the complete Python install."""
    result: dict[str, str] = {}
    for directory in directories:
        base = root / directory
        for parent, subdirectories, files in os.walk(base, followlinks=False):
            for name in (".", *subdirectories, *files):
                path = Path(parent) if name == "." else Path(parent) / name
                info = path.lstat()
                description = (
                    f"{stat.S_IMODE(info.st_mode)}:{info.st_uid}:{info.st_gid}:"
                )
                if stat.S_ISLNK(info.st_mode):
                    description += "link:" + os.readlink(path)
                elif stat.S_ISDIR(info.st_mode):
                    description += "directory"
                else:
                    assert stat.S_ISREG(info.st_mode), path
                    description += hashlib.sha256(path.read_bytes()).hexdigest()
                result[str(path.relative_to(root))] = description
    return result


def install_data(session: Session, cycle: str) -> None:
    source = f"""import base64, hashlib, pathlib, zipfile
label = {session.label!r}
marker = pathlib.Path('/workspace/marker.txt')
if not marker.exists():
    for location in ('/workspace/marker.txt', '/home/workspace/marker', '/cache/marker', '/dependencies/marker'):
        pathlib.Path(location).write_text(label)
    metadata = 'restart_probe-1.0.0.dist-info'
    files = {{
        'restart_probe.py': 'VALUE = ' + repr(label) + '\\n',
        metadata + '/METADATA': 'Metadata-Version: 2.1\\nName: restart-probe\\nVersion: 1.0.0\\n',
        metadata + '/WHEEL': 'Wheel-Version: 1.0\\nGenerator: restart-contract\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n',
    }}
    records = []
    with zipfile.ZipFile('/workspace/restart_probe-1.0.0-py3-none-any.whl', 'w') as wheel:
        for name, value in files.items():
            encoded = value.encode()
            wheel.writestr(name, encoded)
            digest = base64.urlsafe_b64encode(hashlib.sha256(encoded).digest()).rstrip(b'=')
            records.append(name + ',sha256=' + digest.decode() + ',' + str(len(encoded)))
        records.append(metadata + '/RECORD,,')
        wheel.writestr(metadata + '/RECORD', '\\n'.join(records) + '\\n')
print('data-ready')
"""
    assert "data-ready" in python(session, source)
    command(
        session,
        "python -m pip install --no-index --no-deps --no-compile /workspace/restart_probe-1.0.0-py3-none-any.whl",
    )
    verify = f"""import pathlib, restart_probe
assert restart_probe.VALUE == {session.label!r}
for location in ('/workspace/marker.txt', '/home/workspace/marker', '/cache/marker', '/dependencies/marker'):
    assert pathlib.Path(location).read_text() == {session.label!r}
assert not pathlib.Path('/var/lib/tinkerfin-workspaces/records').exists()
for name in ('control.sock', 'lifetime', 'ready'):
    assert not (pathlib.Path('/var/lib/execd/isolation/workspace-control') / name).exists()
target = pathlib.Path('/workspace') / {("round-" + cycle)!r}
target.write_text({session.label!r})
assert target.read_text() == {session.label!r}
print('read-write-execute-passed')
"""
    assert "read-write-execute-passed" in python(session, verify)


def egress(token: str) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
        endpoint.settimeout(60)
        endpoint.connect("/var/lib/execd/isolation/workspace-control/egress.sock")
        endpoint.sendall(token.encode() + b"\n")
        endpoint.sendall(
            b"CONNECT control.example:443 HTTP/1.1\r\nHost: control.example:443\r\n\r\n"
        )
        try:
            return endpoint.recv(4096)
        except ConnectionResetError:
            return b""


def seed(cycle: str) -> None:
    sessions = [new_session(label) for label in ("project-a", "project-b", "deleting")]
    manifests: dict[str, dict[str, str]] = {}
    for session in sessions:
        install_data(session, cycle)
        leave_descendant(session)
        manifests[session.label] = manifest(session.root)
        python(
            session,
            "from pathlib import Path\n"
            f"temporary = Path('/dependencies/.python-{session.session_id}')\n"
            "temporary.mkdir(mode=0o700)\n"
            "(temporary / 'incomplete').write_text('unpublished environment')\n",
        )
    registry(sessions[-1].project, "begin_delete", {})
    state = {
        "sessions": [asdict(session) for session in sessions],
        "manifests": manifests,
    }
    PHASE_STATE.write_text(json.dumps(state))
    PHASE_STATE.chmod(0o600)
    duplicate = run(
        ["/opt/sandbox-runtime/bin/entrypoint.sh", "/usr/bin/true"], check=False
    )
    assert (
        duplicate.returncode != 0
        and "FileExistsError" in duplicate.stderr
        and "lifetime" in duplicate.stderr
    )
    assert network("status") == {"ready": True}
    assert namespace() == sessions[0].session_namespace
    assert egress(sessions[0].token).startswith(b"HTTP/1.1 403")
    assert "still-running" in command(sessions[0], "printf still-running")
    print(
        json.dumps(
            {
                "phase": "seed",
                "cycle": cycle,
                "active_sessions": len(sessions),
                "duplicate_supervisor": "rejected",
            }
        ),
        flush=True,
    )


def load_state() -> tuple[list[Session], dict[str, dict[str, str]]]:
    state = mapping(json.loads(PHASE_STATE.read_text()))
    raw_sessions = state["sessions"]
    assert isinstance(raw_sessions, list)
    sessions: list[Session] = []
    for value in cast(list[object], raw_sessions):
        item = mapping(value)
        sessions.append(
            Session(
                text(item["label"]),
                text(item["incarnation"]),
                text(item["session_id"]),
                text(item["session_namespace"]),
                text(item["token"]),
                mapping(item["request"]),
                strings(item["environment"]),
                text(item["marker"]),
            )
        )
    return sessions, {
        key: strings(value) for key, value in mapping(state["manifests"]).items()
    }


def finish_project(session: Session) -> None:
    sealed = mapping(registry(session.project, "begin_delete", {}))
    registry(
        session.project,
        "finish_delete",
        {
            "incarnation": session.incarnation,
            "deletion_id": sealed["deletion_id"],
            "confirmed": sealed["sessions"],
        },
    )
    assert not session.root.exists()


def recover(cycle: str, *, final: bool) -> None:
    sessions, manifests = load_state()
    current = namespace()
    for session in sessions:
        assert current != session.session_namespace and not marker_live(session.marker)
        record = mapping(
            json.loads((REGISTRY / "records" / session.project).read_text())
        )
        assert record["sessions"] == [session.owner]
        assert record["phase"] == (
            "deleting" if session.label == "deleting" else "active"
        )
        assert manifest(session.root) == manifests[session.label], session.label
        assert not (
            session.root / "runs" / session.session_namespace / session.session_id
        ).exists()
        assert not (
            session.root / "dependencies" / f".python-{session.session_id}"
        ).exists()
        assert egress(session.token) == b"", "A previous token remained authenticated"
        for operation in ("reserve", "session_request"):
            registry(session.project, operation, session.arguments, rejected=True)
        network("grant", session.owner, rejected=True)
        rejected_post = native("POST", "/v1/isolated/session", session.request)
        assert rejected_post[0] == 400, rejected_post
        query = urlencode({"session_namespace": session.session_namespace})
        assert (
            native("DELETE", f"/v1/isolated/session/{session.session_id}?{query}")[0]
            == 409
        )
        assert network("revoke", session.owner) == {"stopped": True}
        if session.label == "deleting":
            finish_project(session)
        else:
            registry(session.project, "release", session.arguments)
    sample = sessions[0]
    unknown = {
        "session_id": str(uuid4()),
        "session_namespace": sample.session_namespace,
    }
    unknown_arguments: dict[str, object] = {
        "incarnation": sample.incarnation,
        **unknown,
    }
    registry(sample.project, "reserve", unknown_arguments, rejected=True)
    registry(sample.project, "session_request", unknown_arguments, rejected=True)
    network("grant", unknown, rejected=True)
    assert network("revoke", unknown) == {"stopped": False}
    assert network("revoke", {**sample.owner, "session_namespace": current}) == {
        "stopped": False
    }
    print(
        json.dumps(
            {
                "phase": "recovered",
                "cycle": cycle,
                "preserved_projects": [session.label for session in sessions],
                "old_sessions": "stopped",
                "old_requests": "rejected",
            }
        ),
        flush=True,
    )
    seed(cycle)
    current_sessions, _manifests = load_state()
    for owner in (sample.owner, unknown):
        rejected_post = native(
            "POST", "/v1/isolated/session", {**current_sessions[0].request, **owner}
        )
        assert rejected_post[0] == 409, rejected_post
    if final:
        for session in current_sessions:
            registry(session.project, "cancel", dict(session.owner))
            assert network("revoke", session.owner) == {"stopped": False}
            query = urlencode({"session_namespace": session.session_namespace})
            assert (
                native("DELETE", f"/v1/isolated/session/{session.session_id}?{query}")[
                    0
                ]
                == 200
            )
            assert not marker_live(session.marker)
            if session.label == "deleting":
                finish_project(session)
            else:
                registry(session.project, "release", session.arguments)


def interrupted(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?")
    parser.add_argument("execd_image", nargs="?", default=EXECD_IMAGE)
    parser.add_argument("--phase", choices=("seed", "recover", "finish"))
    parser.add_argument("--cycle", default="initial")
    parser.add_argument("--startup-only", action="store_true")
    parser.add_argument("--wait-for-signal", action="store_true")
    arguments = parser.parse_args()
    if arguments.phase:
        if arguments.phase == "seed":
            seed(arguments.cycle)
        else:
            recover(arguments.cycle, final=arguments.phase == "finish")
        return
    if not arguments.image:
        parser.error("a cached runtime image is required")
    for interrupt in (signal.SIGINT, signal.SIGTERM):
        signal.signal(interrupt, interrupted)
    with tempfile.TemporaryDirectory(prefix="workspace-restart-") as directory:
        owned = DockerRun(Path(directory))
        print(
            json.dumps(
                {"run_id": owned.identity, "containers": [owned.source, owned.parent]}
            ),
            flush=True,
        )
        try:
            owned.prepare(arguments.image, arguments.execd_image)
            unrelated = manifest(owned.unrelated, directories=(".",))
            if not arguments.startup_only and not arguments.wait_for_signal:
                owned.verify_mount_guards(arguments.image)
            first = owned.start()
            if arguments.wait_for_signal:
                print(
                    json.dumps({"signal_ready": True, "run_id": owned.identity}),
                    flush=True,
                )
                signal.pause()
                raise AssertionError("An unhandled signal resumed the owner")
            if not arguments.startup_only:
                owned.phase("seed", "initial")
            owned.stop(first)
            second = owned.start()
            if not arguments.startup_only:
                owned.phase("recover", "normal")
                owned.stop(second, kill=True)
                third = owned.start()
                owned.phase("finish", "killed")
                owned.stop(third)
            else:
                owned.stop(second)
            assert manifest(owned.unrelated, directories=(".",)) == unrelated
            print(
                json.dumps({"result": "passed", "run_id": owned.identity}), flush=True
            )
        finally:
            for interrupt in (signal.SIGINT, signal.SIGTERM):
                signal.signal(interrupt, signal.SIG_IGN)
            owned.close()


if __name__ == "__main__":
    main()
