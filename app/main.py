from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI
from app.core.response import success
from app.core.database import (
    Base,
    close_database_connection,
    engine,
    test_database_connection,
)

from app.models import tables
from app.routers import (
    tasks,
    audit,
    resources,
    capabilities,
    evaluations,
    kpis,
    data_resources,
    data_governance,
    model_workbench,
    files,
    training_tasks,
    compliance,
)

from app.core.seed import (
    ensure_dataset_source_type_column,
    ensure_resource_display_values,
    ensure_ingest_demo_tasks,
    ensure_process_demo_tasks,
    ensure_model_evaluation_versions,
    ensure_evaluation_metrics,
    ensure_evaluation_tasks,
    normalize_evaluation_display_values,
    ensure_model_columns,
    ensure_training_task_columns,
    ensure_evaluation_metric_columns,
    ensure_dataset_record_columns,
    ensure_task_columns,
    migrate_legacy_resources,
    seed_initial_data,
)
from app.repositories.evaluation_repository import ensure_demo_evaluations
from app.services.compliance_service import seed_compliance_data

from fastapi.responses import JSONResponse

from app.core.response import (
    set_trace_id,
    reset_trace_id,
    fail,
    get_trace_id,
)


from uuid import uuid4
from fastapi import Request

from app.core.response import (
    set_trace_id,
    reset_trace_id,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 测试 MySQL 连接
    test_database_connection()

    # 根据 tables.py 中的模型创建表
    Base.metadata.create_all(bind=engine)

    ensure_dataset_source_type_column()
    ensure_task_columns()
    ensure_dataset_record_columns()
    ensure_model_columns()
    ensure_training_task_columns()
    # 迁移旧的统一资源表，并确保独立资源表有初始数据
    migrate_legacy_resources()
    seed_initial_data()
    ensure_evaluation_metric_columns()
    ensure_evaluation_metrics()
    ensure_evaluation_tasks()
    normalize_evaluation_display_values()
    # 初始模型写入后再执行一次来源/版本迁移，确保首启即得到数据库分布。
    ensure_model_columns()
    ensure_training_task_columns()
    ensure_resource_display_values()
    ensure_ingest_demo_tasks()
    ensure_process_demo_tasks()
    ensure_model_evaluation_versions()
    ensure_demo_evaluations()
    seed_compliance_data()

    print("MySQL 数据表初始化完成")
    yield

    close_database_connection()
    print("后端服务已关闭")







app = FastAPI(
    title="内容安全治理平台",
    description="后端模拟接口学习项目",
    version="0.1.0",
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



@app.middleware("http")
async def trace_id_middleware(request: Request, call_next):
    trace_id = (
        request.headers.get("X-Trace-Id")
        or str(uuid4())
    )

    token = set_trace_id(trace_id)

    try:
        response = await call_next(request)

        response.headers["X-Trace-Id"] = trace_id

        return response

    finally:
        reset_trace_id(token)

@app.exception_handler(ValueError)
async def value_error_handler(
    request: Request,
    exc: ValueError,
):
    return JSONResponse(
        status_code=200,
        content=fail(
            message=str(exc),
            code=400,
            trace_id=get_trace_id(),
        ),
    )



@app.exception_handler(Exception)
async def general_exception_handler(
    request: Request,
    exc: Exception,
):
    return JSONResponse(
        status_code=500,
        content=fail(
            message="服务器内部错误",
            code=500,
            trace_id=get_trace_id(),
        ),
    )



app.include_router(
    kpis.router,
    prefix="/api/v1",
    tags=["首页指标"],
)

app.include_router(
    data_resources.router,
    prefix="/api/v1",
    tags=["数据资源汇总"],
)
app.include_router(
    data_governance.router,
    prefix="/api/v1",
    tags=["数据治理"],
)
app.include_router(
    model_workbench.router,
    prefix="/api/v1",
    tags=["模型工作台"],
)

app.include_router(
    files.router,
    prefix="/api/v1",
    tags=["文件管理"],
)

app.include_router(
    tasks.router,
    prefix="/api/v1",
    tags=["任务管理"],
)

app.include_router(
    evaluations.router,
    prefix="/api/v1",
    tags=["测试评估"],
)

app.include_router(
    training_tasks.router,
    prefix="/api/v1",
    tags=["模型训练"],
)

app.include_router(
    compliance.router,
    prefix="/api/v1",
    tags=["全链路合规"],
)

app.include_router(
    audit.router,
    prefix="/api/v1",
    tags=["审计日志"],
)

app.include_router(
    resources.router,
    prefix="/api/v1",
    tags=["数据资源"],
)
app.include_router(
    capabilities.router,
    prefix="/api/v1",
    tags=["能力目录"],
)


@app.get("/health")
def health():
    return success(
        data={
            "status": "ok",
            "service": "content-safety-platform",
        },
        message="后端服务已启动",
    )


@app.get("/")
def root():
    return success(
        data={
            "docs": "/docs",
        },
        message="欢迎使用内容安全治理平台后端",
    )


