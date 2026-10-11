"""接入层入口：把任意受支持的文件变成 RawTable 列表.

对外只暴露 ingest_file / ingest_paths 两个函数，上层（管线、CLI、Web）不需要知道
具体格式是怎么解析的。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from docflow.config import Settings
from docflow.errors import DocFlowError, UnsupportedFormatError
from docflow.ingestion.detector import detect_kind
from docflow.ingestion.docx import parse_docx
from docflow.ingestion.pdf import parse_pdf, pdf_is_scanned
from docflow.ingestion.tabular import parse_tabular
from docflow.models.table import RawTable, SourceKind

__all__ = [
    "detect_kind",
    "ingest_file",
    "ingest_paths",
    "parse_docx",
    "parse_pdf",
    "parse_tabular",
]


def ingest_file(path: Path, settings: Settings, column_hint: Sequence[str] | None = None) -> list[RawTable]:
    """解析单个文件，返回其中所有可用的表（Excel 多 Sheet、PDF 多页多表）.

    `column_hint` 是目标 Schema 的列名，仅用于 OCR 链路给 VL 模型提供表头参考。
    """
    if not path.exists():
        raise UnsupportedFormatError(f"文件不存在：{path}")

    kind = detect_kind(path)
    if kind in (SourceKind.EXCEL, SourceKind.CSV):
        return parse_tabular(path, kind, settings)
    if kind in (SourceKind.PDF_TEXT, SourceKind.DOCX):
        if settings.document_backend == "docling":
            from docflow.ingestion.docling_backend import parse_with_docling

            return parse_with_docling(path, settings, column_hint=column_hint)
        if kind is SourceKind.PDF_TEXT:
            return parse_pdf(path, settings, column_hint=column_hint)
        return parse_docx(path, settings)
    if kind is SourceKind.IMAGE:
        if not settings.ocr_enabled:
            raise UnsupportedFormatError(
                f"{path.name}：图片需要 OCR 链路，请设置 DOCFLOW_OCR_ENABLED=true 并安装 "
                'ocr 依赖（pip install "docflow-ai[ocr]"）'
            )
        from docflow.ocr.ingest import parse_image

        return parse_image(path, settings, column_hint=column_hint)
    raise UnsupportedFormatError(f"{path.name}：暂不支持的来源类型 {kind}")


def ingest_paths(
    paths: list[Path],
    settings: Settings,
    column_hint: Sequence[str] | None = None,
) -> tuple[list[RawTable], list[tuple[str, str]], list[tuple[str, str]]]:
    """批量接入。返回 (表列表, 解析失败, 空产出告警).

    - 解析失败（异常）：文件进 failures，整批继续；
    - **解析成功但一张表都没产出**：进 warnings。这类文件过去会被静默丢弃，
      运行照样报成功、却一条记录都没有（扫描件未开 OCR、空 CSV 都属此类），
      必须显式告诉用户「这个文件为什么没进来」。
    """
    tables: list[RawTable] = []
    failures: list[tuple[str, str]] = []
    warnings: list[tuple[str, str]] = []
    for path in paths:
        try:
            produced = ingest_file(path, settings, column_hint=column_hint)
        except DocFlowError as exc:
            failures.append((path.name, str(exc)))
            continue
        if not produced:
            warnings.append((path.name, _empty_reason(path, settings)))
        tables.extend(produced)
    return tables, failures, warnings


def _empty_reason(path: Path, settings: Settings) -> str:
    """文件解析成功却零产出时，给出最可能的原因，避免「静默成功」。"""
    try:
        kind = detect_kind(path)
    except DocFlowError:
        return "未解析出任何数据表"
    if kind is SourceKind.PDF_TEXT:
        if not settings.ocr_enabled and pdf_is_scanned(path, settings):
            return (
                "未解析出数据表：扫描型 PDF（文本层过薄），且未启用 OCR；"
                "设置 DOCFLOW_OCR_ENABLED=true 并安装 ocr 依赖后可走 OCR 链路"
            )
        return "未解析出数据表：PDF 中有文本，但未识别到表格或键值结构"
    if kind is SourceKind.IMAGE:
        return "未解析出数据表：图片 OCR 未识别到有效表格"
    if kind is SourceKind.CSV:
        return "未解析出数据表：CSV 为空或没有可识别的数据行"
    if kind is SourceKind.EXCEL:
        return "未解析出数据表：Excel 各 Sheet 均为空"
    return "未解析出任何数据表（文件可能为空或缺少可识别内容）"
