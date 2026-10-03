"""Execute OCI publication guards without accessing Docker or a registry."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path
from tempfile import TemporaryDirectory

DOCKER_STUB = r"""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

root = Path(os.environ["CONTRACT_ROOT"])
scenario = os.environ["CONTRACT_SCENARIO"]
arguments = sys.argv[1:]
anonymous = arguments[:1] == ["--config"]
if anonymous:
    config = Path(arguments[1])
    assert config.is_dir() and not list(config.iterdir())
    with (root / "anonymous-configs").open("a") as record:
        record.write(str(config) + "\n")
    arguments = arguments[2:]
assert arguments[:2] == ["buildx", "imagetools"], arguments
operation = arguments[2]
if operation == "create":
    (root / "created").write_text(json.dumps(arguments[3:]))
    raise SystemExit(0)
assert operation == "inspect", arguments
reference = arguments[3]
if "@sha256:" not in reference:
    if (root / "created").exists() or scenario == "existing-tag":
        architectures = ["amd64", "arm64"]
    else:
        message = "registry unavailable" if scenario == "registry-error" else "manifest unknown"
        print(message, file=sys.stderr)
        raise SystemExit(1)
else:
    architecture = "amd64" if reference.endswith("a" * 64) else "arm64"
    if anonymous and (
        scenario == "private"
        or scenario == "private-arm64" and architecture == "arm64"
        or scenario == "candidate-network-error"
    ):
        print("anonymous candidate unavailable", file=sys.stderr)
        raise SystemExit(1)
    architectures = ["arm64" if scenario == "wrong-platform" else architecture]
print(json.dumps({
    "digest": "sha256:" + "c" * 64,
    "manifests": [{"platform": {"os": "linux", "architecture": value}} for value in architectures],
}))
"""


def publication_script(component: str) -> str:
    """Read the executable publication step from its checked-in workflow."""
    repository = Path(__file__).resolve().parents[1]
    workflow = repository / ".github" / "workflows" / f"opensandbox-{component}.yml"
    step = workflow.read_text().split("      - name: Publish OCI index\n", 1)[1]
    body = step.split("        run: |\n", 1)[1]
    lines: list[str] = []
    for line in body.splitlines():
        if line and not line.startswith("          "):
            break
        lines.append(line)
    return textwrap.dedent("\n".join(lines))


def verify_publication(component: str, scenario: str) -> None:
    """Require anonymous candidates before creating an immutable version tag."""
    with TemporaryDirectory(prefix="tinkerfin-release-contract-") as directory:
        root = Path(directory)
        docker = root / "docker"
        docker.write_text(DOCKER_STUB)
        docker.chmod(0o700)
        digests = root / f"{component}-digests"
        digests.mkdir()
        (digests / ("amd64-" + "a" * 64)).touch()
        if scenario != "missing-arm64":
            (digests / ("arm64-" + "b" * 64)).touch()
        output = root / "output"
        image = f"ghcr.io/tinkerfin-ai/opensandbox-{component}"
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail"],
            input=publication_script(component),
            text=True,
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "PATH": str(root) + os.pathsep + os.environ["PATH"],
                "CONTRACT_ROOT": directory,
                "CONTRACT_SCENARIO": scenario,
                "RUNNER_TEMP": directory,
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(root / "summary"),
                "IMAGE_NAME": image,
                f"{component.upper()}_VERSION": "0.1.0-contract",
                "TMPDIR": directory,
            },
        )
        if scenario == "public":
            assert result.returncode == 0, result.stderr
            created = json.loads((root / "created").read_text())
            assert created == [
                "--tag",
                image + ":0.1.0-contract",
                image + "@sha256:" + "a" * 64,
                image + "@sha256:" + "b" * 64,
            ], created
            assert output.read_text() == "digest=sha256:" + "c" * 64 + "\n"
        else:
            assert result.returncode != 0, (component, scenario, result.stdout)
            assert not (root / "created").exists(), (component, scenario)
            assert not output.exists(), (component, scenario)
        configurations = root / "anonymous-configs"
        if configurations.exists():
            paths = configurations.read_text().splitlines()
            assert all(not Path(path).exists() for path in paths), paths
        if scenario == "public":
            assert configurations.exists()
            assert len(configurations.read_text().splitlines()) == 2
        print(f"publication contract passed: {component}/{scenario}")


def main() -> None:
    """Verify both release entry points against deterministic registry outcomes."""
    for component in ("server", "execd"):
        for scenario in (
            "private",
            "private-arm64",
            "candidate-network-error",
            "public",
            "existing-tag",
            "registry-error",
            "missing-arm64",
            "wrong-platform",
        ):
            verify_publication(component, scenario)


if __name__ == "__main__":
    main()
