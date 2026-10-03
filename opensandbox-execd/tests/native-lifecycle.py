"""Verify session ownership and namespace exit in an owned Linux container."""

from __future__ import annotations

import http.client
import json
import os
import select
import shlex
import shutil
import socket
import stat
import subprocess
from pathlib import Path
from queue import Queue
from threading import Thread
from urllib.parse import urlencode
from uuid import uuid4


def request(
    method: str, path: str, body: dict[str, object] | None = None
) -> tuple[int, str]:
    connection = http.client.HTTPConnection("127.0.0.1", 44772, timeout=30)
    try:
        connection.request(
            method,
            path,
            json.dumps(body) if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        content = response.read().decode()
        print(
            json.dumps(
                {
                    "method": method,
                    "path": path,
                    "status": response.status,
                    "body": content,
                }
            ),
            flush=True,
        )
        return response.status, content
    finally:
        connection.close()


def main() -> None:
    print(json.dumps({"kernel": os.uname().release}), flush=True)
    Path("/tmp/isolation.toml").write_text('allowed_writable = ["/workspace"]\n')
    with Path("/tmp/execd.log").open("w+") as log:
        process = subprocess.Popen(
            [
                "/usr/local/bin/execd",
                "--port",
                "44772",
                "--isolation-config",
                "/tmp/isolation.toml",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        output = process.stdout
        assert output is not None
        startup: Queue[bool] = Queue(maxsize=1)

        def collect_logs() -> None:
            ready = False
            try:
                for line in output:
                    log.write(line)
                    if not ready and "execd listening on :44772 (IPv4)" in line:
                        ready = True
                        startup.put(True)
            finally:
                if not ready:
                    startup.put(False)

        log_reader = Thread(target=collect_logs, name="execd-test-logs")
        sessions: list[str] = []
        pidfds: list[int] = []
        session_namespace = ""
        try:
            log_reader.start()
            if not startup.get():
                raise RuntimeError("execd exited before accepting requests")
            status, payload = request("GET", "/v1/isolated/capabilities")
            assert status == 200, payload
            capabilities = json.loads(payload)
            assert capabilities["available"], capabilities
            assert capabilities["client_session_ownership"], capabilities
            assert capabilities["namespace_exit_confirmation"], capabilities
            assert capabilities["rooted_filesystem"], capabilities
            assert capabilities["session_id_limit"] == 65536, capabilities
            session_namespace = capabilities["session_namespace"]

            def delete(identity: str) -> tuple[int, str]:
                query = urlencode({"session_namespace": session_namespace})
                return request("DELETE", f"/v1/isolated/session/{identity}?{query}")

            def create(identity: str, workspace: Path) -> tuple[int, str]:
                return request(
                    "POST",
                    "/v1/isolated/session",
                    {
                        "session_id": identity,
                        "session_namespace": session_namespace,
                        "workspace": {"path": str(workspace), "mode": "rw"},
                        "profile": "strict",
                        "share_net": False,
                        "uid_mode": "setpriv",
                        "uid": 1000,
                        "gid": 1000,
                        "env_passthrough": {"mode": "allow", "keys": ["PATH"]},
                        "idle_timeout_seconds": 0,
                    },
                )

            retired = str(uuid4())
            assert request("DELETE", f"/v1/isolated/session/{retired}")[0] == 409
            assert delete(retired)[0] == 200
            assert create(retired, Path("/workspace/retired"))[0] == 409
            assert not Path("/workspace/retired").exists()
            assert delete(retired)[0] == 200

            for project in ("a", "b"):
                workspace = Path("/workspace") / project
                workspace.mkdir()
                workspace.chmod(0o777)
                identity = str(uuid4())
                sessions.append(identity)
                status, payload = create(identity, workspace)
                assert status == 201, payload
                assert json.loads(payload)["session_id"] == identity
            first, second = sessions
            credentials = """import ctypes
import errno
import json
import os
import subprocess
from pathlib import Path
fields = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
caps = {name: int(fields[name].strip(), 16) for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')}
print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'capabilities': caps, 'no_new_privs': fields['NoNewPrivs'].strip()}), flush=True)
print(subprocess.check_output(['setpriv', '--version'], text=True).strip(), flush=True)
assert os.getuid() == 1000 and os.getgid() == 1000
assert not any(caps.values()), caps
assert fields['NoNewPrivs'].strip() == '1'
Path('/workspace/a/mount-check').mkdir()
libc = ctypes.CDLL(None, use_errno=True)
libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p]
libc.mount.restype = ctypes.c_int
result = libc.mount(b'tmpfs', b'/workspace/a/mount-check', b'tmpfs', 0, None)
assert result == -1 and ctypes.get_errno() in (errno.EPERM, errno.EACCES), 'mount syscall was not denied'
assert ' /workspace/a/mount-check ' not in Path('/proc/self/mountinfo').read_text()
print('unprivileged-verified')
"""
            status, payload = request(
                "POST",
                f"/v1/isolated/session/{first}/run",
                {"code": "python3 -I -S -c " + shlex.quote(credentials)},
            )
            assert status == 200 and "unprivileged-verified" in payload, payload
            first_root = Path("/workspace/a")
            second_root = Path("/workspace/b")
            secret = second_root / "secret.txt"
            secret.write_text("project-b-secret")
            secret.chmod(0o644)
            (first_root / "escape").symlink_to(second_root, target_is_directory=True)
            first_files = f"/v1/isolated/session/{first}"
            query = urlencode({"path": "escape"})
            assert request("GET", f"{first_files}/directories/list?{query}")[0] != 200
            query = urlencode({"path": "escape/secret.txt"})
            assert request("GET", f"{first_files}/files/download?{query}")[0] != 200
            assert (
                request(
                    "POST",
                    f"{first_files}/directories",
                    {"escape/created-by-a": {"mode": 755}},
                )[0]
                != 200
            )
            assert (
                request(
                    "POST",
                    f"{first_files}/files/permissions",
                    {"escape/secret.txt": {"mode": 600}},
                )[0]
                != 200
            )
            assert not (second_root / "created-by-a").exists()
            assert secret.stat().st_mode & 0o777 == 0o644
            assert secret.read_text() == "project-b-secret"
            marker = f"execd-descendant-{uuid4().hex}"
            daemon = """
import os
import signal
import sys
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
    os.write(writer, b'1')
    os.close(writer)
    while True:
        signal.pause()
os.close(writer)
assert os.read(reader, 1) == b'1'
os.close(reader)
os.waitpid(child, 0)
print('descendant-ready')
"""
            command = "python3 -c " + shlex.quote(daemon) + " " + marker
            status, payload = request(
                "POST", f"/v1/isolated/session/{first}/run", {"code": command}
            )
            assert status == 200 and "descendant-ready" in payload, payload
            for process_dir in Path("/proc").iterdir():
                if not process_dir.name.isdigit():
                    continue
                try:
                    arguments = (process_dir / "cmdline").read_bytes().split(b"\0")
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if marker.encode() in arguments:
                    pidfds.append(os.pidfd_open(int(process_dir.name)))
            assert pidfds, "the setsid/double-fork descendant was not observed"
            ready, _, _ = select.select(pidfds, [], [], 0)
            assert not ready, "the descendant exited before deletion"
            status, payload = request(
                "POST",
                f"/v1/isolated/session/{first}/run",
                {
                    "code": (
                        "mkdir -p /workspace/a/.execd && "
                        "mkfifo /workspace/a/.execd/background-runs"
                    )
                },
            )
            assert status == 200 and '"type":"execution_complete"' in payload, payload
            assert stat.S_ISFIFO(
                Path("/workspace/a/.execd/background-runs").stat().st_mode
            )
            protected_user_file = first_root / ".probe-user-data"
            protected_user_file.write_text("keep-user-data")
            pinned_root = Path("/workspace/a-pinned")
            first_root.rename(pinned_root)
            first_root.symlink_to(second_root, target_is_directory=True)
            assert (
                request(
                    "POST",
                    f"{first_files}/directories",
                    {"pinned-write": {"mode": 755}},
                )[0]
                == 200
            )
            assert (pinned_root / "pinned-write").is_dir()
            assert not (second_root / "pinned-write").exists()
            protected_probe = second_root / ".probe-project-b"
            protected_probe.write_text("preserve-b")
            status, payload = delete(first)
            assert status == 200, payload
            assert (pinned_root / ".probe-user-data").read_text() == "keep-user-data"
            assert protected_probe.read_text() == "preserve-b", (
                "A cleanup traversed the replacement root"
            )
            ready, _, _ = select.select(pidfds, [], [], 0)
            assert len(ready) == len(pidfds), (
                "DELETE returned before descendant exit notification"
            )
            status, payload = request(
                "POST",
                f"/v1/isolated/session/{second}/run",
                {"code": "printf 'project-b-alive\\n'"},
            )
            assert status == 200 and "project-b-alive" in payload, payload
            for descriptor in Path(f"/proc/{process.pid}/fd").iterdir():
                try:
                    target = str(descriptor.readlink())
                except FileNotFoundError:
                    continue
                assert not target.startswith("/workspace/a"), target
            first_root.unlink()
            shutil.rmtree(pinned_root)
            assert create(first, Path("/workspace/a"))[0] == 409
            assert delete(first)[0] == 200

            readiness_path = second_root / "background-ready.sock"
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as readiness:
                    readiness.settimeout(30)
                    readiness.bind(str(readiness_path))
                    readiness_path.chmod(0o666)
                    readiness.listen(1)
                    background = (
                        "import signal, socket; "
                        "connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); "
                        f"connection.connect({str(readiness_path)!r}); "
                        "connection.sendall(b'R'); connection.close(); signal.pause()"
                    )
                    status, payload = request(
                        "POST",
                        f"/v1/isolated/session/{second}/run",
                        {
                            "code": "python3 -I -S -c " + shlex.quote(background),
                            "background": True,
                        },
                    )
                    assert status == 202, payload
                    with readiness.accept()[0] as ready_connection:
                        ready_connection.settimeout(30)
                        assert ready_connection.recv(1) == b"R"
            finally:
                readiness_path.unlink(missing_ok=True)
            run_id = json.loads(payload)["run_id"]
            status, payload = request(
                "POST",
                f"/v1/isolated/session/{second}/run",
                {
                    "code": (
                        f"rm -f /workspace/b/.execd/background-runs/{run_id}.log "
                        f"/workspace/b/.execd/background-runs/{run_id}.code && "
                        f"mkfifo /workspace/b/.execd/background-runs/{run_id}.log "
                        f"/workspace/b/.execd/background-runs/{run_id}.code"
                    )
                },
            )
            assert status == 200 and '"type":"execution_complete"' in payload, payload
            for suffix in ("log", "code"):
                control_file = Path(
                    f"/workspace/b/.execd/background-runs/{run_id}.{suffix}"
                )
                assert stat.S_ISFIFO(control_file.stat().st_mode)
            status, payload = request(
                "GET", f"/v1/isolated/session/{second}/runs/{run_id}/logs"
            )
            assert status == 400 and "regular file" in payload, payload
            assert delete(second)[0] == 200
            for descriptor in Path(f"/proc/{process.pid}/fd").iterdir():
                try:
                    target = str(descriptor.readlink())
                except FileNotFoundError:
                    continue
                assert not target.startswith("/workspace/b"), target
            shutil.rmtree(second_root)
            assert delete(second)[0] == 200
            sessions.clear()
            status, payload = request("GET", "/v1/isolated/sessions")
            assert status == 200 and json.loads(payload)["sessions"] == [], payload
            query = urlencode({"session_namespace": str(uuid4())})
            assert (
                request("DELETE", f"/v1/isolated/session/{retired}?{query}")[0] == 409
            )
            print(
                json.dumps(
                    {
                        "result": "passed",
                        "descendant_pidfds": len(pidfds),
                        "namespace": session_namespace,
                    }
                ),
                flush=True,
            )
        finally:
            try:
                if process.poll() is None and session_namespace:
                    for identity in sessions:
                        query = urlencode({"session_namespace": session_namespace})
                        request("DELETE", f"/v1/isolated/session/{identity}?{query}")
            finally:
                for descriptor in pidfds:
                    os.close(descriptor)
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                if log_reader.ident is not None:
                    log_reader.join()
                output.close()
                log.seek(0)
                print(log.read(), flush=True)


if __name__ == "__main__":
    main()
