#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
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
from datetime import datetime
from common.time_utils import current_timestamp, datetime_format
from api.db.db_models import DB
from api.db.db_models import SystemSettings
from api.db.services.common_service import CommonService


class SystemSettingsService(CommonService):
    """系统级配置项(name→value)读写(与官方 api/db/services/system_settings_service.py 1:1)。

    存 SQLite/DB 启动迁移用的系统设置(如版本), 按 name 主键读写。
    ⚠️ 官方 update_by_name 写入 obj 里的 update_time/update_date, 但
    SystemSettings 表的列是 name/source/data_type/value, 无这两列 —— 官方
    疑似 bug, 学习版保持原样以保 diff, 调用时传入未知列会触 DB 报错(属预期的
    官方行为, 目前无调用方触发)。
    """
    model = SystemSettings

    @classmethod
    @DB.connection_context()
    def get_by_name(cls, name):
        objs = cls.model.select().where(cls.model.name == name)
        return objs

    @classmethod
    @DB.connection_context()
    def update_by_name(cls, name, obj):
        obj["update_time"] = current_timestamp()
        obj["update_date"] = datetime_format(datetime.now())
        cls.model.update(obj).where(cls.model.name == name).execute()
        return SystemSettings(**obj)

    @classmethod
    @DB.connection_context()
    def get_record_count(cls):
        count = cls.model.select().count()
        return count