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
"""数据源连接器(Connector)HTTP 路由 — 移植自官方 v0.24.0 api/apps/connector_app.py。

仅移植核心连接器管理路由(/set /list /<id> /logs /resume /rebuild /rm),
去掉官方 Google Drive/Gmail/Box OAuth 外部数据源流程(依赖 common.data_source
与 google/box OAuth SDK, 学习版未移植且不符轻量定位)。前端数据源管理
(知识库详情/设置)只用到这些核心接口。
来源: /home/yang/code/ragflow api/apps/connector_app.py (tag v0.24.0)
"""
import asyncio

from api.db import InputType
from api.db.services.connector_service import ConnectorService, SyncLogsService
from api.utils.api_utils import get_data_error_result, get_json_result, get_request_json, validate_request
from common.constants import RetCode, TaskStatus
from common.misc_utils import get_uuid
from api.apps import login_required, current_user
from quart import request


@manager.route("/set", methods=["POST"])  # noqa: F821
@login_required
async def set_connector():
    req = await get_request_json()
    if req.get("id"):
        conn = {fld: req[fld] for fld in ["prune_freq", "refresh_freq", "config", "timeout_secs"] if fld in req}
        ConnectorService.update_by_id(req["id"], conn)
    else:
        req["id"] = get_uuid()
        conn = {
            "id": req["id"],
            "tenant_id": current_user.id,
            "name": req["name"],
            "source": req["source"],
            "input_type": InputType.POLL,
            "config": req["config"],
            "refresh_freq": int(req.get("refresh_freq", 5)),
            "prune_freq": int(req.get("prune_freq", 720)),
            "timeout_secs": int(req.get("timeout_secs", 60 * 29)),
            "status": TaskStatus.SCHEDULE,
        }
        ConnectorService.save(**conn)

    await asyncio.sleep(1)
    e, conn = ConnectorService.get_by_id(req["id"])

    return get_json_result(data=conn.to_dict())


@manager.route("/list", methods=["GET"])  # noqa: F821
@login_required
def list_connector():
    return get_json_result(data=ConnectorService.list(current_user.id))


@manager.route("/<connector_id>", methods=["GET"])  # noqa: F821
@login_required
def get_connector(connector_id):
    e, conn = ConnectorService.get_by_id(connector_id)
    if not e:
        return get_data_error_result(message="Can't find this Connector!")
    return get_json_result(data=conn.to_dict())


@manager.route("/<connector_id>/logs", methods=["GET"])  # noqa: F821
@login_required
def list_logs(connector_id):
    req = request.args.to_dict(flat=True)
    arr, total = SyncLogsService.list_sync_tasks(connector_id, int(req.get("page", 1)), int(req.get("page_size", 15)))
    return get_json_result(data={"total": total, "logs": arr})


@manager.route("/<connector_id>/resume", methods=["PUT"])  # noqa: F821
@login_required
async def resume(connector_id):
    req = await get_request_json()
    if req.get("resume"):
        ConnectorService.resume(connector_id, TaskStatus.SCHEDULE)
    else:
        ConnectorService.resume(connector_id, TaskStatus.CANCEL)
    return get_json_result(data=True)


@manager.route("/<connector_id>/rebuild", methods=["PUT"])  # noqa: F821
@login_required
@validate_request("kb_id")
async def rebuild(connector_id):
    req = await get_request_json()
    err = ConnectorService.rebuild(req["kb_id"], connector_id, current_user.id)
    if err:
        return get_json_result(data=False, message=err, code=RetCode.SERVER_ERROR)
    return get_json_result(data=True)


@manager.route("/<connector_id>/rm", methods=["POST"])  # noqa: F821
@login_required
def rm_connector(connector_id):
    ConnectorService.resume(connector_id, TaskStatus.CANCEL)
    ConnectorService.delete_by_id(connector_id)
    return get_json_result(data=True)