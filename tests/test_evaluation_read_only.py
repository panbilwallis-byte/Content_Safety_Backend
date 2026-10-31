"""评估列表读取不得补写示例，尤其是已有任务但运行记录为空时。"""

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.tables import Base, EvaluationRun, EvaluationTask
from app.routers import evaluations


@pytest.fixture
def evaluation_api(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(evaluations, "SessionLocal", sessions)
    yield sessions, engine
    engine.dispose()


def existing_task():
    return EvaluationTask(
        task_id="seed-eval-task-1",
        run_id="seed-eval-run-1",
        record_id="seed-eval-record-1",
        name="已有的评估任务",
        status="succeeded",
        target_stage="final",
        dataset_version="v1.0.0",
        model_version="v1.0.0",
        trace_id="test-evaluation-read-only",
        total_count=0,
    )


def make_read_only(engine):
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA query_only = ON")


def read_page(route, **filters):
    if route == "tasks":
        return evaluations.tasks(**filters)
    endpoint = evaluations.runs if route == "runs" else evaluations.records
    return endpoint(from_date=None, to_date=None, **filters)


@pytest.mark.parametrize("route", ["tasks", "runs", "records"])
def test_empty_lists_remain_empty_without_writes(evaluation_api, route):
    sessions, engine = evaluation_api
    make_read_only(engine)

    for _ in range(2):
        response = read_page(route)
        assert response["code"] == 0
        assert response["data"]["items"] == []
        assert response["data"]["total"] == 0

    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(EvaluationTask)) == 0
        assert db.scalar(select(func.count()).select_from(EvaluationRun)) == 0


@pytest.mark.parametrize("route", ["tasks", "runs", "records"])
def test_existing_task_without_run_does_not_reseed(evaluation_api, route):
    sessions, engine = evaluation_api
    with sessions() as db:
        db.add(existing_task())
        db.commit()
    make_read_only(engine)

    for _ in range(2):
        response = read_page(route)
        assert response["code"] == 0
        assert response["data"]["total"] == (1 if route == "tasks" else 0)

    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(EvaluationTask)) == 1
        assert db.scalar(select(func.count()).select_from(EvaluationRun)) == 0
        assert db.get(EvaluationTask, "seed-eval-task-1").name == "已有的评估任务"


def test_existing_terminal_run_is_still_returned(evaluation_api):
    sessions, engine = evaluation_api
    with sessions() as db:
        db.add(existing_task())
        db.add(EvaluationRun(
            run_id="seed-eval-run-1", task_id="seed-eval-task-1",
            record_id="seed-eval-record-1", test_no="", name="已有的评估结果",
            task_status="succeeded", judgment_status="passed",
        ))
        db.commit()
    make_read_only(engine)

    for route in ("runs", "records"):
        filters = {"judgment_status": "passed"}
        if route == "runs":
            filters["terminal_only"] = True
        response = read_page(route, **filters)
        assert response["code"] == 0
        data = response["data"]
        assert data["total"] == 1
        assert data["items"][0]["runId"] == "seed-eval-run-1"
