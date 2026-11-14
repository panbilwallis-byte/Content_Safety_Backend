from copy import deepcopy
from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from sqlalchemy import select, update
from app.core.database import get_db
from app.core.response import success
from app.models.internal_agent import InternalAgentRun
from app.routers.agent_traces import actor, EnvelopeRoute
from app.services import internal_agent as service

router = APIRouter(prefix="/internal-agent", route_class=EnvelopeRoute)


@router.get("/batches")
def batches(q: str = Query("", max_length=100), page: int = Query(1, ge=1),
            page_size: int = Query(20, ge=1, le=50), user=Depends(actor), db=Depends(get_db)):
    from app.services.internal_agent_batches import options as batch_options
    return success(batch_options(db, user, q, page, page_size))


@router.get("/options")
def options(request: Request, user=Depends(actor), db=Depends(get_db)):
    available, error = [], ""
    try:
        available = service.models(request.headers.get("X-LLM-Authorization"))
    except Exception as exc:
        error = service.safe_error(exc)
    return success({"datasets": service.datasets(db), "models": available, "model_error": error,
        "tools": list(service.TOOL_NAMES), "can_create": user.get("tier") != "reader",
        "modes": [{"code": code, **value} for code, value in service.MODES.items()],
        "limits": {"sample_limit": 10, "dataset_records": 100, "sample_characters": 1500,
                   "max_rounds": 8, "max_calls": 12, "timeout_seconds": 300}})


@router.get("/runs")
def runs(user=Depends(actor), db=Depends(get_db)):
    rows = db.scalars(select(InternalAgentRun).where(InternalAgentRun.owner_id == int(user["uid"])).order_by(InternalAgentRun.created_at.desc()).limit(50))
    return success({"items": [service.public(r) for r in rows], "limit": 50})


@router.post("/runs", status_code=202)
def create(body: service.CreateRun, request: Request, background: BackgroundTasks, user=Depends(actor), db=Depends(get_db)):
    token = request.headers.get("X-LLM-Authorization")
    row, created = service.create(db, body, user, request.headers.get("X-Request-Id"), token)
    if created:
        background.add_task(service.execute, row.run_id, token=token)
    return success(service.public(row))


@router.get("/runs/{run_id}")
def detail(run_id: str, user=Depends(actor), db=Depends(get_db)):
    return success(service.public(service.get_run(db, run_id, user), True))


@router.post("/runs/{run_id}/stop")
def stop(run_id: str, user=Depends(actor), db=Depends(get_db)):
    row = service.get_run(db, run_id, user)
    if user.get("tier") == "reader":
        service.fail(403, "当前账号不能停止任务")
    if row.status in service.ACTIVE:
        db.execute(update(InternalAgentRun).where(InternalAgentRun.run_id == run_id).values(stop_requested=True))
        db.commit()
        db.refresh(row)
    return success(service.public(row))


@router.post("/runs/{run_id}/trace")
def trace(run_id: str, user=Depends(actor), db=Depends(get_db)):
    row = service.get_run(db, run_id, user)
    if user.get("tier") == "reader":
        service.fail(403, "当前账号不能发布轨迹")
    if row.status in service.ACTIVE:
        service.fail(409, "任务结束后才能发布固定审计快照")
    p = deepcopy(row.payload)
    p["trace"] = service.publish_trace(db, row, p)
    p.pop("trace_error", None)
    service.save(db, row, p)
    return success(service.public(row, True))
