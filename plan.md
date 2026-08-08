# Sandbox Runtime 实现计划

## 任务 1：建立失败的静态契约测试

- 文件：`tests/static-contract.sh`
- 描述：校验必须存在的镜像、版本锁、依赖锁、入口和发布文件，以及稳定路径与架构约束
- 验证：测试在实现文件不存在时失败
- 依赖：无

## 任务 2：建立运行时冒烟测试

- 文件：`tests/runtime-smoke.sh`、`tests/entrypoint-env.sh`
- 描述：覆盖语言版本、编译、Python 包、无界面绘图、默认环境与 EXECD_ENVS
- 验证：无镜像时明确失败，入口脚本实现前测试失败
- 依赖：任务 1

## 任务 3：实现版本和依赖锁

- 文件：`versions.env`、`requirements.in`、`requirements.lock`
- 描述：固定运行时主版本、镜像 tag 和 Python 全量依赖/hash
- 验证：静态契约测试通过锁文件部分，uv 可重复解析
- 依赖：任务 1

## 任务 4：实现入口脚本

- 文件：`scripts/entrypoint.sh`
- 描述：幂等同步允许的运行时环境并透明执行命令，无命令时保持运行
- 验证：`tests/entrypoint-env.sh`
- 依赖：任务 2

## 任务 5：实现多阶段镜像

- 文件：`Dockerfile`、`.dockerignore`、`pip.conf`
- 描述：组合单版本 Python/JDK/Node/Go/Maven，构建 venv 并清理缓存
- 验证：Docker build、BuildKit check、静态契约测试
- 依赖：任务 3、4

## 任务 6：完善项目文档与治理

- 文件：`README.md`、`SECURITY.md`、`LICENSE`、`THIRD_PARTY_NOTICES.md`
- 描述：说明兼容矩阵、公开使用、镜像源覆盖、维护与安全报告流程
- 验证：链接、命令、OCI 标签和许可证检查
- 依赖：任务 5

## 任务 7：实现 CI 和发布

- 文件：`.github/workflows/ci.yml`、`.github/workflows/release.yml`、Dependabot 配置
- 描述：双架构构建测试、GHCR 发布、SBOM/provenance、签名和漏洞扫描
- 验证：actionlint/yamllint 等静态检查及 GitHub Actions 实际运行
- 依赖：任务 5、6

## 任务 8：发布与集成

- 文件：TinkerFin `models.py`、相关测试和 OpenSandbox 集成文档
- 描述：发布候选/稳定镜像，固定 manifest digest，保留业务 initializer 边界
- 验证：匿名拉取、真实 OpenSandbox 0.1.14、pytest、Ruff、Pyright
- 依赖：任务 7

## 任务 9：审查与交付

- 文件：`review.md`、`final_report.md`
- 描述：逐项核对设计、测试、体积、性能、供应链和未核验项
- 验证：两个仓库工作树、远端 release/package 和所有检查均有可复查证据
- 依赖：任务 8
