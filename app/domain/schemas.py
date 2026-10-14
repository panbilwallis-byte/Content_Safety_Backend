from typing import Any
from pydantic import BaseModel, Field, field_validator

from typing import Any
from pydantic import BaseModel, Field


class UpdateDatasetRequest(BaseModel):
    # PATCH 中所有字段都可选，只更新实际传入的字段
    name: str | None = Field(default=None, min_length=1)
    category: str | None = None
    source_type: str | None = None
    version: str | None = None
    status: str | None = None
    description: str | None = None

    # 更新 metadata_json 中的字段
    record_count: int | None = Field(default=None, ge=0)
    languages: list[str] | None = None
    modalities: list[str] | None = None

    # 允许一次性补充其他元数据
    metadata: dict[str, Any] | None = None
# 前端请求 POST /api/v1/tasks/execute 时的数据格式
class ExecuteTaskRequest(BaseModel):
    # 要调用的模拟能力，例如 semantic_risk
    capability_code: str

    # 任务名称，可不传
    name: str | None = None

    # 具体输入内容，例如 {"content": "这是一段待检测文本"}
    input: dict[str, Any] = Field(default_factory=dict)

    # 可选配置，例如 {"threshold": 0.8}
    config: dict[str, Any] = Field(default_factory=dict)



class CreateDatasetRequest(BaseModel):
    """
    前端创建数据集时需要提交的数据。
    """

    # 数据集名称，必填
    name: str

    # 数据来源类型，例如 business、internet、industry、synthetic
    source_type: str = "business"

    # 数据集说明，可不传
    description: str = ""

    # 样本数量，可不传
    record_count: int = 0

    # 数据集包含哪些语言，可不传
    languages: list[str] = Field(default_factory=list)

class ApiResponse(BaseModel):
    """
    所有接口统一返回的数据格式。
    """

    code: int
    message: str
    data: Any
    trace_id: str | None = None
    timestamp: str


class CreateModelRequest(BaseModel):
    """
    注册一个模型。
    """

    name: str

    model_type: str = "text_classification"
    source: str = "self_developed"
    status: str = "pending"
    service_url: str | None = None

    category: str = "内容审核"

    version: str = "v1.0.0"

    description: str | None = ""

    metadata: dict[str, Any] = Field(
        default_factory=dict
    )


class CreateTrainingTaskRequest(BaseModel):
    """登记模型训练任务，不触发实际训练。"""

    name: str = Field(min_length=1, max_length=80)
    model_id: str | int
    base_version: str = Field(min_length=1, max_length=100)
    dataset_id: str | int
    dataset_version: str = Field(min_length=1, max_length=100)
    epochs: int = Field(ge=1, le=100)
    learning_rate: float = Field(gt=0, le=1)
    batch_size: int = Field(ge=1, le=256)
    target_version: str = Field(min_length=1, max_length=100)
    method: str = Field(default="低秩适配微调", min_length=1, max_length=64)


class RegisterModelServiceRequest(BaseModel):
    """模型部署页登记推理服务的请求体。"""

    name: str = Field(min_length=1, max_length=80)
    model_id: str | int
    version: str = Field(min_length=1, max_length=100)
    type: str = Field(pattern="^(local|external)$")
    endpoint: str = Field(min_length=1, max_length=500)

    @field_validator("name", "version", "endpoint")
    @classmethod
    def strip_required_values(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        return value

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("服务地址必须使用 HTTP 或 HTTPS 协议")
        return value





