"""仓储层：把 PipelineResult 落库，并提供复核与统计查询.

上层（CLI / Web）只依赖这里的函数，不直接碰 ORM。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docflow.models.dedupe import DedupeResult, DuplicateGroup
from docflow.models.mapping import ColumnMapping, MappingMethod, MappingResult
from docflow.models.record import FieldValue, QualityIssue, ReviewStatus, UnifiedRecord
from docflow.models.table import SourceKind, SourceRef
from docflow.pipeline.report import FileFailure, FileWarning, PipelineResult, SourceOutcome
from docflow.security.audit import AuditEvent
from docflow.store.models import AuditLogRow, RecordRow, ReviewActionRow, RunRow


def _record_to_row(record: Any, run_id: str) -> RecordRow:
    return RecordRow(
        record_id=record.record_id,
        run_id=run_id,
        schema_name=record.schema_name,
        source_file=record.source_file,
        source_location=record.source_location,
        row_index=record.row_index,
        review_status=record.review_status.value,
        min_confidence=record.min_confidence,
        has_error=1 if record.has_error else 0,
        payload=record.to_flat(),
        fields_meta={
            key: {
                "confidence": field.confidence,
                "raw": field.raw,
                "source_column": field.source_column,
                "note": field.note,
            }
            for key, field in record.fields.items()
        },
        issues=[issue.model_dump(mode="json") for issue in record.issues],
    )


def save_run(session: Session, result: PipelineResult) -> RunRow:
    """落库一次处理结果（覆盖同 run_id，保证重复调用幂等）."""
    existing = session.get(RunRow, result.run_id)
    if existing is not None:
        session.delete(existing)
        session.flush()

    run = RunRow(
        run_id=result.run_id,
        schema_name=result.schema_name,
        schema_label=result.schema_label,
        status="done",
        started_at=result.started_at,
        finished_at=result.finished_at,
        source_files=[source.source.file_name for source in result.sources],
        failures=[failure.model_dump(mode="json") for failure in result.failures],
        file_warnings=[warning.model_dump(mode="json") for warning in result.file_warnings],
        sources=[
            {
                "label": source.source.label(),
                "file_name": source.source.file_name,
                "location": source.source.location,
                "kind": source.source.kind.value,
                "record_count": source.record_count,
                "review_pending": source.review_pending,
                "status": source.status,
                "columns": [
                    {
                        "source_column": column.source_column,
                        "target_field": column.target_field,
                        "confidence": column.confidence,
                        "method": column.method.value,
                        "is_mapped": column.is_mapped,
                        "reason": column.reason,
                    }
                    for column in source.mapping.columns
                ],
                "issues": [issue.model_dump(mode="json") for issue in source.table_issues],
            }
            for source in result.sources
        ],
        table_issues=[
            {"source": source.source.label(), **issue.model_dump(mode="json")}
            for source in result.sources
            for issue in source.table_issues
        ],
        dedupe_groups=[group.model_dump(mode="json") for group in result.dedupe.groups],
        file_count=len({source.source.file_name for source in result.sources}),
        record_count=result.total_records,
        error_count=result.error_count,
        warning_count=result.warning_count,
        pending_count=result.review_pending,
        approved_count=result.auto_approved,
    )
    session.add(run)
    session.flush()

    session.add_all([_record_to_row(record, result.run_id) for record in result.records])
    session.flush()
    return run


def list_runs(session: Session, limit: int = 50) -> list[RunRow]:
    statement = select(RunRow).order_by(RunRow.started_at.desc()).limit(limit)
    return list(session.scalars(statement))


def get_run(session: Session, run_id: str) -> RunRow | None:
    return session.get(RunRow, run_id)


def list_records(
    session: Session,
    run_id: str | None = None,
    status: ReviewStatus | None = None,
    limit: int | None = None,
) -> list[RecordRow]:
    statement = select(RecordRow)
    if run_id is not None:
        statement = statement.where(RecordRow.run_id == run_id)
    if status is not None:
        statement = statement.where(RecordRow.review_status == status.value)
    statement = statement.order_by(RecordRow.source_file, RecordRow.row_index)
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def get_record(session: Session, run_id: str, record_id: str) -> RecordRow | None:
    return session.get(RecordRow, (run_id, record_id))


def find_records_by_id(session: Session, record_id: str) -> list[RecordRow]:
    """按内容指纹跨运行查找（record_id 本身不保证全局唯一，可能命中多条）."""
    return list(session.scalars(select(RecordRow).where(RecordRow.record_id == record_id)))


def apply_review(
    session: Session,
    run_id: str,
    record_id: str,
    action: ReviewStatus,
    reviewer: str = "",
    note: str = "",
) -> RecordRow | None:
    """人工复核：更新记录状态并追加一条流水."""
    if action not in (ReviewStatus.APPROVED, ReviewStatus.REJECTED, ReviewStatus.PENDING):
        raise ValueError(f"不支持的复核动作：{action}")

    record = session.get(RecordRow, (run_id, record_id))
    if record is None:
        return None

    record.review_status = action.value
    record.reviewer = reviewer or record.reviewer
    record.review_note = note
    record.reviewed_at = datetime.now(UTC)
    session.add(
        ReviewActionRow(
            record_id=record_id,
            run_id=record.run_id,
            action=action.value,
            reviewer=reviewer,
            note=note,
        )
    )
    session.flush()
    _refresh_run_counts(session, record.run_id)
    return record


def _refresh_run_counts(session: Session, run_id: str) -> None:
    run = session.get(RunRow, run_id)
    if run is None:
        return
    pending = session.scalar(
        select(func.count())
        .select_from(RecordRow)
        .where(RecordRow.run_id == run_id, RecordRow.review_status == ReviewStatus.PENDING.value)
    )
    approved = session.scalar(
        select(func.count())
        .select_from(RecordRow)
        .where(
            RecordRow.run_id == run_id,
            # 「已通过」既含自动通过，也含人工确认——否则复核任意一条后，自动通过数会被抹掉
            RecordRow.review_status.in_([ReviewStatus.AUTO_APPROVED.value, ReviewStatus.APPROVED.value]),
        )
    )
    run.pending_count = int(pending or 0)
    run.approved_count = int(approved or 0)
    session.flush()


def review_stats(session: Session) -> dict[str, int]:
    """全局复核概览，用于首页看板."""
    rows = session.execute(select(RecordRow.review_status, func.count()).group_by(RecordRow.review_status)).all()
    stats = {status.value: 0 for status in ReviewStatus}
    for status_value, count in rows:
        stats[str(status_value)] = int(count)
    return stats


# ── 审计日志 ──────────────────────────────────────────────────
def record_audit(session: Session, event: AuditEvent) -> AuditLogRow:
    """追加一条审计流水并 flush，让同一事务里的后续查询能立刻看到."""
    row = AuditLogRow(
        actor=event.actor,
        role=event.role,
        action=event.action,
        target=event.target,
        detail=event.detail,
        created_at=event.at,
    )
    session.add(row)
    session.flush()
    return row


def list_audit(session: Session, limit: int = 100, action: str | None = None) -> list[AuditLogRow]:
    statement = select(AuditLogRow)
    if action:
        statement = statement.where(AuditLogRow.action == action)
    statement = statement.order_by(AuditLogRow.created_at.desc(), AuditLogRow.id.desc()).limit(limit)
    return list(session.scalars(statement))


# ── 从库里还原一次运行结果（用于「重新导出」「查看详情」而无需重跑）──
def _row_to_record(row: RecordRow) -> UnifiedRecord:
    fields: dict[str, FieldValue] = {}
    for key, value in (row.payload or {}).items():
        meta = (row.fields_meta or {}).get(key, {})
        fields[key] = FieldValue(
            value=value,
            raw=meta.get("raw"),
            confidence=meta.get("confidence", 1.0),
            source_column=meta.get("source_column"),
            note=meta.get("note", ""),
        )
    return UnifiedRecord(
        record_id=row.record_id,
        schema_name=row.schema_name,
        source_file=row.source_file,
        source_location=row.source_location,
        row_index=row.row_index,
        fields=fields,
        issues=[QualityIssue.model_validate(issue) for issue in (row.issues or [])],
        review_status=ReviewStatus(row.review_status),
        created_at=row.created_at,
    )


def _source_outcome(payload: dict[str, Any], records: list[UnifiedRecord]) -> SourceOutcome:
    source = SourceRef(
        file_name=payload.get("file_name", ""),
        kind=SourceKind(payload.get("kind", SourceKind.CSV.value)),
        location=payload.get("location", ""),
    )
    mapping = MappingResult(
        source=source,
        schema_name="",
        columns=[
            ColumnMapping(
                source_column=column["source_column"],
                target_field=column.get("target_field"),
                confidence=column.get("confidence", 0.0),
                method=MappingMethod(column.get("method", MappingMethod.NONE.value)),
                reason=column.get("reason", ""),
            )
            for column in payload.get("columns", [])
        ],
    )
    label = payload.get("label", "")
    matched = [record for record in records if f"{record.source_file}#{record.source_location}" == label]
    return SourceOutcome(
        source=source,
        mapping=mapping,
        record_count=payload.get("record_count", len(matched)),
        review_pending=payload.get("review_pending", 0),
        table_issues=[QualityIssue.model_validate(issue) for issue in payload.get("issues", [])],
    )


def load_run_result(session: Session, run_id: str) -> PipelineResult | None:
    """把库里的一次运行还原成 PipelineResult."""
    run = session.get(RunRow, run_id)
    if run is None:
        return None

    records = [_row_to_record(row) for row in list_records(session, run_id=run_id)]
    return PipelineResult(
        run_id=run.run_id,
        schema_name=run.schema_name,
        schema_label=run.schema_label,
        started_at=run.started_at,
        finished_at=run.finished_at,
        records=records,
        sources=[_source_outcome(payload, records) for payload in (run.sources or [])],
        failures=[FileFailure.model_validate(failure) for failure in (run.failures or [])],
        file_warnings=[FileWarning.model_validate(warning) for warning in (run.file_warnings or [])],
        dedupe=DedupeResult(groups=[DuplicateGroup.model_validate(group) for group in (run.dedupe_groups or [])]),
    )
