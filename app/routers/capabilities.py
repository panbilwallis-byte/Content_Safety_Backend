from fastapi import APIRouter

from app.core.response import success
from app.domain.capabilities import CAPABILITIES


# 创建能力目录路由对象
router = APIRouter()


# 查询当前平台支持的全部能力
@router.get("/capabilities")
def get_capabilities():
    """
    对应：
    GET /api/v1/capabilities
    """

    return success(
        data=CAPABILITIES,
        message="能力目录查询成功",
    )