def execute_mock_capability(
    capability_code: str,
    input_data: dict,
):
    """
    根据 capability_code 执行对应的模拟能力。
    当前只实现 semantic_risk。
    """

    if capability_code == "semantic_risk":
        content = input_data.get("content", "")

        if "诈骗" in content:
            risk_level = "high"
            reason = "文本中包含“诈骗”关键词"
        else:
            risk_level = "low"
            reason = "文本未命中当前模拟风险关键词"

        return {
            "risk_level": risk_level,
            "risk_category": "content_safety",
            "confidence": 0.91,
            "reason": reason,
        }
    if capability_code == "anomaly_detect":
        content = input_data.get("content", "")

        # 简单模拟规则：
        # 文本出现“异常”或“污染”时，认为检测到异常数据。
        if "异常" in content or "污染" in content:
            return {
                "anomaly": True,
                "anomaly_type": "data_pollution",
                "risk_score": 0.88,
                "reason": "文本命中“异常”或“污染”模拟规则",
            }

        return {
            "anomaly": False,
            "anomaly_type": None,
            "risk_score": 0.12,
            "reason": "文本未命中异常数据模拟规则",
        }

    if capability_code == "evaluation":
        metric_code = input_data.get("metric_code", "risk_recall")

        return {
            "metric_code": metric_code,
            "metric_name": "风险召回率",
            "value": 0.96,
            "target": 0.95,
            "passed": True,
            "reason": "模拟评估值高于目标值",
        }

    if capability_code == "value_score":
        return {
            "overall_score": 0.86,
            "dimensions": {
                "quality": 0.90,
                "representativeness": 0.84,
                "rarity": 0.79,
                "credibility": 0.91,
            },
            "level": "high",
            "reason": "模拟数据在质量、代表性和可信度维度表现较好",
        }

    if capability_code == "model_risk_governance":
        prompt = input_data.get("prompt", "")
        sensitive = any(word in prompt for word in ("手机", "姓名", "住址", "联系人", "隐私"))
        return {
            "original_output": f"模型原始回复：{prompt}",
            "governed_output": "公开版本已移除个人身份与联系方式，仅保留可公开的业务信息。" if sensitive else "该请求未发现需治理内容，已按原意生成回复。",
            "risk_check": {"level": "high" if sensitive else "low", "reason": "检测到个人信息，需要脱敏处理。" if sensitive else "未命中当前风险规则。"},
            "reconstruction": "已对敏感身份和联系方式进行脱敏。" if sensitive else "输入无需重构。",
        }
    if capability_code == "data_ingest":
        record_count = int(
            input_data.get("record_count", 1000) or 1000
        )

        return {
            "dataset_id": input_data.get("dataset_id", 1),
            "accepted": True,
            "record_count": record_count,
            "success_count": record_count,
            "duplicate_count": 0,
            "anomaly_count": 0,
            "languages": input_data.get("languages", ["zh"]),
            "source_type": input_data.get("source_type", "manual"),
            "quality_status": "pending_review",
            "message": "模拟数据接入完成",
        }

    if capability_code == "high_value_detect":
        top_n = input_data.get("top_n", 5)

        return {
            "dataset_id": input_data.get("dataset_id"),
            "threshold": input_data.get("threshold", 0.8),
            "items": [
                {
                    "sample_id": index,
                    "value_score": round(0.98 - index * 0.03, 2),
                    "is_high_value": True,
                }
                for index in range(1, top_n + 1)
            ],
            "message": "模拟高价值语料发现完成",
        }

    if capability_code == "causal_trace":
        return {
            "nodes": [
                {
                    "id": "dataset",
                    "label": "数据集",
                    "type": "dataset",
                },
                {
                    "id": "governance_task",
                    "label": "治理任务",
                    "type": "task",
                },
                {
                    "id": "model",
                    "label": "模型",
                    "type": "model",
                },
                {
                    "id": "output",
                    "label": "输出结果",
                    "type": "output",
                },
            ],
            "edges": [
                {
                    "source": "dataset",
                    "target": "governance_task",
                    "relation": "input",
                },
                {
                    "source": "governance_task",
                    "target": "model",
                    "relation": "processed_by",
                },
                {
                    "source": "model",
                    "target": "output",
                    "relation": "generated",
                },
            ],
            "message": "模拟因果路径追踪完成",
        }

    if capability_code == "data_risk_weight":
        dataset_id = input_data.get("dataset_id")

        return {
            "dataset_id": dataset_id,
            "risk_weight": 0.22,
            "risk_score": 0.18,
            "trust_score": 0.82,
            "trust_status": "可信",
            "risk_level": "low",
            "factors": [
                {
                    "name": "来源可信度",
                    "score": 0.90,
                },
                {
                    "name": "内容完整性",
                    "score": 0.86,
                },
                {
                    "name": "异常比例",
                    "score": 0.08,
                },
            ],
            "reason": "模拟数据未发现明显高风险特征",
        }   

    if capability_code == "provenance_trace":
        dataset_id = input_data.get("dataset_id")

        return {
            "dataset_id": dataset_id,
            "triple_count": 128,
            "entity_types": [
                "dataset",
                "source",
                "governance_task",
                "model",
            ],
            "relation_types": [
                "collected_from",
                "processed_by",
                "used_by",
                "generated",
            ],
            "nodes": [
                {
                    "id": "source_001",
                    "type": "source",
                    "label": "业务数据源",
                },
                {
                    "id": f"dataset_{dataset_id}",
                    "type": "dataset",
                    "label": "目标数据集",
                },
                {
                    "id": "task_001",
                    "type": "governance_task",
                    "label": "数据治理任务",
                },
                {
                    "id": "model_001",
                    "type": "model",
                    "label": "内容安全识别模型",
                },
            ],
            "edges": [
                {
                    "source": "source_001",
                    "target": f"dataset_{dataset_id}",
                    "relation": "collected_from",
                },
                {
                    "source": f"dataset_{dataset_id}",
                    "target": "task_001",
                    "relation": "processed_by",
                },
                {
                    "source": "task_001",
                    "target": "model_001",
                    "relation": "used_by",
                },
            ],
            "audit_status": "pass",
            "message": "模拟数据溯源链路生成完成",
        }

    if capability_code == "poison_trace":
        dataset_id = input_data.get("dataset_id")

        return {
            "dataset_id": dataset_id,
            "poison_detected": True,
            "poison_type": "label_pollution",
            "source": "source_001",
            "confidence": 0.87,
            "recovery_rate": 0.80,
            "chain": [
                {
                    "stage": "source",
                    "node": "source_001",
                    "status": "suspicious",
                },
                {
                    "stage": "ingest",
                    "node": f"dataset_{dataset_id}",
                    "status": "polluted",
                },
                {
                    "stage": "training",
                    "node": "training_task_001",
                    "status": "affected",
                },
                {
                    "stage": "model",
                    "node": "model_001",
                    "status": "at_risk",
                },
            ],
            "reason": "模拟发现标签污染，并还原数据传播链路",
        }

    if capability_code == "anomaly_repair":
        content = input_data.get("content", "")
        anomaly_type = input_data.get(
            "anomaly_type",
            "format_error",
        )

        # 模拟修复结果
        repaired_content = content.strip()

        if anomaly_type == "text_noise":
            repaired_content = (
                repaired_content
                .replace("!!!", "。")
                .replace("???", "？")
            )

        if anomaly_type == "format_error":
            repaired_content = " ".join(
                repaired_content.split()
            )

        return {
            "original_content": content,
            "repaired_content": repaired_content,
            "anomaly_type": anomaly_type,
            "repair_status": "succeeded",
            "before_trust_score": 0.42,
            "after_trust_score": 0.88,
            "confidence": 0.91,
            "changes": [
                "清理多余空格",
                "修复文本格式",
                "提升样本可信度",
            ],
            "message": "模拟异常样本修复完成",
        }


    if capability_code == "risk_knowledge":
        query = input_data.get("query", "")
        top_k = input_data.get("top_k", 5)

        knowledge_items = [
            {
                "id": "risk_knowledge_001",
                "title": "隐私信息保护规范",
                "category": "privacy",
                "risk_level": "high",
                "score": 0.92,
                "summary": "涉及个人身份、联系方式和敏感信息的内容需要进行保护。",
            },
            {
                "id": "risk_knowledge_002",
                "title": "网络诈骗识别规则",
                "category": "fraud",
                "risk_level": "high",
                "score": 0.89,
                "summary": "识别诱导转账、虚假承诺和冒充身份等风险内容。",
            },
            {
                "id": "risk_knowledge_003",
                "title": "内容安全审核规范",
                "category": "content_safety",
                "risk_level": "medium",
                "score": 0.84,
                "summary": "根据风险等级对内容执行通过、复核或拦截处理。",
            },
        ]

        return {
            "query": query,
            "mode": input_data.get("mode", "hybrid"),
            "top_k": top_k,
            "matches": knowledge_items[:top_k],
            "total": min(top_k, len(knowledge_items)),
            "message": "模拟风险知识库检索完成",
        }

    if capability_code == "knowledge_edit":
        model_id = input_data.get("model_id")
        target_knowledge = input_data.get(
            "target_knowledge",
            "待编辑风险知识",
        )
        edit_type = input_data.get(
            "edit_type",
            "replace",
        )

        return {
            "model_id": model_id,
            "target_knowledge": target_knowledge,
            "edit_type": edit_type,
            "before_version": "v1.0.0",
            "after_version": "v1.0.1",
            "before_answer": (
                "这是模型编辑前的模拟回答，可能包含风险表述。"
            ),
            "after_answer": (
                "这是模型编辑后的模拟回答，已增加合规约束。"
            ),
            "edited_positions": [
                {
                    "layer": 12,
                    "position": 128,
                    "type": "risk_knowledge",
                }
            ],
            "ability_retention": 0.97,
            "generalization_score": 0.91,
            "edit_status": "succeeded",
            "message": "模拟模型知识编辑完成",
        }

    if capability_code == "knowledge_edit":
        model_id = input_data.get("model_id")
        target_knowledge = input_data.get(
            "target_knowledge",
            "待编辑风险知识",
        )
        edit_type = input_data.get(
            "edit_type",
            "replace",
        )

        return {
            "model_id": model_id,
            "target_knowledge": target_knowledge,
            "edit_type": edit_type,
            "before": {
                "version": "v1.0.0",
                "risk_behavior": "可能生成不合规回答",
                "target_knowledge_found": True,
            },
            "after": {
                "version": "v1.0.1",
                "risk_behavior": "生成合规回答",
                "target_knowledge_updated": True,
            },
            "edited_positions": [
                {
                    "layer": 16,
                    "neuron": 128,
                    "importance": 0.91,
                },
                {
                    "layer": 21,
                    "neuron": 256,
                    "importance": 0.84,
                },
            ],
            "regression_score": 0.97,
            "generalization_score": 0.93,
            "side_effect_level": "low",
            "edit_status": "succeeded",
            "message": "模拟模型知识编辑完成",
        }
    if capability_code == "lineage_audit":
        dataset_id = input_data.get("dataset_id")
        model_id = input_data.get("model_id")

        return {
            "dataset_id": dataset_id,
            "model_id": model_id,
            "lineage_status": "complete",
            "nodes": [
                {
                    "id": f"dataset_{dataset_id}",
                    "type": "dataset",
                    "label": "数据集",
                    "version": "v1.0.0",
                },
                {
                    "id": "governance_task_001",
                    "type": "governance_task",
                    "label": "数据治理任务",
                    "status": "succeeded",
                },
                {
                    "id": "training_task_001",
                    "type": "training_task",
                    "label": "模型训练任务",
                    "status": "succeeded",
                },
                {
                    "id": f"model_{model_id}",
                    "type": "model",
                    "label": "内容安全识别模型",
                    "version": "v1.0.0",
                },
                {
                    "id": "output_001",
                    "type": "output",
                    "label": "模型输出",
                },
            ],
            "edges": [
                {
                    "source": f"dataset_{dataset_id}",
                    "target": "governance_task_001",
                    "relation": "input",
                },
                {
                    "source": "governance_task_001",
                    "target": "training_task_001",
                    "relation": "used_by",
                },
                {
                    "source": "training_task_001",
                    "target": f"model_{model_id}",
                    "relation": "produced",
                },
                {
                    "source": f"model_{model_id}",
                    "target": "output_001",
                    "relation": "generated",
                },
            ],
            "stages": [
                {
                    "name": "data_input",
                    "status": "recorded",
                },
                {
                    "name": "governance",
                    "status": "recorded",
                },
                {
                    "name": "training",
                    "status": "recorded",
                },
                {
                    "name": "inference",
                    "status": "recorded",
                },
                {
                    "name": "output",
                    "status": "recorded",
                },
            ],
            "audit_status": "pass",
            "message": "模拟全链路数据谱系审计完成",
        }
    
    if capability_code == "training_monitor":
        training_task_id = input_data.get(
            "training_task_id",
            "training_task_001",
        )

        model_id = input_data.get("model_id")
        dataset_id = input_data.get("dataset_id")

        total_epochs = input_data.get("total_epochs", 10)
        current_epoch = min(8, total_epochs)

        return {
            "training_task_id": training_task_id,
            "model_id": model_id,
            "dataset_id": dataset_id,
            "epoch": current_epoch,
            "total_epochs": total_epochs,
            "loss": 0.14,
            "status": "healthy",
            "checkpoints": [
                {
                    "epoch": 1,
                    "loss": 0.42,
                    "status": "saved",
                },
                {
                    "epoch": 5,
                    "loss": 0.21,
                    "status": "saved",
                },
                {
                    "epoch": current_epoch,
                    "loss": 0.14,
                    "status": "latest",
                },
            ],
            "alerts": [],
            "compliance_status": "pass",
            "message": "模拟训练动态合规监控完成",
        }
    if capability_code == "reasoning_audit":
        model_id = input_data.get("model_id")
        content = input_data.get("content", "")

        return {
            "model_id": model_id,
            "input": content,
            "steps": [
                {
                    "step": 1,
                    "name": "输入接收",
                    "status": "completed",
                },
                {
                    "step": 2,
                    "name": "风险特征提取",
                    "status": "completed",
                },
                {
                    "step": 3,
                    "name": "风险等级判断",
                    "status": "completed",
                },
                {
                    "step": 4,
                    "name": "安全策略决策",
                    "status": "completed",
                },
                {
                    "step": 5,
                    "name": "结果生成",
                    "status": "completed",
                },
            ],
            "risk_nodes": [
                {
                    "step": 2,
                    "type": "keyword_detection",
                    "risk_level": "low",
                }
            ],
            "audit_status": "pass",
            "explanation": "模拟推理路径未发现异常节点",
            "message": "模拟推理路径审计完成",
        }
    if capability_code == "neuron_audit":
        model_id = input_data.get("model_id")
        layer = input_data.get("layer", 16)

        return {
            "model_id": model_id,
            "layer": layer,
            "layer_count": 32,
            "hotspots": [
                {
                    "neuron": 128,
                    "activation": 0.91,
                    "risk_score": 0.18,
                    "status": "normal",
                },
                {
                    "neuron": 256,
                    "activation": 0.84,
                    "risk_score": 0.22,
                    "status": "normal",
                },
                {
                    "neuron": 512,
                    "activation": 0.76,
                    "risk_score": 0.31,
                    "status": "normal",
                },
            ],
            "abnormal_neurons": [],
            "risk_status": "normal",
            "audit_status": "pass",
            "message": "模拟神经元激活审计完成",
        }
    if capability_code == "full_chain_audit":
        dataset_id = input_data.get("dataset_id")
        model_id = input_data.get("model_id")
        task_id = input_data.get("task_id")

        return {
            "dataset_id": dataset_id,
            "model_id": model_id,
            "task_id": task_id,
            "chain_status": "complete",
            "stages": [
                {
                    "stage": "data_ingest",
                    "name": "数据接入",
                    "status": "completed",
                },
                {
                    "stage": "data_governance",
                    "name": "数据治理",
                    "status": "completed",
                },
                {
                    "stage": "training",
                    "name": "模型训练",
                    "status": "completed",
                },
                {
                    "stage": "inference",
                    "name": "模型推理",
                    "status": "completed",
                },
                {
                    "stage": "audit",
                    "name": "合规审计",
                    "status": "completed",
                },
                {
                    "stage": "output",
                    "name": "结果输出",
                    "status": "completed",
                },
            ],
            "nodes": [
                {
                    "id": f"dataset_{dataset_id}",
                    "type": "dataset",
                    "label": "数据集",
                },
                {
                    "id": "governance_task_001",
                    "type": "governance_task",
                    "label": "数据治理任务",
                },
                {
                    "id": "training_task_001",
                    "type": "training_task",
                    "label": "模型训练任务",
                },
                {
                    "id": f"model_{model_id}",
                    "type": "model",
                    "label": "模型",
                },
                {
                    "id": "audit_001",
                    "type": "audit",
                    "label": "合规审计",
                },
                {
                    "id": "output_001",
                    "type": "output",
                    "label": "最终输出",
                },
            ],
            "edges": [
                {
                    "source": f"dataset_{dataset_id}",
                    "target": "governance_task_001",
                    "relation": "input",
                },
                {
                    "source": "governance_task_001",
                    "target": "training_task_001",
                    "relation": "processed_by",
                },
                {
                    "source": "training_task_001",
                    "target": f"model_{model_id}",
                    "relation": "trained",
                },
                {
                    "source": f"model_{model_id}",
                    "target": "audit_001",
                    "relation": "audited_by",
                },
                {
                    "source": "audit_001",
                    "target": "output_001",
                    "relation": "generated",
                },
            ],
            "audit_status": "pass",
            "risk_level": "low",
            "message": "模拟全链路治理审计完成",
        }
    if capability_code == "scenario_governance":
        scenario_code = input_data.get(
            "scenario_code",
            "content_review",
        )

        content = input_data.get("content", "")

        risk_level = (
            "high"
            if any(word in content for word in ["诈骗", "暴力", "违法"])
            else "low"
        )

        if risk_level == "high":
            intervention = "block"
            intervention_name = "拦截并转人工复核"
        else:
            intervention = "allow"
            intervention_name = "正常放行"

        return {
            "scenario_code": scenario_code,
            "scenario_name": "内容安全审核场景",
            "content": content,
            "risk_level": risk_level,
            "risk_score": 0.91 if risk_level == "high" else 0.12,
            "intervention": intervention,
            "intervention_name": intervention_name,
            "actions": [
                "风险识别",
                "策略匹配",
                "主动干预",
                "审计记录",
            ],
            "governance_status": "completed",
            "audit_status": "pass",
            "message": "模拟场景主动干预完成",
        }
    
    # 后续会在这里继续增加 anomaly_detect、value_score 等能力。
    return {
        "error": f"暂未实现能力：{capability_code}",
    }
