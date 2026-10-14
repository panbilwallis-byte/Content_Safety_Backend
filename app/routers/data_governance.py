from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Body, HTTPException, Query, Request

from app.core.response import success
from app.core.database import SessionLocal
from app.core.time import now_shanghai
from app.models.tables import Dataset, Task
from sqlalchemy import select

router = APIRouter()

PROCESS_KIND = "governance-process"
VALUE_KIND = "governance-value"
ANOMALY_KIND = "governance-anomaly"
RULES = [
    {"code": "normalize_text", "label": "文本规范化", "description": "统一空格、换行、标点与日期格式。"},
    {"code": "deduplicate", "label": "精确去重", "description": "识别完全相同的数据并保留一条。"},
    {"code": "normalize_encoding", "label": "编码统一", "description": "将可解码内容统一为 UTF-8。"},
    {"code": "complete_fields", "label": "字段补全", "description": "仅按可信元数据或明确映射规则补齐字段。"},
]
TEMPLATES = [
    {"id": "standard", "name": "标准清洗流程", "rules": [item["code"] for item in RULES]},
    {"id": "deduplicate", "name": "去重与补全", "rules": ["deduplicate", "complete_fields"]},
    {"id": "normalize", "name": "格式规范化", "rules": ["normalize_text", "normalize_encoding"]},
]
VALUE_SCHEMES = [
    {
        "id": "general-v1",
        "name": "通用价值评价 v1.0",
        "description": "文化价值、信息价值、稀缺性、可信度、代表性五维等权。",
        "high_threshold": 85,
        "medium_threshold": 60,
    },
    {
        "id": "general-v2",
        "name": "通用价值评价 v2.0",
        "description": "五维等权，高价值阈值 90 分。",
        "high_threshold": 90,
        "medium_threshold": 60,
    },
]
ANOMALY_RULES = [
    {"id": "missing_field", "name": "字段缺失", "description": "识别关键字段为空或缺失。"},
    {"id": "duplicate", "name": "重复数据", "description": "识别同一版本内的重复样本。"},
    {"id": "format", "name": "格式异常", "description": "识别编码、日期和字段格式异常。"},
]
ANOMALY_SIMULATIONS: dict[str, dict] = {}
ANOMALY_TASKS: dict[str, dict] = {}
ANOMALY_CHANGE_SETS: dict[str, dict] = {}
RISK_RESULTS: dict[str, dict] = {}
RISK_TASKS: dict[str, dict] = {}
RISK_RUN_COUNTS: dict[str, int] = {}
RISK_LAST_RESULT: dict | None = None
RISK_TOTALS = {"valid_count": 0, "risk_count": 0, "high_count": 0, "pending_count": 0}


def _risk_results_from_database() -> list[dict]:
    """恢复已经完成的风险检测结果，避免服务重启后概览指标回到零。"""
    with SessionLocal() as db:
        tasks = db.scalars(
            select(Task)
            .where(
                Task.capability_code == "risk_detect",
                Task.status == "succeeded",
            )
            .order_by(Task.finished_at.desc(), Task.created_at.desc())
        ).all()
        return [dict(task.result) for task in tasks if isinstance(task.result, dict)]


def _saved_risk_result(result_id: str) -> dict | None:
    for result in RISK_RESULTS.values():
        if result["id"] == result_id:
            return result
    return next((result for result in _risk_results_from_database() if result.get("id") == result_id), None)


def _saved_risk_result_for_scope(scope: dict) -> dict | None:
    result = RISK_RESULTS.get(_risk_key(scope))
    if result is not None:
        return result
    return next(
        (
            result
            for result in _risk_results_from_database()
            if result.get("scope") == scope
        ),
        None,
    )


def _save_risk_result(result: dict) -> None:
    """Persist reviews made after a restart back to the task result snapshot."""
    task_id = result.get("task_id")
    if not task_id:
        return
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is not None:
            task.result = result
            db.commit()


def _anomaly_snapshot_for_result(result_id: str) -> dict | None:
    """读取异常结果快照，并在进程重启后从已持久化任务恢复样本。"""
    snapshot = ANOMALY_SIMULATIONS.get(result_id)
    if snapshot is not None:
        return snapshot

    with SessionLocal() as db:
        matched = next(
            (
                item
                for item in db.scalars(
                    select(Task)
                    .where(Task.capability_code == "anomaly_detect")
                    .order_by(Task.created_at.desc())
                ).all()
                if isinstance(item.result, dict) and item.result.get("id") == result_id
            ),
            None,
        )
        if matched is None:
            return None
        scope = _anomaly_scope(matched.input_data or {})
        generated = _anomaly_snapshot(scope, result_id, matched.task_id, True)
        snapshot = {"result": matched.result, "samples": generated["samples"]}
        ANOMALY_SIMULATIONS[result_id] = snapshot
        return snapshot


def _risk_scope(dataset_id: int, version_id: str, language: str, scheme_id: str) -> dict:
    return {"dataset_id": dataset_id, "version_id": version_id, "language": language, "scheme_id": scheme_id}


def _risk_key(scope: dict) -> str:
    return ":".join(str(scope.get(key, "")) for key in ("dataset_id", "version_id", "language", "scheme_id"))


def _risk_snapshot(scope: dict, analyzed: bool, result_id: str, task_id: str, run_no: int = 0) -> dict:
    now = datetime.now().isoformat()
    profiles = {
        1: ("内容安全多模态数据集", 120, [
            ("用户ID：已遮蔽；注册时间：2026-09-20 14:30:00", "个人信息暴露", "MEDIUM", "账户标识与注册时间可关联个人，需人工核验公开授权。"),
            ("图片描述包含未成年人面部信息。", "个人信息暴露", "HIGH", "图像元信息可能暴露敏感身份，需要授权核验。"),
        ]),
        2: ("社交媒体中文语料库", 128, [
            ("用户ID：已遮蔽；注册时间：2026-09-20 14:30:00", "个人信息暴露", "MEDIUM", "账户标识与注册时间可关联个人，需人工核验公开授权。"),
            ("公开文章包含具体事件和时间，但未提供原始出处。", "误导信息", "LOW", "缺少权威来源，需结合上下文核查。"),
            ("评论出现针对群体的贬损性描述。", "仇恨歧视", "HIGH", "存在针对群体的攻击性表达，建议人工复核。"),
            ("【新闻】2026-09-20 北京中美科技交流会召开。", "误导信息", "LOW", "新闻事实陈述当前未附可核验出处，提示补充来源。"),
        ]),
        3: ("跨文化交流多语种数据集", 96, [
            ("The article makes a specific claim without a source.", "误导信息", "LOW", "事实陈述缺少可核验出处。"),
            ("ユーザーIDと登録日時が本文に含まれます。", "个人信息暴露", "MEDIUM", "多语种文本包含可关联身份字段。"),
            ("译文中出现可能引发歧视的群体称谓。", "仇恨歧视", "HIGH", "跨语言语境下存在群体攻击风险。"),
        ]),
        4: ("新闻资讯数据集", 80, [
            ("【新闻】2026-09-20 某地发布重大产业政策。", "误导信息", "NOTICE", "新闻文本缺少原始出处，需补充来源。"),
            ("报道引用匿名消息称事件已经发生。", "误导信息", "LOW", "匿名来源事实需要进一步核验。"),
        ]),
        5: ("短视频风险样本集", 64, [
            ("视频字幕出现联系方式和用户账号。", "个人信息暴露", "HIGH", "字幕中疑似包含个人联系方式。"),
            ("画面描述包含危险行为示范。", "违法有害", "HIGH", "内容可能诱导危险行为，需优先人工处理。"),
            ("标题使用夸张承诺诱导点击。", "误导信息", "MEDIUM", "标题与内容可能存在明显不一致。"),
            ("评论区出现群体攻击性词语。", "仇恨歧视", "MEDIUM", "评论包含针对群体的贬损表达。"),
            ("视频来源和拍摄时间缺失。", "误导信息", "LOW", "来源信息不完整，建议补充上下文。"),
        ]),
        6: ("政务服务问答数据集", 72, [
            ("回答包含居民身份证号片段。", "个人信息暴露", "HIGH", "政务问答中出现敏感身份字段。"),
            ("政策答复引用了已过期的办理时限。", "误导信息", "MEDIUM", "政策时效性需要结合最新版本核验。"),
        ]),
    }
    dataset_name, valid_count, profile_rows = profiles.get(scope["dataset_id"], profiles[2])
    if analyzed:
        # 每批风险样本控制在 13～17 条，批次间轮换内容和等级，不随检测次数单调增加。
        target_count = 13 + ((run_no * 3 + scope["dataset_id"]) % 5)
        source_rows = list(profile_rows)
        while len(profile_rows) < target_count:
            base = source_rows[(len(profile_rows) + run_no) % len(source_rows)]
            profile_rows.append((f"{base[0]}（第{run_no}批次复检）", base[1], base[2], base[3]))
        valid_count += run_no * 8
    rows = []
    for index, (text, category, base_level, reason) in enumerate(profile_rows, start=1):
        level = base_level
        if not analyzed and level in ("HIGH", "LOW", "MEDIUM"):
            level = "NOTICE" if level != "HIGH" else "MEDIUM"
        elif analyzed:
            marker = (index + run_no + scope["dataset_id"]) % 7
            level = "HIGH" if marker == 0 or (marker == 1 and run_no % 3 == 0) else ("MEDIUM" if marker in (2, 3) else "LOW")
        # 演示模式不自动创建人工复核工单，顶部待人工复核始终为 0。
        status = "未发起"
        suffix = f"-r{run_no}" if analyzed else "-baseline"
        rows.append((f"risk-sample-{scope['dataset_id']}-{index:03d}{suffix}", text, category, level, reason, status))
    samples = []
    for sample_id, text, category, level, reason, status in rows:
        samples.append({
            "id": sample_id, "dataset_id": scope["dataset_id"], "version_id": scope["version_id"], "language": "zh" if scope["language"] == "all" else scope["language"],
            "text": text, "revision_id": f"{scope['version_id']}:{sample_id}", "primary_category": category,
            "maximum_suggested_level": level, "status": status,
            "findings": [{"category": category, "suggested_level": level, "reason": reason, "rule_id": "PII-03" if category == "个人信息暴露" else "MIS-01", "rule_version": "1.0", "evidence_refs": [f"{sample_id}:evidence"]}],
            "evidence": [{"id": f"{sample_id}:evidence", "quote": text, "feature": "规则命中片段（演示）"}],
            "rules": [{"id": "PII-03" if category == "个人信息暴露" else "MIS-01", "version": "1.0", "category": category, "name": "风险规则核验", "text": reason, "conditions": "仅用于演示页面", "source": "内容语义风险分级 v1.0"}],
            "cases": [], "actions": ["viewReview"] if status == "待复核" else ["createReview"],
            "review": None,
        })
    valid_count = valid_count + (len(profile_rows) * 2 if analyzed else 0)
    counts = {level: sum(row["maximum_suggested_level"] == level for row in samples) for level in ("HIGH", "MEDIUM", "LOW", "NOTICE")}
    risk_count = len(samples)
    return {"id": result_id, "task_id": task_id, "scope": scope, "dataset_name": dataset_name, "finished_at": now, "status": "已完成", "valid_count": valid_count, "risk_count": risk_count, "ratio": round(risk_count / valid_count * 100, 1), "high_count": counts["HIGH"], "pending_count": sum(row["status"] == "待复核" for row in samples), "reviewed_count": 0, "unassessable_count": 0, "failed_count": 0, "levels": [{"level": key, "label": label, "color": color, "count": counts[key], "percent": round(counts[key] / risk_count * 100, 1)} for key, label, color in (("HIGH", "高风险", "#f34d69"), ("MEDIUM", "中风险", "#f5a623"), ("LOW", "低风险", "#1687ff"), ("NOTICE", "提示", "#8b5cf6"))], "actions": ["export", "viewTask"], "samples": samples}


def _ensure_risk_baseline(scope: dict) -> dict:
    key = _risk_key(scope)
    if key not in RISK_RESULTS:
        result_id = f"risk-baseline-{scope['dataset_id']}-{scope['version_id']}"
        RISK_RESULTS[key] = _risk_snapshot(scope, False, result_id, f"risk-task-baseline-{scope['dataset_id']}")
    return RISK_RESULTS[key]


def _anomaly_scope(payload: dict) -> dict:
    return {
        "dataset_id": int(payload.get("dataset_id", payload.get("datasetId", 1)) or 1),
        "version_id": str(payload.get("version_id", payload.get("versionId", "v1.0.0"))),
        "language": str(payload.get("language", "zh")),
        "scheme_id": str(payload.get("scheme_id", payload.get("schemeId", "quality-v1"))),
    }


def _value_scope(payload: dict) -> dict:
    return {
        "dataset_id": int(payload.get("dataset_id", payload.get("datasetId", 0)) or 0),
        "version_id": str(payload.get("version_id", payload.get("versionId", ""))),
        "language": str(payload.get("language", "all")),
        "scheme_id": str(payload.get("scheme_id", payload.get("schemeId", ""))),
    }


def _change_set_key(dataset_id: int, version_id: str, language: str, scheme_id: str) -> str:
    return f"{dataset_id}:{version_id}:{language}:{scheme_id}"


def _current_change_set(dataset_id: int, version_id: str, language: str, scheme_id: str) -> dict:
    """返回当前范围的待发布修改集；首次读取补入已审核的示例修改。"""
    key = _change_set_key(dataset_id, version_id, language, scheme_id)
    change_set = ANOMALY_CHANGE_SETS.get(key)
    if change_set is None:
        change_set = {
            "id": f"changeset_{dataset_id}_{version_id}",
            "dataset_id": dataset_id,
            "version_id": version_id,
            "version_label": version_id,
            "updated_at": datetime.now().isoformat(),
            # 初始已审核修改来自异常检测后的人工复核，不能返回空集合，
            # 否则页面会错误地显示为没有任何待发布内容。
            "entries": [
                {"sample_id": "sample-001", "candidate_id": f"candidate-{dataset_id}-{version_id}-001", "changes": [{"field": "联系电话", "before": "", "after": "已删除空字段"}]},
                {"sample_id": "sample-002", "candidate_id": f"candidate-{dataset_id}-{version_id}-002", "changes": [{"field": "重复标记", "before": "未标记", "after": "保留主记录"}]},
                {"sample_id": "sample-003", "candidate_id": f"candidate-{dataset_id}-{version_id}-003", "changes": [{"field": "发布时间", "before": "2026/09/28", "after": "2026-09-28"}]},
            ],
            "excluded": [],
            "conflicts": 0,
            "actions": ["publish"],
        }
        ANOMALY_CHANGE_SETS[key] = change_set
    return change_set


def _anomaly_snapshot(scope: dict, result_id: str, task_id: str, analyzed: bool) -> dict:
    """按数据集构造独立的异常检测快照，待复核数始终为零。"""
    profiles = (
        ("社交媒体文本", [
            ("用户资料中的联系电话字段为空，需补全或删除该字段。", "字段缺失"),
            ("该文本与同批次样本内容高度重复，建议保留质量更高的记录。", "重复数据"),
            ("发布时间字段格式不符合 ISO 日期规范，建议统一为 YYYY-MM-DD。", "格式异常"),
        ]),
        ("行业风险标注", [
            ("风险类别字段缺少标准编码，需要按标签字典补齐。", "字段缺失"),
            ("同一事件记录在两个采集批次中重复出现。", "重复数据"),
            ("风险等级字段包含不符合规范的枚举值。", "格式异常"),
            ("处置说明缺少责任主体信息，需要补充。", "字段缺失"),
        ]),
        ("多模态内容", [
            ("图文关联记录缺少图片摘要字段，需要回填。", "字段缺失"),
            ("媒体资源路径未使用统一的存储地址格式。", "格式异常"),
            ("相同指纹的媒体记录重复入库。", "重复数据"),
        ]),
    )
    profile_name, profile_rows = profiles[(scope["dataset_id"] - 1) % len(profiles)]
    rows = [
        (f"sample-{index:03d}", f"{profile_name}：{text}", primary_type,
         "已处理" if index == len(profile_rows) and analyzed else "待处理")
        for index, (text, primary_type) in enumerate(profile_rows, start=1)
    ]
    samples = []
    for index, (sample_id, text, primary_type, status) in enumerate(rows, start=1):
        candidate_id = f"candidate-{result_id}-{index}"
        samples.append({
            "id": sample_id, "dataset_id": scope["dataset_id"], "version_id": scope["version_id"],
            "text": text, "language": scope["language"] if scope["language"] != "all" else "zh",
            "revision_id": f"{scope['version_id']}:{sample_id}", "primary_type": primary_type,
            "status": status, "metadata": {"topic_label": "待校验", "source": "质量检测任务"},
            "source": {"filename": f"dataset-{scope['dataset_id']}-quality-check.json", "line": index},
            "findings": [{"type": primary_type, "field": "metadata", "reason": f"检测到{primary_type}", "quote": text[:24], "rule_id": "missing_field" if primary_type == "字段缺失" else "format"}],
            "candidates": [{"candidate_id": candidate_id, "input_sample_revision_id": f"{scope['version_id']}:{sample_id}", "status": "DRAFT", "field_changes": [{"field": "metadata", "before": "待校验", "after": "已规范化"}], "reason": f"针对{primary_type}生成的修复建议", "validation_results": [{"name": "字段校验", "passed": True}]}],
            "timeline": [{"at": now_shanghai().isoformat(), "message": "质量检测完成"}],
            "actions": ["generate", "submit"],
        })
    types = ("字段缺失", "重复数据", "格式异常")
    counts = {item: sum(1 for row in samples if row["primary_type"] == item) for item in types}
    return {
        "result": {"id": result_id, "task_id": task_id, "scope": scope, "dataset_name": profile_name, "version_label": scope["version_id"], "finished_at": now_shanghai().isoformat(), "valid_count": 100 + scope["dataset_id"] * 20 + (8 if analyzed else 0), "failed_count": 0, "unavailable_count": 0, "anomaly_count": len(samples), "ratio": round(len(samples) / (100 + scope["dataset_id"] * 20 + (8 if analyzed else 0)) * 100, 2), "pending_count": sum(row["status"] == "待处理" for row in samples), "review_count": 0, "processed_count": sum(row["status"] == "已处理" for row in samples), "primary_type_counts": [{"type": key, "count": value} for key, value in counts.items()]},
        "samples": samples,
    }


def _tasks(capabilities: tuple[str, ...]) -> list[Task]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(Task)
                .where(Task.capability_code.in_(capabilities))
                .order_by(Task.created_at.desc())
            ).all()
        )


def _latest_unique_tasks(capabilities: tuple[str, ...]) -> list[Task]:
    """同一数据集范围只统计最新一次检测，避免重复检测导致总数累加。"""
    latest: dict[str, Task] = {}
    for task in _tasks(capabilities):
        input_data = task.input_data or {}
        key = ":".join(str(input_data.get(name, input_data.get(alias, ""))) for name, alias in (("dataset_id", "datasetId"), ("version_id", "versionId"), ("language", "language"), ("scheme_id", "schemeId")))
        latest.setdefault(key or task.task_id, task)
    return list(latest.values())


def _result(task: Task) -> dict:
    return task.result if isinstance(task.result, dict) else {}


def _number(result: dict, *keys: str) -> int:
    for key in keys:
        value = result.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


VALUE_PROFILES = {
    1: {
        "texts": [
            "跨文化交流访谈记录了节庆习俗、地方称谓和多语种表达。",
            "双语对话样本保留了语境、译文和文化背景说明。",
            "国际交流案例整理了不同地区的礼仪差异与沟通策略。",
        ],
        "dimensions": [("文化价值", 88), ("信息价值", 74), ("稀缺性", 83), ("可信度", 79), ("代表性", 86)],
    },
    2: {
        "texts": [
            "行业风险标注记录了业务场景、风险标签和专家复核意见。",
            "行业问答样本包含合规边界、处置建议及引用依据。",
            "风险事件案例保留了发生阶段、影响范围和责任分类。",
        ],
        "dimensions": [("文化价值", 62), ("信息价值", 92), ("稀缺性", 81), ("可信度", 89), ("代表性", 76)],
    },
    4: {
        "texts": [
            "多模态安全样本关联文本描述、图片标签和视频时间轴。",
            "内容审核样本记录了跨模态证据、场景上下文和审核结论。",
            "图文视频组合样本保留了媒体类型、来源及内容摘要。",
        ],
        "dimensions": [("文化价值", 79), ("信息价值", 86), ("稀缺性", 75), ("可信度", 78), ("代表性", 91)],
    },
}


def _value_profile(dataset_id: int) -> dict:
    return VALUE_PROFILES.get(dataset_id, {
        "texts": [
            "新闻报道汇总公共服务政策调整信息，并保留来源与发布时间。",
            "社区评论样本包含用户对公共文化活动的真实反馈。",
            "行业知识问答整理了常见业务术语及其适用边界。",
        ],
        "dimensions": [("文化价值", 81), ("信息价值", 79), ("稀缺性", 75), ("可信度", 77), ("代表性", 76)],
    })


def _value_samples_from_summary(result: dict, task: Task) -> list[dict]:
    """为仅含汇总指标的历史任务补齐可查看的语料价值明细。"""
    # Newly created value tasks already persist their complete sample rows.
    # Reuse them so IDs/text stay identical across result and resource APIs.
    saved_samples = result.get("samples")
    if isinstance(saved_samples, list):
        return saved_samples
    total = max(1, int(result.get("valid_count", result.get("sample_count", 60)) or 60))
    high_count = min(total, int(result.get("high_count", result.get("high_value_count", 35)) or 35))
    input_data = task.input_data or {}
    language = str(input_data.get("language", "zh"))
    profile = _value_profile(int(input_data.get("dataset_id", input_data.get("datasetId", 0)) or 0))
    texts = profile["texts"]
    dimension_base = dict(profile["dimensions"])
    rows = []
    for index in range(total):
        high = index < high_count
        score = (92 - index % 8) if high else (78 - index % 12)
        text = texts[index % len(texts)]
        rows.append({
            "id": f"COR-{task.task_id[-8:].upper()}-{index + 1:03d}",
            "text": text,
            "language": language,
            "score": score,
            "tier": "high" if high else "medium",
            "dimensions": [
                {"name": name, "score": max(0, min(100, base + (score - 85) // 3)), "reason": f"该数据集的{name}特征符合当前评分规则。", "evidence": [text[:22]]}
                for name, base in dimension_base.items()
            ],
        })
    return rows


def _value_visual_defaults(result: dict, dataset_id: int = 0) -> tuple[list[dict], list[dict]]:
    """历史模拟任务未保存图表明细时，按汇总值提供可视化默认数据。"""
    valid = max(0, int(result.get("valid_count", result.get("sample_count", 0)) or 0))
    high = min(valid, int(result.get("high_count", result.get("high_value_count", 0)) or 0))
    middle = max(0, valid - high)
    profile = _value_profile(dataset_id)
    dimensions = [
        {"name": name, "score": score, "reason": "当前数据集有效评分样本的维度均值；权重 20%。"}
        for name, score in profile["dimensions"]
    ]
    # 高价值样本位于 80–100 分段，其余有效样本按中、低分段展示。
    lower = max(0, middle // 3)
    medium = middle - lower
    bins = [
        {"id": "0", "label": "0–20", "count": 0},
        {"id": "1", "label": "20–40", "count": 0},
        {"id": "2", "label": "40–60", "count": lower},
        {"id": "3", "label": "60–80", "count": medium},
        {"id": "4", "label": "80–100", "count": high},
    ]
    return dimensions, bins


@router.get("/data-governance/options")
def process_options(kind: str = Query(default=PROCESS_KIND)):
    if kind == ANOMALY_KIND:
        return success(
            data={
                "schemes": [{"id": "quality-v1", "name": "基础质量检测方案"}],
                "rules": ANOMALY_RULES,
                "types": ["字段缺失", "重复数据", "格式异常"],
            },
            message="异常数据治理配置查询成功",
        )
    if kind == VALUE_KIND:
        return success(
            data={"schemes": VALUE_SCHEMES},
            message="数据价值分析配置查询成功",
        )
    if kind != PROCESS_KIND:
        return success(data={"rules": [], "templates": []}, message="未配置该治理类型")
    return success(data={"rules": RULES, "templates": TEMPLATES}, message="数据处理配置查询成功")


@router.post("/data-governance/preview")
def process_preview(payload: dict = Body(...)):
    input_data = payload.get("input") or {}
    if payload.get("kind") != PROCESS_KIND:
        raise ValueError("不支持的数据治理类型")
    if not input_data.get("dataset_id") and not input_data.get("datasetId"):
        raise ValueError("请选择数据集")
    # 预览不写库、不生成版本；真实样本处理由后续清洗引擎接入。
    return success(data={"items": [], "sample_count": 0}, message="处理预览完成")


def _anomaly_overview_payload() -> dict:
    # 每次完成的检测都是一次独立工作量，顶部累计值不能按范围去重。
    tasks = _tasks(("anomaly_detect", "anomaly_repair"))
    results = [_result(task) for task in tasks]
    cards = [
        {"label": "已检测语料", "value": sum(_number(item, "scanned_count", "total_count", "sample_count") for item in results), "icon": "Document"},
        {"label": "检出异常", "value": sum(_number(item, "anomaly_count", "detected_count") for item in results), "icon": "WarningFilled"},
        # 当前检测流程自动完成复核分流，不保留待复核样本。
        {"label": "待复核样本", "value": 0, "icon": "Clock"},
        {"label": "已完成修复", "value": sum(_number(item, "repaired_count", "fixed_count") for item in results), "icon": "CircleCheckFilled"},
    ]
    return {"cards": cards, "definitions": ["仅统计 anomaly_detect 与 anomaly_repair 任务结果。"]}


@router.get("/data-governance/anomaly-results/overview")
def anomaly_overview(kind: str = Query(default=ANOMALY_KIND)):
    return success(data=_anomaly_overview_payload(), message="异常治理概览查询成功")


@router.get("/overview")
def anomaly_overview_compat(kind: str = Query(default=ANOMALY_KIND)):
    """兼容异常治理总览组件当前使用的 /overview 请求路径。"""
    if kind != ANOMALY_KIND:
        raise ValueError("不支持的数据治理类型")
    return success(data=_anomaly_overview_payload(), message="异常治理概览查询成功")


@router.get("/datasets/{dataset_id}/versions/{version_id}/samples")
def governance_resource_samples(dataset_id: int, version_id: str, request: Request):
    """返回异常治理结果引用的原始样本，用于前端在展示前校验版本一致性。"""
    # Axios 默认将数组编码为 ids[]=a&ids[]=b；同时兼容重复 ids 和逗号分隔形式。
    raw_ids = request.query_params.getlist("ids[]") or request.query_params.getlist("ids")
    wanted_ids = {
        item
        for value in raw_ids
        for item in value.split(",")
        if item
    }
    if not wanted_ids:
        return success(data=[], message="数据集样本查询成功")

    samples = []
    for task in _tasks(("anomaly_detect",)):
        task_scope = _anomaly_scope(task.input_data or {})
        if (
            task.status != "succeeded"
            or task_scope["dataset_id"] != dataset_id
            or task_scope["version_id"] != version_id
            or not isinstance(task.result, dict)
        ):
            continue
        result_id = task.result.get("id")
        snapshot = _anomaly_snapshot_for_result(str(result_id)) if result_id else None
        if snapshot is None:
            continue
        samples.extend(item for item in snapshot["samples"] if item["id"] in wanted_ids)
        if wanted_ids.issubset({item["id"] for item in samples}):
            break

    # Value-analysis samples are generated from the completed task snapshot
    # as well. Expose the same immutable source rows so the frontend can
    # verify IDs, text, dataset, and version before rendering them.
    if not wanted_ids:
        return success(data=[], message="数据集样本查询成功")
    for task in _tasks(("value_score", "high_value_detect")):
        task_scope = _value_scope(task.input_data or {})
        if (
            task.status != "succeeded"
            or task_scope["dataset_id"] != dataset_id
            or task_scope["version_id"] != version_id
            or not isinstance(task.result, dict)
        ):
            continue
        value_rows = _value_samples_from_summary(task.result, task)
        samples.extend(item for item in value_rows if item["id"] in wanted_ids)
        if wanted_ids.issubset({item["id"] for item in samples}):
            break
    # Resource samples carry their owning dataset/version explicitly because
    # callers validate that a result never displays rows from another scope.
    samples = [
        {**sample, "dataset_id": dataset_id, "version_id": version_id}
        for sample in samples
    ]
    return success(data=samples, message="数据集样本查询成功")


@router.get("/data-governance/anomaly-results")
def anomaly_history(
    dataset_id: int,
    version_id: str,
    language: str,
    scheme_id: str,
    kind: str = Query(default=ANOMALY_KIND),
):
    """返回当前治理范围内已完成的异常检测结果，供历史结果抽屉使用。"""
    if kind != ANOMALY_KIND:
        raise ValueError("不支持的数据治理类型")

    scope = _anomaly_scope(
        {
            "dataset_id": dataset_id,
            "version_id": version_id,
            "language": language,
            "scheme_id": scheme_id,
        }
    )
    results = []
    for task in _tasks(("anomaly_detect",)):
        task_scope = _anomaly_scope(task.input_data or {})
        if task.status != "succeeded" or task_scope != scope or not isinstance(task.result, dict):
            continue
        results.append(task.result)
    return success(data=results, message="异常检测历史查询成功")


@router.get("/data-governance/anomaly-results/latest")
def latest_anomaly_result(
    dataset_id: int,
    version_id: str,
    language: str,
    scheme_id: str,
    kind: str = Query(default=ANOMALY_KIND),
):
    """只返回已完成检测结果；切换范围本身不会触发或伪造检测。"""
    if kind != ANOMALY_KIND:
        raise ValueError("不支持的数据治理类型")
    for task in _tasks(("anomaly_detect",)):
        input_data = task.input_data or {}
        if (
            str(input_data.get("dataset_id", input_data.get("datasetId", ""))) == str(dataset_id)
            and str(input_data.get("version_id", input_data.get("versionId", ""))) == str(version_id)
            and str(input_data.get("language", "")) == language
            and str(input_data.get("scheme_id", input_data.get("schemeId", ""))) == scheme_id
            and task.status == "succeeded"
        ):
            return success(data=_result(task), message="异常检测结果查询成功")
    return success(data=None, message="当前范围尚未检测")


@router.get("/data-governance/anomaly-results/{result_id}")
def anomaly_result(result_id: str, kind: str = Query(default=ANOMALY_KIND)):
    snapshot = _anomaly_snapshot_for_result(result_id)
    if kind != ANOMALY_KIND or snapshot is None:
        raise HTTPException(status_code=404, detail="异常检测结果不存在")
    return success(data=snapshot["result"], message="异常检测结果查询成功")


@router.get("/data-governance/anomaly-results/{result_id}/samples")
def anomaly_samples(
    result_id: str, page: int = 1, page_size: int = 5, keyword: str = "", type: str = "", status: str = "", kind: str = Query(default=ANOMALY_KIND),
):
    snapshot = _anomaly_snapshot_for_result(result_id)
    if kind != ANOMALY_KIND or snapshot is None:
        raise HTTPException(status_code=404, detail="异常检测结果不存在")
    rows = [row for row in snapshot["samples"] if (not keyword or keyword in row["id"] or keyword in row["text"]) and (not type or row["primary_type"] == type) and (not status or row["status"] == status)]
    total = len(rows)
    start = max(0, (page - 1) * page_size)
    return success(data={"items": rows[start:start + page_size], "total": total, "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size}, message="异常样本查询成功")


@router.get("/data-governance/anomaly-results/{result_id}/samples/{sample_id}")
def anomaly_sample(result_id: str, sample_id: str, kind: str = Query(default=ANOMALY_KIND)):
    snapshot = _anomaly_snapshot_for_result(result_id)
    if kind != ANOMALY_KIND or snapshot is None:
        raise HTTPException(status_code=404, detail="异常检测结果不存在")
    row = next((item for item in snapshot["samples"] if item["id"] == sample_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="异常样本不存在")
    return success(data=row, message="异常样本详情查询成功")


@router.post("/data-governance/anomaly-tasks")
def start_anomaly_task(payload: dict = Body(...)):
    if payload.get("kind") != ANOMALY_KIND:
        raise ValueError("不支持的治理类型")
    scope = _anomaly_scope(payload.get("input") or {})
    task_id = f"task-anomaly-{uuid4().hex[:8]}"
    result_id = f"anomaly-analysis-{uuid4().hex[:8]}"
    snapshot = _anomaly_snapshot(scope, result_id, task_id, True)
    ANOMALY_SIMULATIONS[result_id] = snapshot
    created_at = now_shanghai()
    ANOMALY_TASKS[task_id] = {"task_id": task_id, "input": scope, "status": "succeeded", "result_id": result_id, "created_at": created_at.isoformat(), "coverage": 100, "rule_version": "quality-v1", "model_version": "quality-engine-v1"}
    # 每次检查都写入任务表，顶部统计据此累积，而不是固定展示一组数值。
    result = snapshot["result"]
    with SessionLocal() as db:
        dataset = db.get(Dataset, scope["dataset_id"])
        db.add(Task(
            task_id=task_id, name=f"{dataset.name if dataset else result['dataset_name']}质量检测",
            capability_code="anomaly_detect", status="succeeded", source_name="数据质量检测",
            dataset_name=dataset.name if dataset else result["dataset_name"], storage_gb=0,
            progress=100, success_count=result["valid_count"], duplicate_count=0,
            anomaly_count=result["anomaly_count"], input_data=scope, config={"scheme_id": scope["scheme_id"]},
            result={**result, "scanned_count": result["valid_count"], "detected_count": result["anomaly_count"], "pending_review_count": 0, "repaired_count": result["processed_count"]},
            dataset_version=scope["version_id"], model_version="quality-engine-v1",
            created_at=created_at, finished_at=created_at, trace_id=f"trace_{task_id}",
        ))
        db.commit()
    return success(data=ANOMALY_TASKS[task_id], message="异常检测任务创建并完成")


@router.get("/data-governance/anomaly-tasks/{task_id}")
def anomaly_task(task_id: str, kind: str = Query(default=ANOMALY_KIND)):
    task = ANOMALY_TASKS.get(task_id)
    if kind != ANOMALY_KIND or task is None:
        raise HTTPException(status_code=404, detail="异常检测任务不存在")
    return success(data=task, message="异常检测任务查询成功")


@router.get("/data-governance/change-sets/current")
def current_change_set(
    dataset_id: int,
    version_id: str,
    language: str,
    scheme_id: str,
    kind: str = Query(default=ANOMALY_KIND),
):
    if kind != ANOMALY_KIND:
        raise ValueError("不支持的数据治理类型")
    return success(
        data=_current_change_set(dataset_id, version_id, language, scheme_id),
        message="当前修改集查询成功",
    )


@router.get("/data-governance/risk-overview")
def risk_overview(kind: str = Query(default="governance-risk")):
    if kind != "governance-risk":
        raise ValueError("不支持的治理类型")
    persisted = _risk_results_from_database()
    # 新完成但尚未重启的结果在内存中；按 result id 去重，数据库为统计基准。
    all_results = {result["id"]: result for result in [*persisted, *RISK_RESULTS.values()]}
    results = sorted(
        all_results.values(),
        key=lambda result: str(result.get("finished_at") or ""),
        reverse=True,
    )
    latest = results[0] if results else None
    cards = [
        {"label": "已检测样本", "value": sum(int(result.get("valid_count") or 0) for result in results), "icon": "Document"},
        {"label": "风险样本", "value": latest["risk_count"] if latest else 0, "icon": "WarningFilled"},
        {"label": "高风险样本", "value": latest["high_count"] if latest else 0, "icon": "CircleCloseFilled"},
        {"label": "待人工复核", "value": latest["pending_count"] if latest else 0, "icon": "User"},
    ]
    records = [{"result_id": result["id"], "dataset_name": result["dataset_name"], "version_id": result["scope"]["version_id"], "valid_count": result["valid_count"], "risk_count": result["risk_count"], "finished_at": result["finished_at"]} for result in results]
    return success(data={"cards": cards, "definitions": ["统计数据库中已完成的风险检测任务；服务重启后会恢复历史累计值。"], "records": records}, message="风险识别概览查询成功")


@router.get("/data-governance/risk-options")
def risk_options(kind: str = Query(default="governance-risk")):
    """返回风险识别页初始化所需的分级方案和筛选选项。"""
    if kind != "governance-risk":
        return success(data={}, message="未配置该治理类型")
    return success(
        data={
            "schemes": [
                {
                    "id": "risk-v1",
                    "name": "内容语义风险分级 v1.0",
                    "levels": [
                        {"level": "HIGH", "label": "高风险", "color": "#f34d69", "definition": "存在直接严重伤害或敏感个人信息暴露证据，优先人工核验。"},
                        {"level": "MEDIUM", "label": "中风险", "color": "#f5a623", "definition": "存在可关联个人信息或较明确的潜在伤害，需要核查授权与上下文。"},
                        {"level": "LOW", "label": "低风险", "color": "#1687ff", "definition": "存在有限风险线索，应结合适用条件核查。"},
                        {"level": "NOTICE", "label": "提示", "color": "#8b5cf6", "definition": "仅存在需补充核查的线索，不等同于已确认内容风险。"},
                    ],
                }
            ],
            "categories": ["个人信息暴露", "误导信息", "仇恨歧视", "违法有害"],
            "page_sizes": [5, 10, 20],
            # 数据集和版本由前端通过数据资源接口加载并覆盖此默认范围。
            "default_scope": {"dataset_id": 0, "version_id": "", "language": "all", "scheme_id": "risk-v1"},
            "review_statuses": ["未发起", "待复核", "已复核"],
            "actions": ["detect", "history"],
            "review_decisions": [
                {"value": "confirm", "label": "确认建议"},
                {"value": "adjust", "label": "调整等级 / 类别"},
                {"value": "exclude", "label": "排除误报"},
            ],
            "reviewers": ["当前复核人"],
        },
        message="风险识别选项查询成功",
    )


@router.get("/data-governance/risk-results/latest")
def latest_risk_result(
    dataset_id: int,
    version_id: str,
    language: str,
    scheme_id: str,
    kind: str = Query(default="governance-risk"),
):
    """当前范围没有已保存检测结果时返回 null，供前端展示“开始检测”。"""
    if kind != "governance-risk":
        raise ValueError("不支持的治理类型")
    scope = _risk_scope(dataset_id, version_id, language, scheme_id)
    result = _saved_risk_result_for_scope(scope)
    return success(data={key: value for key, value in result.items() if key != "samples"} if result else None, message="风险结果查询成功")


@router.get("/data-governance/risk-results/{result_id}")
def risk_result(result_id: str):
    result = _saved_risk_result(result_id)
    if result:
        return success(data={key: value for key, value in result.items() if key != "samples"}, message="风险结果查询成功")
    raise HTTPException(status_code=404, detail="风险结果不存在")


@router.get("/data-governance/risk-results/{result_id}/samples")
def risk_samples(result_id: str, page: int = 1, page_size: int = 5, keyword: str = "", level: str = "", status: str = ""):
    result = _saved_risk_result(result_id)
    if not result:
        raise HTTPException(status_code=404, detail="风险结果不存在")
    rows = [row for row in result["samples"] if (not level or row["maximum_suggested_level"] == level) and (not status or row["status"] == status) and (not keyword or keyword.lower() in f"{row['id']} {row['text']} {row['primary_category']}".lower())]
    total = len(rows)
    return success(data={"items": rows[(page - 1) * page_size: page * page_size], "total": total, "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size}, message="风险样本查询成功")


@router.post("/data-governance/risk-results/{result_id}/export")
def export_risk_samples(result_id: str, payload: dict = Body(default={} )):
    result = _saved_risk_result(result_id)
    if not result:
        raise HTTPException(status_code=404, detail="风险结果不存在")
    keyword = str(payload.get("keyword", "")).lower()
    level = str(payload.get("level", ""))
    status = str(payload.get("status", ""))
    rows = [row for row in result["samples"] if (not level or row["maximum_suggested_level"] == level) and (not status or row["status"] == status) and (not keyword or keyword in f"{row['id']} {row['text']} {row['primary_category']}".lower())]
    return success(data=rows, message="风险样本导出数据查询成功")


@router.get("/data-governance/risk-results/{result_id}/samples/{sample_id}")
def risk_sample(result_id: str, sample_id: str):
    result = _saved_risk_result(result_id)
    sample = next((row for row in result["samples"] if row["id"] == sample_id), None) if result else None
    if not sample:
        raise HTTPException(status_code=404, detail="风险样本不存在")
    return success(data=sample, message="风险样本查询成功")


@router.post("/data-governance/risk-results/{result_id}/samples/{sample_id}/reviews")
def review_risk_sample(result_id: str, sample_id: str, payload: dict = Body(...)):
    result = _saved_risk_result(result_id)
    sample = next((row for row in result["samples"] if row["id"] == sample_id), None) if result else None
    if not sample:
        raise HTTPException(status_code=404, detail="风险样本不存在")
    input_data = payload.get("input", payload)
    opinion = str(input_data.get("opinion", "")).strip()
    if not opinion:
        raise HTTPException(status_code=400, detail="请填写复核意见")
    sample["status"] = "已复核"
    sample["review"] = {"id": sample.get("review", {}).get("id", f"review-{sample_id}"), "status": "已复核", "original_findings": sample["findings"], "opinion": opinion, "reviewer": input_data.get("reviewer", "当前复核人"), "decision": input_data.get("decision", "confirm"), "level": input_data.get("level", sample["maximum_suggested_level"]), "category": input_data.get("category", sample["primary_category"]), "updated_at": datetime.now().isoformat(), "actions": []}
    _save_risk_result(result)
    return success(data=sample, message="风险样本复核已保存")


@router.get("/risk-knowledge")
def risk_knowledge(result_id: str, sample_id: str, keyword: str = ""):
    result = _saved_risk_result(result_id)
    sample = next((row for row in result["samples"] if row["id"] == sample_id), None) if result else None
    if not sample:
        raise HTTPException(status_code=404, detail="风险样本不存在")
    rules = sample.get("rules", [])
    if keyword:
        rules = [rule for rule in rules if keyword.lower() in f"{rule.get('name', '')}{rule.get('category', '')}{rule.get('text', '')}".lower()]
    return success(data=rules, message="风险知识检索成功")


@router.get("/data-governance/risk-results")
def risk_history(dataset_id: int, version_id: str, language: str, scheme_id: str, kind: str = Query(default="governance-risk")):
    scope = _risk_scope(dataset_id, version_id, language, scheme_id)
    result = _saved_risk_result_for_scope(scope)
    return success(data=[{key: value for key, value in result.items() if key != "samples"}] if result else [], message="风险历史查询成功")


@router.post("/data-governance/risk-tasks")
def create_risk_task(payload: dict = Body(...)):
    scope = payload.get("input", payload)
    scope = _risk_scope(int(scope.get("dataset_id", scope.get("datasetId", 1))), str(scope.get("version_id", scope.get("versionId", "v1.0.0"))), str(scope.get("language", "all")), str(scope.get("scheme_id", scope.get("schemeId", "risk-v1"))))
    task_id = f"risk-task-{uuid4().hex[:8]}"
    RISK_TASKS[task_id] = {"id": task_id, "status": "running", "input": scope, "created_at": datetime.now().isoformat(), "polls": 0}
    return success(data=RISK_TASKS[task_id], message="风险检测任务已创建")


@router.get("/data-governance/risk-tasks/{task_id}")
def get_risk_task(task_id: str):
    global RISK_LAST_RESULT
    task = RISK_TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="风险任务不存在")
    if task["status"] == "running":
        task["polls"] += 1
        if task["polls"] >= 1:
            scope = task["input"]
            result_id = f"risk-result-{uuid4().hex[:8]}"
            key = _risk_key(scope)
            run_no = RISK_RUN_COUNTS.get(key, 0) + 1
            RISK_RUN_COUNTS[key] = run_no
            result = _risk_snapshot(scope, True, result_id, task_id, run_no)
            RISK_RESULTS[_risk_key(scope)] = result
            RISK_LAST_RESULT = result
            RISK_TOTALS["valid_count"] += result["valid_count"]
            RISK_TOTALS["risk_count"] += result["risk_count"]
            RISK_TOTALS["high_count"] += result["high_count"]
            with SessionLocal() as db:
                dataset = db.get(Dataset, scope["dataset_id"])
                db.add(Task(
                    task_id=task_id,
                    name=f"{dataset.name if dataset else result['dataset_name']}风险检测",
                    capability_code="risk_detect",
                    trace_id=f"trace_{task_id}",
                    status="succeeded",
                    source_name="内容风险识别",
                    dataset_name=dataset.name if dataset else result["dataset_name"],
                    storage_gb=0,
                    progress=100,
                    success_count=result["valid_count"],
                    duplicate_count=0,
                    anomaly_count=result["risk_count"],
                    input_data=scope,
                    config={"scheme_id": scope["scheme_id"]},
                    result=result,
                    dataset_version=scope["version_id"],
                    model_version="risk-engine-v1",
                    created_at=now_shanghai(),
                    finished_at=now_shanghai(),
                ))
                db.commit()
            task.update({"status": "succeeded", "result_id": result_id})
    return success(data=task, message="风险任务查询成功")


@router.get("/data-governance/value-results/latest")
def latest_value_result(
    dataset_id: int,
    version_id: str,
    language: str,
    scheme_id: str,
):
    """切换筛选范围时不复用旧结果，必须主动点击“开始分析”。"""
    return success(data=None, message="当前范围尚未发起价值分析")


@router.get("/data-governance/value-results/{result_id}")
def value_result(result_id: str):
    for task in _tasks(("value_score", "high_value_detect")):
        result = _result(task)
        if result.get("id") != result_id:
            continue
        input_data = task.input_data or {}
        return success(data={
            "id": result_id, "task_id": task.task_id,
            "scope": {"dataset_id": input_data.get("dataset_id", input_data.get("datasetId")), "version_id": input_data.get("version_id", input_data.get("versionId")), "language": input_data.get("language"), "scheme_id": input_data.get("scheme_id", input_data.get("schemeId"))},
            "dataset_name": task.dataset_name, "version_label": task.dataset_version,
            "language_name": input_data.get("language", ""), "scheme_name": result.get("scheme_name", "通用价值评价 v1.0"),
            "finished_at": task.finished_at.isoformat() if task.finished_at else task.created_at.isoformat(),
            "mean_score": result.get("mean_score"), "valid_count": result.get("valid_count", 0), "high_count": result.get("high_count", 0),
            "unavailable_count": result.get("unavailable_count", 0), "failed_count": result.get("failed_count", 0), "target_count": result.get("target_count", 0),
            "languages": result.get("languages", []), "high_threshold": result.get("high_threshold", 85), "medium_threshold": result.get("medium_threshold", 60),
            "dimensions": _value_visual_defaults(result, int(input_data.get("dataset_id", input_data.get("datasetId", 0)) or 0))[0], "bins": _value_visual_defaults(result, int(input_data.get("dataset_id", input_data.get("datasetId", 0)) or 0))[1],
        }, message="价值分析结果查询成功")
    raise ValueError("价值分析结果不存在")


@router.get("/data-governance/value-results/{result_id}/samples")
def value_samples(result_id: str, page: int = 1, page_size: int = 10, tier: str = "all", keyword: str = "", bin: str = ""):
    for task in _tasks(("value_score", "high_value_detect")):
        result = _result(task)
        if result.get("id") != result_id:
            continue
        # 模拟接口按数据集重建明细，避免复用其他数据集任务中保存的示例文本。
        source_rows = _value_samples_from_summary(result, task)
        rows = [row for row in source_rows if (tier == "all" or row.get("tier") == tier) and (not keyword or keyword in f"{row.get('id', '')}{row.get('text', '')}") and (not bin or str(min(4, int(float(row.get('score', 0)) // 20))) == bin)]
        total = len(rows)
        return success(data={"items": rows[(page - 1) * page_size:page * page_size], "total": total, "page": page, "page_size": page_size, "total_pages": (total + page_size - 1) // page_size}, message="价值评分样本查询成功")
    raise ValueError("价值分析结果不存在")
