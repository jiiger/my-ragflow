"""api/utils/api_utils.py — 学习版子集(只拆 knowledgebase_service 依赖的 4 个函数)。

官方 api_utils.py 有 735 行, 依赖 quart/APIToken/MCPToolCallSession/LLMFactoriesService
等还没建的东西。这里只保留自包含的部分:
- deep_merge: 递归合并配置字典
- _safe_jsonify: app 上下文里有 request 时返回 flask Response, 否则返回 dict
- get_data_error_result: 统一错误响应
- get_parser_config: 各切块模板(naive/qa/tag...)的默认配置合并

等 HTTP 层(第 2 步)再按需从官方补齐其余函数。
"""
import logging
from copy import deepcopy

from quart import jsonify, has_app_context

from common.constants import RetCode


def deep_merge(default: dict, custom: dict) -> dict:
    """递归合并两个字典, custom 的优先级更高(官方 api_utils.deep_merge 原封)。

    - 嵌套 dict 逐层合并; 非 dict 值(如 list/str)整体覆盖
    - 不修改入参, 返回新字典
    """
    merged = deepcopy(default)
    stack = [(merged, custom)]

    while stack:
        base_dict, override_dict = stack.pop()

        for key, val in override_dict.items():
            if key in base_dict and isinstance(val, dict) and isinstance(base_dict[key], dict):
                stack.append((base_dict[key], val))
            else:
                base_dict[key] = val

    return merged


def _safe_jsonify(payload: dict):
    """在 Quart 请求上下文中返回 Response, 否则(纯函数调用/测试)原样返回 dict。"""
    if has_app_context():
        return jsonify(payload)
    return payload


def get_data_error_result(code=RetCode.DATA_ERROR, message="Sorry! Data missing!"):
    """统一错误响应: {code, message}, message 为 None 时省略。"""
    logging.exception(Exception(message))
    result_dict = {"code": code, "message": message}
    response = {}
    for key, value in result_dict.items():
        if value is None and key != "code":
            continue
        response[key] = value
    return _safe_jsonify(response)


def get_parser_config(chunk_method, parser_config):
    """返回某个切块模板(chunk_method)的完整配置: 默认值 + 用户配置合并。

    模板见 common/constants.ParserType; 用户没传 parser_config 时给官方默认,
    传了则默认在下、用户配置在上逐层覆盖。
    """
    if not chunk_method:
        chunk_method = "naive"

    base_defaults = {
        "table_context_size": 0,
        "image_context_size": 0,
    }
    key_mapping = {
        "naive": {
            "layout_recognize": "DeepDOC",
            "chunk_token_num": 512,
            "delimiter": "\n",
            "auto_keywords": 0,
            "auto_questions": 0,
            "html4excel": False,
            "topn_tags": 3,
            "raptor": {
                "use_raptor": True,
                "prompt": "Please summarize the following paragraphs. Be careful with the numbers, do not make things up. Paragraphs as following:\n      {cluster_content}\nThe above is the content you need to summarize.",
                "max_token": 256,
                "threshold": 0.1,
                "max_cluster": 64,
                "random_seed": 0,
            },
            "graphrag": {
                "use_graphrag": True,
                "entity_types": [
                    "organization",
                    "person",
                    "geo",
                    "event",
                    "category",
                ],
                "method": "light",
            },
        },
        "qa": {"raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}},
        "tag": None,
        "resume": None,
        "manual": {"raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}},
        "table": None,
        "paper": {"raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}},
        "book": {"raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}},
        "laws": {"raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}},
        "presentation": {"raptor": {"use_raptor": False}, "graphrag": {"use_graphrag": False}},
        "one": None,
        "knowledge_graph": {
            "chunk_token_num": 8192,
            "delimiter": r"\n",
            "entity_types": ["organization", "person", "location", "event", "time"],
            "raptor": {"use_raptor": False},
            "graphrag": {"use_graphrag": False},
        },
        "email": None,
        "picture": None,
    }

    default_config = key_mapping[chunk_method]

    if not parser_config:
        if default_config is None:
            return deep_merge(base_defaults, {})
        return deep_merge(base_defaults, default_config)

    if default_config is None:
        return deep_merge(base_defaults, parser_config)

    merged_config = deep_merge(base_defaults, default_config)
    merged_config = deep_merge(merged_config, parser_config)

    return merged_config