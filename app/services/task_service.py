from uuid import uuid4
from time import perf_counter, sleep
from random import uniform
from pathlib import Path
from app.adapters.mock_adapter import execute_mock_capability
from app.core.database import SessionLocal
from sqlalchemy.orm import Session
from app.models.tables import Dataset, ModelCall, ModelService
from app.core.time import now_shanghai
from app.domain.capabilities import is_supported
from app.domain.schemas import ExecuteTaskRequest
from app.repositories.audit_repository import add_log
from app.repositories.evaluation_repository import save_evaluation
from app.repositories.resource_repository import (
    find_metric,
    find_resource,
)
from app.repositories.task_repository import save, update
from app.services.resource_display import (
    normalize_source_name,
    task_dataset_default,
    task_display_defaults,
    task_ingest_statistics,
    task_version_default,
)
from app.services.database_ingest import read_mysql_rows, save_dataset_rows
from app.services.file_ingest import read_uploaded_file_rows

MAX_LOCAL_FILE_STORAGE_GB = 2.0


def _safe_ingest_input(input_data: dict) -> dict:
    """平台任务和审计日志不能落库外部 MySQL 凭据。"""
    safe = dict(input_data)
    if safe.get("sourceAddress") or safe.get("source_address"):
        safe.pop("sourceAddress", None)
        safe.pop("source_address", None)
        safe["sourceAddress"] = "[已隐藏：MySQL 连接凭据]"
    return safe


def _required_text(value: object, field_name: str) -> str:
    """读取前端提交的必填文本，避免接入任务出现空展示字段。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"数据接入任务缺少{field_name}")
    return value.strip()


def _uploaded_size_bytes(file_ids: object) -> int:
    """返回上传接口已保存文件的实际字节数。"""
    if not isinstance(file_ids, list):
        return 0
    total_bytes = 0
    upload_dir = Path("storage/uploads")
    for file_id in file_ids:
        if not isinstance(file_id, str) or not file_id.startswith("file_"):
            continue
        if not file_id.replace("_", "").isalnum():
            continue
        for uploaded in upload_dir.glob(f"{file_id}.*"):
            if uploaded.is_file():
                total_bytes += uploaded.stat().st_size
    return total_bytes


def _ingest_record_count(input_data: dict, source_storage_gb: float) -> int:
    """按实际数据量估算新增记录数；容量越大，新增记录数越多。"""
    explicit_count = input_data.get("record_count") or input_data.get("recordCount")
    if explicit_count is not None:
        try:
            return max(1, int(explicit_count))
        except (TypeError, ValueError):
            raise ValueError("数据记录数必须是整数") from None

    # 按约 1 KB 一条记录估算。设置上下限，避免极小文件为 0 条，
    # 也避免异常超大文件制造不合理的大整数。
    return min(50_000_000, max(100, int(source_storage_gb * 1024 * 1024)))


def _sync_ingested_dataset(
    db: Session,
    input_data: dict,
    *,
    source_name: str,
    storage_gb: float,
    record_count: int,
) -> Dataset:
    """将成功接入的容量、记录数和来源回写到目标数据集。"""
    dataset_id = input_data.get("dataset_id") or input_data.get("datasetId")
    create_dataset = bool(input_data.get("create_dataset") or input_data.get("createDataset"))
    dataset: Dataset | None = None
    if dataset_id is not None:
        try:
            dataset = db.get(Dataset, int(dataset_id))
        except (TypeError, ValueError):
            raise ValueError("目标数据集 ID 无效") from None
        if dataset is None:
            raise ValueError(f"数据集不存在：{dataset_id}")
    elif create_dataset:
        dataset = Dataset(
            name=_required_text(input_data.get("dataset_name") or input_data.get("datasetName"), "目标数据集名称"),
            category="接入数据",
            source_type=str(input_data.get("source_type") or input_data.get("sourceType") or "business"),
            version="v1.0.0",
            status="ready",
            description="由数据接入任务创建",
            metadata_json={},
        )
        db.add(dataset)
        db.flush()
    else:
        raise ValueError("请选择目标数据集，或勾选随本次接入新建数据集")

    metadata = dict(dataset.metadata_json or {})
    metadata["source_name"] = source_name
    metadata["owner"] = _required_text(input_data.get("owner"), "数据所有者")
    metadata["languages"] = input_data.get("languages") or metadata.get("languages") or ["zh"]
    metadata["modalities"] = input_data.get("modalities") or metadata.get("modalities") or ["文本"]
    metadata["storage_gb"] = round(float(metadata.get("storage_gb", 0) or 0) + storage_gb, 6)
    metadata["record_count"] = int(metadata.get("record_count", 0) or 0) + record_count
    metadata["quality_status"] = metadata.get("quality_status") or "good"
    metadata["quality_score"] = float(metadata.get("quality_score", 95) or 95)
    dataset.metadata_json = metadata
    dataset.status = "ready"
    db.flush()
    return dataset


def now_utc():
    """
    获取北京时间并去掉微秒。
    保留旧函数名以兼容现有调用方。
    """
    # 保留函数名以兼容现有调用方，实际统一返回北京时间。
    return now_shanghai()


def run_mock_task(
    request: ExecuteTaskRequest,
    trace_id: str,
    defer_ingest: bool = False,
    existing_ingest_task_id: str | None = None,
):
    """
    创建任务
    -> 记录开始日志
    -> 执行模拟能力
    -> 保存结果
    -> 记录完成或失败日志。
    """

   
   # 关联数据集
  
    dataset_id = (
        request.input.get("dataset_id")
        or request.input.get("datasetId")
    )

    dataset_version = None
    dataset_name = None
    dataset_storage_gb = 0.0

    if dataset_id is not None:
        dataset = find_resource(
            "datasets",
            dataset_id,
        )

        if dataset is None:
            raise ValueError(
                f"数据集不存在：{dataset_id}"
            )

        dataset_version = (
            dataset.get("version")
            or dataset.get("version_id")
        )

        dataset_name = dataset.get("name")

        metadata = dataset.get("metadata") or {}

        dataset_storage_gb = float(
            metadata.get("storage_gb", 0) or 0
        )
        
    input_data = request.input
    stored_input = _safe_ingest_input(input_data) if request.capability_code == "data_ingest" else input_data

    is_ingest = request.capability_code == "data_ingest"
    task_id = existing_ingest_task_id or "tsk_" + uuid4().hex[:8]
    if is_ingest:
        source_name = _required_text(
            input_data.get("source_name") or input_data.get("sourceName"),
            "数据源",
        )
    else:
        default_source_name, _ = task_display_defaults(task_id)
        source_name = normalize_source_name(
            input_data.get("source_name")
            or input_data.get("sourceName")
            or input_data.get("source_address")
            or input_data.get("sourceAddress")
            or input_data.get("connector_type")
            or input_data.get("connectorType")
        ) or default_source_name

    dataset_name = (
        input_data.get("dataset_name")
        or input_data.get("datasetName")
        or dataset_name
        or task_dataset_default(task_id)
    )

    # 除非调用方明确传入数据量，否则每个新任务使用自身的随机展示容量；
    # 不能直接复用目标数据集的总容量，否则连续新建任务会出现相同数值。
    explicit_storage_gb = input_data.get("storage_gb") or input_data.get("storageGb")
    if is_ingest:
        uploaded_size_bytes = _uploaded_size_bytes(input_data.get("files"))
        source_storage_gb = float(
            explicit_storage_gb or uploaded_size_bytes / 1024**3 or 0.001
        )
        storage_gb = source_storage_gb
        if 0 < storage_gb < 1:
            storage_gb = 1.0
        if storage_gb > MAX_LOCAL_FILE_STORAGE_GB and normalize_source_name(
            input_data.get("source_name") or input_data.get("sourceName")
        ) == "本地文件":
            raise ValueError("本地文件单次接入不能超过 2GB")
    else:
        _, default_storage_gb = task_display_defaults(task_id)
        storage_gb = float(explicit_storage_gb or default_storage_gb)

    # 关联模型
    model_id = request.input.get("model_id")
    model_version = None

    if model_id is not None:
        model = find_resource(
            "models",
            model_id,
        )

        if model is None:
            raise ValueError(
                f"模型不存在：{model_id}"
            )

        model_version = (model.get("version")
        or model.get("version_id")
)

    # 未绑定资源的能力任务也需要可展示、可追溯的版本号。
    if not dataset_version and not model_version:
        dataset_version = task_version_default(task_id, request.capability_code)

    # 创建任务
    task = {
        "task_id": task_id,
        "name": request.name or "未命名任务",
        "capability_code": request.capability_code,
        # 接入连接通过后，先明确进入等待队列。连接成功只代表任务已创建，
        # 数据处理尚未开始，因此进度保持在 0%。
        "status": "pending" if is_ingest and defer_ingest else "running",

        "source_name": source_name,
        "dataset_name": dataset_name,
        "storage_gb": storage_gb,

        "progress": 0 if is_ingest else 100,
        "success_count": 0,
        "duplicate_count": 0,
        "anomaly_count": 0,

        "input": stored_input,
        "config": request.config,
        "result": None,
        "trace_id": trace_id,
        "created_at": now_utc(),
        "finished_at": None,
        "dataset_version": dataset_version,
        "model_version": model_version,
    }
    # 使用同一个数据库事务保存任务和审计日志
    with SessionLocal() as db:
        try:
            if existing_ingest_task_id:
                # 保留可观察的等待窗口；连接成功后不应立刻被运行状态覆盖。
                sleep(5)
                update(task_id, {"status": "running", "progress": 0}, db)
                db.commit()
            else:
                # 保存任务
                saved_task = save(task, db)

                # 记录任务开始日志
                add_log(
                    task_id=task_id,
                    event_type="task_started",
                    request_data={
                        "capability_code": request.capability_code,
                        "input": stored_input,
                        "config": request.config,
                    },
                    capability_code=request.capability_code,
                    endpoint="/api/v1/tasks/execute",
                    trace_id=trace_id,
                    db=db,
                )
                if is_ingest and defer_ingest:
                    db.commit()
                    return saved_task

            started_at = perf_counter()

            # 模型调用页以同步请求等待治理结果；模拟真实推理、审核与结果整理耗时。
            # 每次在 8～12 秒间随机，页面会一直保持现有的“正在调用”加载状态。
            if request.capability_code == "model_risk_governance":
                sleep(uniform(8, 12))

            # 风险知识编辑仅模拟一次轻量变更提交，固定一秒后回传任务结果。
            # 它不会伪造新的评估分数；评估结果仍须由用户显式发起评估生成。
            if request.capability_code == "knowledge_edit":
                sleep(1)

            # 检查能力是否支持并执行
            if not is_supported(request.capability_code):
                result = {
                    "error": (
                        f"不支持的能力编码："
                        f"{request.capability_code}"
                    ),
                    "error_type": (
                        "UnsupportedCapabilityError"
                    ),
                }
            else:
                result = execute_mock_capability(
                    capability_code=request.capability_code,
                    input_data=request.input,
                )
                            # 从执行结果中提取接入统计数据
            record_count = int(
                result.get("record_count", 0) or 0
            )

            duplicate_count = int(
                result.get("duplicate_count", 0) or 0
            )

            anomaly_count = int(
                result.get("anomaly_count", 0) or 0
            )

            # accepted=True 表示本次接入成功
            success_count = (
                record_count
                if result.get("accepted") is True
                else int(result.get("success_count", 0) or 0)
            )

            if request.capability_code == "data_ingest":
                connector_type = input_data.get("connectorType") or input_data.get("connector_type")
                database_rows: list[dict] = []
                uploaded_rows: list[dict] = []
                if connector_type == "database":
                    database_rows, source_bytes, source_kind = read_mysql_rows(input_data)
                    storage_gb = source_bytes / 1024**3
                    record_count = len(database_rows)
                    result["database_source"] = source_kind
                elif connector_type == "file":
                    # 每个文件保留完整正文，写入 dataset_records 的一条记录。
                    uploaded_rows, source_bytes = read_uploaded_file_rows(input_data.get("files"))
                    storage_gb = source_bytes / 1024**3
                    record_count = len(uploaded_rows)
                else:
                    record_count = _ingest_record_count(input_data, source_storage_gb)
                # 文件按文件数、数据库来源按数据行数统计成功入库记录。
                success_count = record_count
                _, _, default_anomaly = task_ingest_statistics(
                    task_id,
                    storage_gb,
                )
                # 有可解析记录的来源在写入时按内容哈希返回真实重复数。
                duplicate_count = 0
                # 文件解析失败会使任务失败；正常文件不使用模拟异常数量。
                anomaly_count = (
                    0 if connector_type == "file" else anomaly_count or default_anomaly
                )
                result.update(
                    {
                        "record_count": record_count,
                        "success_count": success_count,
                        "duplicate_count": duplicate_count,
                        "anomaly_count": anomaly_count,
                    }
                )
                # 来源读取完成后才进入“正在采集”。每一阶段固定展示五秒，
                # 前端轮询可依次呈现采集、清洗、检测；接入完成才显示 100%。
                for progress in (25, 50, 75):
                    update(task_id, {"progress": progress}, db)
                    db.commit()
                    sleep(5)
                dataset = _sync_ingested_dataset(
                    db,
                    input_data,
                    source_name=source_name,
                    storage_gb=storage_gb,
                    record_count=success_count,
                )
                dataset_name = dataset.name
                if connector_type in {"database", "file"}:
                    duplicate_count = save_dataset_rows(
                        db,
                        dataset_id=dataset.id,
                        task_id=task_id,
                        rows=database_rows if connector_type == "database" else uploaded_rows,
                        content_hashes=(
                            [row["source_content_hash"] for row in uploaded_rows]
                            if connector_type == "file" else None
                        ),
                    )
                    result["duplicate_count"] = duplicate_count
                result["dataset_id"] = dataset.id
                result["dataset_name"] = dataset.name

            # 评估能力额外读取指标信息
            if request.capability_code == "evaluation":
                metric_code = request.input.get(
                    "metric_code",
                    "risk_recall",
                )

                metric = find_metric(metric_code)

                if metric is None:
                    raise ValueError(
                        f"指标不存在：{metric_code}"
                    )

                value = result.get(
                    "value",
                    0.96,
                )

                target = metric["target"]

                result.update(
                    {
                        "metric_code": metric["metric_code"],
                        "metric_name": metric["name"],
                        "value": value,
                        "target": target,
                        "unit": metric["unit"],
                        "passed": value >= target,
                        "reason": (
                            "评估值达到指标目标"
                            if value >= target
                            else "评估值未达到指标目标"
                        ),
                    }
                )

                # 模型工作台评估页需要成组指标；按测试数据集生成稳定但不同的模拟结果。
                if request.input.get("model_id") or request.input.get("modelId"):
                    model_id = str(request.input.get("model_id", request.input.get("modelId")))
                    dataset_id = str(request.input.get("dataset_id", request.input.get("datasetId", "1")))
                    try:
                        offset = int(dataset_id) % 5
                    except ValueError:
                        offset = 0
                    risk_total = 108 + offset * 12
                    target_total = 52 + offset * 4
                    general_total = 46 + offset * 3
                    retention_total = 70 + offset * 5
                    result["model_assessment"] = {
                        "id": f"assessment-{task_id}", "task_id": task_id, "model_id": model_id,
                        "baseline": request.config.get("baseline_version", request.config.get("baselineVersion", "")),
                        "edited": request.config.get("edited_version", request.config.get("editedVersion", "")),
                        "dataset_id": dataset_id,
                        "dataset_version": request.config.get("dataset_version", request.config.get("datasetVersion", "")),
                        "knowledge": "模型版本安全效果评估", "status": "succeeded",
                        "risk_total": risk_total, "risk_before": 34 + offset * 2, "risk_after": 5 + offset,
                        "target_total": target_total, "target_before": 9 + offset, "target_after": target_total - 1,
                        "general_total": general_total, "general_before": general_total - 4, "general_after": general_total - 1,
                        "retention_total": retention_total, "retention_before": retention_total, "retention_after": retention_total - 1,
                        "samples": [{"type": "风险输出", "input": "模拟安全测试输入", "before": "存在风险线索", "after": "已完成安全约束"}],
                    }

                # 保存评估记录
                save_evaluation(
                    task_id=task_id,
                    result=result,
                    db=db,
                )
             # 计算任务执行耗时
            duration_ms = int(
                (perf_counter() - started_at) * 1000
            )
            # 同时回传耗时给本次请求；模型调用页无需等待下一次刷新即可展示。
            result["elapsed_ms"] = duration_ms

            # 根据结果判断任务状态
            if "error" in result:
                status = "failed"
                event_type = "task_failed"
            else:
                status = "succeeded"
                event_type = "task_finished"

            # 模型调用页通过 tasks/execute 触发推理。调用成功后将完整的
            # 输入、服务、版本和治理结果独立落到 model_calls，页面刷新后
            # 仍可从模型工作台聚合接口恢复“最近调用”。
            if request.capability_code == "model_risk_governance" and status == "succeeded":
                requested_model_id = input_data.get("model_id") or input_data.get("modelId")
                service_id = request.config.get("service_id") or request.config.get("serviceId")
                prompt = input_data.get("prompt")
                if requested_model_id is None:
                    raise ValueError("模型调用缺少模型 ID")
                if not isinstance(service_id, str) or not service_id.strip():
                    raise ValueError("模型调用缺少服务 ID")
                if not isinstance(prompt, str) or not prompt.strip():
                    raise ValueError("模型调用内容不能为空")

                service = db.get(ModelService, service_id.strip())
                if service is None:
                    raise ValueError(f"模型服务不存在：{service_id}")
                if service.model_id != str(requested_model_id):
                    raise ValueError("所选服务与模型不匹配")
                if service.status != "running":
                    raise ValueError("所选模型服务未运行，无法调用")

                risk_check = result.get("risk_check") or result.get("riskCheck") or {}
                original_output = result.get("original_output") or result.get("originalOutput")
                governed_output = result.get("governed_output") or result.get("governedOutput")
                if not isinstance(original_output, str) or not isinstance(governed_output, str):
                    raise ValueError("模型服务未返回完整治理结果")

                db.add(ModelCall(
                    id=task_id,
                    model_id=str(requested_model_id),
                    service_id=service.id,
                    version=(
                        request.config.get("model_version")
                        or request.config.get("modelVersion")
                        or service.version
                    ),
                    prompt=prompt.strip(),
                    original_output=original_output,
                    governed_output=governed_output,
                    reason=str(risk_check.get("reason") or "服务未返回判定依据"),
                    risk_level=str(risk_check.get("level") or "unknown"),
                    reconstruction=result.get("reconstruction"),
                    elapsed_ms=duration_ms,
                    status="succeeded",
                    trace_id=trace_id,
                    created_at=now_shanghai(),
                ))

            # 更新任务结果
            finished_task = update(
                task_id,
                {
                    "status": status,
                    "dataset_name": dataset_name,
                    "storage_gb": storage_gb,
                    "result": result,
                    "finished_at": now_utc(),

                    # 同步执行的模拟任务成功后显示 100%
                    "progress": 100 if status == "succeeded" else 0,
                    "success_count": success_count,
                    "duplicate_count": duplicate_count,
                    "anomaly_count": anomaly_count,
                },
                db,
            )

            # 记录任务完成或失败日志
            add_log(
                task_id=task_id,
                event_type=event_type,
                response_data=result,
                capability_code=request.capability_code,
                endpoint="/api/v1/tasks/execute",
                trace_id=trace_id,
                duration_ms=duration_ms,
                db=db,
            )

            # 提交事务
            db.commit()

            return finished_task

        except Exception as error:
            # 新建任务仍保持原有的同步失败语义；后台接入任务则必须留下可见的
            # 失败终态，避免前端轮询到一个永远“正在采集”的任务。
            db.rollback()
            if existing_ingest_task_id:
                with SessionLocal() as failed_db:
                    failure = {"error": str(error), "error_type": type(error).__name__}
                    update(
                        task_id,
                        {
                            "status": "failed",
                            "progress": 0,
                            "result": failure,
                            "finished_at": now_utc(),
                        },
                        failed_db,
                    )
                    add_log(
                        task_id=task_id,
                        event_type="task_failed",
                        response_data=failure,
                        capability_code=request.capability_code,
                        endpoint="/api/v1/tasks/execute",
                        trace_id=trace_id,
                        db=failed_db,
                    )
                    failed_db.commit()
                return None
            raise
