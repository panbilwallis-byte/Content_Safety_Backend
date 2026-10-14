# capability_code：能力唯一编码
# label：给页面和接口文档展示的中文名称

CAPABILITIES = {
    # 课题一：数据价值
    "data_ingest": {
        "label": "数据接入",
        "category": "数据资源",
        "topic": "t1",
    },
    "value_score": {
        "label": "数据价值评分",
        "category": "数据治理",
        "topic": "t1",
    },
    "high_value_detect": {
        "label": "高价值语料发现",
        "category": "数据治理",
        "topic": "t1",
    },
    "causal_trace": {
        "label": "因果追踪",
        "category": "数据治理",
        "topic": "t1",
    },

    # 课题二：异常数据
    "data_risk_weight": {
        "label": "数据风险权重评估",
        "category": "异常数据治理",
        "topic": "t2",
    },
    "provenance_trace": {
        "label": "数据溯源链路",
        "category": "全链路合规",
        "topic": "t2",
    },
    "anomaly_detect": {
        "label": "异常数据检测",
        "category": "异常数据治理",
        "topic": "t2",
    },
    "poison_trace": {
        "label": "投毒链路还原",
        "category": "异常数据治理",
        "topic": "t2",
    },
    "anomaly_repair": {
        "label": "异常样本修复",
        "category": "异常数据治理",
        "topic": "t2",
    },

    # 课题三：语义风险和模型治理
    "semantic_risk": {
        "label": "语义风险识别",
        "category": "风险识别",
        "topic": "t3",
    },
    "risk_knowledge": {
        "label": "风险知识库",
        "category": "风险识别",
        "topic": "t3",
    },
    "knowledge_edit": {
        "label": "模型知识编辑",
        "category": "模型训推",
        "topic": "t3",
    },
    "model_risk_governance": {
        "label": "模型风险治理",
        "category": "模型训推",
        "topic": "t3",
    },

    # 课题四：全链路合规和场景应用
    "lineage_audit": {
        "label": "数据谱系审计",
        "category": "全链路合规",
        "topic": "t4",
    },
    "training_monitor": {
        "label": "训练动态合规监控",
        "category": "全链路合规",
        "topic": "t4",
    },
    "reasoning_audit": {
        "label": "推理路径审计",
        "category": "全链路合规",
        "topic": "t4",
    },
    "neuron_audit": {
        "label": "神经元激活审计",
        "category": "全链路合规",
        "topic": "t4",
    },
    "full_chain_audit": {
        "label": "全链路治理",
        "category": "全链路合规",
        "topic": "t4",
    },
    "scenario_governance": {
        "label": "场景主动干预",
        "category": "场景应用",
        "topic": "t4",
    },

    # 公共平台能力
    "evaluation": {
        "label": "测试评估",
        "category": "测试评估",
        "topic": "platform",
    },
}


def is_supported(capability_code: str) -> bool:
    """
    判断能力编码是否已登记。
    """

    return capability_code in CAPABILITIES