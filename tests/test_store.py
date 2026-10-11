"""持久化层：重复处理不撞主键（回归）+ 复核流水 + 旧库迁移."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import func, select

from docflow.config import Settings
from docflow.models.record import ReviewStatus
from docflow.pipeline import run_pipeline
from docflow.store import (
    apply_review,
    build_session_factory,
    create_db_engine,
    find_records_by_id,
    get_record,
    get_run,
    init_database,
    load_run_result,
    review_stats,
    save_run,
)
from docflow.store.models import RecordRow

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"

_LEGACY_SCHEMA = """
CREATE TABLE runs (
    run_id VARCHAR(32) NOT NULL,
    schema_name VARCHAR(64) NOT NULL,
    schema_label VARCHAR(128) NOT NULL,
    status VARCHAR(16) NOT NULL,
    started_at DATETIME NOT NULL,
    finished_at DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    source_files JSON NOT NULL,
    failures JSON NOT NULL,
    sources JSON NOT NULL,
    table_issues JSON NOT NULL,
    file_count INTEGER NOT NULL,
    record_count INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    warning_count INTEGER NOT NULL,
    pending_count INTEGER NOT NULL,
    approved_count INTEGER NOT NULL,
    PRIMARY KEY (run_id)
);
CREATE TABLE records (
    record_id VARCHAR(32) NOT NULL,
    run_id VARCHAR(32) NOT NULL,
    schema_name VARCHAR(64) NOT NULL,
    source_file VARCHAR(255) NOT NULL,
    source_location VARCHAR(128) NOT NULL,
    row_index INTEGER,
    review_status VARCHAR(16) NOT NULL,
    min_confidence FLOAT NOT NULL,
    has_error INTEGER NOT NULL,
    payload JSON NOT NULL,
    fields_meta JSON NOT NULL,
    issues JSON NOT NULL,
    reviewer VARCHAR(128),
    review_note TEXT NOT NULL,
    reviewed_at DATETIME,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (record_id),
    FOREIGN KEY(run_id) REFERENCES runs (run_id) ON DELETE CASCADE
);
CREATE INDEX ix_records_run_id ON records (run_id);
INSERT INTO runs (run_id, schema_name, schema_label, status, started_at, finished_at,
                  source_files, failures, sources, table_issues, file_count, record_count,
                  error_count, warning_count, pending_count, approved_count)
VALUES ('r1', 'purchase_order', '采购订单', 'done', '2024-01-01', '2024-01-01',
        '[]', '[]', '[]', '[]', 1, 1, 0, 0, 0, 0);
INSERT INTO records (record_id, run_id, schema_name, source_file, source_location, row_index,
                     review_status, min_confidence, has_error, payload, fields_meta, issues,
                     review_note, created_at)
VALUES ('rec1', 'r1', 'purchase_order', 'a.csv', '', 0, 'auto_approved', 1.0, 0,
        '{"order_no": "PO-2024-001"}', '{}', '[]', '', '2024-01-01');
"""


def test_processing_same_file_twice_does_not_collide(settings, purchase_order, order_csv):
    factory = build_session_factory(settings)
    first = run_pipeline(purchase_order, settings, [order_csv])
    second = run_pipeline(purchase_order, settings, [order_csv])

    # 同一份文件 → 内容指纹相同（record_id 一致），但运行 ID 不同
    assert first.run_id != second.run_id
    assert {record.record_id for record in first.records} == {record.record_id for record in second.records}

    with factory() as session:
        save_run(session, first)
        session.commit()
    with factory() as session:
        save_run(session, second)  # 旧结构在这里会抛 UNIQUE constraint failed
        session.commit()
        total = session.scalar(select(func.count()).select_from(RecordRow))

    assert total == 2 * first.total_records


def test_review_flow_updates_status_and_run_counts(settings, purchase_order, order_csv):
    factory = build_session_factory(settings)
    result = run_pipeline(purchase_order, settings, [order_csv])
    with factory() as session:
        save_run(session, result)
        session.commit()

    record_id = result.records[0].record_id
    with factory() as session:
        assert get_record(session, result.run_id, record_id) is not None
        assert len(find_records_by_id(session, record_id)) == 1
        apply_review(session, result.run_id, record_id, ReviewStatus.REJECTED, reviewer="qa", note="有问题")
        session.commit()

    with factory() as session:
        row = get_record(session, result.run_id, record_id)
        assert row is not None
        assert row.review_status == ReviewStatus.REJECTED.value
        assert row.reviewer == "qa"

        run = get_run(session, result.run_id)
        assert run is not None
        assert run.approved_count == result.total_records - 1
        assert review_stats(session)[ReviewStatus.REJECTED.value] == 1


def test_load_run_result_round_trips(settings, purchase_order, order_csv):
    factory = build_session_factory(settings)
    result = run_pipeline(purchase_order, settings, [order_csv])
    with factory() as session:
        save_run(session, result)
        session.commit()

    with factory() as session:
        loaded = load_run_result(session, result.run_id)

    assert loaded is not None
    assert loaded.total_records == 2
    assert loaded.schema_name == "purchase_order"
    assert loaded.records[0].get("order_no") == "PO-2024-001"
    assert loaded.sources[0].record_count == 2


def test_init_database_migrates_legacy_records_primary_key(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(_LEGACY_SCHEMA)
    connection.commit()
    connection.close()

    settings = Settings(
        data_dir=tmp_path,
        schemas_dir=SCHEMAS_DIR,
        database_url=f"sqlite+pysqlite:///{db_path.as_posix()}",
    )
    engine = create_db_engine(settings)
    init_database(engine)

    inspector = sa.inspect(engine)
    assert set(inspector.get_pk_constraint("records")["constrained_columns"]) == {
        "run_id",
        "record_id",
    }
    assert "records_legacy" not in inspector.get_table_names()

    with engine.connect() as conn:
        assert conn.exec_driver_sql("select count(*) from records").scalar() == 1
