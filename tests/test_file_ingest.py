import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.domain.schemas import ExecuteTaskRequest
from app.models.tables import Dataset, DatasetRecord, DatasetVersionItem, Task
from app.services.database_ingest import save_dataset_rows
from app.services.dataset_version_service import create_ingest_version, register_version
from app.services.file_ingest import read_uploaded_file_rows
from app.services.task_service import run_mock_task


class FileIngestTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.addCleanup(self.engine.dispose)

    def create_dataset(self):
        with self.session_factory() as db:
            dataset = Dataset(
                name="文件接入测试", category="测试", source_type="business",
                version="v1.0.0", status="ready", description="", metadata_json={},
            )
            db.add(dataset)
            db.flush()
            dataset_id = dataset.id
            db.commit()
        return dataset_id

    def test_text_and_json_files_store_one_record_with_complete_content(self):
        documents = {
            ".txt": "第一段\r\n\r\n  第二段  \r\n第三段\n",
            ".json": '[\r\n  {"content": "第一条"},\r\n  {"content": "第二条"}\r\n]\r\n',
        }
        for extension, content in documents.items():
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as temporary_directory:
                upload_dir = Path(temporary_directory)
                raw = content.encode("utf-8-sig")
                filename = f"file_complete{extension}"
                (upload_dir / filename).write_bytes(raw)
                with patch("app.services.file_ingest.UPLOAD_DIR", upload_dir):
                    rows, byte_count = read_uploaded_file_rows(["file_complete"])
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["content"], content)
                self.assertEqual(rows[0]["storage_granularity"], "file")
                self.assertEqual(rows[0]["source_file_id"], "file_complete")
                self.assertEqual(rows[0]["source_filename"], filename)
                self.assertEqual(rows[0]["source_extension"], extension)
                self.assertEqual(rows[0]["document_size_bytes"], len(raw))
                self.assertEqual(rows[0]["source_content_hash"], sha256(raw).hexdigest())
                self.assertEqual(byte_count, len(raw))

    def test_csv_produces_one_record_per_non_empty_data_row(self):
        content = 'id,content\r\n1,"hello\r\nworld"\r\n2,另一条记录\r\n\r\n'
        raw = content.encode("utf-8-sig")
        with tempfile.TemporaryDirectory() as temporary_directory:
            upload_dir = Path(temporary_directory)
            (upload_dir / "file_rows.csv").write_bytes(raw)
            with patch("app.services.file_ingest.UPLOAD_DIR", upload_dir):
                rows, byte_count = read_uploaded_file_rows(["file_rows"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], "1")
        self.assertEqual(rows[0]["content"], "hello\r\nworld")
        self.assertEqual(rows[1]["id"], "2")
        self.assertEqual(rows[1]["content"], "另一条记录")
        self.assertEqual(rows[0]["_source"]["source_row_number"], 2)
        self.assertEqual(rows[1]["_source"]["source_row_number"], 3)
        self.assertEqual(len(rows[0]["_source"]["content_hash"]), 64)
        self.assertEqual(byte_count, len(raw))

    def test_multiple_files_produce_one_record_each(self):
        documents = {
            "file_first.txt": b"first line\n\nsecond line\n",
            "file_second.csv": b"id,content\n1,hello\n2,world\n",
            "file_third.json": b'[{"content": "one"}, {"content": "two"}]\n',
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            upload_dir = Path(temporary_directory)
            for filename, raw in documents.items():
                (upload_dir / filename).write_bytes(raw)
            with patch("app.services.file_ingest.UPLOAD_DIR", upload_dir):
                rows, byte_count = read_uploaded_file_rows(["file_first", "file_second", "file_third"])
        self.assertEqual(len(rows), 4)
        file_names = [row.get("source_filename") or row["_source"]["source_filename"] for row in rows]
        self.assertEqual(file_names, ["file_first.txt", "file_second.csv", "file_second.csv", "file_third.json"])
        self.assertEqual(rows[0]["content"], documents["file_first.txt"].decode("utf-8"))
        self.assertEqual([row["content"] for row in rows[1:3]], ["hello", "world"])
        self.assertEqual(rows[3]["content"], documents["file_third.json"].decode("utf-8"))
        self.assertEqual(byte_count, sum(len(raw) for raw in documents.values()))

    def test_csv_rows_persist_as_records_and_new_version_references_full_snapshot(self):
        dataset_id = self.create_dataset()
        with tempfile.TemporaryDirectory() as temporary_directory:
            upload_dir = Path(temporary_directory)
            content = "id,content\n" + "".join(f"{i},样本{i}\n" for i in range(20))
            (upload_dir / "file_twenty.csv").write_text(content, encoding="utf-8")
            with patch("app.services.file_ingest.UPLOAD_DIR", upload_dir):
                new_rows, _ = read_uploaded_file_rows(["file_twenty"])

        with self.session_factory() as db:
            dataset = db.get(Dataset, dataset_id)
            old_rows = [{"id": i, "content": f"旧样本{i}"} for i in range(10)]
            save_dataset_rows(db, dataset_id=dataset_id, task_id="tsk_initial", rows=old_rows)
            old_ids = list(db.scalars(select(DatasetRecord.id).where(
                DatasetRecord.dataset_id == dataset_id).order_by(DatasetRecord.id)).all())
            original = register_version(db, dataset, "v1.0.0", old_ids,
                                        change_summary="原始接入快照")
            original_id = original.version_id
            duplicate_count = save_dataset_rows(
                db, dataset_id=dataset_id, task_id="tsk_csv",
                rows=new_rows,
                content_hashes=[row["_source"]["content_hash"] for row in new_rows],
            )
            self.assertEqual(duplicate_count, 0)
            version = create_ingest_version(db, dataset, task_id="tsk_csv", note="新增 20 条 CSV 样本")
            self.assertEqual(version.label, "v1.0.1")
            self.assertEqual(version.sample_count, 30)
            self.assertEqual(version.change_summary, "新增 20 条 CSV 样本")
            old_item_count = db.scalar(select(func.count(DatasetVersionItem.id)).where(
                DatasetVersionItem.version_id == original_id))
            old_items = list(db.scalars(select(DatasetVersionItem.record_id).where(
                DatasetVersionItem.version_id == original_id)).all())
            new_items = list(db.scalars(select(DatasetVersionItem.record_id).where(
                DatasetVersionItem.version_id == version.version_id)).all())
            total_records = db.scalar(select(func.count(DatasetRecord.id)).where(
                DatasetRecord.dataset_id == dataset_id))
            self.assertEqual(old_item_count, 10)
            self.assertEqual(len(old_items), 10)
            self.assertEqual(len(new_items), 30)
            self.assertEqual(len(set(new_items)), 30)
            self.assertEqual(total_records, 30)
            self.assertEqual(dataset.version, "v1.0.1")
            db.commit()

    def test_complete_file_content_is_persisted_and_readable_in_a_new_session(self):
        content = "第一段\r\n\r\n第二段\r\n第三段\n"
        raw = content.encode("utf-8-sig")
        with tempfile.TemporaryDirectory() as temporary_directory:
            upload_dir = Path(temporary_directory)
            (upload_dir / "file_persisted.txt").write_bytes(raw)
            with patch("app.services.file_ingest.UPLOAD_DIR", upload_dir):
                rows, _ = read_uploaded_file_rows(["file_persisted"])
        dataset_id = self.create_dataset()
        with self.session_factory() as db:
            duplicate_count = save_dataset_rows(
                db, dataset_id=dataset_id, task_id="tsk_file", rows=rows,
                content_hashes=[row["source_content_hash"] for row in rows],
            )
            self.assertEqual(duplicate_count, 0)
            db.commit()
        with self.session_factory() as db:
            saved = db.scalars(select(DatasetRecord)).all()
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].payload["content"], content)
            self.assertEqual(saved[0].content_hash, sha256(raw).hexdigest())
            self.assertEqual(saved[0].dataset_id, dataset_id)
            self.assertEqual(saved[0].task_id, "tsk_file")

    def test_identical_files_are_duplicates_even_with_different_upload_ids(self):
        raw = b"first paragraph\n\nsecond paragraph\n"
        with tempfile.TemporaryDirectory() as temporary_directory:
            upload_dir = Path(temporary_directory)
            (upload_dir / "file_original.txt").write_bytes(raw)
            (upload_dir / "file_reuploaded.txt").write_bytes(raw)
            with patch("app.services.file_ingest.UPLOAD_DIR", upload_dir):
                rows, _ = read_uploaded_file_rows(["file_original", "file_reuploaded"])
        self.assertNotEqual(rows[0]["source_file_id"], rows[1]["source_file_id"])
        self.assertNotEqual(rows[0]["source_filename"], rows[1]["source_filename"])
        self.assertEqual(rows[0]["source_content_hash"], rows[1]["source_content_hash"])
        dataset_id = self.create_dataset()
        for index, row in enumerate(rows):
            with self.session_factory() as db:
                duplicate_count = save_dataset_rows(
                    db, dataset_id=dataset_id, task_id=f"tsk_upload{index}", rows=[row],
                    content_hashes=[row["source_content_hash"]],
                )
                self.assertEqual(duplicate_count, index)
                db.commit()
        with self.session_factory() as db:
            saved = db.scalars(select(DatasetRecord).order_by(DatasetRecord.id)).all()
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[0].content_hash, saved[1].content_hash)
            self.assertEqual([record.payload["content"] for record in saved], [raw.decode("utf-8")] * 2)

    def test_database_rows_still_use_default_hash_and_row_granularity(self):
        rows = [{"id": 1, "content": "hello"}, {"content": "hello", "id": 1}, {"id": 2, "content": "world"}]
        dataset_id = self.create_dataset()
        with self.session_factory() as db:
            self.assertEqual(save_dataset_rows(db, dataset_id=dataset_id, task_id="tsk_database", rows=rows), 1)
            db.commit()
        with self.session_factory() as db:
            saved = db.scalars(select(DatasetRecord).order_by(DatasetRecord.id)).all()
            self.assertEqual([record.payload for record in saved], rows)
            self.assertEqual(saved[0].content_hash, saved[1].content_hash)
            self.assertNotEqual(saved[0].content_hash, saved[2].content_hash)

    def test_file_task_persists_one_complete_record_and_counts_one_file(self):
        content = "第一段\r\n\r\n第二段\r\n第三段\n"
        raw = content.encode("utf-8-sig")
        request = ExecuteTaskRequest(
            capability_code="data_ingest", name="整份文件接入测试",
            input={
                "connectorType": "file", "files": ["file_task"],
                "createDataset": True, "datasetName": "测试",
                "sourceName": "测试", "owner": "测试",
            },
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            upload_dir = Path(temporary_directory)
            (upload_dir / "file_task.txt").write_bytes(raw)
            with (
                patch("app.services.file_ingest.UPLOAD_DIR", upload_dir),
                patch("app.services.task_service.SessionLocal", self.session_factory),
                patch("app.services.task_service.sleep"),
                patch("app.services.task_service._uploaded_size_bytes", return_value=len(raw)),
            ):
                result = run_mock_task(request, trace_id="trace_single_file")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["result"]["record_count"], 1)
        self.assertEqual(result["anomaly_count"], 0)
        with self.session_factory() as db:
            task = db.get(Task, result["task_id"])
            self.assertEqual(task.status, "succeeded")
            self.assertEqual(task.success_count, 1)
            self.assertEqual(task.result["record_count"], 1)
            dataset = db.get(Dataset, task.result["dataset_id"])
            self.assertEqual(dataset.metadata_json["record_count"], 1)
            saved = db.scalars(select(DatasetRecord).where(DatasetRecord.task_id == task.task_id)).all()
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].dataset_id, dataset.id)
            self.assertEqual(saved[0].payload["content"], content)
            self.assertEqual(saved[0].content_hash, sha256(raw).hexdigest())


if __name__ == "__main__":
    unittest.main()
