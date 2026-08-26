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
"""学习版后端 HTTP 服务入口(官方 v0.24.0 api/ragflow_server.py 裁剪版)。

职责:初始化日志/配置 → 建库表 → 灌种子数据 → 起 Quart HTTP 服务(默认 0.0.0.0:9380)。
支持 --version / --debug / --init-superuser 三个命令行参数。

裁剪说明(依据学习版原则——能读懂、能跑通,依赖缺失的模块一律去掉):
- ⚠️ 去掉 `from common.mcp_tool_call_conn import shutdown_all_mcp_sessions`
  (学习版无 mcp 模块),signal_handler 里的调用同步去掉。
- ⚠️ 去掉 `from agent.plugin import GlobalPluginManager`(学习版无 agent 层),
  对应的 GlobalPluginManager.load_plugins() 调用同步去掉。
- ⚠️ settings.print_rag_settings() 学习版 settings 无此方法,去掉该调用。
- ⚠️ settings.HOST_IP/HOST_PORT 官方由 init_settings 填充,学习版精简版不填
  (None)→ 入口改用 settings.get_base_config("ragflow") 读 conf(默认
  0.0.0.0:9380),同时喂给 RuntimeConfig 和 app.run。

官方文件:api/ragflow_server.py@v0.24.0
"""

# from beartype import BeartypeConf
# from beartype.claw import beartype_all  # <-- you didn't sign up for this
# beartype_all(conf=BeartypeConf(violation_type=UserWarning))    # <-- emit warnings from all code

import time
start_ts = time.time()

import logging
import os
import signal
import sys
import traceback
import threading
import uuid
import faulthandler

from api.apps import app
from api.db.runtime_config import RuntimeConfig
from api.db.services.document_service import DocumentService
from common.file_utils import get_project_base_directory
from common import settings
from api.db.db_models import init_database_tables as init_web_db
from api.db.init_data import init_web_data, init_superuser
from common.versions import get_ragflow_version
from common.config_utils import show_configs
from common.log_utils import init_root_logger
from rag.utils.redis_conn import RedisDistributedLock

stop_event = threading.Event()

RAGFLOW_DEBUGPY_LISTEN = int(os.environ.get('RAGFLOW_DEBUGPY_LISTEN', "0"))


def update_progress():
    lock_value = str(uuid.uuid4())
    redis_lock = RedisDistributedLock("update_progress", lock_value=lock_value, timeout=60)
    logging.info(f"update_progress lock_value: {lock_value}")
    while not stop_event.is_set():
        try:
            if redis_lock.acquire():
                DocumentService.update_progress()
                redis_lock.release()
        except Exception:
            logging.exception("update_progress exception")
        finally:
            try:
                redis_lock.release()
            except Exception:
                logging.exception("update_progress exception")
            stop_event.wait(6)


def signal_handler(sig, frame):
    logging.info("Received interrupt signal, shutting down...")
    # ⚠️ 裁剪:官方此处调 shutdown_all_mcp_sessions(),学习版无 mcp 模块。
    stop_event.set()
    stop_event.wait(1)
    sys.exit(0)


if __name__ == '__main__':
    faulthandler.enable()
    init_root_logger("ragflow_server")
    logging.info(r"""
        ____   ___    ______ ______ __
       / __ \ /   |  / ____// ____// /____  _      __
      / /_/ // /| | / / __ / /_   / // __ \| | /| / /
     / _, _// ___ |/ /_/ // __/  / // /_/ /| |/ |/ /
    /_/ |_|/_/  |_|\____//_/    /_/ \____/ |__/|__/

    """)
    logging.info(
        f'RAGFlow version: {get_ragflow_version()}'
    )
    logging.info(
        f'project base: {get_project_base_directory()}'
    )
    show_configs()
    settings.init_settings()
    # ⚠️ 裁剪:官方此处调 settings.print_rag_settings(),学习版 settings 无此方法。

    if RAGFLOW_DEBUGPY_LISTEN > 0:
        logging.info(f"debugpy listen on {RAGFLOW_DEBUGPY_LISTEN}")
        import debugpy
        debugpy.listen(("0.0.0.0", RAGFLOW_DEBUGPY_LISTEN))

    # init db
    init_web_db()
    init_web_data()
    # init runtime config
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--version", default=False, help="RAGFlow version", action="store_true"
    )
    parser.add_argument(
        "--debug", default=False, help="debug mode", action="store_true"
    )
    parser.add_argument(
        "--init-superuser", default=False, help="init superuser", action="store_true"
    )
    args = parser.parse_args()
    if args.version:
        print(get_ragflow_version())
        sys.exit(0)

    if args.init_superuser:
        init_superuser()
    RuntimeConfig.DEBUG = args.debug
    if RuntimeConfig.DEBUG:
        logging.info("run on debug mode")

    # ⚠️ 裁剪说明:官方此处用 settings.HOST_IP/HOST_PORT(init_settings 填充),
    # 学习版精简 settings 不填这两个值 → 自行从 conf/service_conf.yaml 的
    # ragflow 段读取,默认 0.0.0.0:9380。
    ragflow_conf = settings.get_base_config("ragflow", {}) or {}
    host = ragflow_conf.get("host", "0.0.0.0")
    http_port = int(ragflow_conf.get("http_port", 9380))

    RuntimeConfig.init_env()
    RuntimeConfig.init_config(JOB_SERVER_HOST=host, HTTP_PORT=http_port)

    # ⚠️ 裁剪:官方此处调 GlobalPluginManager.load_plugins(),学习版无 agent 层。

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    def delayed_start_update_progress():
        logging.info("Starting update_progress thread (delayed)")
        t = threading.Thread(target=update_progress, daemon=True)
        t.start()

    if RuntimeConfig.DEBUG:
        if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
            threading.Timer(1.0, delayed_start_update_progress).start()
    else:
        threading.Timer(1.0, delayed_start_update_progress).start()

    # start http server
    try:
        logging.info(f"RAGFlow server is ready after {time.time() - start_ts}s initialization.")
        app.run(host=host, port=http_port)
    except Exception:
        traceback.print_exc()
        stop_event.set()
        stop_event.wait(1)
        os.kill(os.getpid(), signal.SIGKILL)