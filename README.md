# TinkerFin Sandbox Runtime

[![CI](https://github.com/tinkerfin-ai/sandbox-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/tinkerfin-ai/sandbox-runtime/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

A compact, multi-architecture Linux runtime image for
[OpenSandbox](https://github.com/alibaba/OpenSandbox) and general-purpose coding
agents. The image provides one maintained version of each language toolchain and
starts without creating a virtual environment or downloading common Python
packages.

## Included toolchains

| Toolchain | Version | Stable location |
| --- | --- | --- |
| Python | 3.11.15 | `/opt/sandbox-runtime/python` |
| Python virtual environment | 3.11 | `/opt/sandbox-runtime/venv` |
| OpenJDK | 21 | `/opt/sandbox-runtime/jdk` |
| Node.js | 22.23.2 | `/opt/sandbox-runtime/node` |
| npm | 10.9.9 | `/opt/sandbox-runtime/node` |
| Go | 1.25.7 | `/opt/sandbox-runtime/go` |
| Apache Maven | 3.9.9 | `/opt/sandbox-runtime/maven` |

The Python environment includes NumPy, pandas, Matplotlib, Requests, and
Beautiful Soup. Bash, GCC/G++, Make, Git, curl, jq, ripgrep, and common archive
tools are also available. Matplotlib defaults to the non-interactive `Agg`
backend.

Maven 3.9.9 is intentionally used instead of Maven 4. The image does not seed
the Maven local repository, so application artifacts remain a consumer-owned
cache and are never baked into the public runtime.

## Platforms

Published OCI indexes contain:

- `linux/amd64` for x86-64 Linux and Intel Mac Docker hosts
- `linux/arm64` for ARM64 Linux and Apple Silicon Docker hosts

Containers always run Linux. There is no Darwin container format; Docker Desktop
selects the matching Linux image automatically on macOS.

## Use the image

After the first public release:

```bash
docker run --rm ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.0 python --version
docker run --rm ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.0 mvn --version
```

Use an immutable manifest digest in production. Version tags are immutable;
`latest` is intended only for interactive evaluation.

For OpenSandbox 0.1.x, pass the runtime entrypoint as the sandbox command:

```python
from datetime import timedelta

from opensandbox import SandboxSync

sandbox = SandboxSync.create(
    "ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.0",
    entrypoint=["/opt/sandbox-runtime/bin/entrypoint.sh"],
    timeout=timedelta(hours=2),
)
```

The fixed runtime environment is declared in the image. When OpenSandbox
provides an `EXECD_ENVS` file, the entrypoint synchronizes the same allowlisted
values without deleting consumer-defined keys.

## Package mirrors and proxies

Official registries are the defaults. Override them at container or sandbox
creation time when a deployment needs a regional mirror:

| Ecosystem | Override |
| --- | --- |
| Python | `PIP_INDEX_URL` |
| Node.js | `NPM_CONFIG_REGISTRY` |
| Go | `GOPROXY` |
| Maven | Mount or create `/root/.m2/settings.xml` |

Do not bake credentials into a derived image. Inject short-lived credentials at
runtime and keep business-specific setup in a consumer initializer.

## Build and test

Docker Buildx and `uv` are required for a full development workflow:

```bash
make verify
make lock
make build IMAGE=sandbox-runtime:dev
make smoke IMAGE=sandbox-runtime:dev
```

`versions.env` is the source of toolchain versions and upstream archive hashes.
The Python dependency graph is fully pinned with hashes in `requirements.lock`.

## Scope

This repository owns only the reusable operating environment. It does not contain
skills, agent instructions, customer files, application source, or business
dependencies. Consumers can run their own idempotent initializer after sandbox
creation for those concerns.

See [CONTRIBUTING.md](CONTRIBUTING.md) for maintenance and verification rules and
[SECURITY.md](SECURITY.md) for vulnerability reporting.
