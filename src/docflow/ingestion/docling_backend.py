"""Docling 高级解析后端（可选）：更精准的版面分析与表格结构识别.

定位：作为内置 PyMuPDF / python-docx 链路之外的**可插拔后端**，默认关闭、不改变现有行为。
Docling 会做版面分析、阅读顺序还原与表格结构识别，对复杂排版的 PDF/Word 明显更稳；
代价是依赖重（torch 等）、速度慢，所以按需在 `.env` 里打开 `DOCFLOW_DOCUMENT_BACKEND=docling`。

本模块对 Docling 的 API 保持防御式调用：不同版本的方法签名有差异，
宁可退化为「没有表格」也不让整份文档解析失败。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from docflow.config import Settings
from docflow.errors import ParseError
from docflow.ingestion.header import build_table
from docflow.ingestion.keyvalue import extract_key_value_rows, key_value_table
from docflow.models.table import RawTable, SourceKind, SourceRef

_INSTALL_HINT = 'Docling 后端需要额外依赖：pip install "docflow-ai[docling]"'


def docling_available() -> bool:
    try:
        import docling  # noqa: F401
    except ImportError:
        return False
    return True


def parse_with_docling(
    path: Path,
    settings: Settings,
    column_hint: Sequence[str] | None = None,
) -> list[RawTable]:
    """用 Docling 解析 PDF / Word，抽出其中的表格."""
    if not docling_available():
        raise ParseError(f"{path.name}：{_INSTALL_HINT}")
    converter = _build_converter(settings)
    try:
        result = converter.convert(str(path))
    except Exception as exc:
        raise ParseError(f"{path.name} Docling 解析失败：{exc}") from exc

    document = result.document
    tables = _document_to_tables(document, path, settings)
    if tables:
        return tables
    return _text_fallback(document, path, settings)


def _build_converter(settings: Settings) -> Any:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = settings.docling_do_ocr
    pipeline_options.do_table_structure = True
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)})


def _document_to_tables(document: Any, path: Path, settings: Settings) -> list[RawTable]:
    tables: list[RawTable] = []
    for order, table in enumerate(getattr(document, "tables", None) or [], start=1):
        frame = _export_frame(table, document)
        if frame is None:
            continue
        columns = [str(column) for column in frame.columns]
        rows = [
            {column: _clean(value) for column, value in record.items()} for record in frame.to_dict(orient="records")
        ]
        rows = [row for row in rows if any(value is not None for value in row.values())]
        if not rows:
            continue
        source = SourceRef(
            file_name=path.name,
            kind=SourceKind.PDF_TEXT,
            file_path=str(path),
            location=f"表{order}",
        )
        built = build_table(
            [columns, *[[row[column] for column in columns] for row in rows]],
            source,
            max_scan_rows=1,
            min_fill_ratio=1.0,
        )
        if built is not None and built.row_count > 0:
            built.notes.append("由 Docling 后端解析")
            tables.append(built)
    return tables


def _export_frame(table: Any, document: Any) -> Any:
    """把 Docling 的表格对象导成 DataFrame；版本差异用「先带参再退化为无参」兜底."""
    exporter = getattr(table, "export_to_dataframe", None)
    if exporter is None:
        return None
    for kwargs in ({"doc": document}, {}):
        try:
            return exporter(**kwargs)
        except TypeError:
            continue
        except Exception:
            return None
    return None


def _text_fallback(document: Any, path: Path, settings: Settings) -> list[RawTable]:
    """没识别出表格时，退回「键：值」抽取，把正文转成「键作字段、值作一行」的表."""
    text = ""
    exporter = getattr(document, "export_to_markdown", None)
    if exporter is not None:
        try:
            text = exporter() or ""
        except Exception:
            text = ""
    pairs = extract_key_value_rows(text.splitlines()) if text else []
    if len(pairs) < 2:
        return []
    source = SourceRef(file_name=path.name, kind=SourceKind.PDF_TEXT, file_path=str(path), location="正文")
    built = build_table(
        key_value_table(pairs),
        source,
        max_scan_rows=1,
        min_fill_ratio=1.0,
        header_index=0,
    )
    if built is None or built.row_count == 0:
        return []
    built.notes.append("由 Docling 后端解析，未识别到表格结构，已按「键：值」抽取（键作字段名）")
    return [built]


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str):
        return value.strip() or None
    return value
