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
from typing import Any

from quart import jsonify, has_app_context, request

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


def server_error_response(e):
    """全局异常兜底处理器(挂 app.errorhandler(Exception))。

    Quart 在 except 块之外调用该 handler, 必须手动传 exc_info。
    401/unauthorized 归为 UNAUTHORIZED, ES 索引缺失给提示, 其余 EXCEPTION_ERROR。
    对齐官方 api_utils.server_error_response。
    """
    # Quart invokes this handler outside the original except block, so we must pass exc_info manually.
    logging.error("Unhandled exception during request", exc_info=(type(e), e, e.__traceback__))
    try:
        msg = repr(e).lower()
        if getattr(e, "code", None) == 401 or ("unauthorized" in msg) or ("401" in msg):
            resp = get_json_result(code=RetCode.UNAUTHORIZED, message="Unauthorized")
            resp.status_code = RetCode.UNAUTHORIZED
            return resp
    except Exception as ex:  # noqa: F841 # 官方原样: ex 未使用, 日志为官方打码的 *** 字面量
        logging.warning(f"error checking authorization: ***")  # noqa: F541 # 官方原样: 无占位符 f-string

    if repr(e).find("index_not_found_exception") >= 0:
        return get_json_result(code=RetCode.EXCEPTION_ERROR, message="No chunk found, please upload file and parse it.")

    return get_json_result(code=RetCode.EXCEPTION_ERROR, message=repr(e))


def get_json_result(code: RetCode = RetCode.SUCCESS, message="success", data=None):
    """统一成功/失败响应: {code, message, data}(官方 api_utils.get_json_result 原封)。"""
    response = {"code": code, "message": message, "data": data}
    return _safe_jsonify(response)


async def _coerce_request_data() -> dict:
    """Fetch JSON body with sane defaults; fallback to form data.(官方原封)"""
    if hasattr(request, "_cached_payload"):
        return request._cached_payload
    payload: Any = None

    body_bytes = await request.get_data()
    has_body = bool(body_bytes)
    content_type = (request.content_type or "").lower()
    is_json = content_type.startswith("application/json")

    if not has_body:
        payload = {}
    elif is_json:
        payload = await request.get_json(force=False, silent=False)
        if isinstance(payload, dict):
            payload = payload or {}
        elif isinstance(payload, str):
            raise AttributeError("'str' object has no attribute 'get'")
        else:
            raise TypeError("JSON payload must be an object.")
    else:
        form = await request.form
        payload = form.to_dict() if form else None
        if payload is None:
            raise TypeError("Request body is not a valid form payload.")

    request._cached_payload = payload
    return payload


async def get_request_json():
    """读取并解析请求体: JSON body 优先, 无 body 给 {}, 表单 fallback(官方 api_utils.get_request_json 原封)。"""
    return await _coerce_request_data()