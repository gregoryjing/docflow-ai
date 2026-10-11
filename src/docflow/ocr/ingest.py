"""把 OCR 结果接进接入层：图片 / 扫描 PDF 页 → RawTable.

这里只负责「拿到矩阵 → 交给 build_table 做表头识别」，不重复实现表头逻辑。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from docflow.config import Settings
from docflow.ingestion.header import build_table
from docflow.models.table import RawTable, SourceKind, SourceRef
from docflow.ocr.engine import TieredOcrEngine
from docflow.ocr.models import OcrPage


def _table_from_page(
    page: OcrPage,
    source: SourceRef,
    settings: Settings,
) -> RawTable | None:
    matrix = page.to_matrix()
    if not matrix:
        return None
    built = build_table(
        matrix,
        source,
        max_scan_rows=settings.max_header_scan_rows,
        min_fill_ratio=settings.header_min_fill_ratio,
    )
    if built is None:
        return None

    notes = [f"OCR 引擎：{page.engine.value}"]
    if page.escalated_from is not None:
        notes.append(f"由 {page.escalated_from.value} 升级而来")
    if page.lines:
        notes.append(f"平均置信度 {page.avg_confidence:.2f}")
    built.notes.extend([*notes, *page.notes])
    return built


def render_page_rgb(page: Any, dpi: int) -> Any:
    """用 PyMuPDF 把 PDF 页渲染成 RGB numpy 数组."""
    import numpy as np

    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover - 兼容旧版
        import fitz  # type: ignore[no-redef]

    pixmap = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB)
    flat = np.frombuffer(pixmap.samples, dtype=np.uint8)
    array = flat.reshape(pixmap.height, pixmap.width, pixmap.n)
    return array[:, :, :3]


def ocr_pdf_page(
    page: Any,
    page_number: int,
    path: Path,
    settings: Settings,
    column_hint: Sequence[str] | None = None,
) -> list[RawTable]:
    """对单个扫描 PDF 页跑 OCR."""
    image = render_page_rgb(page, settings.pdf_render_dpi)
    ocr_page = TieredOcrEngine(settings).run(image, column_hint=column_hint)
    source = SourceRef(
        file_name=path.name,
        kind=SourceKind.PDF_OCR,
        file_path=str(path),
        location=f"第{page_number}页",
    )
    built = _table_from_page(ocr_page, source, settings)
    return [built] if built is not None else []


def parse_image(path: Path, settings: Settings, column_hint: Sequence[str] | None = None) -> list[RawTable]:
    """单张图片（扫描件照片/截图）接入."""
    ocr_page = TieredOcrEngine(settings).run(path, column_hint=column_hint)
    source = SourceRef(
        file_name=path.name,
        kind=SourceKind.IMAGE,
        file_path=str(path),
        location="",
    )
    built = _table_from_page(ocr_page, source, settings)
    return [built] if built is not None else []
