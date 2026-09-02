#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""高级 RAG(与官方 rag/advanced_rag/__init__.py 1:1 移植, 2026-09-02)。

对外只暴露深度研究器 TreeStructuredQueryDecompositionRetrieval, 别名
DeepResearcher —— dialog_service 的 reasoning 分支直接
`from rag.advanced_rag import DeepResearcher` 使用。
"""

from .tree_structured_query_decomposition_retrieval import TreeStructuredQueryDecompositionRetrieval as DeepResearcher

__all__ = ["DeepResearcher"]