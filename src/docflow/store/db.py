"""数据库连接与会话管理.

默认落 SQLite（零配置、单文件、便于演示），`DOCFLOW_DATABASE_URL` 可切到 PostgreSQL。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from docflow.config import Settings
from docflow.errors import StorageError
from docflow.store.models import Base


def create_db_engine(settings: Settings) -> Engine:
    url = settings.resolved_database_url
    if url.startswith("sqlite"):
        db_path = settings.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, future=True, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection: object, _record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            # WAL 让「读」不阻塞「写」，CLI 与 Web 并发访问同一库时不至于互相卡死；
            # busy_timeout 让并发写在短暂锁竞争时排队等待，而不是立刻抛 database is locked。
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        return engine
    return create_engine(url, future=True, pool_pre_ping=True)


def init_database(engine: Engine) -> None:
    """建表（幂等），并在建表前修复历史库的主键结构、建表后补齐新增列."""
    _migrate_records_composite_pk(engine)
    Base.metadata.create_all(engine)
    _add_missing_run_columns(engine)


def _add_missing_run_columns(engine: Engine) -> None:
    """给旧库补上后续版本新增的列（SQLite 的 create_all 不会改动已存在的表）."""
    inspector = inspect(engine)
    if "runs" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("runs")}
    additions = {
        "dedupe_groups": "JSON NOT NULL DEFAULT '[]'",
        "file_warnings": "JSON NOT NULL DEFAULT '[]'",
    }
    missing = {name: ddl for name, ddl in additions.items() if name not in existing}
    if not missing:
        return
    with engine.begin() as connection:
        for name, ddl in missing.items():
            connection.exec_driver_sql(f"ALTER TABLE runs ADD COLUMN {name} {ddl}")


def _migrate_records_composite_pk(engine: Engine) -> None:
    """把旧版 `records` 表就地迁移到 (run_id, record_id) 联合主键.

    record_id 是内容指纹，同一文件重复处理会得到相同值；旧结构只用 record_id 做主键，
    第二次处理必然撞主键。SQLite 的 `create_all` 不会改动已存在的表，所以这里重建并搬运数据。
    """
    inspector = inspect(engine)
    if "records" not in inspector.get_table_names():
        return
    pk_columns = inspector.get_pk_constraint("records").get("constrained_columns") or []
    if set(pk_columns) == {"run_id", "record_id"}:
        return

    legacy_indexes = [index["name"] for index in inspector.get_indexes("records") if index.get("name")]
    table = Base.metadata.tables["records"]
    # 只搬运「新旧表都存在」的列：旧库可能缺少后续版本新增的列，
    # 若按当前 metadata 的全列拼 SELECT，老库会因未知列直接迁移失败。
    legacy_columns = {column["name"] for column in inspector.get_columns("records")}
    shared = [column.name for column in table.columns if column.name in legacy_columns]
    if not shared:  # pragma: no cover - 表结构完全对不上时放弃迁移，交给 create_all
        return
    columns = ", ".join(shared)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE records RENAME TO records_legacy")
        # 索引名与表名无关，重命名后仍占用原名，先清掉再建新表
        for name in legacy_indexes:
            connection.exec_driver_sql(f'DROP INDEX IF EXISTS "{name}"')
        table.create(connection)
        connection.exec_driver_sql(f"INSERT INTO records ({columns}) SELECT {columns} FROM records_legacy")
        connection.exec_driver_sql("DROP TABLE records_legacy")


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception as exc:
        session.rollback()
        raise StorageError(f"数据库操作失败：{exc}") from exc
    finally:
        session.close()


def build_session_factory(settings: Settings) -> sessionmaker[Session]:
    engine = create_db_engine(settings)
    init_database(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def ensure_sqlite_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
