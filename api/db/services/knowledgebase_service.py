"""knowledgebase_service.py — 知识库(数据集)业务服务, 对齐官方 v0.24.0。

知识库是 RAG 的"资产容器": 文档挂在 kb 下, kb 挂在 tenant 下。
本文件学习要点:
- 权限模型: 单用户可见(permission=ME, tenant_id==user_id)/ 团队可见(permission=TEAM, tenant 在 joined_tenant_ids 里)
- parser_config: 每个 kb 存一份切块配置, 用深合并(deep_merge)留默认值
- 文档计数: 上传/删除文档时用原子操作维护 kb.doc_num/chunk_num/token_num
"""

from datetime import datetime

from peewee import JOIN, fn

from api.constants import DATASET_NAME_LIMIT
from api.db import TenantPermission
from api.db.db_models import DB, Document, Knowledgebase, User, UserCanvas, UserTenant
from api.db.services import duplicate_name
from api.db.services.common_service import CommonService
from api.db.services.user_service import TenantService
from api.utils.api_utils import get_data_error_result, get_parser_config
from common.constants import StatusEnum
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp, datetime_format


class KnowledgebaseService(CommonService):
    """知识库业务服务: 建库(名称去重+默认切块配置)、列表/详情、权限校验、文档计数维护。"""

    model = Knowledgebase

    @classmethod
    @DB.connection_context()
    def accessible4deletion(cls, kb_id, user_id):
        """能否删除某 kb: 只看创建者(created_by)是不是这个用户。"""
        docs = cls.model.select(cls.model.id).where(cls.model.id == kb_id, cls.model.created_by == user_id).paginate(0, 1)
        docs = docs.dicts()
        if not docs:
            return False
        return True

    @classmethod
    @DB.connection_context()
    def is_parsed_done(cls, kb_id):
        """kb 下所有文档是否都已解析完成(建对话/问答前要等解析)。

        返回 (True, None) 表示可以开始; 否则 (False, 原因)。
        注意: 依赖 document_service, 这里延迟 import 避免启动循环依赖。
        """
        from api.db.services.document_service import DocumentService

        from common.constants import TaskStatus

        kbs = cls.query(id=kb_id)
        if not kbs:
            return False, "Knowledge base not found"
        kb = kbs[0]

        docs, _ = DocumentService.get_by_kb_id(kb_id, 1, 1000, "create_time", True, "", [], [])

        for doc in docs:
            if doc["run"] == TaskStatus.RUNNING.value or doc["run"] == TaskStatus.CANCEL.value or doc["run"] == TaskStatus.FAIL.value:
                return False, f"Document '{doc['name']}' in dataset '{kb.name}' is still being parsed. Please wait until all documents are parsed before starting a chat."
            if doc["run"] == TaskStatus.UNSTART.value and doc["chunk_num"] == 0:
                return False, f"Document '{doc['name']}' in dataset '{kb.name}' has not been parsed yet. Please parse all documents before starting a chat."

        return True, None

    @classmethod
    @DB.connection_context()
    def list_documents_by_ids(cls, kb_ids):
        """按 kb id 列表, 联查返回其下全部文档 id。"""
        doc_ids = cls.model.select(Document.id.alias("document_id")).join(Document, on=(cls.model.id == Document.kb_id)).where(cls.model.id.in_(kb_ids))
        doc_ids = list(doc_ids.dicts())
        doc_ids = [doc["document_id"] for doc in doc_ids]
        return doc_ids

    @classmethod
    @DB.connection_context()
    def get_by_tenant_ids(cls, joined_tenant_ids, user_id, page_number, items_per_page, orderby, desc, keywords, parser_id=None):
        """按租户+权限分页查 kb 列表。

        权限过滤: (租户在 joined_tenant_ids 里 且 该 kb 是团队可见 TEAM) 或 (kb 属于自己)
        联 user 表取租户主的昵称/头像(界面上要显示"创建者")。
        """
        fields = [
            cls.model.id,
            cls.model.avatar,
            cls.model.name,
            cls.model.language,
            cls.model.description,
            cls.model.tenant_id,
            cls.model.permission,
            cls.model.doc_num,
            cls.model.token_num,
            cls.model.chunk_num,
            cls.model.parser_id,
            cls.model.embd_id,
            User.nickname,
            User.avatar.alias("tenant_avatar"),
            cls.model.update_time,
        ]
        if keywords:
            kbs = (
                cls.model.select(*fields)
                .join(User, on=(cls.model.tenant_id == User.id))
                .where(
                    ((cls.model.tenant_id.in_(joined_tenant_ids) & (cls.model.permission == TenantPermission.TEAM.value)) | (cls.model.tenant_id == user_id))
                    & (cls.model.status == StatusEnum.VALID.value),
                    (fn.LOWER(cls.model.name).contains(keywords.lower())),
                )
            )
        else:
            kbs = (
                cls.model.select(*fields)
                .join(User, on=(cls.model.tenant_id == User.id))
                .where(
                    ((cls.model.tenant_id.in_(joined_tenant_ids) & (cls.model.permission == TenantPermission.TEAM.value)) | (cls.model.tenant_id == user_id))
                    & (cls.model.status == StatusEnum.VALID.value)
                )
            )
        if parser_id:
            kbs = kbs.where(cls.model.parser_id == parser_id)
        if desc:
            kbs = kbs.order_by(cls.model.getter_by(orderby).desc())
        else:
            kbs = kbs.order_by(cls.model.getter_by(orderby).asc())

        count = kbs.count()

        if page_number and items_per_page:
            kbs = kbs.paginate(page_number, items_per_page)

        return list(kbs.dicts()), count

    @classmethod
    @DB.connection_context()
    def get_all_kb_by_tenant_ids(cls, tenant_ids, user_id):
        """取全部有权限的 kb(深分页, 慎用): 团队可见 + 自己的。

        每次 50 条循环取, 避免一次 offset 到很深的深分页慢查询。
        """
        fields = [
            cls.model.name,
            cls.model.avatar,
            cls.model.language,
            cls.model.permission,
            cls.model.doc_num,
            cls.model.token_num,
            cls.model.chunk_num,
            cls.model.status,
            cls.model.create_date,
            cls.model.update_date,
        ]
        kbs = cls.model.select(*fields).where((cls.model.tenant_id.in_(tenant_ids) & (cls.model.permission == TenantPermission.TEAM.value)) | (cls.model.tenant_id == user_id))
        kbs.order_by(cls.model.create_time.asc())
        offset, limit = 0, 50
        res = []
        while True:
            kb_batch = kbs.offset(offset).limit(limit)
            _temp = list(kb_batch.dicts())
            if not _temp:
                break
            res.extend(_temp)
            offset += limit
        return res

    @classmethod
    @DB.connection_context()
    def get_kb_ids(cls, tenant_id):
        """某租户下全部 kb id。"""
        fields = [
            cls.model.id,
        ]
        kbs = cls.model.select(*fields).where(cls.model.tenant_id == tenant_id)
        kb_ids = [kb.id for kb in kbs]
        return kb_ids

    @classmethod
    @DB.connection_context()
    def get_detail(cls, kb_id):
        """kb 详情: 含 pipeline(画布)信息, 左连接 UserCanvas。

        注意用 JOIN.LEFT_OUTER: kb 没绑画布时 pipeline 字段为 null, 不能丢行。
        """
        fields = [
            cls.model.id,
            cls.model.embd_id,
            cls.model.avatar,
            cls.model.name,
            cls.model.language,
            cls.model.description,
            cls.model.permission,
            cls.model.doc_num,
            cls.model.token_num,
            cls.model.chunk_num,
            cls.model.parser_id,
            cls.model.pipeline_id,
            UserCanvas.title.alias("pipeline_name"),
            UserCanvas.avatar.alias("pipeline_avatar"),
            cls.model.parser_config,
            cls.model.pagerank,
            cls.model.graphrag_task_id,
            cls.model.graphrag_task_finish_at,
            cls.model.raptor_task_id,
            cls.model.raptor_task_finish_at,
            cls.model.mindmap_task_id,
            cls.model.mindmap_task_finish_at,
            cls.model.create_time,
            cls.model.update_time,
        ]
        kbs = (
            cls.model.select(*fields)
            .join(UserCanvas, on=(cls.model.pipeline_id == UserCanvas.id), join_type=JOIN.LEFT_OUTER)
            .where((cls.model.id == kb_id), (cls.model.status == StatusEnum.VALID.value))
            .dicts()
        )
        if not kbs:
            return None
        return kbs[0]

    @classmethod
    @DB.connection_context()
    def update_parser_config(cls, id, config):
        """更新 kb 的切块配置(深合并: 只覆盖传入的键, 保留其余默认值)。"""
        e, m = cls.get_by_id(id)
        if not e:
            raise LookupError(f"dataset({id}) not found.")

        def dfs_update(old, new):
            """递归合并: dict 继续下钻, list 做并集去重, 其余值直接覆盖。"""
            for k, v in new.items():
                if k not in old:
                    old[k] = v
                    continue
                if isinstance(v, dict):
                    assert isinstance(old[k], dict)
                    dfs_update(old[k], v)
                elif isinstance(v, list):
                    assert isinstance(old[k], list)
                    old[k] = list(set(old[k] + v))
                else:
                    old[k] = v

        dfs_update(m.parser_config, config)
        cls.update_by_id(id, {"parser_config": m.parser_config})

    @classmethod
    @DB.connection_context()
    def delete_field_map(cls, id):
        """删掉 parser_config 里的 field_map(字段映射, 一般用于 excel/csv 导入)。"""
        e, m = cls.get_by_id(id)
        if not e:
            raise LookupError(f"dataset({id}) not found.")

        m.parser_config.pop("field_map", None)
        cls.update_by_id(id, {"parser_config": m.parser_config})

    @classmethod
    @DB.connection_context()
    def get_field_map(cls, ids):
        """汇总多个 kb 的 field_map(后查到的覆盖先查到的)。"""
        conf = {}
        for k in cls.get_by_ids(ids):
            if k.parser_config and "field_map" in k.parser_config:
                conf.update(k.parser_config["field_map"])
        return conf

    @classmethod
    @DB.connection_context()
    def get_by_name(cls, kb_name, tenant_id):
        """同租户下按名字查 kb(建库前查重用), 返回 (是否存在, kb 或 None)。"""
        kb = cls.model.select().where((cls.model.name == kb_name) & (cls.model.tenant_id == tenant_id) & (cls.model.status == StatusEnum.VALID.value))
        if kb:
            return True, kb[0]
        return False, None

    @classmethod
    @DB.connection_context()
    def get_all_ids(cls):
        """全部 kb id(运维场景用)。"""
        return [m["id"] for m in cls.model.select(cls.model.id).dicts()]

    @classmethod
    @DB.connection_context()
    def create_with_name(cls, *, name: str, tenant_id: str, parser_id: str | None = None, **kwargs):
        """只按名字建库(官方 kb_app.create 抽出来的公共逻辑, RESTful 接口也复用)。

        流程: 名校验 → 租户内重名自动加 (1)(2) → 校验租户存在 →
        生成默认 parser_config(模板默认 + 传入覆盖) → 注入租户默认 llm_id。
        返回 (True, payload_dict) 或 (False, 错误响应)。
        注意: 只组装数据, 不落库; 落库由调用方 save 完成。
        """
        if not isinstance(name, str):
            return False, get_data_error_result(message="Dataset name must be string.")
        dataset_name = name.strip()
        if dataset_name == "":
            return False, get_data_error_result(message="Dataset name can't be empty.")
        if len(dataset_name.encode("utf-8")) > DATASET_NAME_LIMIT:
            return False, get_data_error_result(message=f"Dataset name length is {len(dataset_name)} which is large than {DATASET_NAME_LIMIT}")

        dataset_name = duplicate_name(
            cls.query,
            name=dataset_name,
            tenant_id=tenant_id,
            status=StatusEnum.VALID.value,
        )

        ok, _t = TenantService.get_by_id(tenant_id)
        if not ok:
            return False, get_data_error_result(message="Tenant not found.")

        kb_id = get_uuid()
        payload = {
            "id": kb_id,
            "name": dataset_name,
            "tenant_id": tenant_id,
            "created_by": tenant_id,
            "parser_id": (parser_id or "naive"),
            **kwargs,  # 可选字段: description/language/permission/avatar/parser_config 等
        }

        payload["parser_config"] = get_parser_config(parser_id, kwargs.get("parser_config"))
        payload["parser_config"]["llm_id"] = _t.llm_id

        return True, payload

    @classmethod
    @DB.connection_context()
    def get_list(cls, joined_tenant_ids, user_id, page_number, items_per_page, orderby, desc, id, name):
        """kb 列表(按 id/name 精确过滤版, 配合分页)。权限过滤与 get_by_tenant_ids 相同。"""
        kbs = cls.model.select()
        if id:
            kbs = kbs.where(cls.model.id == id)
        if name:
            kbs = kbs.where(cls.model.name == name)
        kbs = kbs.where(
            ((cls.model.tenant_id.in_(joined_tenant_ids) & (cls.model.permission == TenantPermission.TEAM.value)) | (cls.model.tenant_id == user_id)) & (cls.model.status == StatusEnum.VALID.value)
        )

        if desc:
            kbs = kbs.order_by(cls.model.getter_by(orderby).desc())
        else:
            kbs = kbs.order_by(cls.model.getter_by(orderby).asc())

        total = kbs.count()
        kbs = kbs.paginate(page_number, items_per_page)

        return list(kbs.dicts()), total

    @classmethod
    @DB.connection_context()
    def accessible(cls, kb_id, user_id):
        """该用户能否访问某 kb: 通过 user_tenant 关系判定(能访问租户即能访问 kb)。"""
        docs = cls.model.select(cls.model.id).join(UserTenant, on=(UserTenant.tenant_id == Knowledgebase.tenant_id)).where(cls.model.id == kb_id, UserTenant.user_id == user_id).paginate(0, 1)
        docs = docs.dicts()
        if not docs:
            return False
        return True

    @classmethod
    @DB.connection_context()
    def get_kb_by_id(cls, kb_id, user_id):
        """按 id 取 kb(带 user_tenant 权限过滤), 返回 dict 列表(最多 1 条)。"""
        kbs = cls.model.select().join(UserTenant, on=(UserTenant.tenant_id == Knowledgebase.tenant_id)).where(cls.model.id == kb_id, UserTenant.user_id == user_id).paginate(0, 1)
        kbs = kbs.dicts()
        return list(kbs)

    @classmethod
    @DB.connection_context()
    def get_kb_by_name(cls, kb_name, user_id):
        """按名字取 kb(带 user_tenant 权限过滤)。"""
        kbs = cls.model.select().join(UserTenant, on=(UserTenant.tenant_id == Knowledgebase.tenant_id)).where(cls.model.name == kb_name, UserTenant.user_id == user_id).paginate(0, 1)
        kbs = kbs.dicts()
        return list(kbs)

    @classmethod
    @DB.connection_context()
    def atomic_increase_doc_num_by_id(cls, kb_id):
        """文档数 +1(原子操作, 上传文档成功时调用)。"""
        data = {}
        data["update_time"] = current_timestamp()
        data["update_date"] = datetime_format(datetime.now())
        data["doc_num"] = cls.model.doc_num + 1
        num = cls.model.update(data).where(cls.model.id == kb_id).execute()
        return num

    @classmethod
    @DB.connection_context()
    def update_document_number_in_init(cls, kb_id, doc_num):
        """初始化系统时批量校正 doc_num(只改脏字段, 不碰时间戳)。"""
        ok, kb = cls.get_by_id(kb_id)
        if not ok:
            return
        kb.doc_num = doc_num

        dirty_fields = kb.dirty_fields
        if cls.model._meta.combined.get("update_time") in dirty_fields:
            dirty_fields.remove(cls.model._meta.combined["update_time"])

        if cls.model._meta.combined.get("update_date") in dirty_fields:
            dirty_fields.remove(cls.model._meta.combined["update_date"])

        try:
            kb.save(only=dirty_fields)
        except ValueError as e:
            if str(e) == "no data to save!":
                pass  # 没有脏字段, 正常
            else:
                raise e

    @classmethod
    @DB.connection_context()
    def decrease_document_num_in_delete(cls, kb_id, doc_num_info: dict):
        """删除文档后回减计数: doc_num/chunk_num/token_num 一次更新。"""
        kb_row = cls.model.get_by_id(kb_id)
        if not kb_row:
            raise RuntimeError(f"kb_id {kb_id} does not exist")
        update_dict = {
            "doc_num": kb_row.doc_num - doc_num_info["doc_num"],
            "chunk_num": kb_row.chunk_num - doc_num_info["chunk_num"],
            "token_num": kb_row.token_num - doc_num_info["token_num"],
            "update_time": current_timestamp(),
            "update_date": datetime_format(datetime.now()),
        }
        return cls.model.update(update_dict).where(cls.model.id == kb_id).execute()
