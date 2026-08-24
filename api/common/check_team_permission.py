"""
api.common.check_team_permission — 团队级权限校验。

知识库/文件归属租户 == 当前用户直接放行; 否则要求知识库 permission=TEAM
(允许团队成员访问)且该用户已加入对应租户。kb_app/document_app 路由共用。
来源: 官方 api/common/check_team_permission.py @ v0.24.0
"""
from api.db import TenantPermission
from api.db.db_models import File, Knowledgebase
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import TenantService


def check_kb_team_permission(kb: dict | Knowledgebase, other: str) -> bool:
    """判断 other 用户能否访问知识库 kb: 自己的库直接放行, 否则走 TEAM 权限。"""
    kb = kb.to_dict() if isinstance(kb, Knowledgebase) else kb

    kb_tenant_id = kb["tenant_id"]

    if kb_tenant_id == other:
        return True

    if kb["permission"] != TenantPermission.TEAM:
        return False

    joined_tenants = TenantService.get_joined_tenants_by_user_id(other)
    return any(tenant["tenant_id"] == kb_tenant_id for tenant in joined_tenants)


def check_file_team_permission(file: dict | File, other: str) -> bool:
    """判断 other 用户能否访问 file: 文件关联的任意知识库通过团队校验即放行。"""
    file = file.to_dict() if isinstance(file, File) else file

    file_tenant_id = file["tenant_id"]
    if file_tenant_id == other:
        return True

    file_id = file["id"]

    kb_ids = [kb_info["kb_id"] for kb_info in FileService.get_kb_id_by_file_id(file_id)]

    for kb_id in kb_ids:
        ok, kb = KnowledgebaseService.get_by_id(kb_id)
        if not ok:
            continue

        if check_kb_team_permission(kb, other):
            return True

    return False