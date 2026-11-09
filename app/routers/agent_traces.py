"""Agent trace ingestion, statistics, execution and tools contracts."""
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import select

from app.core.database import get_db
from app.core.response import success, fail
from app.models.tables import SystemUser
from app.services import agent_trace_query as service
from app.services import agent_trace_export as exports
from app.services import agent_trace_statistics as statistics


class EnvelopeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def wrapped(request):
            try:
                return await handler(request)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content=fail(str(exc.detail), code=exc.status_code))
            except RequestValidationError:
                return JSONResponse(status_code=422, content=fail("请求参数类型或范围无效", code=422))
        return wrapped


router = APIRouter(route_class=EnvelopeRoute)
root = "/compliance/agent-traces"


def actor(request: Request, db=Depends(get_db)):
    user = getattr(request.state, "user", None)
    if not user:
        service.fail(401, "请先登录")
    stored = db.get(SystemUser, int(user["uid"]))
    if not stored or stored.status not in {"active", "normal"}:
        service.fail(403, "当前用户不存在或已停用")
    from app.core.auth import permission_tier
    return {**user, "role": stored.role, "tier": permission_tier(stored.role)}


def view_param(view):
    if view not in {"tools", "execution", "ingest", "statistics"}:
        service.fail(422, "不支持的轨迹视图")
    return view


@router.get(root + "/workspace")
def workspace(request: Request, view: str = Query("ingest"), user=Depends(actor), db=Depends(get_db)):
    if view == "ingest":
        from app.services import agent_trace_ingest as ingest
        return success(ingest.workspace(db, dict(request.query_params), user))
    return success(service.workspace(db, dict(request.query_params), view_param(view), user))


@router.get(root + "/statistics")
def statistics_workspace(request: Request, user=Depends(actor), db=Depends(get_db)):
    return success(statistics.workspace(db, dict(request.query_params), user))


@router.post(root + "/batches")
def create_batch(request: Request, body: dict = Body(...), user=Depends(actor), db=Depends(get_db)):
    from app.services import agent_trace_ingest as ingest
    return success(ingest.create_batch(db, user, body.get("name"), request.headers.get("X-Request-Id")))


@router.post(root + "/batches/{batch_id}/files")
def bind_files(batch_id: str, request: Request, body: dict = Body(...), user=Depends(actor), db=Depends(get_db)):
    from app.services import agent_trace_ingest as ingest
    return success(ingest.bind(db, user, batch_id, body, request.headers.get("X-Request-Id")))


@router.get(root + "/batches/{batch_id}/samples")
def samples(batch_id: str, request: Request, view: str = Query("ingest"), user=Depends(actor), db=Depends(get_db)):
    view_param(view)
    params = dict(request.query_params)
    if view == "statistics":
        params.setdefault("projection_version", statistics.VERSION)
    _, scope = service.resolve(db, batch_id, params)
    if view == "statistics":
        return success(statistics.samples(db, scope, params, user))
    if view == "ingest":
        from app.services import agent_trace_ingest as ingest
        return success(ingest.samples(db, scope, params))
    return success(service.samples(db, scope, dict(request.query_params), view))


@router.get(root + "/batches/{batch_id}/samples/{trace_id}")
def detail(batch_id: str, trace_id: str, request: Request, view: str = Query("ingest"), user=Depends(actor), db=Depends(get_db)):
    view_param(view)
    params = dict(request.query_params)
    if view == "statistics":
        params.setdefault("projection_version", statistics.VERSION)
    batch, scope = service.resolve(db, batch_id, params)
    if view == "statistics":
        return success(statistics.detail(db, scope, trace_id, params, user))
    if view == "ingest":
        from app.services import agent_trace_ingest as ingest
        return success(ingest.detail(db, scope, trace_id, params, user))
    data = service.tool_detail(db, batch, scope, trace_id, params, user) if view == "tools" else service.execution_detail(db, scope, trace_id, params, user)
    return success(data)


@router.post(root + "/artifacts/{artifact_id}/download-tickets")
def ticket(artifact_id: str, request: Request, body: dict = Body(...), user=Depends(actor), db=Depends(get_db)):
    if body:
        service.fail(422, "下载票据请求体必须为空对象")
    return success(exports.ticket(db, artifact_id, user, request.headers.get("X-Request-Id")))


@router.get(root + "/downloads")
def download(t: str = Query(..., max_length=3000), db=Depends(get_db)):
    artifact, path = exports.download(db, t)
    return FileResponse(path, media_type=artifact.mime_type, filename=artifact.filename,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
