"""
api.db.services.api_service — API 令牌与会话记录服务。

APITokenService: SDK/API 访问令牌的增查改删(对应 db_models.APIToken);
API4ConversationService: 外部 API 会话的查询/统计/追加消息(dialog_app 等后续路由用)。
来源: 官方 api/db/services/api_service.py @ v0.24.0
"""
from datetime import datetime

import peewee

from api.db.db_models import DB, API4Conversation, APIToken, Dialog
from api.db.services.common_service import CommonService
from common.time_utils import current_timestamp, datetime_format


class APITokenService(CommonService):
    model = APIToken

    @classmethod
    @DB.connection_context()
    def used(cls, token):
        """令牌被使用时刷新 update_time/update_date。"""
        return cls.model.update({
            "update_time": current_timestamp(),
            "update_date": datetime_format(datetime.now()),
        }).where(
            cls.model.token == token
        )

    @classmethod
    @DB.connection_context()
    def delete_by_tenant_id(cls, tenant_id):
        """按租户清空全部令牌。"""
        return cls.model.delete().where(cls.model.tenant_id == tenant_id).execute()


class API4ConversationService(CommonService):
    model = API4Conversation

    @classmethod
    @DB.connection_context()
    def get_list(cls, dialog_id, tenant_id,
                 page_number, items_per_page,
                 orderby, desc, id=None, user_id=None, include_dsl=True, keywords="",
                 from_date=None, to_date=None, exp_user_id=None
                 ):
        """会话列表: 支持 id/用户/关键词/时间过滤, include_dsl=False 时跳过 dsl 大字段。"""
        if include_dsl:
            sessions = cls.model.select().where(cls.model.dialog_id == dialog_id)
        else:
            fields = [field for field in cls.model._meta.fields.values() if field.name != 'dsl']
            sessions = cls.model.select(*fields).where(cls.model.dialog_id == dialog_id)
        if id:
            sessions = sessions.where(cls.model.id == id)
        if user_id:
            sessions = sessions.where(cls.model.user_id == user_id)
        if keywords:
            sessions = sessions.where(peewee.fn.LOWER(cls.model.message).contains(keywords.lower()))
        if from_date:
            sessions = sessions.where(cls.model.create_date >= from_date)
        if to_date:
            sessions = sessions.where(cls.model.create_date <= to_date)
        if exp_user_id:
            sessions = sessions.where(cls.model.exp_user_id == exp_user_id)
        if desc:
            sessions = sessions.order_by(cls.model.getter_by(orderby).desc())
        else:
            sessions = sessions.order_by(cls.model.getter_by(orderby).asc())
        count = sessions.count()
        sessions = sessions.paginate(page_number, items_per_page)

        return count, list(sessions.dicts())

    @classmethod
    @DB.connection_context()
    def get_names(cls, dialog_id, exp_user_id):
        """会话名称列表(按创建时间倒序)。"""
        fields = [cls.model.id, cls.model.name, ]
        sessions = cls.model.select(*fields).where(
            cls.model.dialog_id == dialog_id,
            cls.model.exp_user_id == exp_user_id
        ).order_by(cls.model.getter_by("create_date").desc())

        return list(sessions.dicts())

    @classmethod
    @DB.connection_context()
    def append_message(cls, id, conversation):
        """追加一条会话消息并让 round 计数 +1。"""
        cls.update_by_id(id, conversation)
        return cls.model.update(round=cls.model.round + 1).where(cls.model.id == id).execute()

    @classmethod
    @DB.connection_context()
    def stats(cls, tenant_id, from_date, to_date, source=None):
        """会话统计: 按天聚合 PV/UV/tokens/时长/轮次/点赞。"""
        if len(to_date) == 10:
            to_date += " 23:59:59"
        return cls.model.select(
            cls.model.create_date.truncate("day").alias("dt"),
            peewee.fn.COUNT(
                cls.model.id).alias("pv"),
            peewee.fn.COUNT(
                cls.model.user_id.distinct()).alias("uv"),
            peewee.fn.SUM(
                cls.model.tokens).alias("tokens"),
            peewee.fn.SUM(
                cls.model.duration).alias("duration"),
            peewee.fn.AVG(
                cls.model.round).alias("round"),
            peewee.fn.SUM(
                cls.model.thumb_up).alias("thumb_up")
        ).join(Dialog, on=((cls.model.dialog_id == Dialog.id) & (Dialog.tenant_id == tenant_id))).where(
            cls.model.create_date >= from_date,
            cls.model.create_date <= to_date,
            cls.model.source == source
        ).group_by(cls.model.create_date.truncate("day")).dicts()

    @classmethod
    @DB.connection_context()
    def delete_by_dialog_ids(cls, dialog_ids):
        """按对话 id 列表批量删除会话。"""
        return cls.model.delete().where(cls.model.dialog_id.in_(dialog_ids)).execute()