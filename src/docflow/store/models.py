"""SQLAlchemy ORM 表结构.

设计取舍：
- `runs` 存一次处理的汇总（指标、失败文件、表级问题、各来源映射），详情页不必重跑；
- `records` 存每条统一记录（标准字段值 + 字段级元信息 + 问题），复核状态直接落列，
  这样「跨运行查所有待复核记录」是一次普通索引查询，而不是遍历 JSON；
- `review_actions` 记流水，保证「谁在什么时候改了什么」可追溯。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class RunRow(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    schema_name: Mapped[str] = mapped_column(String(64), index=True)
    schema_label: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(16), default="done")

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source_files: Mapped[list[str]] = mapped_column(JSON, default=list)
    failures: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    file_warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    table_issues: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    dedupe_groups: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    file_count: Mapped[int] = mapped_column(Integer, default=0)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, default=0)
    pending_count: Mapped[int] = mapped_column(Integer, default=0)
    approved_count: Mapped[int] = mapped_column(Integer, default=0)

    records: Mapped[list[RecordRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )


class RecordRow(Base):
    __tablename__ = "records"

    # record_id 是「内容指纹」（文件+行+主键值），同一份文件在不同运行里会得到相同值，
    # 所以必须和 run_id 组成联合主键，否则第二次处理同一文件就会撞主键。
    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(32), primary_key=True, index=True)
    schema_name: Mapped[str] = mapped_column(String(64), index=True)

    source_file: Mapped[str] = mapped_column(String(255), default="")
    source_location: Mapped[str] = mapped_column(String(128), default="")
    row_index: Mapped[int | None] = mapped_column(Integer, nullable=True)

    review_status: Mapped[str] = mapped_column(String(16), default="auto_approved", index=True)
    min_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    has_error: Mapped[int] = mapped_column(Integer, default=0)

    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    fields_meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    issues: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    reviewer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    run: Mapped[RunRow] = relationship(back_populates="records")


class ReviewActionRow(Base):
    __tablename__ = "review_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(String(32), index=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(16))
    reviewer: Mapped[str] = mapped_column(String(128), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditLogRow(Base):
    """审计流水：覆盖处理 / 复核 / 导出 / 越权拦截，比 review_actions 更广."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(128), default="", index=True)
    role: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(32), index=True)
    target: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
