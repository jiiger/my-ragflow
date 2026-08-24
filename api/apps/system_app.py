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
#  limitations under the License
#
"""
api/apps/system_app.py — 系统级路由(版本/健康检查/API 令牌管理)。

status 接口聚合四路探活(docEngine/存储/数据库/Redis)与任务执行器心跳;
令牌接口挂在用户 owner 租户下(APITokenService)。
来源: 官方 api/apps/system_app.py @ v0.24.0
"""
import json
import logging
from datetime import datetime
from timeit import default_timer as timer

from quart import jsonify

from api.apps import current_user, login_required
from api.db.db_models import APIToken
from api.db.services.api_service import APITokenService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import UserTenantService
from api.utils.api_utils import (
    generate_confirmation_token,
    get_data_error_result,
    get_json_result,
    server_error_response,
)
from common import settings
from common.time_utils import current_timestamp, datetime_format
from common.versions import get_ragflow_version


@manager.route("/version", methods=["GET"])  # noqa: F821
@login_required
def version():
    # 学习注释: 版本号查询(读 VERSION 文件, 缺失时 git describe 兜底)
    """
    Get the current version of the application.
    ---
    tags:
      - System
    security:
      - ApiKeyAuth: []
    responses:
      200:
        description: Version retrieved successfully.
        schema:
          type: object
          properties:
            version:
              type: string
              description: Version number.
    """
    return get_json_result(data=get_ragflow_version())


@manager.route("/status", methods=["GET"])  # noqa: F821
@login_required
def status():
    # 学习注释: 系统健康状态 —— docEngine/存储/数据库/Redis 四路探活 + 任务执行器心跳;
    # ⚠️ docStoreConn/STORAGE_IMPL 为 None 占位, 前两路目前会落 red, 等依赖移植后点亮
    """
    Get the system status.
    ---
    tags:
      - System
    security:
      - ApiKeyAuth: []
    responses:
      200:
        description: System is operational.
        schema:
          type: object
          properties:
            es:
              type: object
              description: Elasticsearch status.
            storage:
              type: object
              description: Storage status.
            database:
              type: object
              description: Database status.
      503:
        description: Service unavailable.
        schema:
          type: object
          properties:
            error:
              type: string
              description: Error message.
    """
    res = {}
    st = timer()
    try:
        res["doc_engine"] = settings.docStoreConn.health()
        res["doc_engine"]["elapsed"] = f"{(timer() - st) * 1000.0:.1f}"
    except Exception as e:
        res["doc_engine"] = {
            "type": "unknown",
            "status": "red",
            "elapsed": f"{(timer() - st) * 1000.0:.1f}",
            "error": str(e),
        }

    st = timer()
    try:
        settings.STORAGE_IMPL.health()
        res["storage"] = {
            "storage": settings.STORAGE_IMPL_TYPE.lower(),
            "status": "green",
            "elapsed": f"{(timer() - st) * 1000.0:.1f}",
        }
    except Exception as e:
        res["storage"] = {
            "storage": settings.STORAGE_IMPL_TYPE.lower(),
            "status": "red",
            "elapsed": f"{(timer() - st) * 1000.0:.1f}",
            "error": str(e),
        }

    st = timer()
    try:
        KnowledgebaseService.get_by_id("x")
        res["database"] = {
            "database": settings.DATABASE_TYPE.lower(),
            "status": "green",
            "elapsed": f"{(timer() - st) * 1000.0:.1f}",
        }
    except Exception as e:
        res["database"] = {
            "database": settings.DATABASE_TYPE.lower(),
            "status": "red",
            "elapsed": f"{(timer() - st) * 1000.0:.1f}",
            "error": str(e),
        }

    st = timer()
    try:
        from rag.utils.redis_conn import REDIS_CONN  # ⚠️ redis_conn 未移植, 延迟 import
        if not REDIS_CONN.health():
            raise Exception("Lost connection!")
        res["redis"] = {
            "status": "green",
            "elapsed": f"{(timer() - st) * 1000.0:.1f}",
        }
    except Exception as e:
        res["redis"] = {
            "status": "red",
            "elapsed": f"{(timer() - st) * 1000.0:.1f}",
            "error": str(e),
        }

    task_executor_heartbeats = {}
    try:
        task_executors = REDIS_CONN.smembers("TASKEXE")
        now = datetime.now().timestamp()
        for task_executor_id in task_executors:
            heartbeats = REDIS_CONN.zrangebyscore(task_executor_id, now - 60 * 30, now)
            heartbeats = [json.loads(heartbeat) for heartbeat in heartbeats]
            task_executor_heartbeats[task_executor_id] = heartbeats
    except Exception:
        logging.exception("get task executor heartbeats failed!")
    res["task_executor_heartbeats"] = task_executor_heartbeats

    return get_json_result(data=res)


@manager.route("/healthz", methods=["GET"])  # noqa: F821
def healthz():
    """k8s 存活探针: 跑全套健康检查, 全绿返回 200, 否则 500。
    ⚠️ run_health_checks 依赖 health_utils(未移植), 该端点暂不可用。
    """
    from api.utils.health_utils import run_health_checks  # ⚠️ health_utils 未移植, 延迟 import
    result, all_ok = run_health_checks()
    return jsonify(result), (200 if all_ok else 500)


@manager.route("/ping", methods=["GET"])  # noqa: F821
async def ping():
    """存活探针: 恒返回 200 pong(不带登录态)。"""
    return "pong", 200


@manager.route("/oceanbase/status", methods=["GET"])  # noqa: F821
@login_required
def oceanbase_status():
    # 学习注释: OceanBase 健康状态与性能指标查询(需接入 OB 后才可用)
    """
    Get OceanBase health status and performance metrics.
    ---
    tags:
      - System
    security:
      - ApiKeyAuth: []
    responses:
      200:
        description: OceanBase status retrieved successfully.
        schema:
          type: object
          properties:
            status:
              type: string
              description: Status (alive/timeout).
            message:
              type: object
              description: Detailed status information including health and performance metrics.
    """
    try:
        from api.utils.health_utils import get_oceanbase_status  # ⚠️ health_utils 未移植, 延迟 import
        status_info = get_oceanbase_status()
        return get_json_result(data=status_info)
    except Exception as e:
        return get_json_result(data={"status": "error", "message": f"Failed to get OceanBase status: {e!s}"}, code=500)


@manager.route("/new_token", methods=["POST"])  # noqa: F821
@login_required
def new_token():
    # 学习注释: 生成新 API 令牌(token + 短 beta 字段), 挂在 owner 租户下
    """
    Generate a new API token.
    ---
    tags:
      - API Tokens
    security:
      - ApiKeyAuth: []
    parameters:
      - in: query
        name: name
        type: string
        required: false
        description: Name of the token.
    responses:
      200:
        description: Token generated successfully.
        schema:
          type: object
          properties:
            token:
              type: string
              description: The generated API token.
    """
    try:
        tenants = UserTenantService.query(user_id=current_user.id)
        if not tenants:
            return get_data_error_result(message="Tenant not found!")

        tenant_id = [tenant for tenant in tenants if tenant.role == "owner"][0].tenant_id
        obj = {
            "tenant_id": tenant_id,
            "token": generate_confirmation_token(),
            "beta": generate_confirmation_token().replace("ragflow-", "")[:32],
            "create_time": current_timestamp(),
            "create_date": datetime_format(datetime.now()),
            "update_time": None,
            "update_date": None,
        }

        if not APITokenService.save(**obj):
            return get_data_error_result(message="Fail to new a dialog!")

        return get_json_result(data=obj)
    except Exception as e:
        return server_error_response(e)


@manager.route("/token_list", methods=["GET"])  # noqa: F821
@login_required
def token_list():
    # 学习注释: 令牌列表; 缺 beta 字段的旧令牌自动补发并回写
    """
    List all API tokens for the current user.
    ---
    tags:
      - API Tokens
    security:
      - ApiKeyAuth: []
    responses:
      200:
        description: List of API tokens.
        schema:
          type: object
          properties:
            tokens:
              type: array
              items:
                type: object
                properties:
                  token:
                    type: string
                    description: The API token.
                  name:
                    type: string
                    description: Name of the token.
                  create_time:
                    type: string
                    description: Token creation time.
    """
    try:
        tenants = UserTenantService.query(user_id=current_user.id)
        if not tenants:
            return get_data_error_result(message="Tenant not found!")

        tenant_id = [tenant for tenant in tenants if tenant.role == "owner"][0].tenant_id
        objs = APITokenService.query(tenant_id=tenant_id)
        objs = [o.to_dict() for o in objs]
        for o in objs:
            if not o["beta"]:
                o["beta"] = generate_confirmation_token().replace("ragflow-", "")[:32]
                APITokenService.filter_update([APIToken.tenant_id == tenant_id, APIToken.token == o["token"]], o)
        return get_json_result(data=objs)
    except Exception as e:
        return server_error_response(e)


@manager.route("/token/<token>", methods=["DELETE"])  # noqa: F821
@login_required
def rm(token):
    # 学习注释: 删除指定 API 令牌(按 token 精确匹配)
    """
    Remove an API token.
    ---
    tags:
      - API Tokens
    security:
      - ApiKeyAuth: []
    parameters:
      - in: path
        name: token
        type: string
        required: true
        description: The API token to remove.
    responses:
      200:
        description: Token removed successfully.
        schema:
          type: object
          properties:
            success:
              type: boolean
              description: Deletion status.
    """
    try:
        tenants = UserTenantService.query(user_id=current_user.id)
        if not tenants:
            return get_data_error_result(message="Tenant not found!")

        tenant_id = tenants[0].tenant_id
        APITokenService.filter_delete([APIToken.tenant_id == tenant_id, APIToken.token == token])
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@manager.route("/config", methods=["GET"])  # noqa: F821
def get_config():
    # 学习注释: 系统配置 —— 注册开关 registerEnabled
    """
    Get system configuration.
    ---
    tags:
        - System
    responses:
        200:
            description: Return system configuration
            schema:
                type: object
                properties:
                    registerEnable:
                        type: integer 0 means disabled, 1 means enabled
                        description: Whether user registration is enabled
    """
    return get_json_result(data={"registerEnabled": settings.REGISTER_ENABLED})
