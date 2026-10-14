from datetime import timedelta
import re

from sqlalchemy import inspect, select, text

from app.core.database import SessionLocal, engine
from app.models.tables import (
    Dataset,
    Metric,
    Model,
    ModelVersion,
    ModelService,
    Resource,
    SystemRole,
    SystemUser,
    Task,
    EvaluationMetric,
    EvaluationMetricRevision,
    EvaluationTask,
    EvaluationRun,
    EvaluationEvent,
)
from app.services.resource_display import (
    dataset_storage_default,
    dataset_display_defaults,
    model_version_default,
    normalize_source_name,
    task_dataset_default,
    task_display_defaults,
    task_ingest_statistics,
    task_version_default,
)

MAX_LOCAL_FILE_STORAGE_GB = 2.0


def ensure_evaluation_metric_columns():
    """补齐旧版评估指标表字段，避免模型升级后启动查询失败。"""
    inspector = inspect(engine)
    if not inspector.has_table("evaluation_metrics"):
        return

    columns = {column["name"] for column in inspector.get_columns("evaluation_metrics")}
    additions = {
        "configuration_status": "VARCHAR(30) NOT NULL DEFAULT 'published'",
        "issues": "JSON NULL",
    }
    with engine.begin() as connection:
        # 旧版指标表的 updated_at 没有默认值，新 ORM 已不再维护它，需放宽约束。
        if "updated_at" in columns:
            connection.execute(text("ALTER TABLE evaluation_metrics MODIFY COLUMN updated_at DATETIME NULL"))
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE evaluation_metrics ADD COLUMN {name} {definition}"))
        if "issues" not in columns:
            connection.execute(text("UPDATE evaluation_metrics SET issues = '[]' WHERE issues IS NULL"))

    # 早期版本的修订表只保存 metric_id 与 definition_json；新版接口还需
    # 指标展示字段并统一使用 definition。这里以可重复执行的增量迁移兼容已有库。
    if not inspector.has_table("evaluation_metric_revisions"):
        return
    revision_columns = {column["name"] for column in inspector.get_columns("evaluation_metric_revisions")}
    revision_additions = {
        "metric_code": "VARCHAR(100) NULL",
        "name": "VARCHAR(255) NULL",
        "category": "VARCHAR(80) NULL",
        "definition": "JSON NULL",
        "revision_no": "INT NOT NULL DEFAULT 1",
        "expected_revision": "INT NOT NULL DEFAULT 1",
        "configuration_status": "VARCHAR(30) NOT NULL DEFAULT 'draft'",
        "created_at": "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "published_at": "DATETIME NULL",
    }
    with engine.begin() as connection:
        for name, definition in revision_additions.items():
            if name not in revision_columns:
                connection.execute(text(f"ALTER TABLE evaluation_metric_revisions ADD COLUMN {name} {definition}"))
        if "definition_json" in revision_columns:
            connection.execute(text("UPDATE evaluation_metric_revisions SET definition = definition_json WHERE definition IS NULL"))
            connection.execute(text("ALTER TABLE evaluation_metric_revisions MODIFY COLUMN definition_json JSON NULL"))
        connection.execute(text("UPDATE evaluation_metric_revisions SET definition = JSON_OBJECT() WHERE definition IS NULL"))
        connection.execute(text("""
            UPDATE evaluation_metric_revisions r
            JOIN evaluation_metrics m ON m.metric_id = r.metric_id
            SET r.metric_code = COALESCE(r.metric_code, m.code),
                r.name = COALESCE(r.name, m.name),
                r.category = COALESCE(r.category, m.category)
            WHERE r.metric_code IS NULL OR r.name IS NULL OR r.category IS NULL
        """))


def ensure_evaluation_metrics():
    """初始化评估指标库；仅在编码不存在时写入，保证重复启动幂等。"""
    seeds = [
        ("alert_coverage", "风险预警覆盖率", "risk_detect", "coverage", "gte", .90, .95, "自动计算"),
        ("false_positive_rate", "风险识别误报率", "risk_detect", "fpr", "lte", .10, .05, "自动计算"),
        ("classification_accuracy", "内容分类准确率", "model_capability", "accuracy", "gte", .90, .95, "自动计算"),
        ("value_recall", "高价值数据召回率", "data_value", "recall", "gte", .90, .95, "自动计算"),
        ("anomaly_recall", "异常数据识别召回率", "data_governance", "recall", "gte", .90, .95, "自动计算"),
        ("trace_complete", "全链路留痕完整性", "compliance", "trace_complete", "eq", None, 1, "证据核验"),
        ("pollution_identification", "污染样本识别率", "data_governance", "needs_definition", "gte", .90, .95, "自动计算"),
        ("risk_reduction", "治理后风险下降率", "data_governance", "risk_reduction", "gte", .90, .95, "自动计算"),
    ]
    with SessionLocal() as db:
        for code, name, category, formula, comparator, mid, final, test_method in seeds:
            metric = db.scalar(select(EvaluationMetric).where(EvaluationMetric.code == code))
            if metric:
                continue
            import uuid
            metric_id = f"metric-{uuid.uuid4().hex[:16]}"
            metric = EvaluationMetric(metric_id=metric_id, code=code, name=name, category=category,
                                      description="示例验收指标，用于展示配置、修订与阶段阈值。",
                                      configuration_status="needs_definition" if formula == "needs_definition" else "published",
                                      issues=[] if formula != "needs_definition" else [{"field": "formula", "reasonCode": "needs_definition", "message": "需补齐计算口径后发布"}])
            db.add(metric)
            thresholds = []
            if mid is not None:
                thresholds.append({"stage": "midterm", "comparator": comparator, "value": mid, "unit": "ratio"})
            thresholds.append({"stage": "final", "comparator": comparator, "value": final, "unit": "boolean" if formula == "trace_complete" else "ratio"})
            definition = {"formulaCode": formula, "formulaVersion": "1.0", "formula": "待补齐业务定义" if formula == "needs_definition" else "TP / (TP + FN)",
                          "denominatorDefinition": "全部已标注样本", "positiveClass": "风险内容", "unit": "boolean" if formula == "trace_complete" else "ratio",
                          "testMethod": "evidence" if formula == "trace_complete" else "automatic", "applicableObjects": "内容安全检测结果",
                          "parameters": [], "thresholds": thresholds, "inputRequirements": ["标准标签", "预测结果"],
                          "requiredEvidence": ["样本明细"], "sourceDocumentRefs": []}
            db.add(EvaluationMetricRevision(revision_id=f"revision-{uuid.uuid4().hex[:16]}", metric_id=metric_id,
                                            metric_code=code, name=name, category=category,
                                            revision_no=1, expected_revision=1,
                                            configuration_status="needs_definition" if formula == "needs_definition" else "published",
                                            definition=definition,
                                            published_at=now_shanghai() if formula != "needs_definition" else None))
        db.commit()


def ensure_evaluation_tasks():
    """初始化评估任务页所需的可复现演示数据，所有状态和结果均持久化。"""
    names = [
        ("多模态风险检测验收", "succeeded"),
        ("内容分类能力复测", "succeeded"),
        ("数据治理来源连通测试", "failed"),
        ("风险识别完成期验收", "pending"),
        ("多模态内容安全评估", "running"),
    ]
    with SessionLocal() as db:
        if db.scalar(select(EvaluationTask).limit(1)):
            return
        now = now_shanghai()
        for index, (name, status) in enumerate(names, 1):
            task_id = f"eval_demo_task_{index}"
            run_id = f"eval_demo_run_{index}"
            record_id = f"eval_demo_record_{index}"
            terminal = status in {"succeeded", "failed"}
            task = EvaluationTask(task_id=task_id, run_id=run_id, record_id=record_id, name=name, status=status,
                target_stage="final", dataset_version="示例数据集 v2.1", model_version="示例模型 v1.0", metric_count=3,
                judgment_status="failed" if status == "failed" else ("passed" if status == "succeeded" else "not_evaluated"),
                allowed_actions=["start", "cancel"] if status == "pending" else (["cancel"] if status == "running" else []),
                stage="archived" if terminal else ("calculating" if status == "running" else "frozen"), trace_id=f"eval-demo-trace-{index}",
                processed_count=1000 if terminal else (640 if status == "running" else 0), total_count=1000,
                config={"targetStage":"final", "datasetVersionRef":"示例数据集 v2.1", "modelVersionRef":"示例模型 v1.0", "metricRevisionRefs":["demo-1","demo-2","demo-3"]},
                error="示例来源服务暂不可用" if status == "failed" else None, created_at=now, started_at=now if status != "pending" else None,
                finished_at=now if terminal else None, updated_at=now)
            run = EvaluationRun(run_id=run_id, task_id=task_id, record_id=record_id, test_no="", name=name,
                task_status=status, judgment_status=task.judgment_status, algorithm_mode="mock", snapshot={"datasetVersion":"示例数据集 v2.1", "modelVersion":"示例模型 v1.0", "sampleCount":1000, "targetStage":"final"},
                metric_results=[], allowed_actions=[], integrity_state="complete" if terminal else "incomplete", config=task.config,
                error=task.error, created_at=now, started_at=task.started_at, finished_at=task.finished_at)
            db.add_all([task, run, EvaluationEvent(task_id=task_id, stage=task.stage, message="示例评估任务已初始化。", status=status)])
        db.commit()


def normalize_evaluation_display_values():
    """Remove legacy demo labels from persisted evaluation records.

    The initial dataset used labels such as ``（示例）`` and ``DEMO-1``.
    Existing databases keep those values after a code upgrade, so normalize
    them once on every startup as an idempotent data migration.
    """
    with SessionLocal() as db:
        tasks = db.scalars(select(EvaluationTask)).all()
        runs = db.scalars(select(EvaluationRun)).all()
        changed = False
        for task in tasks:
            name = task.name.replace("（示例）", "").replace("(示例)", "")
            if task.name != name:
                task.name = name
                changed = True
        for run in runs:
            name = run.name.replace("（示例）", "").replace("(示例)", "")
            test_no = "" if re.fullmatch(r"(?:DEMO|TEST)-\d+", run.test_no) else run.test_no
            if run.name != name:
                run.name = name
                changed = True
            if run.test_no != test_no:
                run.test_no = test_no
                changed = True
        if changed:
            db.commit()
from app.core.time import now_shanghai
from sqlalchemy import inspect, text



from sqlalchemy import inspect, text

def ensure_task_columns():
    inspector = inspect(engine)

    if not inspector.has_table("tasks"):
        return

    columns = {
        column["name"]
        for column in inspector.get_columns("tasks")
    }

    additions = {
        "source_name": "VARCHAR(255) NULL",
        "dataset_name": "VARCHAR(255) NULL",
        "progress": "INT NOT NULL DEFAULT 0",
        "success_count": "INT NOT NULL DEFAULT 0",
        "duplicate_count": "INT NOT NULL DEFAULT 0",
        "anomaly_count": "INT NOT NULL DEFAULT 0",
    }

    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    text(
                        f"ALTER TABLE tasks "
                        f"ADD COLUMN {name} {definition}"
                    )
                )


def ensure_dataset_record_columns():
    """为历史接入记录补齐内容哈希列，供后续接入去重统计。"""
    inspector = inspect(engine)
    if not inspector.has_table("dataset_records"):
        return
    columns = {column["name"] for column in inspector.get_columns("dataset_records")}
    if "content_hash" not in columns:
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE dataset_records ADD COLUMN content_hash VARCHAR(64) NULL"
            ))
            connection.execute(text(
                "CREATE INDEX ix_dataset_records_content_hash ON dataset_records (content_hash)"
            ))


def ensure_model_columns():
    """为已有 models 表补齐模型管理页所需的档案字段和首个版本记录。"""
    inspector = inspect(engine)
    if not inspector.has_table("models"):
        return
    columns = {column["name"] for column in inspector.get_columns("models")}
    additions = {
        "model_type": "VARCHAR(100) NOT NULL DEFAULT '文本分类'",
        "source": "VARCHAR(50) NOT NULL DEFAULT 'self_developed'",
        "creator": "VARCHAR(100) NOT NULL DEFAULT '系统管理员'",
        "service_url": "VARCHAR(500) NULL",
        "updated_at": "DATETIME NULL",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE models ADD COLUMN {name} {definition}"))
        connection.execute(text("UPDATE models SET updated_at = created_at WHERE updated_at IS NULL"))

    with SessionLocal() as db:
        models = list(db.scalars(select(Model).order_by(Model.id.asc())).all())
        # 管理页的来源指标来自数据库。初始化记录采用 1 条自主研发、其余
        # 均分行业模型和开源模型的分布，避免任一来源为 0 且自主研发最少。
        for index, model in enumerate(models):
            model.source = (
                "self_developed"
                if index == 0
                else ("industry" if index % 2 else "open_source")
            )
            exists = db.scalar(select(ModelVersion).where(ModelVersion.model_id == model.id))
            if exists is None:
                db.add(ModelVersion(
                    model_id=model.id, version=model.version,
                    description="初始注册版本", created_at=model.created_at,
                ))
            service_id = f"svc_model_{model.id}"
            # 历史库可能已有同一固定主键但 model_id 未规范化；优先按主键
            # 查找，避免启动迁移重复插入而中断整个服务。
            service = db.get(ModelService, service_id) or db.scalar(
                select(ModelService).where(ModelService.model_id == str(model.id))
            )
            if service is None:
                db.add(ModelService(
                    id=service_id, name=f"{model.name}推理服务",
                    model_id=str(model.id), version=model.version, service_type="local",
                    status="running", endpoint="local://content-safety-model",
                    healthy=True,
                ))
        db.commit()


def ensure_training_task_columns():
    """为已有训练任务表补齐训练日志和检查点的 JSON 持久化字段。"""
    inspector = inspect(engine)
    if not inspector.has_table("training_tasks"):
        return
    columns = {column["name"] for column in inspector.get_columns("training_tasks")}
    additions = {
        "method": "VARCHAR(100) NOT NULL DEFAULT '低秩适配微调'",
        "loss_history": "JSON NOT NULL",
        "validation_loss_history": "JSON NOT NULL",
        "checkpoints": "JSON NOT NULL",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE training_tasks ADD COLUMN {name} {definition}"))
                if name in {"loss_history", "validation_loss_history", "checkpoints"}:
                    connection.execute(text(f"UPDATE training_tasks SET {name} = JSON_ARRAY() WHERE {name} IS NULL"))
        if "method" in columns or "method" in additions:
            connection.execute(text("UPDATE training_tasks SET method = '低秩适配微调' WHERE method IS NULL OR method = ''"))



def ensure_dataset_source_type_column():
    """为已有数据库补充数据集来源类型列；新表由 ORM 自动创建。"""
from sqlalchemy import inspect, text

# 假设 engine 已经在当前模块中定义，例如：
# from app.db import engine


from sqlalchemy import inspect, text

# 假设 engine 已经在当前模块中定义，例如：
# from app.db import engine


def ensure_dataset_source_type_column():
    """为已有数据库补充数据集来源类型列；新表由 ORM 自动创建。"""
    inspector = inspect(engine)

    # 1. 表不存在，说明 ORM 还没建表，直接返回
    if not inspector.has_table("datasets"):
        return

    # 2. 获取 datasets 表现有列名
    columns = {column["name"] for column in inspector.get_columns("datasets")}

    # 3. 如果已经有 source_type，说明迁移过了，直接返回
    #    关键：不要再次执行下面的 UPDATE，否则会覆盖人工设置的值
    if "source_type" in columns:
        return

    # 4. 事务内执行：加列 + 一次性回填
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE datasets "
            "ADD COLUMN source_type VARCHAR(30) NOT NULL DEFAULT 'business'"
        ))

        # 5. 只在新加列后回填一次
        #    用 CASE WHEN 明确优先级：互联网 > 风险 > 多模态 > 默认 business
        connection.execute(text("""
            UPDATE datasets
            SET source_type = CASE
                WHEN category LIKE '%互联网%' THEN 'internet'
                WHEN category LIKE '%风险%'   THEN 'industry'
                WHEN category LIKE '%多模态%' THEN 'synthetic'
                ELSE 'business'
            END
            WHERE source_type = 'business'
        """))


def migrate_legacy_resources():
    """把旧 resources 表中的数据迁移到独立的数据集和模型表。"""

    with SessionLocal() as db:
        legacy_resources = db.scalars(select(Resource)).all()

        for resource in legacy_resources:
            if resource.resource_type == "datasets":
                exists = db.scalar(
                    select(Dataset).where(Dataset.name == resource.name)
                )
                if exists is None:
                    db.add(
                    Dataset(
                            name=resource.name,
                            category=resource.category,
                            version=resource.version,
                            status=resource.status,
                            description=resource.description,
                            metadata_json=resource.metadata_json or {},
                            source_type=(resource.metadata_json or {}).get("source_type", "business"),
                            created_at=resource.created_at,
                        )
                    )
            elif resource.resource_type == "models":
                exists = db.scalar(select(Model).where(Model.name == resource.name))
                if exists is None:
                    db.add(
                        Model(
                            name=resource.name,
                            category=resource.category,
                            version=resource.version,
                            status=resource.status,
                            description=resource.description,
                            metadata_json=resource.metadata_json or {},
                            created_at=resource.created_at,
                        )
                    )

        db.commit()


def ensure_resource_display_values():
    """补齐历史演示记录的来源和数据量，不覆盖真实已填写的值。"""
    demo_dataset_names = {
        "跨文化交流语料",
        "行业风险标注数据集",
        "社交媒体中文语料库",
        "多模态内容安全样本集",
    }

    with SessionLocal() as db:
        for dataset in db.scalars(select(Dataset)).all():
            metadata = dict(dataset.metadata_json or {})
            should_seed_demo = dataset.name in demo_dataset_names and not metadata.get(
                "display_storage_initialized"
            )
            if should_seed_demo or float(metadata.get("storage_gb", 0) or 0) <= 0:
                metadata["storage_gb"] = dataset_display_defaults(
                    f"{dataset.id}:{dataset.name}"
                )[0]
                metadata["display_storage_initialized"] = True
            storage_gb, record_count, quality_score = dataset_display_defaults(
                f"{dataset.id}:{dataset.name}",
                float(metadata.get("storage_gb", 0) or 0),
            )
            if int(metadata.get("record_count", 0) or 0) <= 0:
                metadata["record_count"] = record_count
            if float(metadata.get("quality_score", 0) or 0) <= 0:
                metadata["quality_score"] = quality_score
            if not normalize_source_name(metadata.get("source_name")):
                metadata["source_name"] = {
                    "business": "本地文件",
                    "internet": "网络采集",
                    "industry": "API接口",
                    "synthetic": "对象存储",
                }.get(dataset.source_type, "数据库")
            dataset.metadata_json = metadata

        # 旧演示模型此前全部为 v1.0.0；按模型 ID 回填不同的稳定版本。
        for model in db.scalars(select(Model)).all():
            if not model.version or model.version == "v1.0.0":
                model.version = model_version_default(model.id, model.name)

        for task in db.scalars(select(Task)).all():
            default_source_name, default_storage_gb = task_display_defaults(task.task_id)
            source_name = normalize_source_name(task.source_name)
            if not source_name:
                task.source_name = default_source_name
            elif source_name != task.source_name:
                task.source_name = source_name
            # 新接入的小文件按 1 GB 展示，避免前端固定 GB 单位时出现一长串小数。
            # 已有的大文件容量保持原样，不在启动时擅自改写。
            storage_gb = float(task.storage_gb or 0)
            if task.capability_code == "data_ingest" and 0 < storage_gb < 1:
                delta_gb = 1 - storage_gb
                task.storage_gb = 1
                result = task.result or {}
                dataset_id = result.get("dataset_id") or (task.input_data or {}).get("dataset_id")
                if dataset_id is not None:
                    dataset = db.get(Dataset, dataset_id)
                    if dataset is not None:
                        metadata = dict(dataset.metadata_json or {})
                        metadata["storage_gb"] = round(
                            float(metadata.get("storage_gb", 0) or 0) + delta_gb,
                            6,
                        )
                        dataset.metadata_json = metadata
            elif storage_gb <= 0:
                task.storage_gb = default_storage_gb
            elif task.capability_code == "data_ingest" and task.source_name == "本地文件":
                # 历史演示数据可能使用旧的 80–360GB 本地文件范围，启动时迁移到新上限。
                task.storage_gb = min(storage_gb, MAX_LOCAL_FILE_STORAGE_GB)
            if not task.dataset_version and not task.model_version:
                task.dataset_version = task_version_default(
                    task.task_id,
                    task.capability_code,
                )
            if task.capability_code == "data_ingest":
                if not task.dataset_name:
                    task.dataset_name = task_dataset_default(task.task_id)
                default_success, default_duplicate, default_anomaly = task_ingest_statistics(
                    task.task_id,
                    float(task.storage_gb or 0),
                )
                if not task.success_count:
                    task.success_count = default_success
                if not task.duplicate_count:
                    task.duplicate_count = default_duplicate
                if not task.anomaly_count:
                    task.anomaly_count = default_anomaly

        db.commit()


def ensure_ingest_demo_tasks():
    """补充数据资源总览展示所需的唯一接入任务。"""
    demo_tasks = [
        ("社交媒体中文语料库接入", "网络采集", "社交媒体中文语料库"),
        ("短视频风险样本集接入", "抖音开放平台", "短视频风险样本集"),
        ("新闻资讯数据集接入", "新闻爬虫", "新闻资讯数据集"),
        ("内容安全多模态数据集接入", "本地文件", "内容安全多模态数据集"),
        ("政务服务问答数据集接入", "本地文件", "政务服务问答数据集"),
    ]
    with SessionLocal() as db:
        existing_names = {
            (task.name or "").strip()
            for task in db.scalars(
                select(Task).where(Task.capability_code == "data_ingest")
            ).all()
        }
        current_time = now_shanghai()
        for index, (name, source_name, dataset_name) in enumerate(demo_tasks, start=1):
            task_id = f"tsk_ingest_demo_{index:02d}"
            existing_task = db.get(Task, task_id)
            # 早期版本用相同稳定 ID 写入了不同文案；原地迁移，避免产生重复记录。
            if existing_task is not None:
                existing_task.name = name
                existing_task.source_name = source_name
                existing_task.dataset_name = dataset_name
                existing_task.status = "succeeded"
                existing_task.progress = 100
                existing_task.finished_at = existing_task.finished_at or existing_task.created_at
                existing_names.add(name)
                continue
            if name in existing_names:
                continue
            default_source, storage_gb = task_display_defaults(task_id)
            db.add(
                Task(
                    task_id=task_id,
                    name=name,
                    capability_code="data_ingest",
                    status="succeeded",
                    source_name=source_name or default_source,
                    dataset_name=dataset_name,
                    storage_gb=storage_gb,
                    progress=100,
                    success_count=8600 + index * 137,
                    duplicate_count=80 + index * 9,
                    anomaly_count=5 + index,
                    input_data={
                        "datasetName": dataset_name,
                        "sourceName": source_name,
                    },
                    config={"qualityCheck": True, "deduplicate": True},
                    result={"accepted": True, "record_count": 8600 + index * 137},
                    dataset_version=f"v1.{index}.0",
                    created_at=current_time - timedelta(minutes=index * 7),
                    finished_at=current_time - timedelta(minutes=index * 7 - 1),
                    trace_id=f"trace_ingest_demo_{index:02d}",
                )
            )
            existing_names.add(name)
        db.commit()


def ensure_process_demo_tasks():
    """补充数据治理页所需的数据处理任务，供页面直接从 tasks 表汇总读取。"""
    task_seeds = [
        ("tsk_process_demo_01", "社交媒体语料标准清洗", "社交媒体中文语料库", "succeeded", 100, 186_420, "v1.1.0"),
        ("tsk_process_demo_02", "多模态样本格式规范化", "多模态内容安全样本集", "succeeded", 100, 124_680, "v1.1.0"),
        ("tsk_process_demo_03", "行业风险数据去重补全", "行业风险标注数据集", "succeeded", 100, 98_350, "v1.2.0"),
        ("tsk_process_demo_04", "跨文化语料文本清洗", "跨文化交流语料", "succeeded", 100, 156_900, "v1.1.0"),
        ("tsk_process_demo_05", "政务问答数据质量处理", "政务服务问答数据集", "succeeded", 100, 72_640, "v1.1.0"),
        ("tsk_process_demo_06", "历史批次异常格式修复", "新闻资讯数据集", "failed", 100, 12_300, None),
        ("tsk_process_demo_07", "新增短视频样本清洗", "短视频风险样本集", "running", 64, 45_800, None),
    ]
    now = now_shanghai()
    with SessionLocal() as db:
        for index, (task_id, name, dataset_name, status, progress, processed, output_version) in enumerate(task_seeds):
            task = db.get(Task, task_id)
            created_at = now - timedelta(hours=(len(task_seeds) - index) * 2)
            result = {
                "total_count": processed,
                "output_version": output_version,
                "steps": [
                    {"name": "读取数据", "status": "succeeded"},
                    {"name": "规范化", "status": "succeeded"},
                    {"name": "去重", "status": "succeeded"},
                    {"name": "字段校验", "status": "succeeded" if status == "succeeded" else status},
                    {"name": "生成版本", "status": "succeeded" if status == "succeeded" else "pending"},
                ],
                "comparisons": [],
            }
            values = {
                "name": name,
                "capability_code": "data_process",
                "status": status,
                "source_name": "数据资源库",
                "dataset_name": dataset_name,
                "storage_gb": 0,
                "progress": progress,
                "success_count": processed,
                "duplicate_count": max(1, processed // 120),
                "anomaly_count": max(1, processed // 1200),
                "input_data": {
                    "dataset_id": index + 1,
                    "dataset_version_id": "v1.0.0",
                    "template_id": "standard",
                    "scope": "all",
                    "rules": ["normalize_text", "deduplicate", "complete_fields"],
                },
                "config": {"template": "standard"},
                "result": result,
                "dataset_version": "v1.0.0",
                "model_version": None,
                "created_at": created_at,
                "finished_at": created_at + timedelta(minutes=18) if status in {"succeeded", "failed"} else None,
                "trace_id": f"trace_process_demo_{index + 1:02d}",
            }
            if task is None:
                db.add(Task(task_id=task_id, **values))
            else:
                for field, value in values.items():
                    setattr(task, field, value)
        db.commit()


def ensure_model_evaluation_versions():
    """为每个模型补充一个可供模型评估页选择的编辑后版本。"""
    with SessionLocal() as db:
        for model in db.scalars(select(Model)).all():
            version = f"{model.version}-edit"
            exists = db.scalar(
                select(ModelVersion).where(
                    ModelVersion.model_id == model.id,
                    ModelVersion.version == version,
                )
            )
            if exists is None:
                db.add(ModelVersion(
                    model_id=model.id,
                    version=version,
                    description="后端模拟知识编辑后的评估版本",
                    task_id=f"seed-model-edit-{model.id}",
                ))
        db.commit()


def seed_initial_data():
    """
    第一次启动时写入演示数据。
    如果数据已经存在，则不重复写入。
    """

    with SessionLocal() as db:
        # 两张表各自使用独立的自增主键；按名称补齐，避免重启重复插入。
        dataset_seeds = [
            {
                "name": "跨文化交流语料",
                "category": "语料数据",
                "source_type": "business",
                "description": "用于内容安全测试的模拟数据集",
                "metadata_json": {"record_count": 1000, "languages": ["zh", "en"]},
            },
            {
                "name": "行业风险标注数据集",
                "category": "风险数据",
                "source_type": "industry",
                "description": "包含风险标签的模拟数据集",
                "metadata_json": {"record_count": 500, "languages": ["zh"]},
            },
            {
                "name": "社交媒体中文语料库",
                "category": "互联网数据",
                "source_type": "internet",
                "description": "用于舆情分析和语义风险识别的中文语料",
                "metadata_json": {"record_count": 2000, "languages": ["zh"]},
            },
            {
                "name": "多模态内容安全样本集",
                "category": "多模态数据",
                "source_type": "synthetic",
                "description": "包含文本、图片和视频的内容安全样本",
                "metadata_json": {
                    "record_count": 800,
                    "languages": ["zh", "en"],
                    "modalities": ["text", "image", "video"],
                },
            },
        ]
        for item in dataset_seeds:
            exists = db.scalar(
                select(Dataset).where(Dataset.name == item["name"])
            )
            if exists is None:
                db.add(Dataset(version="v1.0.0", status="ready", **item))

        # 为已有数据集补齐资源汇总接口使用的扩展元数据，不覆盖已有值。
        metadata_defaults = {
            "跨文化交流语料": {
                "storage_gb": 12.5,
                "source_name": "业务系统",
                "quality_status": "good",
                "quality_score": 94.2,
                "uses": 86,
            },
            "行业风险标注数据集": {
                "storage_gb": 8.6,
                "source_name": "行业数据合作方",
                "quality_status": "good",
                "quality_score": 92.5,
                "uses": 36,
            },
            "社交媒体中文语料库": {
                "storage_gb": 24.8,
                "source_name": "互联网采集",
                "quality_status": "excellent",
                "quality_score": 96.8,
                "uses": 128,
            },
            "多模态内容安全样本集": {
                "storage_gb": 18.2,
                "source_name": "业务系统",
                "quality_status": "good",
                "quality_score": 91.7,
                "uses": 64,
            },
        }
        for dataset in db.scalars(select(Dataset)).all():
            defaults = metadata_defaults.get(dataset.name, {})
            metadata = dict(dataset.metadata_json or {})
            changed = False
            for key, value in defaults.items():
                if key not in metadata:
                    metadata[key] = value
                    changed = True
            if changed:
                dataset.metadata_json = metadata

        model_seeds = [
            {
                "name": "内容安全识别模型",
                "category": "风险识别",
                "description": "用于模拟语义风险识别的模型",
                "metadata_json": {
                    "parameter_count": "7B",
                    "supported_languages": ["zh", "en"],
                },
            },
            {
                "name": "多模态审核模型",
                "category": "内容审核",
                "description": "用于模拟图文内容审核的模型",
                "metadata_json": {"modalities": ["text", "image"]},
            },
            {
                "name": "风险分类大模型",
                "category": "风险识别",
                "description": "用于风险分类、分级和解释的模型",
                "metadata_json": {
                    "parameter_count": "13B",
                    "supported_languages": ["zh", "en", "ja"],
                },
            },
            {
                "name": "内容治理对话模型",
                "category": "模型治理",
                "description": "用于安全对话和合规回答生成的模型",
                "metadata_json": {
                    "parameter_count": "7B",
                    "modalities": ["text"],
                },
            },
        ]
        for item in model_seeds:
            exists = db.scalar(select(Model).where(Model.name == item["name"]))
            if exists is None:
                db.add(Model(version="v1.0.0", status="ready", **item))

        # 写入指标
        metric_exists = db.scalar(
            select(Metric).where(
                Metric.metric_code == "risk_recall"
            )
        )

        if metric_exists is None:
            db.add_all(
                [
                    Metric(
                        metric_code="risk_recall",
                        name="风险召回率",
                        category="风险识别",
                        target_value=0.95,
                        unit="ratio",
                        description="高风险样本召回率",
                    ),
                    Metric(
                        metric_code="false_positive_rate",
                        name="误报率",
                        category="合规治理",
                        target_value=0.05,
                        unit="ratio",
                        description="正常样本被误判为风险的比例",
                    ),
                ]
            )
        # 初始化系统管理页面使用的用户数据。
        # 以 username 判断是否已存在，重复启动不会重复插入。
        user_seeds = [
            {
                "username": "admin",
                "display_name": "系统管理员",
                "email": "admin@content-safety.local",
                "role": "超级管理员",
                "status": "active",
                "description": "负责平台配置、用户管理和系统运维。",
            },
            {
                "username": "governance_manager",
                "display_name": "数据治理管理员",
                "email": "governance@content-safety.local",
                "role": "数据治理管理员",
                "status": "active",
                "description": "负责数据集治理、风险识别和质量评估。",
            },
            {
                "username": "auditor",
                "display_name": "合规审计员",
                "email": "audit@content-safety.local",
                "role": "审计员",
                "status": "active",
                "description": "负责审计日志、模型合规和风险复核。",
            },
            {
                "username": "analyst",
                "display_name": "安全分析师",
                "email": "analyst@content-safety.local",
                "role": "普通用户",
                "status": "active",
                "description": "负责日常安全分析、测试和结果查看。",
            },
        ]

        for item in user_seeds:
            exists = db.scalar(
                select(SystemUser).where(
                    SystemUser.username == item["username"]
                )
            )

            if exists is None:
                db.add(SystemUser(**item))
                # 初始化系统角色及权限。
        # role_code 唯一，重复启动服务时不会重复插入。
        role_seeds = [
            {
                "role_code": "super_admin",
                "name": "超级管理员",
                "description": "拥有平台全部功能、用户管理、角色配置和系统维护权限。",
                "permissions": [
                    "user:read",
                    "user:write",
                    "user:delete",
                    "role:read",
                    "role:write",
                    "dataset:read",
                    "dataset:write",
                    "model:read",
                    "model:write",
                    "task:execute",
                    "audit:read",
                    "system:manage",
                ],
                "status": "active",
            },
            {
                "role_code": "governance_manager",
                "name": "数据治理管理员",
                "description": "负责数据集接入、数据质量检查、异常治理和风险识别。",
                "permissions": [
                    "dataset:read",
                    "dataset:write",
                    "task:execute",
                    "risk:read",
                    "risk:write",
                    "evaluation:read",
                ],
                "status": "active",
            },
            {
                "role_code": "auditor",
                "name": "审计员",
                "description": "负责合规审计、模型审查、日志查看和风险复核。",
                "permissions": [
                    "dataset:read",
                    "model:read",
                    "task:read",
                    "audit:read",
                    "risk:read",
                    "evaluation:read",
                ],
                "status": "active",
            },
            {
                "role_code": "analyst",
                "name": "普通用户",
                "description": "负责查看平台资源、执行授权任务和查看个人任务结果。",
                "permissions": [
                    "dataset:read",
                    "model:read",
                    "task:read",
                    "task:execute",
                    "evaluation:read",
                ],
                "status": "active",
            },
        ]

        for item in role_seeds:
            exists = db.scalar(
                select(SystemRole).where(
                    SystemRole.role_code == item["role_code"]
                )
            )

            if exists is None:
                db.add(SystemRole(**item))
        db.commit()

    print("MySQL 初始资源数据检查完成")





def ensure_task_columns():
    inspector = inspect(engine)

    if not inspector.has_table("tasks"):
        return

    columns = {
        column["name"]
        for column in inspector.get_columns("tasks")
    }

    additions = {
        "source_name": "VARCHAR(255) NULL",
        "dataset_name": "VARCHAR(255) NULL",
        "progress": "INT NOT NULL DEFAULT 100",
        "success_count": "INT NOT NULL DEFAULT 0",
        "duplicate_count": "INT NOT NULL DEFAULT 0",
        "anomaly_count": "INT NOT NULL DEFAULT 0",
        "storage_gb": "DOUBLE NOT NULL DEFAULT 0",
    }

    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    text(
                        f"ALTER TABLE tasks "
                        f"ADD COLUMN {name} {definition}"
                    )
                )

        # 修复已经存在的已完成任务
        connection.execute(
            text(
                """
                UPDATE tasks
                SET progress = 100
                WHERE status = 'succeeded'
                  AND (progress IS NULL OR progress = 0)
                """
            )
        )
