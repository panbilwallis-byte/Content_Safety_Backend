"""真实训练建单支持选择脚本（#2.1）：白名单拦截、按脚本装载参数、事件文案写清脚本身份。"""
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.routers.training_tasks as module
from app.domain.schemas import CreateRealTrainingRequest
from app.models.tables import Base, ComplianceEvent, Dataset, Model


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return ""


def request(script_id=None, **overrides):
    payload = {"name": "服饰分类训练", "model_id": "8", "base_version": "v0.0.0",
               "dataset_id": "9", "dataset_version": "dsv_000009", "epochs": 3,
               "learning_rate": 0.001, "batch_size": 128, "target_version": "v1.0.0",
               "method": "从零开始训练"}
    if script_id is not None:
        payload["script_id"] = script_id
    payload.update(overrides)
    return CreateRealTrainingRequest(**payload)


class RealTrainingScriptTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        with self.Session() as db:
            db.add(Model(id=8, name="FashionMNIST 服饰分类 CNN", version="v0.0.0",
                         model_type="image_classification"))
            db.add(Dataset(id=9, name="FashionMNIST 图像样本集", version="v1.0.0",
                           source_type="industry", metadata_json={}))
            db.commit()
        self.originals = (module.SessionLocal, module.LLM_SETUP_SECRET, module.LLM_ACCESS_TOKEN)
        module.SessionLocal = self.Session
        module.LLM_SETUP_SECRET = "setup-secret"
        module.LLM_ACCESS_TOKEN = "svc-token"

    def tearDown(self):
        module.SessionLocal, module.LLM_SETUP_SECRET, module.LLM_ACCESS_TOKEN = self.originals

    def _upstream(self):
        return FakeResponse(payload={"data": {"id": "0f4c1a9b-2d3e-4a5b-9c8d-7e6f5a4b3c2d"}})

    def test_fashion_script_loads_its_own_parameters(self):
        with patch.object(module.httpx, "post", return_value=self._upstream()) as post:
            module.create_real_training_task(request("fashion-cnn", train_samples=5000),
                                             authorization="")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["script_id"], "fashion-cnn")
        self.assertEqual(body["parameters"], {"epochs": 3, "learning_rate": 0.001,
                                              "batch_size": 128, "train_samples": 5000})

    def test_mini_mlp_keeps_its_narrow_parameter_set(self):
        with patch.object(module.httpx, "post", return_value=self._upstream()) as post:
            module.create_real_training_task(request(), authorization="")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["script_id"], "mini-mlp")  # 缺省脚本不变，老前端不受影响
        self.assertEqual(body["parameters"], {"epochs": 3, "learning_rate": 0.001})

    def test_unlisted_script_is_rejected_without_calling_model_service(self):
        with patch.object(module.httpx, "post", return_value=self._upstream()) as post:
            with self.assertRaises(HTTPException) as caught:
                module.create_real_training_task(request("cifar100-cnn"), authorization="")
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("cifar100-cnn", caught.exception.detail)
        post.assert_not_called()

    def test_start_event_names_the_actual_script_and_dataset(self):
        with patch.object(module.httpx, "post", return_value=self._upstream()):
            module.create_real_training_task(request("fashion-cnn"), authorization="")
        with self.Session() as db:
            event = db.scalar(select(ComplianceEvent).where(
                ComplianceEvent.event_type == "训练任务启动"))
        self.assertIn("fashion-cnn", event.detail)
        self.assertIn("FashionMNIST 图像样本集", event.detail)


if __name__ == "__main__":
    unittest.main()
