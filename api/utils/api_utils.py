"""api/utils/api_utils.py — 学习版子集(按 HTTP 层需要从官方逐步补齐)。

官方 api_utils.py 有 735 行, 依赖 APIToken/MCPToolCallSession 等尚未移植的模块;
学习版按需保留自包含的函数(截至 2026-08-24 共 13 个):
- deep_merge / get_parser_config: 配置合并工具
- _safe_jsonify / get_json_result / get_data_error_result / get_error_data_result /
  server_error_response: 统一响应构造
- get_request_json / _coerce_request_data / validate_request / not_allowed_parameters:
  请求体解析与参数校验
- generate_confirmation_token / get_allowed_llm_factories: 令牌生成与 LLM 厂商白名单
"""

import inspect
import logging
from copy import deepcopy
from functools import wraps
from typing import Any

from quart import has_app_context, jsonify, request
from werkzeug.exceptions import BadRequest as WerkzeugBadRequest

try:
    from quart.exceptions import BadRequest as QuartBadRequest
except ImportError:  # pragma: no cover - optional dependency
    QuartBadRequest = None

from common import settings
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


def get_error_data_result(
    message="Sorry! Data missing!",
    code=RetCode.DATA_ERROR,
):
    """与 get_data_error_result 同族的失败响应构造; 官方同时保留了这两个函数, 学习版原样补齐.

    来源: 官方 api/utils/api_utils.py @ v0.24.0
    """
    result_dict = {"code": code, "message": message}
    response = {}
    for key, value in result_dict.items():
        if value is None and key != "code":
            continue
        else:
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


def validate_request(*args, **kwargs):
    """请求参数校验装饰器(官方 api_utils.validate_request 原封)。

    用法: @validate_request("nickname", "email") 检查必填参数;
    或 @validate_request(flag=("a", "b")) 校验参数值必须落在给定集合。
    缺参/值非法时直接返回 ARGUMENT_ERROR, 不进入业务函数。
    """

    def process_args(input_arguments):
        no_arguments = []
        error_arguments = []
        for arg in args:
            if arg not in input_arguments:
                no_arguments.append(arg)
        for k, v in kwargs.items():
            config_value = input_arguments.get(k, None)
            if config_value is None:
                no_arguments.append(k)
            elif isinstance(v, (tuple, list)):
                if config_value not in v:
                    error_arguments.append((k, set(v)))
            elif config_value != v:
                error_arguments.append((k, v))
        if no_arguments or error_arguments:
            error_string = ""
            if no_arguments:
                error_string += "required argument are missing: {}; ".format(",".join(no_arguments))
            if error_arguments:
                error_string += "required argument values: {}".format(",".join(["{}={}".format(a[0], a[1]) for a in error_arguments]))
            return error_string
        return None

    def wrapper(func):
        @wraps(func)
        async def decorated_function(*_args, **_kwargs):
            exception_types = (AttributeError, TypeError, WerkzeugBadRequest)
            if QuartBadRequest is not None:
                exception_types = exception_types + (QuartBadRequest,)
            if args or kwargs:
                try:
                    input_arguments = await _coerce_request_data()
                except exception_types:
                    input_arguments = {}
            else:
                input_arguments = await _coerce_request_data()
            errs = process_args(input_arguments)
            if errs:
                return get_json_result(code=RetCode.ARGUMENT_ERROR, message=errs)
            if inspect.iscoroutinefunction(func):
                return await func(*_args, **_kwargs)
            return func(*_args, **_kwargs)

        return decorated_function

    return wrapper


def not_allowed_parameters(*params):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            input_arguments = await _coerce_request_data()
            for param in params:
                if param in input_arguments:
                    return get_json_result(code=RetCode.ARGUMENT_ERROR, message=f"Parameter {param} isn't allowed")
            if inspect.iscoroutinefunction(func):
                return await func(*args, **kwargs)
            return func(*args, **kwargs)

        return wrapper

    return decorator


def generate_confirmation_token():
    """生成 API 访问令牌(ragflow- 前缀 + 32 字节 url-safe 随机串)。

    来源: 官方 api/utils/api_utils.py @ v0.24.0
    """
    import secrets

    return "ragflow-" + secrets.token_urlsafe(32)


def get_allowed_llm_factories() -> list:
    """返回当前租户体系允许接入的 LLM 厂商列表(按 rank 排序)。

    settings.ALLOWED_LLM_FACTORIES 为 None 时不限制; 否则只保留白名单内的厂商。
    来源: 官方 api/utils/api_utils.py @ v0.24.0
    """
    from api.db.services.tenant_llm_service import LLMFactoriesService  # 函数内 import 防循环

    factories = list(LLMFactoriesService.get_all(reverse=True, order_by="rank"))
    if settings.ALLOWED_LLM_FACTORIES is None:
        return factories

    return [factory for factory in factories if factory.name in settings.ALLOWED_LLM_FACTORIES]
