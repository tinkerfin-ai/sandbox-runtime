# TinkerFin Sandbox Runtime

[English](README.md)

[![CI](https://github.com/tinkerfin-ai/sandbox-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/tinkerfin-ai/sandbox-runtime/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

面向 OpenSandbox 和代码智能体的多架构 Linux 运行时镜像。镜像为每种工具链只保留
一个版本，内置无界面 Chromium，并提供可直接使用的 Python 环境，
沙箱启动时无需再下载常用依赖。

## 快速使用

```bash
docker run --rm ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.5 python --version
docker run --rm ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.5 mvn --version
```

测试时可以使用精确版本标签；生产环境应固定 OCI manifest digest。精确版本标签
不可变，不要把 `latest` 作为更新渠道。

## 内置环境

| 组件 | 版本 |
| --- | --- |
| Python | 3.11.15 |
| OpenJDK | 21 |
| Node.js / npm | 22.23.2 / 12.0.2 |
| Go | 1.25.13 |
| Apache Maven | 3.9.9 |
| Python Playwright | 1.62.0 |
| Chromium 无界面版本 | Playwright 修订号 1234|

Python 虚拟环境位于 `/opt/sandbox-runtime/venv`，预装 NumPy、pandas、
Matplotlib、Requests、Beautiful Soup 和 Playwright。Chromium 无界面版本
预装在 `/opt/sandbox-runtime/browsers`，由 `PLAYWRIGHT_BROWSERS_PATH` 指定。
浏览器操作和截图无需在沙箱启动时下载浏览器。镜像还包含 Bash、GCC/G++、Make、Git、
curl、jq、ripgrep 及常用归档工具；Matplotlib 默认使用 `Agg` 后端。

OCI 镜像支持 `linux/amd64` 和 `linux/arm64`。Intel Mac 与 Apple Silicon Mac
均可通过 Docker Desktop 自动选择对应的 Linux 镜像。包含浏览器的每个平台镜像
解压后约 2.2～2.3 GB。

## 接入 OpenSandbox

使用 OpenSandbox SDK 创建沙箱时，需要显式传入镜像入口：

```python
from datetime import timedelta

from opensandbox import SandboxSync

sandbox = SandboxSync.create(
    "ghcr.io/tinkerfin-ai/sandbox-runtime:0.1.5",
    entrypoint=["/opt/sandbox-runtime/bin/entrypoint.sh"],
    timeout=timedelta(hours=2),
)
```

工具链环境变量已经写入镜像。OpenSandbox 提供 `EXECD_ENVS` 文件时，入口脚本会
同步受支持的环境变量。业务文件、凭据、Skills 和业务专用依赖应由使用方自行注入。

使用项目工作区时，请部署受控的 [OpenSandbox Server](opensandbox-server/README.md)
与 [OpenSandbox Execd](opensandbox-execd/README.md)，并将服务端 Docker 网络配置为
`bridge`；隔离会话会拒绝上游默认的 `host` 网络，以及共享宿主或其他容器网络命名空间的配置。
服务端为具有额外权限的 execd 父容器启用鉴权，并向可信客户端提供端点认证请求头。
控制面和原始 execd 接口只能由可信业务代码访问；不可信命令必须使用非 root 隔离会话，
并显式限制文件系统和网络访问。
工作区恢复要求 Docker 提供私有、可写的 `overlay` 根文件系统。
外部存储不得覆盖 `/var/lib/tinkerfin-workspaces`、其父目录或子目录，也不得覆盖隔离控制目录。
与这些路径无关的挂载不受影响。

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
make workspace-test IMAGE=sandbox-runtime:dev
make workspace-restart-test IMAGE=sandbox-runtime:dev
make lock
```

`versions.env` 固定工具链版本和归档校验和，`requirements.lock` 使用哈希锁定
Python 依赖。运行测试覆盖现有工具链，并在断网环境下启动无界面 Chromium，
验证 JavaScript 交互和 PNG 截图。`make smoke` 还会针对镜像内安装的辅助程序运行
工作区契约测试；`make workspace-test` 可以单独运行这些测试，覆盖项目存储、
Python 环境、受控出网与取消。`make workspace-restart-test` 下载固定的 execd 依赖，
验证测试容器正常停止或强制终止后再次启动时保留项目数据并拒绝旧会话，且只清理自身创建的测试容器。
使用 Playwright 默认的 `chromium.launch(headless=True)`；
有界面运行或显式选择完整浏览器通道时，需要另行提供对应浏览器。

贡献、安全报告和第三方依赖信息请参阅 [CONTRIBUTING.md](CONTRIBUTING.md)、
[SECURITY.md](SECURITY.md) 与 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
