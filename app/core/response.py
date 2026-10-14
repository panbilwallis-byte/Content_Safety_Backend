# from datetime import datetime


# def success(
#     data,
#     message="请求成功",
#     trace_id: str | None = None,
# ):
#     return {
#         "code": 0,
#         "message": message,
#         "data": data,
#         "trace_id": trace_id,
#         "timestamp": datetime.now().isoformat(),
#     }


# def fail(
#     message: str,
#     code: int = 400,
#     trace_id: str | None = None,
# ):
#     return {
#         "code": code,
#         "message": message,
#         "data": None,
#         "trace_id": trace_id,
#         "timestamp": datetime.now().isoformat(),
#     }


# def fail(message: str, code: int = 400):
#     """
#     所有失败接口统一使用这个函数返回数据。

#     code 常用值：
#     400：前端传入的数据或业务参数不正确
#     404：请求的资源不存在
#     500：后端程序内部出错
#     """
#     return {
#         "code": code,
#         "message": message,
#         "data": None,
#         "timestamp": datetime.now().isoformat(),
#     }


from contextvars import ContextVar
from app.core.time import now_shanghai
from uuid import uuid4


_trace_id_context: ContextVar[str | None] = ContextVar(
    "trace_id",
    default=None,
)


def set_trace_id(trace_id: str):
    return _trace_id_context.set(trace_id)


def reset_trace_id(token):
    _trace_id_context.reset(token)


def get_trace_id() -> str:
    return _trace_id_context.get() or str(uuid4())


def success(
    data,
    message="请求成功",
    trace_id: str | None = None,
):
    return {
        "code": 0,
        "message": message,
        "data": data,
        "trace_id": trace_id or _trace_id_context.get(),
        "timestamp": now_shanghai().isoformat(),
    }


def fail(
    message: str,
    code: int = 400,
    trace_id: str | None = None,
):
    return {
        "code": code,
        "message": message,
        "data": None,
        "trace_id": trace_id or _trace_id_context.get(),
        "timestamp": now_shanghai().isoformat(),
    }
