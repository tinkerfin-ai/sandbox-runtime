"""Prepare project-local dependencies and a Run-local proxy inside its namespace.

Run this only after execd has established the non-root private namespace. Nothing
in this module runs against the privileged parent filesystem. Incomplete Python
environments carry the native session identity so the workspace owner can remove
them after that session's namespace has been confirmed stopped.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from uuid import UUID

_RUNTIME = Path("/opt/sandbox-runtime")
_DEPENDENCIES = Path("/dependencies")
_PROXY = "http://127.0.0.1:18080"


def prepare_python(session_id: str) -> Path:
    """Publish a complete project Python environment without replacing an existing one."""
    if str(UUID(session_id)) != session_id:
        raise ValueError("session_id must be a canonical UUID")
    target = _DEPENDENCIES / "python"
    if target.exists():
        if (
            not (target / "pyvenv.cfg").is_file()
            or not (target / "bin" / "python").is_file()
        ):
            raise RuntimeError("project Python environment is incomplete")
        return target
    temporary = _DEPENDENCIES / f".python-{session_id}"
    temporary.mkdir(mode=0o700)
    try:
        venv.EnvBuilder(with_pip=True, symlinks=True).create(temporary)
        python_directory = f"python{sys.version_info.major}.{sys.version_info.minor}"
        site = temporary / "lib" / python_directory / "site-packages"
        shared = _RUNTIME / "venv" / "lib" / python_directory / "site-packages"
        (site / "runtime-libraries.pth").write_text(f"{shared}\n")
        for launcher in (temporary / "bin").iterdir():
            if launcher.is_symlink() or not launcher.is_file():
                continue
            content = launcher.read_bytes()
            if b"\x00" not in content:
                launcher.write_bytes(
                    content.replace(str(temporary).encode(), str(target).encode())
                )
        configuration = temporary / "pyvenv.cfg"
        configuration.write_text(
            configuration.read_text().replace(str(temporary), str(target))
        )
        try:
            temporary.rename(target)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            if (
                not (target / "pyvenv.cfg").is_file()
                or not (target / "bin" / "python").is_file()
            ):
                raise RuntimeError(
                    "another Run published an incomplete Python environment"
                ) from None
        return target
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def project_environment() -> dict[str, str]:
    """Return explicit project paths without inheriting parent credentials or state."""
    return {
        "HOME": "/home/workspace",
        "USER": "workspace",
        "LOGNAME": "workspace",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": "/tmp",
        "XDG_CACHE_HOME": "/cache",
        "VIRTUAL_ENV": "/dependencies/python",
        "PATH": (
            "/dependencies/python/bin:/dependencies/node/bin:/dependencies/go/bin:"
            "/opt/sandbox-runtime/venv/bin:/opt/sandbox-runtime/node/bin:"
            "/opt/sandbox-runtime/go/bin:/opt/sandbox-runtime/jdk/bin:"
            "/opt/sandbox-runtime/maven/bin:/usr/local/bin:/usr/bin:/bin"
        ),
        "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CONFIG_FILE": "/dev/null",
        "PIP_INDEX_URL": "https://pypi.org/simple",
        "NPM_CONFIG_PREFIX": "/dependencies/node",
        "NPM_CONFIG_CACHE": "/cache/npm",
        "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/",
        "GOPATH": "/dependencies/go",
        "GOCACHE": "/cache/go-build",
        "GOPROXY": "https://proxy.golang.org,direct",
        "GOROOT": "/opt/sandbox-runtime/go",
        "JAVA_HOME": "/opt/sandbox-runtime/jdk",
        "MAVEN_HOME": "/opt/sandbox-runtime/maven",
        "MAVEN_OPTS": "-Dmaven.repo.local=/dependencies/maven",
        "MPLBACKEND": "Agg",
        "PLAYWRIGHT_BROWSERS_PATH": "/opt/sandbox-runtime/browsers",
        "HTTP_PROXY": _PROXY,
        "HTTPS_PROXY": _PROXY,
        "http_proxy": _PROXY,
        "https_proxy": _PROXY,
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
    }


def start_relay(egress_token: str) -> None:
    """Wait for the relay's explicit listen notification, never a scheduling delay."""
    with Path("/run/relay-error.log").open("wb") as error_log:
        process = subprocess.Popen(
            [
                str(_RUNTIME / "venv/bin/python"),
                "-I",
                "-S",
                "-c",
                "import runpy,sys;sys.path.insert(0,'/opt/sandbox-runtime/workspaces');runpy.run_module('relay',run_name='__main__')",
                "--socket",
                "/run/egress.sock",
                "--port",
                "18080",
                "--egress-token",
                egress_token,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=error_log,
            close_fds=True,
            env=project_environment(),
        )
    try:
        assert process.stdout is not None
        try:
            notification = process.stdout.readline(4097)
        finally:
            process.stdout.close()
        if len(notification) > 4096 or json.loads(notification) != {
            "ready": True,
            "port": 18080,
        }:
            raise RuntimeError("workspace proxy did not become ready")
    except BaseException:
        process.kill()
        process.wait()
        raise


def main() -> None:
    """Initialize one native session and report its explicit command environment."""
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    parser.add_argument("egress_token")
    arguments = parser.parse_args()
    if os.getuid() == 0 or os.getgid() == 0:
        raise RuntimeError("workspace initialization must run as a non-root user")
    os.chdir("/workspace")
    os.environ.clear()
    os.environ.update(project_environment())
    prepare_python(arguments.session_id)
    start_relay(arguments.egress_token)
    print(json.dumps(project_environment(), separators=(",", ":")))


if __name__ == "__main__":
    main()
