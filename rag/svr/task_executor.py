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
"""任务执行器:独立进程消费 Redis 任务队列, 跑完 解析→向量化→入库 全流程。

移植自官方 v0.24.0 rag/svr/task_executor.py (1404 行):核心流水线 1:1,
官方同样由 launch/entrypoint 作为独立进程拉起(python rag/svr/task_executor.py)。

主流程(与官方一致的链路):
    main() 循环(信号量限流 5 并发) → task_manager():
      collect()        从 Redis Stream 消费消息(消费者组 + unacked 重取)
      → 回查 Task 表(get_task, 3 次重试上限) → 绑定 embedding 模型拿向量维度
      → init_kb(create_idx, vector_size 定索引维度)
      → build_chunks(STORAGE_IMPL.get 拉文件 + FACTORY[parser_id].chunk)
      → embedding(每批 encode, 文件名向量加权) → 组装 doc 字段 → 批量入库
      → set_progress(进度只升不降) → ack 消息

裁剪点(官方有学习版无, 全部显式标注):
- FACTORY 只挂 naive/presentation(官方还有 paper/book/manual/laws/qa/table/
  resume/picture/one/audio/email/tag 等解析器)
- 特殊任务类型 dataflow/raptor/graphrag/mindmap/memory → 进度 -1 显式报错
- LLM 块增强(autokeywords/autoquestions/metadata/tag)、build_TOC、
  report_status 状态上报、PipelineOperationLog 流水日志 → 裁剪
- 失败即 ack(官方借助 pending + unacked 迭代器重投, 学习版简化)
"""

import asyncio
import copy
import faulthandler
import logging
import os
import re
import signal
import sys
import threading
import time
import xxhash
from datetime import datetime
from functools import partial
from timeit import default_timer as timer

import numpy as np
from peewee import DoesNotExist

from api.db import PIPELINE_SPECIAL_PROGRESS_FREEZE_TASK_TYPES
from api.db.db_models import close_connection
from api.db.services.file2document_service import File2DocumentService
from api.db.services.llm_service import LLMBundle
from api.db.services.task_service import (
    CANVAS_DEBUG_DOC_ID,
    GRAPH_RAPTOR_FAKE_DOC_ID,
    TaskService,
    has_canceled,
)
from common import settings
from common.config_utils import show_configs
from common.connection_utils import timeout
from common.constants import LLMType, PAGERANK_FLD, ParserType, SVR_CONSUMER_GROUP_NAME
from common.log_utils import init_root_logger
from common.misc_utils import thread_pool_exec
from common.token_utils import truncate
from rag.app import naive, presentation
from rag.nlp import search
from rag.utils.redis_conn import REDIS_CONN

start_ts = time.time()
BATCH_SIZE = 64
FACTORY = {
    "general": naive,
    ParserType.NAIVE.value: naive,
    ParserType.PRESENTATION.value: presentation,
    # ⚠️ 官方此处还有 paper/book/manual/laws/qa/table/resume/picture/one/
    # audio/email/kg/tag, 学习版未移植对应解析器, 只挂 naive/presentation
}

CONSUMER_NO = "0" if len(sys.argv) < 2 else sys.argv[1]
CONSUMER_NAME = "task_executor_" + CONSUMER_NO
UNACKED_ITERATOR = None
PENDING_TASKS = 0
LAG_TASKS = 0
DONE_TASKS = 0
FAILED_TASKS = 0
CURRENT_TASKS = {}
MAX_CONCURRENT_TASKS = int(os.environ.get('MAX_CONCURRENT_TASKS', "5"))
task_limiter = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
embed_limiter = asyncio.Semaphore(int(os.environ.get('MAX_CONCURRENT_EMBEDDINGS', "10")))
chunk_limiter = asyncio.Semaphore(int(os.environ.get('MAX_CONCURRENT_CHUNK_BUILDERS', "1")))


class TaskCanceledException(Exception):
    pass


def signal_handler(sig, frame):
    logging.warning(f"Received signal {sig}, shutting down...")
    stop_event.set()


stop_event = threading.Event()


def set_progress(task_id, from_page=0, to_page=-1, prog=None, msg="Processing..."):
    """任务进度更新(官方 1:1):消息带时间戳/页码前缀, 取消标记时进度置 -1。

    内部调用 TaskService.update_progress(只升不降); 取消时抛
    TaskCanceledException 中断当前任务。
    """
    try:
        if prog is not None and prog < 0:
            msg = "[ERROR]" + msg
        cancel = has_canceled(task_id)

        if cancel:
            msg += " [Canceled]"
            prog = -1

        if to_page > 0:
            if msg:
                if from_page < to_page:
                    msg = f"Page({from_page + 1}~{to_page + 1}): " + msg
        if msg:
            msg = datetime.now().strftime("%H:%M:%S") + " " + msg
        d = {"progress_msg": msg}
        if prog is not None:
            d["progress"] = prog

        TaskService.update_progress(task_id, d)

        close_connection()
        if cancel:
            raise TaskCanceledException(msg)
        logging.info(f"set_progress({task_id}), progress: {prog}, progress_msg: {msg}")
    except TaskCanceledException:
        raise
    except DoesNotExist:
        logging.warning(f"set_progress({task_id}) got exception DoesNotExist")
    except Exception as e:
        logging.exception(f"set_progress({task_id}), progress: {prog}, progress_msg: {msg}, got exception: {e}")


async def collect():
    """从任务队列消费一条消息并回查任务详情(官方 1:1, memory 分支裁剪)。

    优先重取本消费者 unacked 消息(上次处理中断的补偿), 否则按队列名
    依次 queue_consumer; 拿到消息后 TaskService.get_task 回查详情
    (附带 retry_count+1 与 3 次上限), 取消/废弃的任务直接 ack 丢弃。
    """
    global CONSUMER_NAME, DONE_TASKS, FAILED_TASKS
    global UNACKED_ITERATOR
    svr_queue_names = settings.get_svr_queue_names()
    redis_msg = None
    try:
        if not UNACKED_ITERATOR:
            UNACKED_ITERATOR = REDIS_CONN.get_unacked_iterator(svr_queue_names, SVR_CONSUMER_GROUP_NAME, CONSUMER_NAME)
        try:
            redis_msg = next(UNACKED_ITERATOR)
        except StopIteration:
            for svr_queue_name in svr_queue_names:
                redis_msg = REDIS_CONN.queue_consumer(svr_queue_name, SVR_CONSUMER_GROUP_NAME, CONSUMER_NAME)
                if redis_msg:
                    break
    except Exception as e:
        logging.exception(f"collect got exception: {e}")
        return None, None
    if not redis_msg:
        return None, None
    msg = redis_msg.get_message()
    if not msg:
        logging.error(f"collect got empty message of {redis_msg.get_msg_id()}")
        redis_msg.ack()
        return None, None
    canceled = False
    if msg.get("doc_id", "") in [GRAPH_RAPTOR_FAKE_DOC_ID, CANVAS_DEBUG_DOC_ID]:
        task = msg
        if task["task_type"] in PIPELINE_SPECIAL_PROGRESS_FREEZE_TASK_TYPES:
            task = TaskService.get_task(msg["id"], msg["doc_ids"])
            if task:
                task["doc_id"] = msg["doc_id"]
                task["doc_ids"] = msg.get("doc_ids", []) or []
    # ⚠️ 官方此处: memory 任务分支(memory_id/source_id/message_dict), 学习版裁剪
    else:
        task = TaskService.get_task(msg["id"])
    if task:
        canceled = has_canceled(task["id"])
    if not task or canceled:
        state = "is unknown" if not task else "has been cancelled"
        FAILED_TASKS += 1
        logging.warning(f"collect task {msg['id']} {state}")
        redis_msg.ack()
        return None, None
    task_type = msg.get("task_type", "")
    task["task_type"] = task_type
    if task_type[:8] == "dataflow":
        task["tenant_id"] = msg["tenant_id"]
        task["dataflow_id"] = msg["dataflow_id"]
        task["kb_id"] = msg.get("kb_id", "")
    # ⚠️ 官方此处: memory 任务字段补充, 学习版裁剪
    return redis_msg, task


async def get_storage_binary(bucket, name):
    """从对象存储拉文件二进制(官方 1:1)。"""
    return await thread_pool_exec(settings.STORAGE_IMPL.get, bucket, name)


async def build_chunks(task, progress_callback):
    """拿文件 → FACTORY[parser_id].chunk 解析切块(官方 1:1)。

    返回 rules 列表: 每项一个解析后的索引文档 dict(已在 parser 里完成
    tokenize, 含 content_with_weight/content_ltks/位置字段等)。
    """
    if task["size"] > settings.DOC_MAXIMUM_SIZE:
        set_progress(task["id"], prog=-1, msg="File size exceeds( <= %dMb )" %
                                              (int(settings.DOC_MAXIMUM_SIZE / 1024 / 1024)))
        return []
    chunker = FACTORY[task["parser_id"].lower()]
    try:
        st = timer()
        bucket, name = File2DocumentService.get_storage_address(doc_id=task["doc_id"])
        binary = await get_storage_binary(bucket, name)
        logging.info("From minio({}) {}/{}".format(timer() - st, task["location"], task["name"]))
    except TimeoutError:
        progress_callback(-1, "Internal server error: Fetch file from minio timeout. Could you try it again.")
        logging.exception(
            "Minio {}/{} got timeout: Fetch file from minio timeout.".format(task["location"], task["name"]))
        raise
    except Exception as e:
        if re.search("(No such file|not found)", str(e)):
            progress_callback(-1, "Can not find file <%s> from minio. Could you try it again?" % task["name"])
        else:
            progress_callback(-1, "Get file from minio: %s" % str(e).replace("'", ""))
        logging.exception("Chunking {}/{} got exception".format(task["location"], task["name"]))
        raise
    try:
        async with chunk_limiter:
            cks = await thread_pool_exec(
                chunker.chunk,
                task["name"],
                binary=binary,
                from_page=task["from_page"],
                to_page=task["to_page"],
                lang=task["language"],
                callback=progress_callback,
                kb_id=task["kb_id"],
                parser_config=task["parser_config"],
                tenant_id=task["tenant_id"],
            )
        logging.info("Chunking({}) {}/{} done".format(timer() - st, task["location"], task["name"]))
    except TaskCanceledException:
        raise
    except Exception as e:
        progress_callback(-1, "Internal server error while chunking: %s" % str(e).replace("'", ""))
        logging.exception("Chunking {}/{} got exception".format(task["location"], task["name"]))
        raise
    # ⚠️ 官方此处: 图片块落 MinIO(upload_to_minio + image2id 得 img_id)与 LLM 块增强
    # (auto_keywords/auto_questions/enable_metadata/tag_kb_ids), 学习版裁剪
    docs = []
    for ck in cks:
        d = {"doc_id": task["doc_id"], "kb_id": str(task["kb_id"])}
        if task["pagerank"]:
            d[PAGERANK_FLD] = int(task["pagerank"])
        d.update(ck)
        if d.get("image"):
            _ = d.pop("image", None)
        d["img_id"] = ""
        docs.append(d)
    return docs


def init_kb(row, vector_size: int):
    """为任务建好向量索引(官方 1:1):tenant 级索引名 + kb 元数据。"""
    idxnm = search.index_name(row["tenant_id"])
    parser_id = row.get("parser_id", None)
    return settings.docStoreConn.create_idx(idxnm, row.get("kb_id", ""), vector_size, parser_id)


async def embedding(docs, mdl, parser_config=None, callback=None):
    """批量向量化(官方 1:1 核心):标题向量按 filename_embd_weight 加权融合。

    docs 是 naive/presentation 输出的索引文档 dict 列表; 为每个 doc 追加
    "q_<dim>_vec" 向量字段, 供 doc_store 向量检索使用。
    """
    if parser_config is None:
        parser_config = {}
    tts, cnts = [], []
    for d in docs:
        tts.append(d.get("docnm_kwd", "Title"))
        c = "\n".join(d.get("question_kwd", []))
        if not c:
            c = d["content_with_weight"]
        c = re.sub(r"</?(table|td|caption|tr|th)( [^<>]{0,12})?>", " ", c)
        if not c:
            c = "None"
        cnts.append(c)

    tk_count = 0
    if len(tts) == len(cnts):
        vts, c = await thread_pool_exec(mdl.encode, tts[0:1])
        tts = np.tile(vts[0], (len(cnts), 1))
        tk_count += c

    @timeout(60)
    def batch_encode(txts):
        nonlocal mdl
        return mdl.encode([truncate(c, mdl.max_length - 10) for c in txts])

    cnts_ = np.array([])
    for i in range(0, len(cnts), settings.EMBEDDING_BATCH_SIZE):
        async with embed_limiter:
            vts, c = await thread_pool_exec(batch_encode, cnts[i: i + settings.EMBEDDING_BATCH_SIZE])
        if len(cnts_) == 0:
            cnts_ = vts
        else:
            cnts_ = np.concatenate((cnts_, vts), axis=0)
        tk_count += c
        callback(prog=0.7 + 0.2 * (i + 1) / len(cnts), msg="")
    cnts = cnts_
    filename_embd_weight = parser_config.get("filename_embd_weight", 0.1)  # due to the db support none value
    if not filename_embd_weight:
        filename_embd_weight = 0.1
    title_w = float(filename_embd_weight)
    if tts.ndim == 2 and cnts.ndim == 2 and tts.shape == cnts.shape:
        vects = title_w * tts + (1 - title_w) * cnts
    else:
        vects = cnts

    assert len(vects) == len(docs)
    for i, d in enumerate(docs):
        v = vects[i].tolist()
        d["q_%d_vec" % len(v)] = v


async def task_manager():
    """单任务执行器:collect → 绑定 embedding → 建索引 → 解析 → 向量化 → 入库。"""
    global DONE_TASKS, FAILED_TASKS, CURRENT_TASKS

    redis_msg, task = await collect()
    if not task:
        return

    task_id = task["id"]
    task_from_page = task["from_page"]
    task_to_page = task["to_page"]
    task_tenant_id = task["tenant_id"]
    task_embedding_id = task["embd_id"]
    task_language = task["language"]
    task_dataset_id = task["kb_id"]
    task_doc_id = task["doc_id"]
    task_parser_config = task["parser_config"]
    task_start_ts = timer()
    progress_callback = partial(set_progress, task_id, task_from_page, task_to_page)

    task_canceled = has_canceled(task_id)
    if task_canceled:
        progress_callback(-1, msg="Task has been canceled.")
        return

    task_type = task.get("task_type", "")
    # 特殊任务类型: 官方分别走 run_dataflow/raptor/graphrag/memory 分支
    # (依赖画布/图谱/记忆模块与 LLM), 学习版未移植 → 显式报错
    if task_type[:8] == "dataflow" or task_type in ("raptor", "graphrag", "mindmap", "memory"):
        progress_callback(prog=-1, msg=f"学习版未移植特殊任务类型: {task_type}")
        redis_msg.ack()
        FAILED_TASKS += 1
        return

    try:
        # 绑定 embedding 模型(LLMBundle)并拿向量维度, 决定索引的向量字段维度
        embedding_model = LLMBundle(task_tenant_id, LLMType.EMBEDDING, llm_name=task_embedding_id, lang=task_language)
        vts, _ = embedding_model.encode(["ok"])
        vector_size = len(vts[0])
    except Exception as e:
        error_message = f'Fail to bind embedding model: {str(e)}'
        progress_callback(-1, msg=error_message)
        logging.exception(error_message)
        raise

    CURRENT_TASKS[task_id] = copy.deepcopy(task)
    try:
        init_kb(task, vector_size)

        progress_callback(0.1, "...Start to parse...")
        chunks = await build_chunks(task, progress_callback)
        if not chunks:
            progress_callback(-1, "No chunks got. Parse has got no content.")
            return
        progress_callback(0.5, "...Start to embed...")
        await embedding(chunks, embedding_model, task_parser_config, progress_callback)

        # 组装索引文档公共字段 + 按任务去重后的 doc_id 归属
        for ck in chunks:
            ck["doc_id"] = task_doc_id
            ck["kb_id"] = [str(task_dataset_id)]
            ck["docnm_kwd"] = task["name"]
            ck["create_time"] = str(datetime.now()).replace("T", " ")[:19]
            ck["create_timestamp_flt"] = datetime.now().timestamp()
            if not ck.get("id"):
                ck["id"] = xxhash.xxh64((ck["text"] + str(ck["doc_id"])).encode("utf-8")).hexdigest()

        # 批量入库(官方同款: 按 BATCH_SIZE 分批 insert)
        idxnm = search.index_name(task_tenant_id)
        for i in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[i:i + BATCH_SIZE]
            await thread_pool_exec(settings.docStoreConn.insert, batch, idxnm, str(task_dataset_id))

        redis_msg.ack()
        DONE_TASKS += 1
        progress_callback(1.0, "Task done ({:.2f}s).".format(timer() - task_start_ts))
    except TaskCanceledException:
        progress_callback(-1, "Task has been canceled.")
    except Exception as e:
        FAILED_TASKS += 1
        err_msg = str(e).replace("'", "")
        progress_callback(-1, f"[Exception]: {err_msg}")
        logging.exception(f"Task {task_id} failed: {e}")
    finally:
        CURRENT_TASKS.pop(task_id, None)
        close_connection()


async def main():
    """入口:初始化 settings → 信号处理 → 并发拉取 task_manager。"""
    settings.init_settings()
    logging.info('RAGFlow ingestion task executor started.')
    show_configs()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    tasks = []
    logging.info(f"RAGFlow ingestion is ready after {time.time() - start_ts}s initialization.")
    try:
        while not stop_event.is_set():
            await task_limiter.acquire()
            t = asyncio.create_task(task_manager())
            tasks.append(t)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    logging.error("BUG!!! You should not reach here!!!")


if __name__ == "__main__":
    faulthandler.enable()
    init_root_logger(CONSUMER_NAME)
    asyncio.run(main())