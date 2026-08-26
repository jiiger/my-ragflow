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
"""学习版 api/db/init_data.py(官方 v0.24.0 裁剪版)。

职责:系统首次启动时向库写入种子数据——超管账号 + 大模型厂商/模型目录表。

裁剪说明(依据学习版原则:能读懂、能跑通,依赖缺失的模块一律去掉):
- init_superuser:保留建 user/tenant/user_tenant/tenant_llm 四件套;
  ⚠️ 去掉官方 81-93 行 LLMBundle 冒烟段(会真调 chat/embedding,无模型配置时
  超时阻塞启动,留到模型层就绪后再验证)。
- init_llm_factory:保留核心(从 settings.FACTORY_LLM_INFOS 填 llm_factories/llm
  表,供 get_init_tenant_llm 查询);⚠️ 去掉 get_openai_models 注入段、
  doc_count 统计段、各 filter_delete 微调段(依赖 tenant_llm 关联数据,
  学习版起步阶段不需要这些清理逻辑)。
- ⚠️ add_graph_templates:整个去掉(无 agent/templates 目录、无 CanvasTemplateService)。
- ⚠️ init_table:整个去掉(无 conf/system_settings.json、无 SystemSettingsService)。
- init_web_data = 只调 init_llm_factory;超管由 ragflow_server.py --init-superuser
  参数单独建(官方 init_web_data 里是注释掉的 init_superuser 调用)。

官方文件:api/db/init_data.py@v0.24.0
"""
import logging
import os
import time
import uuid
from copy import deepcopy

from api.db import UserTenantRole
from api.db.db_models import init_database_tables as init_web_db, LLM
from api.db.services import UserService
from api.db.services.llm_service import LLMService, get_init_tenant_llm
from api.db.services.tenant_llm_service import LLMFactoriesService, TenantLLMService
from api.db.services.user_service import TenantService, UserTenantService
from common import settings
from api.common.base64 import encode_to_base64

DEFAULT_SUPERUSER_NICKNAME = os.getenv("DEFAULT_SUPERUSER_NICKNAME", "admin")
DEFAULT_SUPERUSER_EMAIL = os.getenv("DEFAULT_SUPERUSER_EMAIL", "admin@ragflow.io")
DEFAULT_SUPERUSER_PASSWORD = os.getenv("DEFAULT_SUPERUSER_PASSWORD", "admin")


def init_superuser(nickname=DEFAULT_SUPERUSER_NICKNAME, email=DEFAULT_SUPERUSER_EMAIL, password=DEFAULT_SUPERUSER_PASSWORD, role=UserTenantRole.OWNER):
    user_info = {
        "id": uuid.uuid1().hex,
        "password": encode_to_base64(password),
        "nickname": nickname,
        "is_superuser": True,
        "email": email,
        "creator": "system",
        "status": "1",
    }
    tenant = {
        "id": user_info["id"],
        "name": user_info["nickname"] + "‘s Kingdom",
        "llm_id": settings.CHAT_MDL,
        "embd_id": settings.EMBEDDING_MDL,
        "asr_id": settings.ASR_MDL,
        # ⚠️ 学习版适配:官方 init_settings 从 conf 的 user_default_llm 解析
        # PARSERS(带默认值), 学习版精简 settings 不解析(PARSERS=None) →
        # tenant.parser_ids 列 NOT NULL, 不能写入 None, 这里兜底官方同款默认值。
        "parser_ids": settings.PARSERS
        or "naive:General,qa:Q&A,resume:Resume,manual:Manual,table:Table,paper:Paper,book:Book,laws:Laws,presentation:Presentation,picture:Picture,one:One,audio:Audio,email:Email,tag:Tag",
        "img2txt_id": settings.IMAGE2TEXT_MDL
    }
    usr_tenant = {
        "tenant_id": user_info["id"],
        "user_id": user_info["id"],
        "invited_by": user_info["id"],
        "role": role
    }

    tenant_llm = get_init_tenant_llm(user_info["id"])

    if not UserService.save(**user_info):
        logging.error("can't init admin.")
        return
    TenantService.insert(**tenant)
    UserTenantService.insert(**usr_tenant)
    TenantLLMService.insert_many(tenant_llm)
    logging.info(
        f"Super user initialized. email: {email},A default password has been set; changing the password after login is strongly recommended.")

    # ⚠️ 裁剪:官方此处(81-93 行)用 LLMBundle 真调一次 chat/embedding 冒烟,
    # 无模型配置时会超时阻塞启动;学习版留到模型层(rag/llm)就绪后再验证。


def init_llm_factory():
    LLMFactoriesService.filter_delete([1 == 1])
    factory_llm_infos = settings.FACTORY_LLM_INFOS
    if not factory_llm_infos:
        logging.warning(
            "FACTORY_LLM_INFOS is empty, skip init_llm_factory. 模型配置齐备后再初始化即可.")
        return
    for factory_llm_info in factory_llm_infos:
        info = deepcopy(factory_llm_info)
        llm_infos = info.pop("llm")
        try:
            LLMFactoriesService.save(**info)
        except Exception:
            pass
        LLMService.filter_delete([LLM.fid == factory_llm_info["name"]])
        for llm_info in llm_infos:
            llm_info["fid"] = factory_llm_info["name"]
            try:
                LLMService.save(**llm_info)
            except Exception:
                pass

    # ⚠️ 裁剪:官方此处(114-144 行)有废弃厂商清理(Local/novita/QAnything/cohere
    # 改名)、OpenAI 两个 embedding 注入、各知识库文档数回填——均为存量数据微调,
    # 依赖 get_openai_models/get_all_kb_doc_count 等完整关联数据,学习版起步不需要。


def init_web_data():
    start_time = time.time()

    init_llm_factory()

    # ⚠️ 裁剪:官方 init_web_data 依次调 init_table / add_graph_templates /
    # init_message_id_sequence / init_memory_size_cache / fix_missing_tokenized_memory,
    # 依赖 system_settings.json、agent/templates、memory_message_service,
    # 学习版均无,全部去掉。超管建号由 ragflow_server.py --init-superuser 单独触发。
    logging.info("init web data success:{}".format(time.time() - start_time))


if __name__ == '__main__':
    init_web_db()
    init_web_data()