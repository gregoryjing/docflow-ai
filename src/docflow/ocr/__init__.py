"""OCR 分层链路.

对外只需要知道 `ocr_image` / `parse_image` / `ocr_pdf_page` 三个入口：
一级 RapidOCR 快速识别，置信度不足时自动升级到本地 VL 模型。
"""

from __future__ import annotations

from docflow.ocr.engine import TieredOcrEngine, ocr_image
from docflow.ocr.ingest import ocr_pdf_page, parse_image, render_page_rgb
from docflow.ocr.models import OcrEngineName, OcrLine, OcrPage, lines_to_matrix
from docflow.ocr.rapid import RapidOcrEngine
from docflow.ocr.vl import VlOcrEngine

__all__ = [
    "OcrEngineName",
    "OcrLine",
    "OcrPage",
    "RapidOcrEngine",
    "TieredOcrEngine",
    "VlOcrEngine",
    "lines_to_matrix",
    "ocr_image",
    "ocr_pdf_page",
    "parse_image",
    "render_page_rgb",
]
