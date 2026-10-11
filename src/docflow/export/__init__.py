"""导出层：把一次处理结果导成 Excel（多 Sheet）或 CSV（多文件）.

Excel 是给业务同事看的，所以除了记录本身，还要带「质检报告」和「字段映射」两个 Sheet——
让「数据为什么少了/为什么被标红」在同一个文件里就能解释清楚。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pandas as pd

from docflow.models.dedupe import DuplicateMode
from docflow.models.record import IssueLevel
from docflow.models.schema import SchemaDefinition
from docflow.pipeline.report import PipelineResult

_META_COLUMNS = ["来源文件", "来源位置", "行号", "复核状态", "最低置信度"]


def _as_nullable_int(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    """把「整数但允许为空」的列统一成可空 Int64.

    行号列里只要混入一个 None（表级问题没有行号），pandas 会把整列升成 float，
    于是 CSV/Excel 里出现「1.0」这种别扭写法，和 records.csv 的「1」对不上。
    显式转成 Int64 后，两种导出文件的行号显示一致。
    """
    if column in frame.columns:
        frame[column] = frame[column].astype("Int64")
    return frame


def _ordered_field_keys(result: PipelineResult, schema: SchemaDefinition | None) -> list[str]:
    """导出列顺序：优先按 Schema 定义，未在 Schema 中的字段补在后面."""
    keys = list(schema.field_keys()) if schema is not None else []
    seen = set(keys)
    for record in result.records:
        for key in record.fields:
            if key not in seen:
                keys.append(key)
                seen.add(key)
    return keys


def records_frame(result: PipelineResult, schema: SchemaDefinition | None = None) -> pd.DataFrame:
    keys = _ordered_field_keys(result, schema)
    rows: list[dict[str, Any]] = []
    for record in result.records:
        flat = record.to_flat()
        row: dict[str, Any] = {key: flat.get(key) for key in keys}
        row.update(
            {
                "来源文件": record.source_file,
                "来源位置": record.source_location,
                "行号": (record.row_index + 1) if record.row_index is not None else None,
                "复核状态": record.review_status.value,
                "最低置信度": round(record.min_confidence, 3),
            }
        )
        rows.append(row)
    frame = pd.DataFrame(rows, columns=[*keys, *_META_COLUMNS])
    return _as_nullable_int(frame, "行号")


def issues_frame(result: PipelineResult) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for record in result.records:
        for issue in record.issues:
            rows.append(
                {
                    "来源文件": record.source_file,
                    "来源位置": record.source_location,
                    "行号": (record.row_index + 1) if record.row_index is not None else None,
                    "等级": issue.level.value,
                    "编码": issue.code.value,
                    "字段": issue.field or "",
                    "值": "" if issue.value is None else str(issue.value),
                    "说明": issue.message,
                }
            )
    for source in result.sources:
        for issue in source.table_issues:
            rows.append(
                {
                    "来源文件": source.source.file_name,
                    "来源位置": source.source.location,
                    "行号": None,
                    "等级": issue.level.value,
                    "编码": issue.code.value,
                    "字段": issue.field or "",
                    "值": "" if issue.value is None else str(issue.value),
                    "说明": issue.message,
                }
            )
    frame = pd.DataFrame(rows, columns=["来源文件", "来源位置", "行号", "等级", "编码", "字段", "值", "说明"])
    return _as_nullable_int(frame, "行号")


def mapping_frame(result: PipelineResult) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for source in result.sources:
        for column in source.mapping.columns:
            rows.append(
                {
                    "来源文件": source.source.file_name,
                    "来源位置": source.source.location,
                    "源列": column.source_column,
                    "目标字段": column.target_field or "",
                    "置信度": round(column.confidence, 3),
                    "映射方式": column.method.value,
                    "是否映射": "是" if column.is_mapped else "否",
                    "说明": column.reason,
                }
            )
    return pd.DataFrame(
        rows,
        columns=["来源文件", "来源位置", "源列", "目标字段", "置信度", "映射方式", "是否映射", "说明"],
    )


def summary_frame(result: PipelineResult) -> pd.DataFrame:
    counts = result.issue_counts
    rows = [
        ("运行 ID", result.run_id),
        ("Schema", f"{result.schema_label}（{result.schema_name}）"),
        ("开始时间", result.started_at.strftime("%Y-%m-%d %H:%M:%S")),
        ("耗时（秒）", round(result.duration_seconds, 3)),
        ("来源文件数", len({source.source.file_name for source in result.sources})),
        ("来源表数", len(result.sources)),
        ("记录总数", result.total_records),
        ("自动通过", result.auto_approved),
        ("待复核", result.review_pending),
        ("重复组数", result.duplicate_groups),
        ("冗余记录", result.duplicate_records),
        ("错误（error）", counts[IssueLevel.ERROR.value]),
        ("警告（warning）", counts[IssueLevel.WARNING.value]),
        ("提示（info）", counts[IssueLevel.INFO.value]),
        ("解析失败文件", len(result.failures)),
    ]
    return pd.DataFrame(rows, columns=["指标", "值"])


def dedupe_frame(result: PipelineResult) -> pd.DataFrame:
    """去重报告：哪些记录被判定为指向同一实体，是否必须人工确认."""
    rows: list[dict[str, Any]] = []
    for group in result.dedupe.groups:
        rows.append(
            {
                "模式": "精确" if group.mode is DuplicateMode.EXACT else "模糊",
                "实体标识": group.key_value,
                "记录数": group.size,
                "相似度": round(group.similarity, 3),
                "需人工确认": "是" if group.review_required else "否",
                "说明": group.detail,
                "记录 ID": ", ".join(group.record_ids),
            }
        )
    return pd.DataFrame(rows, columns=["模式", "实体标识", "记录数", "相似度", "需人工确认", "说明", "记录 ID"])


def _failure_frame(result: PipelineResult) -> pd.DataFrame:
    return pd.DataFrame(
        [{"文件": failure.file_name, "错误": failure.error} for failure in result.failures],
        columns=["文件", "错误"],
    )


def export_excel(
    result: PipelineResult,
    path: Path,
    schema: SchemaDefinition | None = None,
) -> Path:
    """导出为单个 Excel：汇总 / 记录 / 问题清单 / 字段映射 / 去重组 / 失败文件."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        summary_frame(result).to_excel(writer, sheet_name="汇总", index=False)
        records_frame(result, schema).to_excel(writer, sheet_name="记录", index=False)
        issues_frame(result).to_excel(writer, sheet_name="问题清单", index=False)
        mapping_frame(result).to_excel(writer, sheet_name="字段映射", index=False)
        if result.dedupe.groups:
            dedupe_frame(result).to_excel(writer, sheet_name="去重组", index=False)
        if result.failures:
            _failure_frame(result).to_excel(writer, sheet_name="失败文件", index=False)
        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A2"
    return path


def export_csv(
    result: PipelineResult,
    directory: Path,
    schema: SchemaDefinition | None = None,
) -> list[Path]:
    """导出为一组 UTF-8-SIG CSV（Excel 直接双击不乱码）."""
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    frames = {
        "summary": summary_frame(result),
        "records": records_frame(result, schema),
        "issues": issues_frame(result),
        "mapping": mapping_frame(result),
    }
    if result.dedupe.groups:
        frames["dedupe"] = dedupe_frame(result)
    if result.failures:
        frames["failures"] = _failure_frame(result)

    for name, frame in frames.items():
        target = directory / f"{name}.csv"
        frame.to_csv(target, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)
        written.append(target)
    return written


def export_auto(
    result: PipelineResult,
    target: Path,
    schema: SchemaDefinition | None = None,
    as_csv: bool = False,
) -> Path:
    """按目标形态选择导出方式：`as_csv` 或目标无后缀 → 一组 CSV；否则单个 Excel."""
    if as_csv or target.suffix == "":
        directory = target.with_suffix("") if (as_csv and target.suffix) else target
        export_csv(result, directory, schema)
        return directory
    return export_excel(result, target, schema)
