"""
rag.prompts — 提示词包: 模板(.md) + 运行时组装器(generator)。

包级直接暴露 generator 的全部公开名字, 用法: from rag.prompts import keyword_extraction。
来源: 官方 rag/prompts/__init__.py @ v0.24.0
"""
from . import generator

__all__ = [name for name in dir(generator)
           if not name.startswith('_')]

globals().update({name: getattr(generator, name) for name in __all__})