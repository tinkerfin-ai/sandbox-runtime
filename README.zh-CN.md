# TinkerFin Sandbox Runtime

[English](README.md)

[![CI](https://github.com/tinkerfin-ai/sandbox-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/tinkerfin-ai/sandbox-runtime/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

面向 OpenSandbox 和代码智能体的多架构 Linux 运行时镜像。镜像为每种工具链只保留
一个版本，并提供可直接使用的 Python 环境，沙箱启动时无需再下载常用依赖。

## 快速使用

```bash
docker run --rm ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.1 python --version
docker run --rm ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.1 mvn --version
```

测试时可以使用精确版本标签；生产环境应固定 OCI manifest digest。精确版本标签
不可变，不要把 `latest` 作为更新渠道。

## 内置环境

| 组件 | 版本 |
| --- | --- |
| Python | 3.11.15 |
| OpenJDK | 21 |
| Node.js / npm | 22.23.2 / 12.0.2 |
| Go | 1.25.12 |
| Apache Maven | 3.9.9 |

Python 虚拟环境位于 `/opt/sandbox-runtime/venv`，预装 NumPy、pandas、
Matplotlib、Requests 和 Beautiful Soup。镜像还包含 Bash、GCC/G++、Make、Git、
curl、jq、ripgrep 及常用归档工具；Matplotlib 默认使用 `Agg` 后端。

OCI 镜像支持 `linux/amd64` 和 `linux/arm64`。Intel Mac 与 Apple Silicon Mac
均可通过 Docker Desktop 自动选择对应的 Linux 镜像。每个平台的压缩体积约
0.5 GB，解压后约 1.44 GB。

## 接入 OpenSandbox

使用 OpenSandbox 0.1.x 创建沙箱时，需要显式传入镜像入口：

```python
from datetime import timedelta

from opensandbox import SandboxSync

sandbox = SandboxSync.create(
    "ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.1",
    entrypoint=["/opt/sandbox-runtime/bin/entrypoint.sh"],
    timeout=timedelta(hours=2),
)
```

工具链环境变量已经写入镜像。OpenSandbox 提供 `EXECD_ENVS` 文件时，入口脚本会
同步受支持的环境变量。业务文件、凭据、Skills 和业务专用依赖应由使用方自行注入。

## 软件源配置

镜像默认使用官方软件源。如需区域镜像或代理，可在运行时覆盖：

| 生态 | 配置方式 |
| --- | --- |
| Python | `PIP_INDEX_URL` |
| Node.js | `NPM_CONFIG_REGISTRY` |
| Go | `GOPROXY` |
| Maven | `/root/.m2/settings.xml` |

不要把软件源凭据写入派生镜像。

## 构建与验证

构建需要 Docker Buildx。只有重新生成 Python 依赖锁时才需要安装
[`uv`](https://docs.astral.sh/uv/)。

```bash
make verify
make build IMAGE=sandbox-runtime:dev
make smoke IMAGE=sandbox-runtime:dev
make lock
```

`versions.env` 固定工具链版本和归档校验和，`requirements.lock` 使用哈希锁定
Python 依赖。

贡献、安全报告和第三方依赖信息请参阅 [CONTRIBUTING.md](CONTRIBUTING.md)、
[SECURITY.md](SECURITY.md) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
