# my-ragflow

RAGFlow 的学习版(Lite/教学版)。

目的不是复刻官方全部功能,而是以**读懂 + 手写**的方式学习 RAGFlow 的核心架构:

- 官方参考源码:纯 Python 版 [RAGFlow v0.24.0](https://github.com/infiniflow/ragflow/tree/v0.24.0)
- 技术栈:Python >=3.12、uv 管理依赖、Peewee ORM(数据库层)、ruamel/FileLock(配置层)
- 目录结构与官方 v0.24.0 对齐:`common/`(基础工具)、`conf/`(配置)、`api/`(API 与 DB 层),后续逐步补齐 `rag/`、`deepdoc/`、`agent/`

详细约定(提交规范、代码规范、参考源码位置)见 [AGENTS.md](AGENTS.md)。