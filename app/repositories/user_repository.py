from typing import Any

from sqlalchemy import func, or_, select

from app.core.database import SessionLocal
from app.models.tables import SystemUser


def user_to_dict(user: SystemUser) -> dict[str, Any]:
    """
    转换为前端 ResourceTable 所需字段。

    ResourceTable 固定读取：
    id、name、category、version、status、description。
    """
    return {
        "id": user.id,

        # 前端“名称”列
        "name": user.display_name,

        # 前端“类别”列
        "category": user.role,

        # 当前通用表格的“版本”列先显示用户创建日期
        "version": (
            user.created_at.strftime("%Y-%m-%d")
            if user.created_at
            else "—"
        ),

        # ResourceTable 只有 pending 和 normal 两种展示状态
        "status": (
            "normal"
            if user.status == "active"
            else "pending"
        ),

        # 点击“查看”时，通用详情弹窗会展示此字段
        "description": (
            f"账号：{user.username}；"
            f"邮箱：{user.email}；"
            f"角色：{user.role}；"
            f"{user.description or ''}"
        ),

        # 保留这些字段，后续前端升级时可直接使用
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "role": user.role,
        "created_at": (
            user.created_at.isoformat()
            if user.created_at
            else None
        ),
        "last_login_at": (
            user.last_login_at.isoformat()
            if user.last_login_at
            else None
        ),
    }

def list_users(
    page: int = 1,
    page_size: int = 20,
    keyword: str | None = None,
) -> dict[str, Any]:
    """
    从 system_users 表分页查询用户。
    """
    with SessionLocal() as db:
        conditions = []

        if keyword:
            keyword_like = f"%{keyword.strip()}%"
            conditions.append(
                or_(
                    SystemUser.username.like(keyword_like),
                    SystemUser.display_name.like(keyword_like),
                    SystemUser.email.like(keyword_like),
                    SystemUser.role.like(keyword_like),
                )
            )

        base_query = select(SystemUser)

        if conditions:
            base_query = base_query.where(*conditions)

        total = db.scalar(
            select(func.count())
            .select_from(SystemUser)
            .where(*conditions)
        ) or 0

        users = db.scalars(
            base_query
            .order_by(SystemUser.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()

        return {
            "items": [user_to_dict(user) for user in users],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (
                (total + page_size - 1) // page_size
                if total
                else 0
            ),
        }