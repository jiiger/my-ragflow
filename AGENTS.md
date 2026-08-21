# AGENTS.md

本文件是本仓库的本地操作指南,供在本仓库内工作的 Agent 阅读并遵守。

## 项目定位:这是 RAGFlow 的学习版

本仓库是 **RAGFlow 的学习版(为学习 Python 而精简复刻的工程)**,不是官方代码的镜像:

- 目标:读懂 RAGFlow 的核心流程(API → RAG → DeepDoc 解析链),并逐步手写复现,代码以能读懂、能跑通、能扩展为准
- 官方参考源码(纯 Python 版):`/home/yang/code/ragflow`,分支 `python-learning`,基于官方 tag v0.24.0
  - v0.24.0 是纯 Python 的最后一代:643 个 .py、0 个 Go 文件,适合学习
  - 注意:该仓库 main 分支(dev 主线)已大规模迁到 Go(cmd/ internal/ 等),不要拿它当 Python 参考
- 从官方抄录/借鉴代码时,在 commit body 注明来源(官方文件路径 + 版本号)

## 工程状态与结构(根目录布局,对齐官方 v0.24.0)

- Python >=3.12,uv 管理依赖(pyproject.toml / uv.lock / .python-version)
- 当前已有:
  - `common/`:基础工具包(config_utils / constants / settings / exceptions / file_utils / misc_utils / time_utils)
  - `conf/`:配置文件(service_conf.yaml)
  - `api/`:API 层骨架(init.py / constants.py / db / common / utils)
- 后续按官方 v0.24.0 的结构逐步补齐 `rag/`、`deepdoc/`、`agent/` 等模块

## 开发与验证

- 安装依赖:`uv sync --python 3.12`(已配置清华 TUNA 镜像 index,见 pyproject.toml)
- 静态检查:`uv run ruff check .`(line-length=200;ignore 已含 E402/BLE001/TRY004/PERF102/LOG015,不要为单文件再改 ignore)
- 语法检查:`python -m compileall <目录>`
- 测试:`uv run pytest`(test 目录,pytest 配置见 pyproject.toml)

## Git 提交规范(标准提交)

所有提交遵循 Conventional Commits 标准,格式:

    <type>(<scope>): <subject>

- **type 必填**,常用取值:
  - `feat` 新功能
  - `fix` 修 bug
  - `docs` 文档(README / AGENTS.md / 注释)
  - `refactor` 重构(不新增功能、不改 bug)
  - `chore` 杂项(依赖、配置、格式化、忽略规则)
  - `test` 测试相关
  - `style` 纯格式调整
  - `perf` 性能优化
- **scope 可选**:涉及的具体模块,如 `feat(common)`、`feat(api/db)`
- **subject 用中文**,动词开头,一句话说清"做了什么",尽量 ≤50 字
- 必要时加 commit body,空一行后说明"为什么这么做"

示例(与仓库已有提交风格保持一致):

    docs: 新增 AGENTS.md, 声明学习版定位与提交规范
    feat(common): 新增 config_utils/constants/settings 包与 conf 配置, 工程改为根目录布局
    chore: ruff 全局忽略 BLE001/TRY004/PERF102/LOG015; 移除根目录重复 settings.py

规则:

1. 一个提交只做一件事,保持原子提交
2. 未完成、跑不通的中间态代码不提交
3. 提交前先 `uv run ruff check .` 确认无新增错误
4. 抄录/借鉴官方源码的改动,commit body 注明来源