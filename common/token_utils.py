"""token_utils.py — 令牌(token)计数/截断工具, 对齐官方 v0.24.0。

学习要点:
- tiktoken 是 OpenAI 的 BPE 分词器, cl100k_base 编码(与 gpt-3.5-turbo 同族)
- 模块加载时就初始化 encoder(首次运行会下载 BPE 词表到项目根目录的缓存)
- 用途: 切块模板按 token 数切文本(chunk_token_num 就是 token 上限)、
  对话前的上下文裁剪、LLM 返回的 usage 统计
"""
import os

import tiktoken

from common.file_utils import get_project_base_directory

tiktoken_cache_dir = get_project_base_directory()
os.environ["TIKTOKEN_CACHE_DIR"] = tiktoken_cache_dir
encoder = tiktoken.get_encoding("cl100k_base")


def num_tokens_from_string(string: str) -> int:
    """统计一段文本的 token 数(编码失败返回 0, 不抛异常)。"""
    try:
        code_list = encoder.encode(string)
        return len(code_list)
    except Exception:
        return 0


def total_token_count_from_response(resp):
    """从各家 LLM 的响应里提取总 token 数, 兼容多种响应结构, 取不到返回 0。

    依次尝试: usage.total_tokens / usage_metadata.total_tokens /
    meta.billed_units.input_tokens / dict 形式的 usage / meta.tokens。
    """
    if resp is None:
        return 0

    try:
        if hasattr(resp, "usage") and hasattr(resp.usage, "total_tokens"):
            return resp.usage.total_tokens
    except Exception:
        pass

    try:
        if hasattr(resp, "usage_metadata") and hasattr(resp.usage_metadata, "total_tokens"):
            return resp.usage_metadata.total_tokens
    except Exception:
        pass

    try:
        if hasattr(resp, "meta") and hasattr(resp.meta, "billed_units") and hasattr(resp.meta.billed_units, "input_tokens"):
            return resp.meta.billed_units.input_tokens
    except Exception:
        pass

    if isinstance(resp, dict) and "usage" in resp and "total_tokens" in resp["usage"]:
        try:
            return resp["usage"]["total_tokens"]
        except Exception:
            pass

    if isinstance(resp, dict) and "usage" in resp and "input_tokens" in resp["usage"] and "output_tokens" in resp["usage"]:
        try:
            return resp["usage"]["input_tokens"] + resp["usage"]["output_tokens"]
        except Exception:
            pass

    if isinstance(resp, dict) and "meta" in resp and "tokens" in resp["meta"] and "input_tokens" in resp["meta"]["tokens"] and "output_tokens" in resp["meta"]["tokens"]:
        try:
            return resp["meta"]["tokens"]["input_tokens"] + resp["meta"]["tokens"]["output_tokens"]
        except Exception:
            pass
    return 0


def truncate(string: str, max_len: int) -> str:
    """按 token 数截断文本到 max_len 以内(按 token 边界截, 不拆半个 token)。"""
    return encoder.decode(encoder.encode(string)[:max_len])
