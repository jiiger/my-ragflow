"""document_service.py — 文档业务服务, 对齐官方 v0.24.0(1120 行)。

文档挂在知识库(kb)下, 是解析/检索链路的入口。本文件学习要点:
- 主查询链路: Document ↔ File2Document ↔ File ↔ UserCanvas 的多表 join
  (文档和"原始上传文件"是多对多, 经 File2Document 关联)
- 状态机: run(TaskStatus: 0未开始/1运行中/2取消/3完成/4失败) + progress(-1失败, 0~1)
- 计数维护: 解析前后用 increment/decrement/clear_chunk_num 同步文档与 kb 的
  token_num/chunk_num/doc_num
- 进度同步: _sync_progress 汇总该文档所有 Task 的进度, 与任务队列(Redis)联动

⚠️ 依赖尚未实现(第 3~4 步再补)的部分, 保持官方逻辑但做了延迟 import:
- DocMetadataService(doc_metadata_service.py)/ TaskService(task_service.py)
- rag.nlp(search/rag_tokenizer)/ rag.utils.redis_conn / common.doc_store(OrderByExpr)
- settings.docStoreConn / settings.STORAGE_IMPL 目前是 None(common/settings.py),
  涉及存储的方法要等存储层接入后才能真跑
方法签名与官方完全一致, 可随时 diff。
"""
import asyncio
import json
import logging
import random
import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from io import BytesIO

import xxhash
from peewee import fn, Case, JOIN

from api.constants import FILE_NAME_LEN_LIMIT, IMG_BASE64_PREFIX
from api.db import CanvasCategory, FileType, PIPELINE_SPECIAL_PROGRESS_FREEZE_TASK_TYPES, UserTenantRole
from api.db.db_models import DB, Document, File, File2Document, Knowledgebase, Task, Tenant, User, UserCanvas, UserTenant
from api.db.services.common_service import CommonService
from api.db.services.knowledgebase_service import KnowledgebaseService
from common import settings
from common.constants import LLMType, ParserType, SVR_CONSUMER_GROUP_NAME, SVR_QUEUE_NAME, StatusEnum, TaskStatus
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp, get_format_time


def _svr_queue_name(priority=0):
    """任务队列名: 优先级 0 用原名, 1 用原名_1(官方在 common/settings.py 里提供了同名方法,
    学习版 settings 还没实现, 先在这里等价实现)。"""
    return SVR_QUEUE_NAME if priority == 0 else SVR_QUEUE_NAME + "_1"


class DocumentService(CommonService):
    """文档业务服务: 上传文件的入库记录、解析编排、进度同步、权限与计数维护。"""

    model = Document

    @classmethod
    def get_cls_model_fields(cls):
        """文档列表接口要展示的字段集合(不含联表字段, 由各查询方法再拼)。"""
        return [
            cls.model.id,
            cls.model.thumbnail,
            cls.model.kb_id,
            cls.model.parser_id,
            cls.model.pipeline_id,
            cls.model.parser_config,
            cls.model.source_type,
            cls.model.type,
            cls.model.created_by,
            cls.model.name,
            cls.model.location,
            cls.model.size,
            cls.model.token_num,
            cls.model.chunk_num,
            cls.model.progress,
            cls.model.progress_msg,
            cls.model.process_begin_at,
            cls.model.process_duration,
            cls.model.suffix,
            cls.model.run,
            cls.model.status,
            cls.model.create_time,
            cls.model.create_date,
            cls.model.update_time,
            cls.model.update_date,
        ]

    @classmethod
    @DB.connection_context()
    def get_list(cls, kb_id, page_number, items_per_page,
                 orderby, desc, keywords, id, name, suffix=None, run=None, doc_ids=None):
        """文档分页列表: Document 联 File2Document/File(取原始文件) 与 UserCanvas(取画布标题)。

        meta_fields 来自 DocMetadataService(ES/Infinity 里的元数据), 该模块未实现, 延迟导入。
        """
        fields = cls.get_cls_model_fields()
        docs = cls.model.select(*[*fields, UserCanvas.title]).join(File2Document, on=(File2Document.document_id == cls.model.id))\
            .join(File, on=(File.id == File2Document.file_id))\
            .join(UserCanvas, on=((cls.model.pipeline_id == UserCanvas.id) & (UserCanvas.canvas_category == CanvasCategory.DataFlow.value)), join_type=JOIN.LEFT_OUTER)\
            .where(cls.model.kb_id == kb_id)
        if id:
            docs = docs.where(
                cls.model.id == id)
        if name:
            docs = docs.where(
                cls.model.name == name
            )
        if keywords:
            docs = docs.where(
                fn.LOWER(cls.model.name).contains(keywords.lower())
            )
        if doc_ids:
            docs = docs.where(cls.model.id.in_(doc_ids))
        if suffix:
            docs = docs.where(cls.model.suffix.in_(suffix))
        if run:
            docs = docs.where(cls.model.run.in_(run))
        if desc:
            docs = docs.order_by(cls.model.getter_by(orderby).desc())
        else:
            docs = docs.order_by(cls.model.getter_by(orderby).asc())

        count = docs.count()
        docs = docs.paginate(page_number, items_per_page)

        docs_list = list(docs.dicts())
        from api.db.services.doc_metadata_service import DocMetadataService
        metadata_map = DocMetadataService.get_metadata_for_documents(None, kb_id)
        for doc in docs_list:
            doc["meta_fields"] = metadata_map.get(doc["id"], {})
        return docs_list, count

    @classmethod
    @DB.connection_context()
    def check_doc_health(cls, tenant_id: str, filename):
        """上传前健康检查: 免费用户文件数上限(环境变量可配)+ 文件名长度上限。"""
        import os
        MAX_FILE_NUM_PER_USER = int(os.environ.get("MAX_FILE_NUM_PER_USER", 0))
        if 0 < MAX_FILE_NUM_PER_USER <= DocumentService.get_doc_count(tenant_id):
            raise RuntimeError("Exceed the maximum file number of a free user!")
        if len(filename.encode("utf-8")) > FILE_NAME_LEN_LIMIT:
            raise RuntimeError("Exceed the maximum length of file name!")
        return True

    @classmethod
    @DB.connection_context()
    def get_by_kb_id(cls, kb_id, page_number, items_per_page, orderby, desc, keywords, run_status, types, suffix, doc_ids=None, return_empty_metadata=False):
        """按 kb 分页查文档(带 pipeline 标题和创建人昵称, 左连接)。

        return_empty_metadata=True 时只返回"还没有元数据"的文档(供补录元数据用):
        先查 metadata_map, 再把已有元数据的文档 id 过滤掉。
        """
        fields = cls.get_cls_model_fields()
        if keywords:
            docs = (
                cls.model.select(*[*fields, UserCanvas.title.alias("pipeline_name"), User.nickname])
                .join(File2Document, on=(File2Document.document_id == cls.model.id))
                .join(File, on=(File.id == File2Document.file_id))
                .join(UserCanvas, on=(cls.model.pipeline_id == UserCanvas.id), join_type=JOIN.LEFT_OUTER)
                .join(User, on=(cls.model.created_by == User.id), join_type=JOIN.LEFT_OUTER)
                .where((cls.model.kb_id == kb_id), (fn.LOWER(cls.model.name).contains(keywords.lower())))
            )
        else:
            docs = (
                cls.model.select(*[*fields, UserCanvas.title.alias("pipeline_name"), User.nickname])
                .join(File2Document, on=(File2Document.document_id == cls.model.id))
                .join(UserCanvas, on=(cls.model.pipeline_id == UserCanvas.id), join_type=JOIN.LEFT_OUTER)
                .join(File, on=(File.id == File2Document.file_id))
                .join(User, on=(cls.model.created_by == User.id), join_type=JOIN.LEFT_OUTER)
                .where(cls.model.kb_id == kb_id)
            )

        if doc_ids:
            docs = docs.where(cls.model.id.in_(doc_ids))
        if run_status:
            docs = docs.where(cls.model.run.in_(run_status))
        if types:
            docs = docs.where(cls.model.type.in_(types))
        if suffix:
            docs = docs.where(cls.model.suffix.in_(suffix))

        from api.db.services.doc_metadata_service import DocMetadataService
        metadata_map = DocMetadataService.get_metadata_for_documents(None, kb_id)
        doc_ids_with_metadata = set(metadata_map.keys())
        if return_empty_metadata and doc_ids_with_metadata:
            docs = docs.where(cls.model.id.not_in(doc_ids_with_metadata))

        count = docs.count()
        if desc:
            docs = docs.order_by(cls.model.getter_by(orderby).desc())
        else:
            docs = docs.order_by(cls.model.getter_by(orderby).asc())

        if page_number and items_per_page:
            docs = docs.paginate(page_number, items_per_page)

        docs_list = list(docs.dicts())
        if return_empty_metadata:
            for doc in docs_list:
                doc["meta_fields"] = {}
        else:
            for doc in docs_list:
                doc["meta_fields"] = metadata_map.get(doc["id"], {})
        return docs_list, count

    @classmethod
    @DB.connection_context()
    def get_filter_by_kb_id(cls, kb_id, keywords, run_status, types, suffix):
        """文档筛选统计(前端筛选项用): 按 suffix/run_status 计数, 并统计元数据取值分布。

        返回:
        {
            "suffix": {"ppt": 1, "docx": 2},
            "run_status": {"1": 2, "2": 2},
            "metadata": {"key1": {"value1": 1}, "empty_metadata": {"true": N}},
        }, total
        """
        fields = cls.get_cls_model_fields()
        if keywords:
            query = cls.model.select(*fields).join(File2Document, on=(File2Document.document_id == cls.model.id)).join(File, on=(File.id == File2Document.file_id)).where(
                (cls.model.kb_id == kb_id),
                (fn.LOWER(cls.model.name).contains(keywords.lower()))
            )
        else:
            query = cls.model.select(*fields).join(File2Document, on=(File2Document.document_id == cls.model.id)).join(File, on=(File.id == File2Document.file_id)).where(cls.model.kb_id == kb_id)

        if run_status:
            query = query.where(cls.model.run.in_(run_status))
        if types:
            query = query.where(cls.model.type.in_(types))
        if suffix:
            query = query.where(cls.model.suffix.in_(suffix))

        rows = query.select(cls.model.run, cls.model.suffix, cls.model.id)
        total = rows.count()

        suffix_counter = {}
        run_status_counter = {}
        metadata_counter = {}
        empty_metadata_count = 0

        doc_ids = [row.id for row in rows]
        metadata = {}
        if doc_ids:
            try:
                from api.db.services.doc_metadata_service import DocMetadataService
                metadata = DocMetadataService.get_metadata_for_documents(doc_ids, kb_id)
            except Exception as e:
                logging.warning(f"Failed to fetch metadata from ES/Infinity: {e}")

        for row in rows:
            suffix_counter[row.suffix] = suffix_counter.get(row.suffix, 0) + 1
            run_status_counter[str(row.run)] = run_status_counter.get(str(row.run), 0) + 1
            meta_fields = metadata.get(row.id, {})
            if not meta_fields:
                empty_metadata_count += 1
                continue
            has_valid_meta = False
            for key, value in meta_fields.items():
                values = value if isinstance(value, list) else [value]
                for vv in values:
                    if vv is None:
                        continue
                    if isinstance(vv, str) and not vv.strip():
                        continue
                    sv = str(vv)
                    if key not in metadata_counter:
                        metadata_counter[key] = {}
                    metadata_counter[key][sv] = metadata_counter[key].get(sv, 0) + 1
                    has_valid_meta = True
            if not has_valid_meta:
                empty_metadata_count += 1

        metadata_counter["empty_metadata"] = {"true": empty_metadata_count}
        return {
            "suffix": suffix_counter,
            "run_status": run_status_counter,
            "metadata": metadata_counter,
        }, total

    @classmethod
    @DB.connection_context()
    def count_by_kb_id(cls, kb_id, keywords, run_status, types):
        """按条件统计 kb 下文档数。"""
        if keywords:
            docs = cls.model.select().where(
                (cls.model.kb_id == kb_id),
                (fn.LOWER(cls.model.name).contains(keywords.lower()))
            )
        else:
            docs = cls.model.select().where(cls.model.kb_id == kb_id)

        if run_status:
            docs = docs.where(cls.model.run.in_(run_status))
        if types:
            docs = docs.where(cls.model.type.in_(types))

        return docs.count()

    @classmethod
    @DB.connection_context()
    def get_total_size_by_kb_id(cls, kb_id, keywords="", run_status=[], types=[]):
        """kb 下文档总大小(SUM(size), 无记录返回 0)。"""
        query = cls.model.select(fn.COALESCE(fn.SUM(cls.model.size), 0)).where(
            cls.model.kb_id == kb_id
        )

        if keywords:
            query = query.where(fn.LOWER(cls.model.name).contains(keywords.lower()))
        if run_status:
            query = query.where(cls.model.run.in_(run_status))
        if types:
            query = query.where(cls.model.type.in_(types))

        return int(query.scalar()) or 0

    @classmethod
    @DB.connection_context()
    def get_all_doc_ids_by_kb_ids(cls, kb_ids):
        """多个 kb 的全部文档 id(深分页 100 条一循环, 避免深 offset 慢查询)。"""
        fields = [cls.model.id]
        docs = cls.model.select(*fields).where(cls.model.kb_id.in_(kb_ids))
        docs.order_by(cls.model.create_time.asc())
        offset, limit = 0, 100
        res = []
        while True:
            doc_batch = docs.offset(offset).limit(limit)
            _temp = list(doc_batch.dicts())
            if not _temp:
                break
            res.extend(_temp)
            offset += limit
        return res

    @classmethod
    @DB.connection_context()
    def get_all_docs_by_creator_id(cls, creator_id):
        """某用户上传的全部文档(联 kb 带出 tenant_id, "我的文件"场景)。"""
        fields = [
            cls.model.id, cls.model.kb_id, cls.model.token_num, cls.model.chunk_num, Knowledgebase.tenant_id
        ]
        docs = cls.model.select(*fields).join(Knowledgebase, on=(Knowledgebase.id == cls.model.kb_id)).where(
            cls.model.created_by == creator_id
        )
        docs.order_by(cls.model.create_time.asc())
        offset, limit = 0, 100
        res = []
        while True:
            doc_batch = docs.offset(offset).limit(limit)
            _temp = list(doc_batch.dicts())
            if not _temp:
                break
            res.extend(_temp)
            offset += limit
        return res

    @classmethod
    @DB.connection_context()
    def insert(cls, doc):
        """落库一条文档, 并原子维护 kb.doc_num +1。"""
        if not cls.save(**doc):
            raise RuntimeError("Database error (Document)!")
        if not KnowledgebaseService.atomic_increase_doc_num_by_id(doc["kb_id"]):
            raise RuntimeError("Database error (Knowledgebase)!")
        return Document(**doc)

    @classmethod
    @DB.connection_context()
    def remove_document(cls, doc, tenant_id):
        """删除文档的完整清理链: 任务取消 → 任务记录 → 切块图片 → 缩略图 →
        检索索引里的切块 → 元数据 → 图谱引用 → 删文档行。

        官方逻辑每步都 try/except + 日志(删除要"尽力而为"), 依赖 task_service/
        docStore/STORAGE_IMPL, 均延迟导入或标注。
        """
        from api.db.services.task_service import TaskService, cancel_all_task_of
        cls.clear_chunk_num(doc.id)

        # 先在 Redis 里取消该文档所有运行中任务
        try:
            cancel_all_task_of(doc.id)
            logging.info(f"Cancelled all tasks for document {doc.id}")
        except Exception as e:
            logging.warning(f"Failed to cancel tasks for document {doc.id}: {e}")

        # 删除任务记录
        try:
            TaskService.filter_delete([Task.doc_id == doc.id])
        except Exception as e:
            logging.warning(f"Failed to delete tasks for document {doc.id}: {e}")

        # 删除切块图片(非关键, 记日志继续)
        try:
            cls.delete_chunk_images(doc, tenant_id)
        except Exception as e:
            logging.warning(f"Failed to delete chunk images for document {doc.id}: {e}")

        # 删除缩略图(非关键)
        try:
            if doc.thumbnail and not doc.thumbnail.startswith(IMG_BASE64_PREFIX):
                if settings.STORAGE_IMPL.obj_exist(doc.kb_id, doc.thumbnail):
                    settings.STORAGE_IMPL.rm(doc.kb_id, doc.thumbnail)
        except Exception as e:
            logging.warning(f"Failed to delete thumbnail for document {doc.id}: {e}")

        # 删除检索索引里的切块(关键, 错误要打 error)
        try:
            from rag.nlp import search
            settings.docStoreConn.delete({"doc_id": doc.id}, search.index_name(tenant_id), doc.kb_id)
        except Exception as e:
            logging.error(f"Failed to delete chunks from doc store for document {doc.id}: {e}")

        # 删除文档元数据(非关键)
        try:
            from api.db.services.doc_metadata_service import DocMetadataService
            DocMetadataService.delete_document_metadata(doc.id)
        except Exception as e:
            logging.warning(f"Failed to delete metadata for document {doc.id}: {e}")

        # 清理知识图谱引用(非关键)
        try:
            from common.doc_store.doc_store_base import OrderByExpr
            from rag.nlp import search
            graph_source = settings.docStoreConn.get_fields(
                settings.docStoreConn.search(["source_id"], [], {"kb_id": doc.kb_id, "knowledge_graph_kwd": ["graph"]}, [], OrderByExpr(), 0, 1, search.index_name(tenant_id), [doc.kb_id]), ["source_id"]
            )
            if len(graph_source) > 0 and doc.id in list(graph_source.values())[0]["source_id"]:
                settings.docStoreConn.update({"kb_id": doc.kb_id, "knowledge_graph_kwd": ["entity", "relation", "graph", "subgraph", "community_report"], "source_id": doc.id},
                                             {"remove": {"source_id": doc.id}},
                                             search.index_name(tenant_id), doc.kb_id)
                settings.docStoreConn.update({"kb_id": doc.kb_id, "knowledge_graph_kwd": ["graph"]},
                                             {"removed_kwd": "Y"},
                                             search.index_name(tenant_id), doc.kb_id)
                settings.docStoreConn.delete({"kb_id": doc.kb_id, "knowledge_graph_kwd": ["entity", "relation", "graph", "subgraph", "community_report"], "must_not": {"exists": "source_id"}},
                                             search.index_name(tenant_id), doc.kb_id)
        except Exception as e:
            logging.warning(f"Failed to cleanup knowledge graph for document {doc.id}: {e}")

        return cls.delete_by_id(doc.id)

    @classmethod
    @DB.connection_context()
    def delete_chunk_images(cls, doc, tenant_id):
        """清掉该文档切块关联的图片(文档里的插图存对象存储, 按页扫 docStore 的 img_id)。"""
        from common.doc_store.doc_store_base import OrderByExpr
        from rag.nlp import search
        page = 0
        page_size = 1000
        while True:
            chunks = settings.docStoreConn.search(["img_id"], [], {"doc_id": doc.id}, [], OrderByExpr(),
                                                  page * page_size, page_size, search.index_name(tenant_id),
                                                  [doc.kb_id])
            chunk_ids = settings.docStoreConn.get_doc_ids(chunks)
            if not chunk_ids:
                break
            for cid in chunk_ids:
                if settings.STORAGE_IMPL.obj_exist(doc.kb_id, cid):
                    settings.STORAGE_IMPL.rm(doc.kb_id, cid)
            page += 1

    @classmethod
    @DB.connection_context()
    def get_newly_uploaded(cls):
        """10 分钟内新上传且正在解析的文档(task_executor 拉取任务用)。"""
        fields = [
            cls.model.id,
            cls.model.kb_id,
            cls.model.parser_id,
            cls.model.parser_config,
            cls.model.name,
            cls.model.type,
            cls.model.location,
            cls.model.size,
            Knowledgebase.tenant_id,
            Tenant.embd_id,
            Tenant.img2txt_id,
            Tenant.asr_id,
            cls.model.update_time]
        docs = cls.model.select(*fields) \
            .join(Knowledgebase, on=(cls.model.kb_id == Knowledgebase.id)) \
            .join(Tenant, on=(Knowledgebase.tenant_id == Tenant.id)) \
            .where(
            cls.model.status == StatusEnum.VALID.value,
            ~(cls.model.type == FileType.VIRTUAL.value),
            cls.model.progress == 0,
            cls.model.update_time >= current_timestamp() - 1000 * 600,
            cls.model.run == TaskStatus.RUNNING.value) \
            .order_by(cls.model.update_time.asc())
        return list(docs.dicts())

    @classmethod
    @DB.connection_context()
    def get_unfinished_docs(cls):
        """解析未完成的文档(进度 0~1, 或还有未完成的任务——含 GraphRAG/RAPTOR/Mindmap)。"""
        fields = [cls.model.id, cls.model.process_begin_at, cls.model.parser_config, cls.model.progress_msg,
                  cls.model.run, cls.model.parser_id]
        unfinished_task_query = Task.select(Task.doc_id).where(
            (Task.progress >= 0) & (Task.progress < 1)
        )

        docs = cls.model.select(*fields) \
            .where(
            cls.model.status == StatusEnum.VALID.value,
            ~(cls.model.type == FileType.VIRTUAL.value),
            ((cls.model.run.is_null(True)) | (cls.model.run != TaskStatus.CANCEL.value)),
            (((cls.model.progress < 1) & (cls.model.progress > 0)) |
             (cls.model.id.in_(unfinished_task_query))))
        return list(docs.dicts())

    @classmethod
    @DB.connection_context()
    def increment_chunk_num(cls, doc_id, kb_id, token_num, chunk_num, duration):
        """解析完成后累加文档/kb 的切块数与 token 数(失败静默: 疑似文档已删)。"""
        num = cls.model.update(token_num=cls.model.token_num + token_num,
                               chunk_num=cls.model.chunk_num + chunk_num,
                               process_duration=cls.model.process_duration + duration).where(
            cls.model.id == doc_id).execute()
        if num == 0:
            logging.warning("Document not found which is supposed to be there")
        num = Knowledgebase.update(
            token_num=Knowledgebase.token_num +
                      token_num,
            chunk_num=Knowledgebase.chunk_num +
                      chunk_num).where(
            Knowledgebase.id == kb_id).execute()
        return num

    @classmethod
    @DB.connection_context()
    def decrement_chunk_num(cls, doc_id, kb_id, token_num, chunk_num, duration):
        """重新解析前回退计数(与 increment 对称, 文档不存在则报错)。"""
        num = cls.model.update(token_num=cls.model.token_num - token_num,
                               chunk_num=cls.model.chunk_num - chunk_num,
                               process_duration=cls.model.process_duration + duration).where(
            cls.model.id == doc_id).execute()
        if num == 0:
            raise LookupError(
                "Document not found which is supposed to be there")
        num = Knowledgebase.update(
            token_num=Knowledgebase.token_num -
                      token_num,
            chunk_num=Knowledgebase.chunk_num -
                      chunk_num
        ).where(
            Knowledgebase.id == kb_id).execute()
        return num

    @classmethod
    @DB.connection_context()
    def clear_chunk_num(cls, doc_id):
        """文档删除时清计数: kb 的 token/chunk 回减, doc_num -1。"""
        doc = cls.model.get_by_id(doc_id)
        assert doc, "Can't fine document in database."

        num = Knowledgebase.update(
            token_num=Knowledgebase.token_num -
                      doc.token_num,
            chunk_num=Knowledgebase.chunk_num -
                      doc.chunk_num,
            doc_num=Knowledgebase.doc_num - 1
        ).where(
            Knowledgebase.id == doc.kb_id).execute()
        return num

    @classmethod
    @DB.connection_context()
    def clear_chunk_num_when_rerun(cls, doc_id):
        """重新解析前的计数回退(不扣 doc_num, 文档还在)。"""
        doc = cls.model.get_by_id(doc_id)
        assert doc, "Can't fine document in database."

        num = (
            Knowledgebase.update(
                token_num=Knowledgebase.token_num - doc.token_num,
                chunk_num=Knowledgebase.chunk_num - doc.chunk_num,
            )
            .where(Knowledgebase.id == doc.kb_id)
            .execute()
        )
        return num

    @classmethod
    @DB.connection_context()
    def get_tenant_id(cls, doc_id):
        """文档所属租户(联 kb 拿 tenant_id, kb 失效则返回 None)。"""
        docs = cls.model.select(
            Knowledgebase.tenant_id).join(
            Knowledgebase, on=(
                    Knowledgebase.id == cls.model.kb_id)).where(
            cls.model.id == doc_id, Knowledgebase.status == StatusEnum.VALID.value)
        docs = docs.dicts()
        if not docs:
            return None
        return docs[0]["tenant_id"]

    @classmethod
    @DB.connection_context()
    def get_knowledgebase_id(cls, doc_id):
        """文档所属 kb id。"""
        docs = cls.model.select(cls.model.kb_id).where(cls.model.id == doc_id)
        docs = docs.dicts()
        if not docs:
            return None
        return docs[0]["kb_id"]

    @classmethod
    @DB.connection_context()
    def get_tenant_id_by_name(cls, name):
        """按文档名查租户(旧接口); 同名文档可能多条, 取第一条。"""
        docs = cls.model.select(
            Knowledgebase.tenant_id).join(
            Knowledgebase, on=(
                    Knowledgebase.id == cls.model.kb_id)).where(
            cls.model.name == name, Knowledgebase.status == StatusEnum.VALID.value)
        docs = docs.dicts()
        if not docs:
            return None
        return docs[0]["tenant_id"]

    @classmethod
    @DB.connection_context()
    def accessible(cls, doc_id, user_id):
        """用户能否访问该文档: 文档 → kb → tenant → user_tenant 关系链。"""
        docs = cls.model.select(
            cls.model.id).join(
            Knowledgebase, on=(
                    Knowledgebase.id == cls.model.kb_id)
        ).join(UserTenant, on=(UserTenant.tenant_id == Knowledgebase.tenant_id)
               ).where(cls.model.id == doc_id, UserTenant.user_id == user_id).paginate(0, 1)
        docs = docs.dicts()
        if not docs:
            return False
        return True

    @classmethod
    @DB.connection_context()
    def accessible4deletion(cls, doc_id, user_id):
        """能否删除该文档: 必须是 kb 创建者租户内的 OWNER 或 NORMAL 成员。

        注意和 accessible 的区别: 这里 join 的是 Knowledgebase.created_by(创建者)
        而不是 tenant_id, 并校验了角色。
        """
        docs = cls.model.select(cls.model.id
                                ).join(
            Knowledgebase, on=(
                    Knowledgebase.id == cls.model.kb_id)
        ).join(
            UserTenant, on=(
                    (UserTenant.tenant_id == Knowledgebase.created_by) & (UserTenant.user_id == user_id))
        ).where(
            cls.model.id == doc_id,
            UserTenant.status == StatusEnum.VALID.value,
            ((UserTenant.role == UserTenantRole.NORMAL) | (UserTenant.role == UserTenantRole.OWNER))
        ).paginate(0, 1)
        docs = docs.dicts()
        if not docs:
            return False
        return True

    @classmethod
    @DB.connection_context()
    def get_embd_id(cls, doc_id):
        """文档所在 kb 的 embedding 模型 id(向量化前查模型)。"""
        docs = cls.model.select(
            Knowledgebase.embd_id).join(
            Knowledgebase, on=(
                    Knowledgebase.id == cls.model.kb_id)).where(
            cls.model.id == doc_id, Knowledgebase.status == StatusEnum.VALID.value)
        docs = docs.dicts()
        if not docs:
            return None
        return docs[0]["embd_id"]

    @classmethod
    @DB.connection_context()
    def get_chunking_config(cls, doc_id):
        """切块配置快照: 文档自己的 parser_config + kb 语言/embedding + 租户模型配置。

        任务执行器拿这一份配置去跑解析(文档级配置优先)。
        """
        configs = (
            cls.model.select(
                cls.model.id,
                cls.model.kb_id,
                cls.model.parser_id,
                cls.model.parser_config,
                Knowledgebase.language,
                Knowledgebase.embd_id,
                Tenant.id.alias("tenant_id"),
                Tenant.img2txt_id,
                Tenant.asr_id,
                Tenant.llm_id,
            )
            .join(Knowledgebase, on=(cls.model.kb_id == Knowledgebase.id))
            .join(Tenant, on=(Knowledgebase.tenant_id == Tenant.id))
            .where(cls.model.id == doc_id)
        )
        configs = configs.dicts()
        if not configs:
            return None
        return configs[0]

    @classmethod
    @DB.connection_context()
    def get_doc_id_by_doc_name(cls, doc_name):
        """按文档名取单个 id(重名时返回第一条)。"""
        fields = [cls.model.id]
        doc_id = cls.model.select(*fields) \
            .where(cls.model.name == doc_name)
        doc_id = doc_id.dicts()
        if not doc_id:
            return None
        return doc_id[0]["id"]

    @classmethod
    @DB.connection_context()
    def get_doc_ids_by_doc_names(cls, doc_names):
        """按文档名列表批量取 id(流式, 避免一次拉太多)。"""
        if not doc_names:
            return []

        query = cls.model.select(cls.model.id).where(cls.model.name.in_(doc_names))
        return list(query.scalars().iterator())

    @classmethod
    @DB.connection_context()
    def get_thumbnails(cls, docids):
        """批量取缩略图(文档列表卡片用)。"""
        fields = [cls.model.id, cls.model.kb_id, cls.model.thumbnail]
        return list(cls.model.select(
            *fields).where(cls.model.id.in_(docids)).dicts())

    @classmethod
    @DB.connection_context()
    def update_parser_config(cls, id, config):
        """更新文档级切块配置(深合并)。

        官方特意处理了 raptor: 新配置里没有 raptor 时, 把旧的整个删掉
        (用户手动关掉 raptor 的场景)。
        """
        if not config:
            return
        e, d = cls.get_by_id(id)
        if not e:
            raise LookupError(f"Document({id}) not found.")

        def dfs_update(old, new):
            for k, v in new.items():
                if k not in old:
                    old[k] = v
                    continue
                if isinstance(v, dict) and isinstance(old[k], dict):
                    dfs_update(old[k], v)
                else:
                    old[k] = v

        dfs_update(d.parser_config, config)
        if not config.get("raptor") and d.parser_config.get("raptor"):
            del d.parser_config["raptor"]
        cls.update_by_id(id, {"parser_config": d.parser_config})

    @classmethod
    @DB.connection_context()
    def get_doc_count(cls, tenant_id):
        """某租户下的文档总数(免费用户配额检查用)。"""
        docs = cls.model.select(cls.model.id).join(Knowledgebase,
                                                   on=(Knowledgebase.id == cls.model.kb_id)).where(
            Knowledgebase.tenant_id == tenant_id)
        return len(docs)

    @classmethod
    @DB.connection_context()
    def begin2parse(cls, doc_id, keep_progress=False):
        """解析开始前的状态标记: progress 置随机小值, run 置 RUNNING。

        keep_progress=True 用于 GraphRAG/RAPTOR/Mindmap 等增强任务:
        文档本身已解析完(DONE), 不能把进度归零。
        """
        info = {
            "progress_msg": "Task is queued...",
            "process_begin_at": get_format_time(),
        }
        if not keep_progress:
            info["progress"] = random.random() * 1 / 100.
            info["run"] = TaskStatus.RUNNING.value

        cls.update_by_id(doc_id, info)

    @classmethod
    @DB.connection_context()
    def update_progress(cls):
        """把未完成文档的进度与任务表同步(定时任务/worker 循环调用)。"""
        docs = cls.get_unfinished_docs()

        cls._sync_progress(docs)

    @classmethod
    @DB.connection_context()
    def update_progress_immediately(cls, docs: list[dict]):
        """立即同步指定文档的进度(上传后马上调, 不等定时器)。"""
        if not docs:
            return

        cls._sync_progress(docs)

    @classmethod
    @DB.connection_context()
    def _sync_progress(cls, docs: list[dict]):
        """进度聚合核心: 汇总一个文档全部 Task 的进度 → 写回 document 表。

        规则: 全部完成且无失败 → progress=1/DONE; 有失败 → -1/FAIL;
        未完成 → 平均进度; 增强任务(special_task)进行中且文档已解析完 → 冻结进度不变。
        队列积压信息来自 Redis(get_queue_length)。
        """
        from api.db.services.task_service import TaskService

        for d in docs:
            try:
                tsks = TaskService.query(doc_id=d["id"], order_by=Task.create_time)
                if not tsks:
                    continue
                msg = []
                prg = 0
                finished = True
                bad = 0
                e, doc = DocumentService.get_by_id(d["id"])
                status = doc.run  # TaskStatus.RUNNING.value
                if status == TaskStatus.CANCEL.value:
                    continue
                doc_progress = doc.progress if doc and doc.progress else 0.0
                special_task_running = False
                priority = 0
                for t in tsks:
                    task_type = (t.task_type or "").lower()
                    if task_type in PIPELINE_SPECIAL_PROGRESS_FREEZE_TASK_TYPES:
                        special_task_running = True
                    if 0 <= t.progress < 1:
                        finished = False
                    if t.progress == -1:
                        bad += 1
                    prg += t.progress if t.progress >= 0 else 0
                    if t.progress_msg.strip():
                        msg.append(t.progress_msg)
                    priority = max(priority, t.priority)
                prg /= len(tsks)
                if finished and bad:
                    prg = -1
                    status = TaskStatus.FAIL.value
                elif finished:
                    prg = 1
                    status = TaskStatus.DONE.value

                # 增强任务进行中且文档已解析完: 冻结进度, 不往回退
                freeze_progress = special_task_running and doc_progress >= 1 and not finished
                msg = "\n".join(sorted(msg))
                begin_at = d.get("process_begin_at")
                if not begin_at:
                    begin_at = datetime.now()
                    cls.update_by_id(d["id"], {"process_begin_at": begin_at})

                info = {
                    "process_duration": max(datetime.timestamp(datetime.now()) - begin_at.timestamp(), 0),
                    "run": status}
                if prg != 0 and not freeze_progress:
                    info["progress"] = prg
                if msg:
                    info["progress_msg"] = msg
                    if msg.endswith("created task graphrag") or msg.endswith("created task raptor") or msg.endswith("created task mindmap"):
                        info["progress_msg"] += "\n%d tasks are ahead in the queue..." % get_queue_length(priority)
                else:
                    info["progress_msg"] = "%d tasks are ahead in the queue..." % get_queue_length(priority)
                info["update_time"] = current_timestamp()
                info["update_date"] = get_format_time()
                (
                    cls.model.update(info)
                    .where(
                        (cls.model.id == d["id"])
                        & ((cls.model.run.is_null(True)) | (cls.model.run != TaskStatus.CANCEL.value))
                    )
                    .execute()
                )
            except Exception as e:
                if str(e).find("'0'") < 0:
                    logging.exception("fetch task exception")

    @classmethod
    @DB.connection_context()
    def get_kb_doc_count(cls, kb_id):
        """单 kb 文档数。"""
        return cls.model.select().where(cls.model.kb_id == kb_id).count()

    @classmethod
    @DB.connection_context()
    def get_all_kb_doc_count(cls):
        """全库按 kb 聚合文档数(kb 概览看板用)。"""
        result = {}
        rows = cls.model.select(cls.model.kb_id, fn.COUNT(cls.model.id).alias("count")).group_by(cls.model.kb_id)
        for row in rows:
            result[row.kb_id] = row.count
        return result

    @classmethod
    @DB.connection_context()
    def do_cancel(cls, doc_id):
        """是否已被取消(取消后 run=CANCEL 或 progress<0)。"""
        try:
            _, doc = DocumentService.get_by_id(doc_id)
            return doc.run == TaskStatus.CANCEL.value or doc.progress < 0
        except Exception:
            pass
        return False

    @classmethod
    @DB.connection_context()
    def knowledgebase_basic_info(cls, kb_id: str) -> dict[str, int]:
        """kb 概览: 处理中/完成/失败/取消/非本地来源 的数量(一条 SQL 聚合)。

        进度语义: 1=完成, -1=失败, 0~1=处理中, run="2"=取消。
        """
        cancelled = (
            cls.model.select(fn.COUNT(1))
            .where((cls.model.kb_id == kb_id) & (cls.model.run == TaskStatus.CANCEL))
            .scalar()
        )
        downloaded = (
            cls.model.select(fn.COUNT(1))
            .where(
                cls.model.kb_id == kb_id,
                cls.model.source_type != "local"
            )
            .scalar()
        )

        row = (
            cls.model.select(
                fn.COALESCE(fn.SUM(Case(None, [(cls.model.progress == 1, 1)], 0)), 0).alias("finished"),
                fn.COALESCE(fn.SUM(Case(None, [(cls.model.progress == -1, 1)], 0)), 0).alias("failed"),
                fn.COALESCE(
                    fn.SUM(
                        Case(
                            None,
                            [
                                (((cls.model.progress == 0) | ((cls.model.progress > 0) & (cls.model.progress < 1))), 1),
                            ],
                            0,
                        )
                    ),
                    0,
                ).alias("processing"),
            )
            .where(
                (cls.model.kb_id == kb_id)
                & ((cls.model.run.is_null(True)) | (cls.model.run != TaskStatus.CANCEL))
            )
            .dicts()
            .get()
        )

        return {
            "processing": int(row["processing"]),
            "finished": int(row["finished"]),
            "failed": int(row["failed"]),
            "cancelled": int(cancelled),
            "downloaded": int(downloaded)
        }

    @classmethod
    def run(cls, tenant_id: str, doc: dict, kb_table_num_map: dict):
        """把文档投递到任务队列: 有 pipeline 走画布数据流, 否则走普通解析任务。

        table 模板特殊处理: 该 kb 若还没有 DONE 的文档且没有 field_map,
        先清掉旧 field_map(避免表格模板残留配置)。
        """
        from api.db.services.task_service import queue_dataflow, queue_tasks
        from api.db.services.file2document_service import File2DocumentService

        doc["tenant_id"] = tenant_id
        doc_parser = doc.get("parser_id", ParserType.NAIVE)
        if doc_parser == ParserType.TABLE:
            kb_id = doc.get("kb_id")
            if not kb_id:
                return
            if kb_id not in kb_table_num_map:
                count = DocumentService.count_by_kb_id(kb_id=kb_id, keywords="", run_status=[TaskStatus.DONE], types=[])
                kb_table_num_map[kb_id] = count
                if kb_table_num_map[kb_id] <= 0:
                    KnowledgebaseService.delete_field_map(kb_id)
        if doc.get("pipeline_id", ""):
            queue_dataflow(tenant_id, flow_id=doc["pipeline_id"], task_id=get_uuid(), doc_id=doc["id"])
        else:
            bucket, name = File2DocumentService.get_storage_address(doc_id=doc["id"])
            queue_tasks(doc, bucket, name, 0)


def queue_raptor_o_graphrag_tasks(sample_doc_id, ty, priority, fake_doc_id="", doc_ids=[]):
    """为知识库级增强任务(GraphRAG/RAPTOR/Mindmap)创建任务并入队。

    sample_doc_id 提供切块配置; fake_doc_id 用于绕过"任务必须挂在文档下"的限制;
    digest 是切块配置 + 文档信息的哈希, 用于任务去重。
    """
    assert ty in ["graphrag", "raptor", "mindmap"], "type should be graphrag, raptor or mindmap"

    chunking_config = DocumentService.get_chunking_config(sample_doc_id["id"])
    hasher = xxhash.xxh64()
    for field in sorted(chunking_config.keys()):
        hasher.update(str(chunking_config[field]).encode("utf-8"))

    def new_task():
        nonlocal sample_doc_id
        return {
            "id": get_uuid(),
            "doc_id": sample_doc_id["id"],
            "from_page": 100000000,
            "to_page": 100000000,
            "task_type": ty,
            "progress_msg": datetime.now().strftime("%H:%M:%S") + " created task " + ty,
            "begin_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    task = new_task()
    for field in ["doc_id", "from_page", "to_page"]:
        hasher.update(str(task.get(field, "")).encode("utf-8"))
    hasher.update(ty.encode("utf-8"))
    task["digest"] = hasher.hexdigest()
    from api.db.db_utils import bulk_insert_into_db
    bulk_insert_into_db(Task, [task], True)

    task["doc_id"] = fake_doc_id
    task["doc_ids"] = doc_ids
    DocumentService.begin2parse(sample_doc_id["id"], keep_progress=True)
    from rag.utils.redis_conn import REDIS_CONN
    assert REDIS_CONN.queue_product(_svr_queue_name(priority), message=task), "Can't access Redis. Please check the Redis' status."
    return task["id"]


def get_queue_length(priority):
    """某优先级任务队列的积压量(Redis stream 的 lag), 展示"前面还有几个任务"。"""
    from rag.utils.redis_conn import REDIS_CONN
    group_info = REDIS_CONN.queue_info(_svr_queue_name(priority), SVR_CONSUMER_GROUP_NAME)
    if not group_info:
        return 0
    return int(group_info.get("lag", 0) or 0)


def doc_upload_and_parse(conversation_id, file_objs, user_id):
    """对话内直接上传并解析文档(老接口): 循环依赖多, 官方逻辑照搬。

    ⚠️ 依赖链: dialogue/conversation/file/llm 服务 + rag.app 解析器 + docStore,
    全部尚未实现, 延迟导入; 后续按 rag 链路逐步点亮。
    """
    from api.db.services.api_service import API4ConversationService
    from api.db.services.conversation_service import ConversationService
    from api.db.services.dialog_service import DialogService
    from api.db.services.file_service import FileService
    from api.db.services.llm_service import LLMBundle
    from api.db.services.user_service import TenantService
    from rag.app import audio, email, naive, picture, presentation

    e, conv = ConversationService.get_by_id(conversation_id)
    if not e:
        e, conv = API4ConversationService.get_by_id(conversation_id)
    assert e, "Conversation not found!"

    e, dia = DialogService.get_by_id(conv.dialog_id)
    if not dia.kb_ids:
        raise LookupError("No dataset associated with this conversation. "
                          "Please add a dataset before uploading documents")
    kb_id = dia.kb_ids[0]
    e, kb = KnowledgebaseService.get_by_id(kb_id)
    if not e:
        raise LookupError("Can't find this dataset!")

    embd_mdl = LLMBundle(kb.tenant_id, LLMType.EMBEDDING, llm_name=kb.embd_id, lang=kb.language)

    err, files = FileService.upload_document(kb, file_objs, user_id)
    assert not err, "\n".join(err)

    def dummy(prog=None, msg=""):
        pass

    FACTORY = {
        ParserType.PRESENTATION.value: presentation,
        ParserType.PICTURE.value: picture,
        ParserType.AUDIO.value: audio,
        ParserType.EMAIL.value: email
    }
    parser_config = {"chunk_token_num": 4096, "delimiter": "\n!?;。；！？", "layout_recognize": "Plain Text", "table_context_size": 0, "image_context_size": 0}
    exe = ThreadPoolExecutor(max_workers=12)
    threads = []
    doc_nm = {}
    for d, blob in files:
        doc_nm[d["id"]] = d["name"]
    for d, blob in files:
        kwargs = {
            "callback": dummy,
            "parser_config": parser_config,
            "from_page": 0,
            "to_page": 100000,
            "tenant_id": kb.tenant_id,
            "lang": kb.language
        }
        threads.append(exe.submit(FACTORY.get(d["parser_id"], naive).chunk, d["name"], blob, **kwargs))

    for (docinfo, _), th in zip(files, threads):
        docs = []
        doc = {
            "doc_id": docinfo["id"],
            "kb_id": [kb.id]
        }
        for ck in th.result():
            d = deepcopy(doc)
            d.update(ck)
            d["id"] = xxhash.xxh64((ck["content_with_weight"] + str(d["doc_id"])).encode("utf-8")).hexdigest()
            d["create_time"] = str(datetime.now()).replace("T", " ")[:19]
            d["create_timestamp_flt"] = datetime.now().timestamp()
            if not d.get("image"):
                docs.append(d)
                continue

            output_buffer = BytesIO()
            if isinstance(d["image"], bytes):
                output_buffer = BytesIO(d["image"])
            else:
                d["image"].save(output_buffer, format="JPEG")

            settings.STORAGE_IMPL.put(kb.id, d["id"], output_buffer.getvalue())
            d["img_id"] = "{}-{}".format(kb.id, d["id"])
            d.pop("image", None)
            docs.append(d)

    parser_ids = {d["id"]: d["parser_id"] for d, _ in files}
    docids = [d["id"] for d, _ in files]
    chunk_counts = {id: 0 for id in docids}
    token_counts = {id: 0 for id in docids}
    es_bulk_size = 64

    def embedding(doc_id, cnts, batch_size=16):
        nonlocal embd_mdl, chunk_counts, token_counts
        vectors = []
        for i in range(0, len(cnts), batch_size):
            vts, c = embd_mdl.encode(cnts[i: i + batch_size])
            vectors.extend(vts.tolist())
            chunk_counts[doc_id] += len(cnts[i:i + batch_size])
            token_counts[doc_id] += c
        return vectors

    from rag.nlp import search, rag_tokenizer
    idxnm = search.index_name(kb.tenant_id)
    try_create_idx = True

    _, tenant = TenantService.get_by_id(kb.tenant_id)
    llm_bdl = LLMBundle(kb.tenant_id, LLMType.CHAT, tenant.llm_id)
    for doc_id in docids:
        cks = [c for c in docs if c["doc_id"] == doc_id]

        if parser_ids[doc_id] != ParserType.PICTURE.value:
            from rag.graphrag.general.mind_map_extractor import MindMapExtractor
            mindmap = MindMapExtractor(llm_bdl)
            try:
                mind_map = asyncio.run(mindmap([c["content_with_weight"] for c in docs if c["doc_id"] == doc_id]))
                mind_map = json.dumps(mind_map.output, ensure_ascii=False, indent=2)
                if len(mind_map) < 32:
                    raise Exception("Few content: " + mind_map)
                cks.append({
                    "id": get_uuid(),
                    "doc_id": doc_id,
                    "kb_id": [kb.id],
                    "docnm_kwd": doc_nm[doc_id],
                    "title_tks": rag_tokenizer.tokenize(re.sub(r"\.[a-zA-Z]+$", "", doc_nm[doc_id])),
                    "content_ltks": rag_tokenizer.tokenize("summary summarize 总结 概况 file 文件 概括"),
                    "content_with_weight": mind_map,
                    "knowledge_graph_kwd": "mind_map"
                })
            except Exception:
                logging.exception("Mind map generation error")

        vectors = embedding(doc_id, [c["content_with_weight"] for c in cks])
        assert len(cks) == len(vectors)
        for i, d in enumerate(cks):
            v = vectors[i]
            d["q_%d_vec" % len(v)] = v
        for b in range(0, len(cks), es_bulk_size):
            if try_create_idx:
                if not settings.docStoreConn.index_exist(idxnm, kb_id):
                    settings.docStoreConn.create_idx(idxnm, kb_id, len(vectors[0]), kb.parser_id)
                try_create_idx = False
            settings.docStoreConn.insert(cks[b:b + es_bulk_size], idxnm, kb_id)

        DocumentService.increment_chunk_num(
            doc_id, kb.id, token_counts[doc_id], chunk_counts[doc_id], 0)

    return [d["id"] for d, _ in files]