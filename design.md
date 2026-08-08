# Sandbox Runtime 设计

## 问题

OpenSandbox 的通用 Code Interpreter 镜像同时携带多版本语言和 Jupyter 内核，
当前 arm64 解压体积约 9.37 GB。ERP 还会在每次沙箱初始化时创建 Python 虚拟
环境并联网安装通用依赖，导致启动慢且运行结果受网络影响。

## 方案比较

### 继续使用上游镜像

兼容成本最低，但无法消除多版本语言、Jupyter 和重复依赖带来的体积与启动开销。

### 在 TinkerFin 仓库内维护镜像

代码与消费者同步方便，但镜像生命周期和应用版本耦合，不利于其他项目独立使用。

### 独立运行时仓库

由 `tinkerfin-ai/sandbox-runtime` 独立维护 OCI 镜像，TinkerFin 只消费不可变
digest。发布、漏洞修复和语言补丁可独立演进，用户也能直接使用。采用此方案。

## 运行时契约

镜像只发布 `linux/amd64` 和 `linux/arm64`。Apple Silicon 通过 Docker Desktop
运行 arm64 Linux 镜像，Intel Mac 和常见 Linux 主机运行 amd64 镜像；不发布
Darwin 容器。

Python 3.11、JDK 21、Node.js 22、Go 1.25、Maven 3.9 和编译工具使用单一版本。
Python 默认环境固定为 `/opt/sandbox-runtime/venv`，预装 numpy、pandas、
matplotlib、requests 和 beautifulsoup4。镜像不包含 Jupyter、Skills 或业务文件。

## OpenSandbox 生命周期

OpenSandbox Server 0.1.14 在用户入口前启动 execd，因此固定 PATH 必须声明为
Dockerfile `ENV`，不能依赖入口脚本后置修改。入口脚本只在服务提供
`EXECD_ENVS` 时同步同一组环境，并执行传入命令；无命令时保持容器存活。

包管理器默认使用官方源。调用方可通过标准环境变量或 Maven settings 覆盖，镜像
不会持久化密钥，也不会在启动时联网安装依赖。

## 发布与维护

源码采用 Apache-2.0。Pull Request 构建并冒烟测试两个架构；版本 tag 发布公开
GHCR 镜像、SBOM、provenance 和 keyless 签名。不可变版本 tag 永不覆盖，
TinkerFin 固定到 manifest digest，`latest` 只供交互试用。

## 成功标准

- 两个架构均可直接运行全部语言和预装 Python 包
- 真实 OpenSandbox 创建、执行和重连不需要手工 PATH 包装
- 每架构压缩体积不超过 1 GB，解压体积不超过 2 GB
- 镜像缓存后 OpenSandbox 就绪不超过 15 秒
- 公开 GHCR 镜像可以匿名拉取并验证来源
