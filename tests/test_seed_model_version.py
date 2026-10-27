"""启动美化脚本只美化演示模型，不得覆盖真实训练产物指向的版本。"""
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.seed import ensure_resource_display_values
from app.core.time import now_shanghai
from app.models.tables import Base, Model, ModelVersion, TrainingTask


class SeedModelVersionTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        self.db = self.Session()
        now = now_shanghai()
        self.db.add_all([
            Model(id=8, name="服饰分类 CNN", category="图像分类", version="v1.0.0",
                  created_at=now, updated_at=now),
            Model(id=9, name="演示模型", category="文本分类", version="v1.0.0",
                  created_at=now, updated_at=now),
        ])
        self.db.add(ModelVersion(model_id=8, version="v1.0.0", description="训练产物",
                                 task_id="TR-1", created_at=now))
        self.db.add(TrainingTask(
            id="TR-1", name="真实训练", status="succeeded", progress=100, model_id="8",
            base_version="v0.0.0", dataset_id="1", dataset_version="v1.0.0", epochs=1,
            epoch=1, learning_rate=0.1, batch_size=8, target_version="v1.0.0",
            created_at=now, updated_at=now,
        ))
        self.db.commit()
        import app.core.seed as seed_module

        self.original = seed_module.SessionLocal
        seed_module.SessionLocal = self.Session

    def tearDown(self):
        import app.core.seed as seed_module

        seed_module.SessionLocal = self.original
        self.db.close()

    def test_model_with_trained_product_keeps_its_version(self):
        """产物指针 v1.0.0 必须原样保留，否则"当前版本"会指向没登记过的版本。"""
        ensure_resource_display_values()
        self.assertEqual(self.db.get(Model, 8).version, "v1.0.0")

    def test_plain_demo_model_still_gets_distinct_version(self):
        ensure_resource_display_values()
        self.assertNotEqual(self.db.get(Model, 9).version, "v1.0.0")


if __name__ == "__main__":
    unittest.main()
