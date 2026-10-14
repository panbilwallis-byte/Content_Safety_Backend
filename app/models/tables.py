from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.time import now_shanghai


class Task(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    capability_code: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
    )

    trace_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        index=True,
    )

    # ===== 新增：接入任务列表展示字段 =====

    # “数据源”列，如：本地文件、业务系统、某个 URL
    source_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    # “目标数据集”列，如：社交媒体中文语料库
    dataset_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    # “数据量”列，单位统一为 GB
    storage_gb: Mapped[float] = mapped_column(
        Float,
        default=0,
        nullable=False,
    )

    # “接入进度”列；当前是同步任务，成功任务最终为 100
    progress: Mapped[int] = mapped_column(
        Integer,
        default=100,
        nullable=False,
    )

    # 接入结果统计
    success_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    duplicate_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    anomaly_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    # ===== 原有字段继续保留 =====

    input_data: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )

    config: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )

    result: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )

    dataset_version: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    model_version: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai,
        nullable=False,
    )

    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

class AuditLog(Base):
    """
    保存任务执行过程中的开始、完成和失败记录。
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    task_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )

    capability_code: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    endpoint: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    trace_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    request_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )

    response_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )

    duration_ms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai,
        nullable=False,
    )


class Dataset(Base):
    """数据集资源。数据集和模型使用独立的数据库表。"""

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100), default="通用", nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), default="business", nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ready", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_shanghai, nullable=False
    )


class Model(Base):
    """模型资源。模型和数据集使用独立的数据库表。"""

    __tablename__ = "models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100), default="通用", nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ready", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 模型管理页档案字段；旧库由启动迁移补齐。
    model_type: Mapped[str] = mapped_column(String(100), default="文本分类", nullable=False)
    source: Mapped[str] = mapped_column(String(50), default="self_developed", nullable=False)
    creator: Mapped[str] = mapped_column(String(100), default="系统管理员", nullable=False)
    service_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_shanghai, nullable=False
    )


class DatasetRecord(Base):
    """平台 MySQL 中保存的外部数据库原始数据行及其内容指纹。"""

    __tablename__ = "dataset_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)


class ModelVersion(Base):
    """模型版本记录，供模型管理页读取，不再由前端写死。"""
    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)


class ModelService(Base):
    __tablename__ = "model_services"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    service_type: Mapped[str] = mapped_column(String(30), default="local", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="running", nullable=False)
    endpoint: Mapped[str] = mapped_column(String(500), default="local://mock", nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    healthy: Mapped[bool | None] = mapped_column(nullable=True)


class ModelCall(Base):
    __tablename__ = "model_calls"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    service_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    original_output: Mapped[str] = mapped_column(Text, nullable=False)
    governed_output: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(30), nullable=False)
    reconstruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)


class TrainingTask(Base):
    """模型训练任务登记；实际训练由后续调度服务异步执行。"""

    __tablename__ = "training_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False, index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    model_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    base_version: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dataset_version: Mapped[str] = mapped_column(String(100), nullable=False)
    epochs: Mapped[int] = mapped_column(Integer, nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    learning_rate: Mapped[float] = mapped_column(Float, nullable=False)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False)
    target_version: Mapped[str] = mapped_column(String(100), nullable=False)
    # 训练方式同时用于模型工作台展示及合规留痕五要素中的“接口”。
    method: Mapped[str] = mapped_column(String(64), default="低秩适配微调", nullable=False)
    # 每轮训练日志和检查点由训练服务写入，供页面刷新后恢复曲线和产物列表。
    loss_history: Mapped[list[float]] = mapped_column(JSON, default=list, nullable=False)
    validation_loss_history: Mapped[list[float]] = mapped_column(JSON, default=list, nullable=False)
    checkpoints: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)


# 合规页面的结果不能只在后端进程或前端 demo 中保存。下列四张表以可筛选的
# 主字段配合 JSON 正文保存复杂审计结构，既支持当前页面的完整合同，也便于后续扩展。
class ComplianceAudit(Base):
    __tablename__ = "compliance_audits"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    capability_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    capture_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    review_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    review_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    execution_status: Mapped[str] = mapped_column(String(32), default="succeeded", nullable=False)
    compliance_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)


class ComplianceAlert(Base):
    __tablename__ = "compliance_alerts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)


class ComplianceEvidence(Base):
    __tablename__ = "compliance_evidences"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)


class ComplianceErrorComment(Base):
    """用户对合规错误的评论；error_id 由前端稳定地生成。"""

    __tablename__ = "compliance_error_comments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    error_id: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    author_name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False, index=True)


class ComplianceLineageEdge(Base):
    __tablename__ = "compliance_lineage_edges"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    from_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    from_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    to_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    to_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)


class Resource(Base):
    """旧的统一资源表，仅保留用于启动时迁移历史数据。"""

    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100), default="通用", nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ready", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_shanghai, nullable=False
    )


class Metric(Base):
    """
    保存指标目标，例如风险召回率和误报率。
    """

    __tablename__ = "metrics"

    metric_code: Mapped[str] = mapped_column(
        String(100),
        primary_key=True,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    category: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    target_value: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    unit: Mapped[str] = mapped_column(
        String(30),
        default="ratio",
        nullable=False,
    )

    description: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )


class EvaluationWorkspaceItem(Base):
    """评估工作区的任务、运行和导出资料。

    工作区数据的字段层级很深且会随指标定义扩展，正文使用 JSON 保存；
    type 和 item_id 保持可查询、可唯一定位，避免把前端回传的上下文留在内存。
    """

    __tablename__ = "evaluation_workspace_items"

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)
class EvaluationRecord(Base):
    """
    保存测试评估结果。
    """

    __tablename__ = "evaluation_records"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    task_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )

    metric_code: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    metric_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    value: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    target: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    passed: Mapped[bool] = mapped_column(
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai,
        nullable=False,
    )


class SystemUser(Base):
    """
    平台用户表。
    当前用于系统管理页面展示；后续接入登录鉴权时可继续扩展。
    """

    __tablename__ = "system_users"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    # 登录账号，例如 admin
    username: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
    )

    # 页面展示名称，例如 系统管理员
    display_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
    )

    # 角色名称，当前先直接保存文字；后续可改为关联 roles 表
    role: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="普通用户",
    )

    # active / disabled
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="active",
    )

    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai,
        nullable=False,
    )

    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
class SystemRole(Base):
    """
    系统角色与权限配置表。
    permissions 使用 JSON 数组保存该角色拥有的权限编码。
    """

    __tablename__ = "system_roles"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    # 机器可读的唯一角色编码，例如 super_admin
    role_code: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
    )

    # 页面展示名称，例如 超级管理员
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
    )

    # 示例：["user:read", "user:write", "dataset:read"]
    permissions: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )

    # active / disabled
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="active",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_shanghai,
        nullable=False,
    )


class EvaluationMetric(Base):
    __tablename__ = "evaluation_metrics"

    metric_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="enabled", nullable=False)
    configuration_status: Mapped[str] = mapped_column(String(30), default="published", nullable=False)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)


class EvaluationMetricRevision(Base):
    __tablename__ = "evaluation_metric_revisions"

    revision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    metric_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    metric_code: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    expected_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    configuration_status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EvaluationTask(Base):
    __tablename__ = "evaluation_tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    record_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    target_stage: Mapped[str] = mapped_column(String(30), default="final", nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    metric_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    judgment_status: Mapped[str] = mapped_column(String(30), default="not_evaluated", nullable=False)
    allowed_actions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    stage: Mapped[str] = mapped_column(String(50), default="frozen", nullable=False)
    trace_id: Mapped[str] = mapped_column(String(100), nullable=False)
    processed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)


class ComplianceRecord(Base):
    """合规审计、证据、告警和链路结果的持久化载体。"""

    __tablename__ = "compliance_records"

    record_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    capability_code: Mapped[str | None] = mapped_column(String(60), index=True, nullable=True)
    subject_type: Mapped[str | None] = mapped_column(String(60), index=True, nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="pending", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    record_id: Mapped[str] = mapped_column(String(64), nullable=False)
    test_no: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    retry_of: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_status: Mapped[str] = mapped_column(String(30), nullable=False)
    judgment_status: Mapped[str] = mapped_column(String(30), default="not_evaluated", nullable=False)
    algorithm_mode: Mapped[str] = mapped_column(String(30), default="mock", nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    metric_results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    allowed_actions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    integrity_state: Mapped[str] = mapped_column(String(30), default="incomplete", nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EvaluationEvent(Base):
    __tablename__ = "evaluation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    stage: Mapped[str] = mapped_column(String(50), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_shanghai, nullable=False)
