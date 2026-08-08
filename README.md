# TinkerFin Sandbox Runtime

[简体中文](README.zh-CN.md)

[![CI](https://github.com/tinkerfin-ai/sandbox-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/tinkerfin-ai/sandbox-runtime/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

A multi-architecture Linux image for OpenSandbox and coding agents. It ships a
single version of each supported toolchain and a ready-to-use Python environment,
so sandbox startup does not need to download common dependencies.

## Quick start

```bash
docker run --rm ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.0 python --version
docker run --rm ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.0 mvn --version
```

Use a release tag for evaluation and pin the OCI manifest digest in production.
Version tags are immutable; no `latest` tag is published.

## Runtime

| Component | Version |
| --- | --- |
| Python | 3.11.15 |
| OpenJDK | 21 |
| Node.js / npm | 22.23.2 / 10.9.9 |
| Go | 1.25.7 |
| Apache Maven | 3.9.9 |

Python packages are installed in `/opt/sandbox-runtime/venv`: NumPy, pandas,
Matplotlib, Requests, and Beautiful Soup. The image also includes Bash, GCC/G++,
Make, Git, curl, jq, ripgrep, and common archive tools. Matplotlib uses the `Agg`
backend by default.

The published OCI index supports `linux/amd64` and `linux/arm64`. Docker Desktop
selects the matching Linux image on Intel and Apple Silicon Macs. The image is
about 0.5 GB compressed per platform and 1.44 GB unpacked.

## OpenSandbox

OpenSandbox 0.1.x requires the image entrypoint to be passed when a sandbox is
created:

```python
from datetime import timedelta

from opensandbox import SandboxSync

sandbox = SandboxSync.create(
    "ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.0",
    entrypoint=["/opt/sandbox-runtime/bin/entrypoint.sh"],
    timeout=timedelta(hours=2),
)
```

The runtime environment is built into the image. The entrypoint also propagates
the supported values through OpenSandbox's `EXECD_ENVS` file when it is present.
Application files, credentials, skills, and business-specific dependencies must
be supplied by the consumer.

## Package sources

Official registries are used by default. Deployments can override them at
runtime:

| Ecosystem | Configuration |
| --- | --- |
| Python | `PIP_INDEX_URL` |
| Node.js | `NPM_CONFIG_REGISTRY` |
| Go | `GOPROXY` |
| Maven | `/root/.m2/settings.xml` |

Do not store registry credentials in derived images.

## Build and verify

Docker Buildx is required. Install [`uv`](https://docs.astral.sh/uv/) only when
regenerating the Python lock file.

```bash
make verify
make build IMAGE=sandbox-runtime:dev
make smoke IMAGE=sandbox-runtime:dev
make lock
```

Toolchain versions and archive checksums are defined in `versions.env`.
Python dependencies are fully pinned with hashes in `requirements.lock`.

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for project policies and
dependency notices.
