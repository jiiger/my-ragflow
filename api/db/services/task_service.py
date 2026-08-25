# -*- coding: utf-8 -*-
#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
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
"""任务服务:解析链路的中枢——把"文档"拆成"任务"并塞进 Redis 队列。

移植自官方 v0.24.0 api/db/services/task_service.py(558 行, 逻辑 1:1)。
本文件是上传链路里缺失的最后一环, 点亮后 document_service.run() 的延迟
import(queue_tasks/queue_dataflow)与 document_app/kb_app 的 TaskService
引用全部可用。

核心语义:
- queue_tasks(doc, bucket, name, priority):按文档类型切任务——PDF 按
  task_page_size 切页范围、table 按 3000 行/任务、其余整篇一个任务;每个任务
  用"切块配置 + 文档信息"算 xxhash digest 用于增量去重;若旧任务同页同配置
  已完成, 直接复用其 chunk_ids(progress=1.0, 任务本体仍入队但 executor 跳过);
  最后 Task 表 bulk 插入 + Redis 队列入队。
- TaskService:任务的查询/进度/取消/删除;get_task 是 executor 消费时回查
  任务详情的入口(带 3 次重试上限)。
- queue_dataflow:画布(pipeline)数据流任务, 独立任务类型, 不依赖画布模块。

⚠️ 运行时依赖(启动前需就绪):settings.STORAGE_IMPL(取文件二进制)、
settings.docStoreConn(旧 chunk 清理)、Redis(队列)。DB/容器就绪前调用
PDF/table 分支会因 STORAGE_IMPL 为 None 而报错, 属预期。
"""

import logging
import os
import random
import xxhash
from datetime import datetime

from api.db import FileType
from api.db.db_models import DB, File, File2Document, Document, Knowledgebase, Task, Tenant
from api.db.db_utils import bulk_insert_into_db
from api.db.services.common_service import CommonService
from api.db.services.document_service import DocumentService
from common import settings
from common.constants import StatusEnum, TaskStatus
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp
from deepdoc.parser import PdfParser
from deepdoc.parser.excel_parser import RAGFlowExcelParser
from peewee import JOIN
from rag.nlp import search
from rag.utils.redis_conn import REDIS_CONN

CANVAS_DEBUG_DOC_ID = "dataflow_x"
GRAPH_RAPTOR_FAKE_DOC_ID = "graph_raptor_x"


def trim_header_by_lines(text: str, max_length) -> str:
    """把 progress_msg 截到 max_length 字符以内, 优先从换行处截(官方 1:1)。"""
    len_text = len(text)
    if len_text <= max_length:
        return text
    for i in range(len_text):
        if text[i] == '\n' and len_text - i <= max_length:
            return text[i + 1:]
    return text


class TaskService(CommonService):
    """任务 CRUD / 进度 / 取消 / 清除。model 是 Task 表。"""
    model = Task

    @classmethod
    @DB.connection_context()
    def get_task(cls, task_id, doc_ids=[]):
        """executor 消费消息后回查任务详情:Task 联 Document/Knowledgebase/Tenant 三表。

        附带任务生命周期管理:每次被取走 retry_count+1, 累计 3 次视为废弃
        (prog=-1 并返回 None)。
        ⚠️ 官方 L90 `doc_id == CANVAS_DEBUG_DOC_ID` 是字段对象与字符串比较,
        恒为 False(画布任务走 task_type 分支处理), 1:1 保留。
        """
        doc_id = cls.model.doc_id
        if doc_id == CANVAS_DEBUG_DOC_ID and doc_ids:
            doc_id = doc_ids[0]

        fields = [
            cls.model.id,
            cls.model.doc_id,
            cls.model.from_page,
            cls.model.to_page,
            cls.model.retry_count,
            Document.kb_id,
            Document.parser_id,
            Document.parser_config,
            Document.name,
            Document.type,
            Document.location,
            Document.size,
            Knowledgebase.tenant_id,
            Knowledgebase.language,
            Knowledgebase.embd_id,
            Knowledgebase.pagerank,
            Knowledgebase.parser_config.alias("kb_parser_config"),
            Tenant.img2txt_id,
            Tenant.asr_id,
            Tenant.llm_id,
            cls.model.update_time,
        ]
        docs = (
            cls.model.select(*fields)
                .join(Document, on=(doc_id == Document.id))
                .join(Knowledgebase, on=(Document.kb_id == Knowledgebase.id))
                .join(Tenant, on=(Knowledgebase.tenant_id == Tenant.id))
                .where(cls.model.id == task_id)
        )
        docs = list(docs.dicts())
        if not docs:
            return None

        msg = f"\n{datetime.now().strftime('%H:%M:%S')} Task has been received."
        prog = random.random() / 10.0
        if docs[0]["retry_count"] >= 3:
            msg = "\nERROR: Task is abandoned after 3 times attempts."
            prog = -1

        cls.model.update(
            progress_msg=cls.model.progress_msg + msg,
            progress=prog,
            retry_count=docs[0]["retry_count"] + 1,
        ).where(cls.model.id == docs[0]["id"]).execute()

        if docs[0]["retry_count"] >= 3:
            return None

        return docs[0]

    @classmethod
    @DB.connection_context()
    def get_tasks(cls, doc_id: str):
        """某文档的全部任务(按起始页升序、创建时间降序), 供 queue_tasks 复用判断。"""
        fields = [
            cls.model.id,
            cls.model.from_page,
            cls.model.progress,
            cls.model.digest,
            cls.model.chunk_ids,
        ]
        tasks = (
            cls.model.select(*fields).order_by(cls.model.from_page.asc(), cls.model.create_time.desc())
            .where(cls.model.doc_id == doc_id)
        )
        tasks = list(tasks.dicts())
        if not tasks:
            return None
        return tasks

    @classmethod
    @DB.connection_context()
    def get_tasks_progress_by_doc_ids(cls, doc_ids: list[str]):
        """一批文档的任务进度(文档列表页轮询用)。"""
        fields = [
            cls.model.id,
            cls.model.doc_id,
            cls.model.from_page,
            cls.model.progress,
            cls.model.progress_msg,
            cls.model.digest,
            cls.model.chunk_ids,
            cls.model.create_time
        ]
        tasks = (
            cls.model.select(*fields).order_by(cls.model.create_time.desc())
            .where(cls.model.doc_id.in_(doc_ids))
        )
        tasks = list(tasks.dicts())
        if not tasks:
            return None
        return tasks

    @classmethod
    @DB.connection_context()
    def update_chunk_ids(cls, id: str, chunk_ids: str):
        """executor 入库后回写该任务的 chunk_id 列表(空格分隔串)。"""
        cls.model.update(chunk_ids=chunk_ids).where(cls.model.id == id).execute()

    @classmethod
    @DB.connection_context()
    def get_ongoing_doc_name(cls):
        """找出"正在处理"的文档存储位置集合, 供上传时去重同名文件用。

        判定:文档有效 + run=RUNNING + 非虚拟类型 + 有 10 分钟内的未完成任务。
        DB.lock 保证并发安全。
        """
        with DB.lock("get_task", -1):
            docs = (
                cls.model.select(
                    *[Document.id, Document.kb_id, Document.location, File.parent_id]
                )
                .join(Document, on=(cls.model.doc_id == Document.id))
                .join(
                    File2Document,
                    on=(File2Document.document_id == Document.id),
                    join_type=JOIN.LEFT_OUTER,
                )
                .join(
                    File,
                    on=(File2Document.file_id == File.id),
                    join_type=JOIN.LEFT_OUTER,
                )
                .where(
                    Document.status == StatusEnum.VALID.value,
                    Document.run == TaskStatus.RUNNING.value,
                    ~(Document.type == FileType.VIRTUAL.value),
                    cls.model.progress < 1,
                    cls.model.create_time >= current_timestamp() - 1000 * 600,
                )
            )
            docs = list(docs.dicts())
            if not docs:
                return []

            return list(
                set(
                    [
                        (
                            d["parent_id"] if d["parent_id"] else d["kb_id"],
                            d["location"],
                        )
                        for d in docs
                    ]
                )
            )

    @classmethod
    @DB.connection_context()
    def do_cancel(cls, id):
        """任务是否应取消: 文档 run=CANCEL 或进度为负。"""
        task = cls.model.get_by_id(id)
        _, doc = DocumentService.get_by_id(task.doc_id)
        return doc.run == TaskStatus.CANCEL.value or doc.progress < 0

    @classmethod
    @DB.connection_context()
    def update_progress(cls, id, info):
        """任务进度更新:progress_msg 追加(截 3000 行);progress 只升不降
        (当前非 -1 且新值更大才写), 防失败任务被后续成功覆盖或倒退。"""
        task = cls.model.get_by_id(id)
        if not task:
            logging.warning("Update_progress error: task not found")
            return

        if os.environ.get("MACOS"):
            if info["progress_msg"]:
                progress_msg = trim_header_by_lines(task.progress_msg + "\n" + info["progress_msg"], 3000)
                cls.model.update(progress_msg=progress_msg).where(cls.model.id == id).execute()
            if "progress" in info:
                prog = info["progress"]
                cls.model.update(progress=prog).where(
                    (cls.model.id == id) &
                    (
                            (cls.model.progress != -1) &
                            ((prog == -1) | (prog > cls.model.progress))
                    )
                ).execute()
        else:
            with DB.lock("update_progress", -1):
                if info["progress_msg"]:
                    progress_msg = trim_header_by_lines(task.progress_msg + "\n" + info["progress_msg"], 3000)
                    cls.model.update(progress_msg=progress_msg).where(cls.model.id == id).execute()
                if "progress" in info:
                    prog = info["progress"]
                    cls.model.update(progress=prog).where(
                        (cls.model.id == id) &
                        (
                            (cls.model.progress != -1) &
                            ((prog == -1) | (prog > cls.model.progress))
                        )
                    ).execute()

        process_duration = (datetime.now() - task.begin_at).total_seconds()
        cls.model.update(process_duration=process_duration).where(cls.model.id == id).execute()

    @classmethod
    @DB.connection_context()
    def delete_by_doc_ids(cls, doc_ids):
        """删除文档时连带清掉其全部任务。"""
        return cls.model.delete().where(cls.model.doc_id.in_(doc_ids)).execute()


def queue_tasks(doc: dict, bucket: str, name: str, priority: int):
    """建任务入队:按文档类型切任务 → 打 digest → 复用旧块 → Task 表落库 → Redis 入队。

    这是 document_service.run() 的下一跳, 也是"上传 → 解析"链路的中枢:
    - PDF:调 PdfParser.total_page_number 拿总页数, 按 task_page_size(默认 12,
      paper 解析器 22;one/knowledge_graph/非 DeepDOC 布局/开 TOC 则整篇一个任务)
      切成页范围任务;pages 配置可指定 1-based 页码范围
    - table 解析器:按 3000 行每任务切
    - 其他:整篇一个任务 (from_page=0, to_page=100000000)
    - digest = xxhash(切块配置 + doc_id/from_page/to_page), 与旧任务比对复用
      (reuse_prev_task_chunks), 复用成功的任务 progress=1.0 且 executor 跳过
    - 有复用导致删除旧任务时, 未复用的旧 chunk 从向量库删除(docStoreConn)
    """
    def new_task():
        return {
            "id": get_uuid(),
            "doc_id": doc["id"],
            "progress": 0.0,
            "from_page": 0,
            "to_page": 100000000,
            "begin_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    parse_task_array = []

    if doc["type"] == FileType.PDF.value:
        file_bin = settings.STORAGE_IMPL.get(bucket, name)
        do_layout = doc["parser_config"].get("layout_recognize", "DeepDOC")
        pages = PdfParser.total_page_number(doc["name"], file_bin)
        if pages is None:
            pages = 0
        page_size = doc["parser_config"].get("task_page_size") or 12
        if doc["parser_id"] == "paper":
            page_size = doc["parser_config"].get("task_page_size") or 22
        if doc["parser_id"] in ["one", "knowledge_graph"] or do_layout != "DeepDOC" or doc["parser_config"].get("toc_extraction", False):
            page_size = 10 ** 9
        page_ranges = doc["parser_config"].get("pages") or [(1, 10 ** 5)]
        for s, e in page_ranges:
            s -= 1
            s = max(0, s)
            e = min(e - 1, pages)
            for p in range(s, e, page_size):
                task = new_task()
                task["from_page"] = p
                task["to_page"] = min(p + page_size, e)
                parse_task_array.append(task)

    elif doc["parser_id"] == "table":
        file_bin = settings.STORAGE_IMPL.get(bucket, name)
        rn = RAGFlowExcelParser.row_number(doc["name"], file_bin)
        for i in range(0, rn, 3000):
            task = new_task()
            task["from_page"] = i
            task["to_page"] = min(i + 3000, rn)
            parse_task_array.append(task)
    else:
        parse_task_array.append(new_task())

    chunking_config = DocumentService.get_chunking_config(doc["id"])
    for task in parse_task_array:
        hasher = xxhash.xxh64()
        for field in sorted(chunking_config.keys()):
            if field == "parser_config":
                for k in ["raptor", "graphrag"]:
                    if k in chunking_config[field]:
                        del chunking_config[field][k]
            hasher.update(str(chunking_config[field]).encode("utf-8"))
        for field in ["doc_id", "from_page", "to_page"]:
            hasher.update(str(task.get(field, "")).encode("utf-8"))
        task_digest = hasher.hexdigest()
        task["digest"] = task_digest
        task["progress"] = 0.0
        task["priority"] = priority

    prev_tasks = TaskService.get_tasks(doc["id"])
    ck_num = 0
    if prev_tasks:
        for task in parse_task_array:
            ck_num += reuse_prev_task_chunks(task, prev_tasks, chunking_config)
        TaskService.filter_delete([Task.doc_id == doc["id"]])
        pre_chunk_ids = []
        for pre_task in prev_tasks:
            if pre_task["chunk_ids"]:
                pre_chunk_ids.extend(pre_task["chunk_ids"].split())
        if pre_chunk_ids:
            settings.docStoreConn.delete({"id": pre_chunk_ids}, search.index_name(chunking_config["tenant_id"]),
                                         chunking_config["kb_id"])
    DocumentService.update_by_id(doc["id"], {"chunk_num": ck_num})

    bulk_insert_into_db(Task, parse_task_array, True)
    DocumentService.begin2parse(doc["id"])

    unfinished_task_array = [task for task in parse_task_array if task["progress"] < 1.0]
    for unfinished_task in unfinished_task_array:
        assert REDIS_CONN.queue_product(
            settings.get_svr_queue_name(priority), message=unfinished_task
        ), "Can't access Redis. Please check the Redis' status."


def reuse_prev_task_chunks(task: dict, prev_tasks: list[dict], chunking_config: dict):
    """增量复用:旧任务同 from_page 同 digest 且已完成 → 直接继承其 chunk_ids。

    复用后任务 progress=1.0(executor 会跳过执行), 旧任务的 chunk_ids 置空
    (防止下面的清理逻辑把被复用的 chunk 误删)。
    """
    idx = 0
    while idx < len(prev_tasks):
        prev_task = prev_tasks[idx]
        if prev_task.get("from_page", 0) == task.get("from_page", 0) \
                and prev_task.get("digest", 0) == task.get("digest", ""):
            break
        idx += 1

    if idx >= len(prev_tasks):
        return 0
    prev_task = prev_tasks[idx]
    if prev_task["progress"] < 1.0 or not prev_task["chunk_ids"]:
        return 0
    task["chunk_ids"] = prev_task["chunk_ids"]
    task["progress"] = 1.0
    if "from_page" in task and "to_page" in task and int(task['to_page']) - int(task['from_page']) >= 10 ** 6:
        task["progress_msg"] = f"Page({task['from_page']}~{task['to_page']}): "
    else:
        task["progress_msg"] = ""
    task["progress_msg"] = " ".join(
        [datetime.now().strftime("%H:%M:%S"), task["progress_msg"], "Reused previous task's chunks."])
    prev_task["chunk_ids"] = ""

    return len(task["chunk_ids"].split())


def cancel_all_task_of(doc_id):
    """取消某文档全部任务:往 Redis 写 <task_id>-cancel 标记, executor 消费时检查。"""
    for t in TaskService.query(doc_id=doc_id):
        try:
            REDIS_CONN.set(f"{t.id}-cancel", "x")
        except Exception as e:
            logging.exception(e)


def has_canceled(task_id):
    """executor 侧检查取消标记(has_canceled) 的 Redis 实现。"""
    try:
        if REDIS_CONN.get(f"{task_id}-cancel"):
            logging.info(f"Task: {task_id} has been canceled")
            return True
    except Exception as e:
        logging.exception(e)
    return False


def queue_dataflow(tenant_id: str, flow_id: str, task_id: str, doc_id: str = CANVAS_DEBUG_DOC_ID, file: dict = None, priority: int = 0, rerun: bool = False) -> tuple[bool, str]:
    """画布(pipeline)数据流任务入队:task_type=dataflow(+ rerun 后缀)。

    与普通解析任务同队列不同任务类型;executor 侧按 task_type 分流。
    真实文档(doc_id 不是调试/占位 ID)触发前先清掉该文档旧任务并 begin2parse。
    """
    task = dict(
        id=task_id,
        doc_id=doc_id,
        from_page=0,
        to_page=100000000,
        task_type="dataflow" if not rerun else "dataflow_rerun",
        priority=priority,
        begin_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    if doc_id not in [CANVAS_DEBUG_DOC_ID, GRAPH_RAPTOR_FAKE_DOC_ID]:
        TaskService.model.delete().where(TaskService.model.doc_id == doc_id).execute()
        DocumentService.begin2parse(doc_id)
    bulk_insert_into_db(model=Task, data_source=[task], replace_on_conflict=True)

    task["kb_id"] = DocumentService.get_knowledgebase_id(doc_id)
    task["tenant_id"] = tenant_id
    task["dataflow_id"] = flow_id
    task["file"] = file

    if not REDIS_CONN.queue_product(
            settings.get_svr_queue_name(priority), message=task
    ):
        return False, "Can't access Redis. Please check the Redis' status."

    return True, ""