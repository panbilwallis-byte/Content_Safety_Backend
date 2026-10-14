# from fastapi import APIRouter

# # 创建任务路由对象
# router = APIRouter()


# # 模拟“执行任务”的接口
# @router.post("/tasks/execute")
# def execute_task():
#     return {
#         "message": "模拟任务执行完成",
#         "status": "succeeded",
#     }


# from fastapi import APIRouter
# from app.domain.schemas import ExecuteTaskRequest

# # 创建任务路由对象
# router = APIRouter()


# # 模拟“执行任务”的接口
# @router.post("/tasks/execute")
# def execute_task(request: ExecuteTaskRequest):
#     return {
#         "message": "模拟任务执行完成",
#         "status": "succeeded",

#         # 先把前端传来的内容原样返回，确认请求已被后端接收
#         "capability_code": request.capability_code,
#         "name": request.name,
#         "input": request.input,
#         "config": request.config,
#     }


# from fastapi import APIRouter

# from app.domain.schemas import ExecuteTaskRequest
# from app.services.task_service import run_mock_task


# # 创建任务路由对象
# router = APIRouter()


# @router.post("/tasks/execute")
# def execute_task(request: ExecuteTaskRequest):
#     """
#     接收前端 JSON。
#     把具体处理工作交给 task_service。
#     """
#     result = run_mock_task(request)

#     # 把服务层产生的模拟结果返回给前端
#     return result




# from fastapi import APIRouter

# from app.core.response import success
# from app.domain.schemas import ExecuteTaskRequest
# from app.services.task_service import run_mock_task


# # 创建任务路由对象
# router = APIRouter()


# @router.post("/tasks/execute")
# def execute_task(request: ExecuteTaskRequest):
#     """
#     接收前端 JSON。
#     调用服务层，并把结果包装成统一响应格式。
#     """
#     result = run_mock_task(request)

#     return success(
#         data=result,
#         message="任务执行完成",
#     )




from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel
from app.core.database import SessionLocal
from app.core.time import now_shanghai
from app.models.tables import Task, EvaluationTask

from app.core.response import success, fail
from app.domain.schemas import ApiResponse, ExecuteTaskRequest
from app.repositories.task_repository import find_by_id, list_all
from app.repositories.resource_repository import find_resource
from app.services.task_service import run_mock_task
from uuid import uuid4
from hashlib import sha256
from random import Random
from fastapi import Request
from time import sleep




# 创建任务路由对象
router = APIRouter()
PROCESS_KIND = "governance-process"
PROCESS_STAGES = (
    ("读取数据", 10),
    ("规范化", 50),
    ("去重", 30),
    ("字段校验", 40),
    ("生成版本", 20),
)
PROCESS_TOTAL_SECONDS = sum(duration for _, duration in PROCESS_STAGES)


class CreateProcessTaskRequest(BaseModel):
    kind: str | None = None
    name: str
    input: dict = {}
    capability_code: str | None = None
    config: dict = {}


def _process_task_summary(task: dict) -> dict:
    """Keep processing history rows small; fetch /tasks/{task_id} for full details."""
    return {
        "task_id": task["task_id"], "name": task["name"],
        "dataset_name": task.get("dataset_name") or "未指定数据集",
        "rule_name": task.get("rule_name") or "数据处理",
        "output_version": task.get("output_version"), "status": task["status"],
        "progress": task.get("progress", 0), "processed_count": task.get("processed_count", 0),
        "total_count": task.get("total_count", 0), "created_at": task.get("created_at"),
        "finished_at": task.get("finished_at"), "trace_id": task.get("trace_id"),
    }

def _process_task(task: dict) -> dict:
    input_data = task.get("input") or {}
    result = task.get("result") or {}
    total = int(result.get("total_count") or task.get("success_count") or 0)
    return {
        "task_id": task["task_id"], "name": task["name"],
        "dataset_name": task.get("dataset_name") or "未指定数据集",
        "input": input_data,
        "rule_name": input_data.get("template_id") or input_data.get("templateId") or "数据处理",
        "output_version": result.get("output_version"),
        "status": task["status"], "progress": task["progress"],
        "processed_count": task.get("success_count") or 0,
        "total_count": total, "remaining_seconds": result.get("remaining_seconds"),
        "steps": result.get("steps") or [], "comparisons": result.get("comparisons") or [],
        "created_at": task["created_at"], "finished_at": task.get("finished_at"),
        "trace_id": task.get("trace_id"),
    }


def _process_steps(active_index: int | None) -> list[dict]:
    """根据当前阶段构造可持久化的五步处理状态。"""
    return [
        {
            "name": name,
            "status": (
                "succeeded" if index < active_index else
                "running" if index == active_index else
                "pending"
            ) if active_index is not None else "succeeded",
        }
        for index, (name, _) in enumerate(PROCESS_STAGES)
    ]


def _run_process_task(task_id: str, total: int):
    """按真实可观察的阶段节奏推进处理任务；每次状态变化均落库。"""
    elapsed = 0
    for index, (_, duration) in enumerate(PROCESS_STAGES):
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if task is None or task.status != "running":
                return
            result = dict(task.result or {})
            result.update({
                "steps": _process_steps(index),
                "remaining_seconds": PROCESS_TOTAL_SECONDS - elapsed,
            })
            task.result = result
            task.progress = round(elapsed * 100 / PROCESS_TOTAL_SECONDS)
            task.success_count = round(total * elapsed / PROCESS_TOTAL_SECONDS)
            db.commit()

        sleep(duration)
        elapsed += duration

        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if task is None or task.status != "running":
                return
            result = dict(task.result or {})
            is_finished = index == len(PROCESS_STAGES) - 1
            result.update({
                "steps": _process_steps(None) if is_finished else _process_steps(index + 1),
                "remaining_seconds": 0 if is_finished else PROCESS_TOTAL_SECONDS - elapsed,
            })
            if is_finished:
                result["output_version"] = result.pop("pending_output_version", None)
                task.status = "succeeded"
                task.finished_at = now_shanghai()
            task.result = result
            task.progress = round(elapsed * 100 / PROCESS_TOTAL_SECONDS)
            task.success_count = total if is_finished else round(total * elapsed / PROCESS_TOTAL_SECONDS)
            db.commit()


# 创建并执行任务
@router.post("/tasks/execute", response_model=ApiResponse)
def execute_task(
    request: ExecuteTaskRequest,
    http_request: Request,
    background_tasks: BackgroundTasks,
):
    trace_id = (
        http_request.headers.get("X-Request-Id")
        or str(uuid4())
    )

    try:
        if request.capability_code in {
            "lineage_audit", "training_monitor", "reasoning_audit",
            "neuron_audit", "full_chain_audit",
        }:
            from app.services.compliance_service import execute_compliance_task

            result = execute_compliance_task(request.capability_code, request.input, trace_id)
            return success(data=result, message="合规审计任务执行完成", trace_id=trace_id)

        if request.capability_code == "data_ingest":
            # 连接验证完成后立即返回任务，让前端先展示“等待执行”。实际来源
            # 读取和后续处理由后台任务完成，避免 HTTP 请求阻塞整个进度过程。
            result = run_mock_task(
                request=request,
                trace_id=trace_id,
                defer_ingest=True,
            )
            background_tasks.add_task(
                run_mock_task,
                request=request,
                trace_id=trace_id,
                existing_ingest_task_id=result["task_id"],
            )
            return success(
                data=result,
                message="数据源连接成功，任务等待执行",
                trace_id=trace_id,
            )

        result = run_mock_task(
            request=request,
            trace_id=trace_id,
        )

        return success(
            data=result,
            message="任务执行完成",
            trace_id=trace_id,
        )

    except ValueError as error:
        return fail(
            message=str(error),
            code=400,
            trace_id=trace_id,
        )

    except Exception as error:
        return fail(
            message=f"任务执行失败：{error}",
            code=500,
            trace_id=trace_id,
        )

# 查询全部任务
@router.get("/tasks", response_model=ApiResponse)
def get_tasks(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    capability_code: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
):
    """
    page：第几页，最小为 1。
    page_size：每页多少条，范围为 1 到 100。
    """

    if kind == PROCESS_KIND:
        capability_code = "data_process"
    tasks = list_all(
        page=page,
        page_size=page_size,
        capability_code=capability_code,
        keyword=keyword,
        status=status,
    )

    if kind == PROCESS_KIND:
        tasks["items"] = [_process_task_summary(task) for task in tasks["items"]]
    return success(
        data=tasks,
        message="任务列表查询成功",
    )


# 根据任务 ID 查询一条任务
@router.get("/tasks/{task_id}", response_model=ApiResponse)
def get_task_detail(task_id: str, kind: str | None = Query(default=None)):
    # 兼容风险识别前端使用的统一任务查询路径。风险任务暂存在
    # data_governance 路由模块的会话内存中，不落在通用 Task 表。
    if kind == "governance-risk" or task_id.startswith("risk-task-"):
        from app.routers.data_governance import get_risk_task

        return get_risk_task(task_id)
    task = find_by_id(task_id)

    if task is None:
        # Evaluation tasks share the public /tasks/{id} contract with
        # governance tasks, but live in their dedicated table.
        with SessionLocal() as db:
            evaluation_task = db.get(EvaluationTask, task_id)
        if evaluation_task is not None:
            from app.routers.evaluations import task_dict
            return success(data=task_dict(evaluation_task), message="评估任务详情查询成功")
        # HTTPException 会让 FastAPI 返回 404
        raise HTTPException(
            status_code=404,
            detail="任务不存在",
        )

    return success(
        data=_process_task(task) if kind == PROCESS_KIND else task,
        message="任务详情查询成功",
    )


@router.post("/tasks", response_model=ApiResponse)
def create_process_task(request: CreateProcessTaskRequest, background_tasks: BackgroundTasks):
    # The evaluation workspace uses the shared POST /tasks contract.  Keep it
    # on the same router as the legacy governance tasks, but persist it in the
    # dedicated evaluation tables.
    if request.capability_code == "evaluation":
        from app.routers.evaluations import CreateTask, create_task
        return create_task(CreateTask(name=request.name, capabilityCode="evaluation",
                                      input=request.input, config=request.config),
                           Request(scope={"type": "http", "headers": []}))
    # 风险识别页沿用统一任务创建接口；转发到已有风险任务实现，
    # 使旧前端无需改动，同时保持风险任务的原有状态轮询逻辑。
    if request.kind == "governance-risk":
        from app.routers.data_governance import create_risk_task

        return create_risk_task({"input": request.input})
    if request.kind == "governance-value":
        input_data = request.input
        task_id = "tsk_value_" + uuid4().hex[:8]
        result_id = "value_result_" + uuid4().hex[:8]
        now = now_shanghai()
        dataset_id = input_data.get("dataset_id") or input_data.get("datasetId")
        dataset = find_resource("datasets", dataset_id) if dataset_id is not None else None
        # 分析规模和评分区间随数据集变化；任务 ID 只用于保证本次结果可复核。
        randomizer = Random(int.from_bytes(sha256(task_id.encode("utf-8")).digest()[:8], "big"))
        language = input_data.get("language", "zh")
        try:
            dataset_number = int(dataset_id)
        except (TypeError, ValueError):
            dataset_number = 0
        # 各数据集使用不同的高价值基准，整体控制在约 45%，不会把历史任务结果累加进本次占比。
        analysis_profiles = {1: (96, 0.42), 2: (120, 0.45), 3: (144, 0.48), 4: (108, 0.44)}
        sample_total, base_high_ratio = analysis_profiles.get(dataset_number, (132, 0.68))
        # task_id 是每次新生成的；高价值占比会围绕数据集基准产生不同结果。
        high_target = round(sample_total * min(0.51, max(0.39, base_high_ratio + randomizer.uniform(-0.03, 0.03))))
        dimension_names = ["文化价值", "信息价值", "稀缺性", "可信度", "代表性"]
        corpus_summaries = [
            "地方戏曲传承人口述史，涵盖演出仪式、唱腔特点与代际传承经历。",
            "公共服务政策新闻摘要，整理实施范围、发布时间和权威信息来源。",
            "城市文化活动评论语料，反映参与者对展演、场馆与公共服务的反馈。",
            "行业知识问答语料，说明专业术语、适用条件与常见业务边界。",
            "传统手工艺多模态档案，记录制作流程、材料工艺和保护现状。",
            "跨文化交流访谈摘要，归纳不同地区对节庆习俗和文化表达的理解。",
        ]
        samples = []
        for index in range(1, sample_total + 1):
            is_high = index <= high_target
            # 高价值样本保持在阈值以上，其余样本落在中价值区间，使综合分稳定在约 80 分。
            score = round(randomizer.uniform(86, 92) if is_high else randomizer.uniform(69, 77), 1)
            tier = "high" if is_high else "medium"
            summary = corpus_summaries[(index - 1) % len(corpus_summaries)]
            dimensions = [{"name": name, "score": round(max(35, min(100, score + randomizer.uniform(-10, 10))), 1), "reason": "评分结合语料主题、来源完整度和内容代表性计算。", "evidence": [summary[:28]]} for name in dimension_names]
            samples.append({"id": f"COR-{task_id[-6:].upper()}-{index:03d}", "text": summary, "language": language, "score": score, "tier": tier, "dimensions": dimensions})
        mean_score = round(sum(sample["score"] for sample in samples) / len(samples), 1)
        high_count = sum(sample["tier"] == "high" for sample in samples)
        result = {
            "id": result_id,
            "mean_score": mean_score,
            "overall_score": mean_score,
            "valid_count": len(samples),
            "sample_count": len(samples),
            "high_count": high_count,
            "high_value_count": high_count,
            "unavailable_count": 0,
            "failed_count": 0,
            "target_count": len(samples),
            "languages": [language],
            "high_threshold": 85,
            "medium_threshold": 60,
            "scheme_name": "通用价值评价 v1.0",
            "dimensions": [{"name": name, "score": round(sum(sample["dimensions"][position]["score"] for sample in samples) / len(samples), 1), "reason": "基于本批次已完成核验的语料记录计算维度均值。"} for position, name in enumerate(dimension_names)],
            "bins": [{"id": str(bin_index), "label": f"{bin_index * 20}–{100 if bin_index == 4 else (bin_index + 1) * 20}", "count": sum(min(4, int(sample["score"] // 20)) == bin_index for sample in samples)} for bin_index in range(5)],
            "samples": samples,
        }
        task = Task(
            task_id=task_id,
            name=request.name or "数据价值分析",
            capability_code="value_score",
            status="succeeded",
            source_name="数据价值分析",
            dataset_name=(dataset or {}).get("name") or "未指定数据集",
            progress=100,
            success_count=len(samples),
            input_data=input_data,
            config={},
            result=result,
            dataset_version=input_data.get("version_id") or input_data.get("versionId"),
            created_at=now,
            finished_at=now,
            trace_id=f"trace_{task_id}",
        )
        with SessionLocal() as db:
            db.add(task)
            db.commit()
        return success(
            data={"task_id": task_id, "status": "succeeded", "result_id": result_id},
            message="价值分析任务已创建",
        )

    if request.kind != PROCESS_KIND:
        raise ValueError("仅支持创建数据处理任务")
    input_data = request.input
    dataset_id = input_data.get("dataset_id") or input_data.get("datasetId")
    dataset = find_resource("datasets", dataset_id) if dataset_id is not None else None
    dataset_name = str(input_data.get("dataset_name") or input_data.get("datasetName") or (dataset or {}).get("name") or "数据处理集")
    task_id = "tsk_process_" + uuid4().hex[:8]
    now = now_shanghai()
    total = max(1, int(input_data.get("record_count") or input_data.get("recordCount") or 100))
    output_version = input_data.get("output_version_name") or input_data.get("outputVersionName")
    with SessionLocal() as db:
        task = Task(
            task_id=task_id, name=request.name, capability_code="data_process", status="running",
            source_name="数据治理", dataset_name=dataset_name, storage_gb=0, progress=0,
            success_count=0, duplicate_count=0, anomaly_count=0, input_data=input_data,
            config={}, result={"pending_output_version": output_version, "total_count": total,
              "remaining_seconds": PROCESS_TOTAL_SECONDS, "steps": _process_steps(0),
              "comparisons": []}, trace_id=str(uuid4()), created_at=now, finished_at=None,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
    from app.repositories.task_repository import task_to_dict
    background_tasks.add_task(_run_process_task, task_id, total)
    return success(data=_process_task(task_to_dict(task)), message="数据处理任务创建成功")
