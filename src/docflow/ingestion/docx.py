"""Word 接入：优先抽表格，没有表格则抽「键：值」段落."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from docflow.config import Settings
from docflow.errors import ParseError
from docflow.ingestion.header import build_table
from docflow.ingestion.keyvalue import extract_key_value_rows, key_value_table
from docflow.models.table import RawTable, SourceKind, SourceRef


def _load_docx() -> Any:
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise ParseError("Word 解析需要 python-docx，请执行 pip install python-docx") from exc
    return Document


def parse_docx(path: Path, settings: Settings) -> list[RawTable]:
    document_cls = _load_docx()
    try:
        document = document_cls(str(path))
    except Exception as exc:
        raise ParseError(f"{path.name} 打开失败：{exc}") from exc

    tables: list[RawTable] = []
    for index, table in enumerate(document.tables, start=1):
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        if not rows:
            continue
        source = SourceRef(
            file_name=path.name,
            kind=SourceKind.DOCX,
            file_path=str(path),
            location=f"表{index}",
        )
        built = build_table(
            rows,
            source,
            max_scan_rows=settings.max_header_scan_rows,
            min_fill_ratio=settings.header_min_fill_ratio,
        )
        if built is not None and built.row_count > 0:
            tables.append(built)

    if tables:
        return tables

    paragraphs = [paragraph.text for paragraph in document.paragraphs]
    pairs = extract_key_value_rows(paragraphs)
    if len(pairs) < 2:
        return []
    source = SourceRef(file_name=path.name, kind=SourceKind.DOCX, file_path=str(path), location="正文")
    built = build_table(
        key_value_table(pairs),
        source,
        max_scan_rows=1,
        min_fill_ratio=1.0,
        header_index=0,
    )
    if built is None or built.row_count == 0:
        return []
    built.notes.append("未识别到表格结构，已按「键：值」抽取（键作字段名）")
    return [built]
