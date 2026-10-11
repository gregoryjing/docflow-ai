"""PDF 接入：文本型走文本层提取，扫描型留给 M2 的 OCR 链路.

策略：
1. 逐页取文本层，字符数低于阈值判定为扫描页 → 跳过（M2 由 OCR 分支接管）；
2. 文本页优先用 PyMuPDF 的表格识别；
3. 识别不到表格就退化为「键：值」抽取，把技术说明/合同转成两列表。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from docflow.config import Settings
from docflow.errors import DocFlowError, ParseError
from docflow.ingestion.header import build_table
from docflow.ingestion.keyvalue import extract_key_value_rows, key_value_table
from docflow.models.table import RawTable, SourceKind, SourceRef


def _load_fitz() -> Any:
    try:
        import pymupdf as fitz  # PyMuPDF 1.24+ 推荐的新名字，避免弃用告警
    except ImportError:
        try:
            import fitz  # type: ignore[no-redef]
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise ParseError("PDF 解析需要 PyMuPDF，请执行 pip install pymupdf") from exc
    return fitz


def _table_from_finder(finder_tables: list[Any], path: Path, location: str, settings: Settings) -> list[RawTable]:
    tables: list[RawTable] = []
    for order, table in enumerate(finder_tables, start=1):
        try:
            rows = table.extract()
        except Exception:
            continue
        if not rows:
            continue
        source = SourceRef(
            file_name=path.name,
            kind=SourceKind.PDF_TEXT,
            file_path=str(path),
            location=f"{location}·表{order}",
        )
        built = build_table(
            [list(row) for row in rows],
            source,
            max_scan_rows=settings.max_header_scan_rows,
            min_fill_ratio=settings.header_min_fill_ratio,
        )
        if built is not None and built.row_count > 0:
            tables.append(built)
    return tables


def _table_from_text(text: str, path: Path, location: str, settings: Settings) -> list[RawTable]:
    pairs = extract_key_value_rows(text.splitlines())
    if len(pairs) < 2:
        return []
    source = SourceRef(file_name=path.name, kind=SourceKind.PDF_TEXT, file_path=str(path), location=location)
    built = build_table(
        key_value_table(pairs),
        source,
        max_scan_rows=1,
        min_fill_ratio=1.0,
        header_index=0,  # 键名即表头，不需要再猜
    )
    if built is None or built.row_count == 0:
        return []
    built.notes.append("未识别到表格结构，已按「键：值」抽取（键作字段名）")
    return [built]


def pdf_is_scanned(path: Path, settings: Settings) -> bool:
    """粗略判断整份 PDF 是否为扫描件：所有页的文本层都薄于阈值.

    仅用于「零产出时给出准确原因」，不参与解析决策，所以失败一律返回 False。
    """
    try:
        fitz = _load_fitz()
        document = fitz.open(str(path))
    except Exception:  # pragma: no cover - 文件已在上游解析过，这里只兜底
        return False
    try:
        if document.needs_pass:
            return False
        return all(len((page.get_text("text") or "").strip()) < settings.pdf_text_min_chars for page in document)
    finally:
        document.close()


def parse_pdf(path: Path, settings: Settings, column_hint: Sequence[str] | None = None) -> list[RawTable]:
    fitz = _load_fitz()
    try:
        document = fitz.open(str(path))
    except Exception as exc:
        raise ParseError(f"{path.name} 打开失败：{exc}") from exc

    tables: list[RawTable] = []
    try:
        if document.needs_pass:
            raise ParseError(f"{path.name} 已加密，请先解除密码保护")
        for page_index, page in enumerate(document, start=1):
            location = f"第{page_index}页"
            text = page.get_text("text") or ""
            if len(text.strip()) < settings.pdf_text_min_chars:
                # 文本层太薄：判定为扫描页。启用 OCR 时交给分层链路，否则跳过。
                if settings.ocr_enabled:
                    try:
                        from docflow.ocr.ingest import ocr_pdf_page

                        tables.extend(ocr_pdf_page(page, page_index, path, settings, column_hint=column_hint))
                    except DocFlowError:
                        continue  # 单页 OCR 失败不阻断整份文档
                continue
            try:
                finder = page.find_tables()
            except Exception:
                finder = None
            page_tables = _table_from_finder(list(finder.tables), path, location, settings) if finder else []
            if page_tables:
                tables.extend(page_tables)
            else:
                tables.extend(_table_from_text(text, path, location, settings))
    finally:
        document.close()
    return tables
