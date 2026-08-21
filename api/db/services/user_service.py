import hashlib
import logging
from datetime import datetime

import peewee
from werkzeug.security import check_password_hash, generate_password_hash

from api.db import UserTenantRole
from api.db.db_models import DB, Tenant, User, UserTenant
from api.db.services.common_service import CommonService
from common import settings
from common.constants import StatusEnum
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp, datetime_format


class UserService(CommonService):
    """用户业务服务:继承 CommonService 基座, 补用户特有的鉴权/密码/软删除逻辑。

    学习要点:
    - query 做了 access_token 防御性校验(对齐官方 api/apps/__init__.py 的鉴权入口校验)
    - save/update 统一走 generate_password_hash 存密码, 明文密码不进数据库
    - 删除是软删除(status=0), 不是物理删除
    """

    model = User

    @classmethod
    @DB.connection_context()
    def query(cls, cols=None, reverse=None, order_by=None, **kwargs):
        """扩展基座 query: 拦截非法 access_token。

        传了 access_token 时先做防御性校验, 非法 token 返回"查不到"的空结果
        而不是抛错或直连库查询(避免把恶意/畸形 token 带进 SQL)。
        注意: 这是防御性校验, query 语义被改了(非法 token 返回空而非异常),
        调用方不应只依赖它做鉴权。
        """
        if "access_token" in kwargs:
            access_token = kwargs["access_token"]
            if not access_token or not str(access_token).strip():
                logging.warning("UserService.query: Rejecting empty access_token query")
                return cls.model.select().where(cls.model.id == "INVALID_EMPTY_TOKEN")

            if len(str(access_token).strip()) < 32:
                logging.warning(f"UserService.query: Rejecting short access_token query: {len(str(access_token))} chars")
                return cls.model.select().where(cls.model.id == "INVALID_SHORT_TOKEN")

            if str(access_token).startswith("INVALID_"):
                logging.warning("UserService.query: Rejecting invalidated access_token")
                return cls.model.select().where(cls.model.id == "INVALID_LOGOUT_TOKEN")
        return super().query(cols=cols, reverse=reverse, order_by=order_by, **kwargs)

    @classmethod
    @DB.connection_context()
    def filter_by_id(cls, user_id):
        """按主键取单个用户, 不存在返回 None(而不是抛 DoesNotExist)。"""
        try:
            return cls.model.select().where(cls.model.id == user_id).get()
        except peewee.DoesNotExist:
            return None

    @classmethod
    @DB.connection_context()
    def query_user(cls, email, password):
        """邮箱 + 明文密码登录校验: 查有效用户并比对密码哈希, 成功返回 User, 失败返回 None。"""
        user = cls.model.select().where((cls.model.email == email), (cls.model.status == StatusEnum.VALID.value)).first()
        if user and check_password_hash(str(user.password), password):
            return user
        return None

    @classmethod
    @DB.connection_context()
    def query_user_by_email(cls, email):
        """按邮箱查用户(可能多个, 返回列表, 不做状态过滤)。"""
        return list(cls.model.select().where(cls.model.email == email))

    @classmethod
    @DB.connection_context()
    def save(cls, **kwargs):
        """创建用户: 自动补 id 和时间戳, 密码必须哈希后落库。"""
        if "id" not in kwargs:
            kwargs["id"] = get_uuid()
        if "password" in kwargs:
            kwargs["password"] = generate_password_hash(str(kwargs["password"]))

        current_ts = current_timestamp()
        current_date = datetime_format(datetime.now())

        kwargs["create_time"] = current_ts
        kwargs["create_date"] = current_date
        kwargs["update_time"] = current_ts
        kwargs["update_date"] = current_date
        return cls.model(**kwargs).save(force_insert=True)

    @classmethod
    @DB.connection_context()
    def delete_user(cls, user_ids):
        """软删除: 只把 status 置为 INVALID, 保留数据行(官方同款, 后面查重/审计还能用)。"""
        with DB.atomic():
            cls.model.update({"status": StatusEnum.INVALID.value}).where(cls.model.id.in_(user_ids)).execute()

    @classmethod
    @DB.connection_context()
    def update_user(cls, user_id, user_dict):
        """更新用户资料字段。改密码请走 update_user_password(会哈希)。"""
        with DB.atomic():
            if user_dict:
                user_dict["update_time"] = current_timestamp()
                user_dict["update_date"] = datetime_format(datetime.now())
                cls.model.update(user_dict).where(cls.model.id == user_id).execute()

    @classmethod
    @DB.connection_context()
    def update_user_password(cls, user_id, new_password):
        """改密码: 哈希后写库, 与 save 保持同一存储格式。"""
        with DB.atomic():
            update_dict = {
                "password": generate_password_hash(str(new_password)),
                "update_time": current_timestamp(),
                "update_date": datetime_format(datetime.now()),
            }
            cls.model.update(update_dict).where(cls.model.id == user_id).execute()

    @classmethod
    @DB.connection_context()
    def is_admin(cls, user_id):
        """判断是否超级管理员。"""
        return cls.model.select().where(cls.model.id == user_id, cls.model.is_superuser).count() > 0

    @classmethod
    @DB.connection_context()
    def get_all_users(cls):
        """按邮箱排序返回全部用户。"""
        return list(cls.model.select().order_by(cls.model.email))


class TenantService(CommonService):
    """租户业务服务: 官方 user_service.py 里的第二、三个类(用户-租户体系)。

    学习要点:
    - RAGFlow 的租户模型: 用户注册即自带一个租户(tenant.id == user.id),
      还可被邀请加入别人的租户(role=NORMAL)
    - get_info_by 查"自己创建的租户", get_joined_tenants_by_user_id 查"别人邀请加入的租户"
    """

    model = Tenant

    @classmethod
    @DB.connection_context()
    def get_info_by(cls, user_id):
        """取 user 以 OWNER 身份拥有的租户信息(自己创建的)。"""
        fields = [
            cls.model.id.alias("tenant_id"),
            cls.model.name,
            cls.model.llm_id,
            cls.model.embd_id,
            cls.model.rerank_id,
            cls.model.asr_id,
            cls.model.img2txt_id,
            cls.model.tts_id,
            cls.model.parser_ids,
            UserTenant.role,
        ]
        return list(
            cls.model.select(*fields)
            .join(UserTenant, on=((cls.model.id == UserTenant.tenant_id) & (UserTenant.user_id == user_id) & (UserTenant.status == StatusEnum.VALID.value) & (UserTenant.role == UserTenantRole.OWNER)))
            .where(cls.model.status == StatusEnum.VALID.value)
            .dicts()
        )

    @classmethod
    @DB.connection_context()
    def get_joined_tenants_by_user_id(cls, user_id):
        """取 user 以 NORMAL 身份加入的租户列表(被邀请加入的)。"""
        fields = [cls.model.id.alias("tenant_id"), cls.model.name, cls.model.llm_id, cls.model.embd_id, cls.model.asr_id, cls.model.img2txt_id, UserTenant.role]
        return list(
            cls.model.select(*fields)
            .join(
                UserTenant, on=((cls.model.id == UserTenant.tenant_id) & (UserTenant.user_id == user_id) & (UserTenant.status == StatusEnum.VALID.value) & (UserTenant.role == UserTenantRole.NORMAL))
            )
            .where(cls.model.status == StatusEnum.VALID.value)
            .dicts()
        )

    @classmethod
    @DB.connection_context()
    def decrease(cls, user_id, num):
        """扣减租户 credit 余额, 减不到对应行(租户不存在)时报 LookupError。"""
        num = cls.model.update(credit=cls.model.credit - num).where(cls.model.id == user_id).execute()
        if num == 0:
            raise LookupError("Tenant not found which is supposed to be there")

    @classmethod
    @DB.connection_context()
    def user_gateway(cls, tenant_id):
        """按租户 id 哈希路由到某台 MinIO(多实例对象存储路由, 官方原封)。

        ⚠️ 依赖 settings.MINIO 非空: 学习版 common/settings.py 目前 MINIO={},
        调用会 ZeroDivisionError。等 conf 配置加载(service_conf.yaml 的 minio 段)
        接入 settings 后再启用。
        """
        hash_obj = hashlib.sha256(tenant_id.encode("utf-8"))
        return int(hash_obj.hexdigest(), 16) % len(settings.MINIO)


class UserTenantService(CommonService):
    """用户-租户关系服务: 维护 user↔tenant 多对多关系与角色(owner/admin/normal/invite)。"""

    model = UserTenant

    @classmethod
    @DB.connection_context()
    def filter_by_id(cls, user_tenant_id):
        """按关系行主键取记录, 只认有效状态, 不存在返回 None。"""
        try:
            return cls.model.select().where((cls.model.id == user_tenant_id) & (cls.model.status == StatusEnum.VALID.value)).get()
        except peewee.DoesNotExist:
            return None

    @classmethod
    @DB.connection_context()
    def save(cls, **kwargs):
        """新增关系行: 自动补 id。"""
        if "id" not in kwargs:
            kwargs["id"] = get_uuid()
        return cls.model(**kwargs).save(force_insert=True)

    @classmethod
    @DB.connection_context()
    def get_by_tenant_id(cls, tenant_id):
        """租户成员列表(排除 OWNER, 即受邀成员), 联表带出用户资料。"""
        fields = [
            cls.model.id,
            cls.model.user_id,
            cls.model.status,
            cls.model.role,
            User.nickname,
            User.email,
            User.avatar,
            User.is_authenticated,
            User.is_active,
            User.is_anonymous,
            User.status,
            User.update_date,
            User.is_superuser,
        ]
        return list(
            cls.model.select(*fields)
            .join(User, on=((cls.model.user_id == User.id) & (cls.model.status == StatusEnum.VALID.value) & (cls.model.role != UserTenantRole.OWNER)))
            .where(cls.model.tenant_id == tenant_id)
            .dicts()
        )

    @classmethod
    @DB.connection_context()
    def get_tenants_by_user_id(cls, user_id):
        """用户加入的全部租户 + 本人资料。

        ⚠️ 官方 join 条件是 tenant_id == User.id, 疑似上游笔误(语义上应是
        user_id == User.id), 这里保持与官方一致便于 diff 对照, 实际使用注意。
        """
        fields = [cls.model.tenant_id, cls.model.role, User.nickname, User.email, User.avatar, User.update_date]
        return list(
            cls.model.select(*fields)
            .join(User, on=((cls.model.tenant_id == User.id) & (UserTenant.user_id == user_id) & (UserTenant.status == StatusEnum.VALID.value)))
            .where(cls.model.status == StatusEnum.VALID.value)
            .dicts()
        )

    @classmethod
    @DB.connection_context()
    def get_user_tenant_relation_by_user_id(cls, user_id):
        """取某用户全部租户关系行(不做角色/状态过滤, 官方原封, 双 dicts() 无害)。"""
        fields = [cls.model.id, cls.model.user_id, cls.model.tenant_id, cls.model.role]
        return list(cls.model.select(*fields).where(cls.model.user_id == user_id).dicts().dicts())

    @classmethod
    @DB.connection_context()
    def get_num_members(cls, user_id: str):
        """某租户的成员数。参数名沿官方叫 user_id, 实际查询的是 tenant_id
        (RAGFlow 里用户自建租户时 tenant.id == user.id, 所以传租户 id 即可)。"""
        return cls.model.select(peewee.fn.COUNT(cls.model.id)).where(cls.model.tenant_id == user_id).scalar()

    @classmethod
    @DB.connection_context()
    def filter_by_tenant_and_user_id(cls, tenant_id, user_id):
        """按 tenant+user 查有效关系行(判断某用户是否在某租户内)。"""
        try:
            return cls.model.select().where((cls.model.tenant_id == tenant_id) & (cls.model.status == StatusEnum.VALID.value) & (cls.model.user_id == user_id)).first()
        except peewee.DoesNotExist:
            return None
