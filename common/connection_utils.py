import os
import queue
import threading
from typing import Any, Callable, Coroutine, Optional, Type, Union
import asyncio
from functools import wraps
from quart import make_response, jsonify
from common.constants import RetCode

# 超时异常类型: 可以是异常类(不实例化直接 raise)或异常实例; on_timeout 是超时后的回调(同步或协程)
TimeoutException = Union[Type[BaseException], BaseException]
OnTimeoutCallback = Union[Callable[..., Any], Coroutine[Any, Any, Any]]


def timeout(seconds: float | int | str = None, attempts: int = 2, *, exception: Optional[TimeoutException] = None,
            on_timeout: Optional[OnTimeoutCallback] = None):
    """给函数套超时限制的装饰器。

    - 同步函数: 丢进 daemon 线程 + queue 取结果, 超时未返回则重试 attempts 次, 最后抛 TimeoutError
    - 异步函数: 用 asyncio.wait_for 实现, 超时后触发 on_timeout 回调或抛 exception/TimeoutError
    - seconds 传 str 时转 float(方便从环境变量/配置读)
    """
    if isinstance(seconds, str):
        seconds = float(seconds)

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 用队列在线程间传递结果(或异常), maxsize=1 保证线程写完就返回
            result_queue = queue.Queue(maxsize=1)

            def target():
                # 目标函数在线程里执行, 结果或异常都塞进队列
                try:
                    result = func(*args, **kwargs)
                    result_queue.put(result)
                except Exception as e:
                    result_queue.put(e)

            thread = threading.Thread(target=target)
            thread.daemon = True  # daemon 线程: 主线程退出时不等待它
            thread.start()

            for a in range(attempts):
                try:
                    # 调试用开关 ENABLE_TIMEOUT_ASSERTION=true 时真正限时, 否则无限等待(测超时逻辑用)
                    if os.environ.get("ENABLE_TIMEOUT_ASSERTION"):
                        result = result_queue.get(timeout=seconds)
                    else:
                        result = result_queue.get()
                    if isinstance(result, Exception):
                        raise result  # 目标是异常就原样抛
                    return result
                except queue.Empty:
                    pass  # 超时未返回 → 进下一次尝试(或最终失败)
            raise TimeoutError(f"Function '{func.__name__}' timed out after {seconds} seconds and {attempts} attempts.")

        @wraps(func)
        async def async_wrapper(*args, **kwargs) -> Any:
            # seconds 为 None 表示不限时, 直接跑
            if seconds is None:
                return await func(*args, **kwargs)

            for a in range(attempts):
                try:
                    if os.environ.get("ENABLE_TIMEOUT_ASSERTION"):
                        return await asyncio.wait_for(func(*args, **kwargs), timeout=seconds)
                    else:
                        return await func(*args, **kwargs)
                except asyncio.TimeoutError:
                    if a < attempts - 1:
                        continue  # 还有机会, 重试
                    if on_timeout is not None:
                        # 用户给了超时回调: 同步回调直接调, 协程回调要 await
                        if callable(on_timeout):
                            result = on_timeout()
                            if isinstance(result, Coroutine):
                                return await result
                            return result
                        return on_timeout

                    if exception is None:
                        raise TimeoutError(f"Operation timed out after {seconds} seconds and {attempts} attempts.")

                    if isinstance(exception, BaseException):
                        raise exception  # 异常实例直接抛

                    if isinstance(exception, type) and issubclass(exception, BaseException):
                        raise exception(f"Operation timed out after {seconds} seconds and {attempts} attempts.")

                    raise RuntimeError("Invalid exception type provided")

        # 目标是协程函数就用异步包装, 否则用线程包装
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return wrapper

    return decorator


async def construct_response(code=RetCode.SUCCESS, message="success", data=None, auth=None):
    """构造异步 HTTP JSON 响应: 附带 CORS 头, auth 非空时写入 Authorization 头(供登录后下发 token)。

    注意与 get_data_error_result 的差别: message 为 None 时省略, data 为 None 时也省略。
    """
    result_dict = {"code": code, "message": message, "data": data}
    response_dict = {}
    for key, value in result_dict.items():
        if value is None and key != "code":
            continue
        else:
            response_dict[key] = value
    response = await make_response(jsonify(response_dict))
    if auth:
        response.headers["Authorization"] = auth  # 登录成功后把新 token 放响应头
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Method"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Expose-Headers"] = "Authorization"
    return response


def sync_construct_response(code=RetCode.SUCCESS, message="success", data=None, auth=None):
    """construct_response 的同步版(用 flask 实现, 给非异步上下文用)。

    官方原样延迟 import flask, 避免在 Quart 环境里无端加载 Flask。
    """
    import flask
    result_dict = {"code": code, "message": message, "data": data}
    response_dict = {}
    for key, value in result_dict.items():
        if value is None and key != "code":
            continue
        else:
            response_dict[key] = value
    response = flask.make_response(flask.jsonify(response_dict))
    if auth:
        response.headers["Authorization"] = auth
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Method"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Expose-Headers"] = "Authorization"
    return response