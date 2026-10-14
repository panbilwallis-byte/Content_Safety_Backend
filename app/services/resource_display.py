"""数据资源页面的演示展示数据规则。

随机值由资源 ID 稳定派生：新任务/新数据集会得到不同的合理值，
同一条已写入数据库的记录在刷新页面后不会无故跳变。
"""

from hashlib import sha256
from random import Random

SOURCE_RANGES_GB: dict[str, tuple[float, float]] = {
    "本地文件": (0.5, 2),
    "网络采集": (260, 820),
    "API接口": (70, 280),
    "对象存储": (180, 650),
    "数据库": (120, 480),
}
SOURCE_NAMES = tuple(SOURCE_RANGES_GB)
SOURCE_ALIASES = {
    "本地": "本地文件",
    "file": "本地文件",
    "database": "数据库",
    "api": "API接口",
    "object_storage": "对象存储",
    "web": "网络采集",
    "queue": "消息队列",
}


def _random_for(key: str) -> Random:
    seed = int.from_bytes(sha256(key.encode("utf-8")).digest()[:8], "big")
    return Random(seed)


def normalize_source_name(value: object | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or cleaned == "未填写来源":
        return None
    return SOURCE_ALIASES.get(cleaned.lower(), cleaned)


def task_display_defaults(task_id: str) -> tuple[str, float]:
    randomizer = _random_for(f"task:{task_id}")
    source_name = SOURCE_NAMES[randomizer.randrange(len(SOURCE_NAMES))]
    low, high = SOURCE_RANGES_GB[source_name]
    return source_name, round(randomizer.uniform(low, high), 1)


def task_ingest_statistics(task_id: str, storage_gb: float) -> tuple[int, int, int]:
    """按接入容量生成合理的成功、重复和异常记录数。"""
    randomizer = _random_for(f"ingest-statistics:{task_id}")
    success_count = max(1000, int(storage_gb * randomizer.uniform(28, 52)))
    duplicate_count = max(1, int(success_count * randomizer.uniform(0.005, 0.025)))
    anomaly_count = max(1, int(success_count * randomizer.uniform(0.001, 0.008)))
    return success_count, duplicate_count, anomaly_count


def task_dataset_default(task_id: str) -> str:
    datasets = (
        "内容安全多模态数据集",
        "社交媒体中文语料库",
        "跨文化交流多语种数据集",
        "新闻资讯数据集",
        "短视频风险样本集",
        "政务服务问答数据集",
    )
    return datasets[_random_for(f"task-dataset:{task_id}").randrange(len(datasets))]


def dataset_storage_default(dataset_key: str) -> float:
    """生成 0.3–2.0 TB 的单数据集展示容量。"""
    return round(_random_for(f"dataset:{dataset_key}").uniform(320, 2000), 1)


def dataset_display_defaults(dataset_key: str, storage_gb: float | None = None) -> tuple[float, int, float]:
    """返回数据集页面使用的存储量、记录数和质量分。"""
    randomizer = _random_for(f"dataset-display:{dataset_key}")
    storage = round(storage_gb or randomizer.uniform(320, 2000), 1)
    records = max(800, int(storage * randomizer.uniform(18_000, 42_000)))
    quality_score = round(randomizer.uniform(86, 98.5), 1)
    return storage, records, quality_score


def task_version_default(task_id: str, capability_code: str) -> str:
    """为未绑定数据集或模型的演示任务提供稳定、可追溯的版本号。"""
    randomizer = _random_for(f"version:{capability_code}:{task_id}")
    return f"v1.{randomizer.randint(0, 9)}.{randomizer.randint(0, 9)}"


def evaluation_version_default(record_id: int | str, task_id: str) -> str:
    """为每次评估执行生成独立且稳定的语义化版本。"""
    randomizer = _random_for(f"evaluation:{record_id}:{task_id}")
    return f"v{randomizer.randint(1, 3)}.{randomizer.randint(0, 9)}.{randomizer.randint(0, 9)}"


def model_version_default(model_key: int | str, model_name: str) -> str:
    """为演示模型生成不同且稳定的语义化版本。"""
    randomizer = _random_for(f"model:{model_key}:{model_name}")
    return f"v{randomizer.randint(1, 3)}.{randomizer.randint(0, 9)}.{randomizer.randint(0, 9)}"


def evaluation_name_default(record_id: int | str, metric_name: str) -> str:
    """为每条评估记录生成唯一、可读的测试名称。"""
    variants = (
        "基线回归测试",
        "高风险样本测试",
        "跨场景一致性测试",
        "多模态覆盖测试",
        "边界样本验证",
        "稳定性复测",
        "线上回放测试",
    )
    randomizer = _random_for(f"evaluation-name:{record_id}:{metric_name}")
    # 7919 与 90000 互质，保证常规数据库自增 ID 在 90000 条范围内映射为不重复的五位数。
    numeric_id = int(record_id)
    suffix = 10000 + (numeric_id * 7919) % 90000
    return f"{metric_name}·{variants[randomizer.randrange(len(variants))]} #{suffix:05d}"


RISK_ALERT_NAME_ALIASES = {
    "semantic_risk test": "语义风险识别回归测试",
    "frontend proxy task": "前端链路风险验证",
    "automated verification": "自动化风险识别校验",
}


def risk_alert_display_name(task_name: str) -> str:
    """风险识别列表统一使用可读的中文业务名称。"""
    normalized = task_name.strip()
    return RISK_ALERT_NAME_ALIASES.get(normalized.lower(), normalized)
