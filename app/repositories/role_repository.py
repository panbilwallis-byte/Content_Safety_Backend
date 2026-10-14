from typing import Any

from sqlalchemy import func, or_, select

from app.core.database import SessionLocal
from app.models.tables import SystemRole


def role_to_dict(role: SystemRole) -> dict[str, Any]:
    """
    转换为现有前端 ResourceTable 能直接展示的结构。
    """
    permissions = list(role.permissions or [])

    return {
        "id": role.id,

        # ResourceTable 的“名称”列
        "name": role.name,

        # ResourceTable 的“类别”列
        "category": f"{len(permissions)} 项权限",

        # ResourceTable 的“版本”列，显示角色编码
        "version": role.role_code,

        # ResourceTable 中 pending 显示待处理，其余显示正常
        "status": (
            "normal"
            if role.status == "active"
            else "pending"
        ),

        # 点击“查看”时会展示此字段
        "description": (
            f"{role.description}\n"
            f"权限清单：{'、'.join(permissions) if permissions else '暂无权限'}"
        ),

        # 额外保留给后续专用角色页面使用
        "role_code": role.role_code,
        "permissions": permissions,
        "permission_count": len(permissions),
        "created_at": (
            role.created_at.isoformat()
            if role.created_at
            else None
        ),
    }


def list_roles(
    page: int = 1,
    page_size: int = 20,
    keyword: str | None = None,
) -> dict[str, Any]:
    """
    从 system_roles 表分页读取角色和权限信息。
    """
    with SessionLocal() as db:
        conditions = []

        if keyword:
            keyword_like = f"%{keyword.strip()}%"
            conditions.append(
                or_(
                    SystemRole.name.like(keyword_like),
                    SystemRole.role_code.like(keyword_like),
                    SystemRole.description.like(keyword_like),
                )
            )

        query = select(SystemRole)

        if conditions:
            query = query.where(*conditions)

        total = db.scalar(
            select(func.count())
            .select_from(SystemRole)
            .where(*conditions)
        ) or 0

        roles = db.scalars(
            query
            .order_by(SystemRole.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()

        return {
            "items": [
                role_to_dict(role)
                for role in roles
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (
                (total + page_size - 1) // page_size
                if total
                else 0
            ),
        }