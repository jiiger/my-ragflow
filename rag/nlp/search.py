"""
rag.nlp.search — 官方 rag/nlp/search.py 的极简裁剪版。

当前仅提供 kb_app 等路由依赖的索引名工具 index_name;
检索/打分逻辑(FulltextQueryer/排序/去重等)与 rag_tokenizer 分词待后续移植。
来源: 官方 rag/nlp/search.py @ v0.24.0
"""


def index_name(uid: str) -> str:
    """生成租户对应的向量检索索引名, 格式 ragflow_<tenant_id>。"""
    return f"ragflow_{uid}"