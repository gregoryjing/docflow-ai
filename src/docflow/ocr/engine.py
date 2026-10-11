"""分层 OCR 编排：先快后准，按置信度决定要不要动用 VL 模型.

分流规则：
- 一级 RapidOCR 平均置信度 ≥ 阈值 → 直接用，零额外成本；
- 低于阈值且配置了 VL 模型 → 升级到二级重识别；
- 低于阈值但没配 VL → 保留一级结果并留下提示，不阻断流程。

「快路径覆盖大多数、慢路径兜住疑难」是这套链路的核心取舍。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from docflow.config import Settings
from docflow.errors import OcrUnavailableError
from docflow.ocr.models import OcrEngineName, OcrPage
from docflow.ocr.rapid import RapidOcrEngine
from docflow.ocr.vl import VlOcrEngine


class TieredOcrEngine:
    """两级 OCR 的编排器。无状态，可复用."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.rapid = RapidOcrEngine()
        self.vl = VlOcrEngine(settings)

    def run(self, image: Any, column_hint: Sequence[str] | None = None) -> OcrPage:
        if not self.settings.ocr_enabled:
            raise OcrUnavailableError("OCR 链路未启用，请设置 DOCFLOW_OCR_ENABLED=true")

        rapid_available = self.rapid.available()
        vl_available = self.vl.available()

        if not rapid_available and not vl_available:
            raise OcrUnavailableError(
                'OCR 不可用：既没有安装一级 OCR（pip install "docflow-ai[ocr]"），'
                "也没有配置本地 VL 模型（DOCFLOW_VL_BASE_URL / DOCFLOW_VL_MODEL）"
            )

        if rapid_available:
            page = self.rapid.run(image)
            if page.lines and page.avg_confidence >= self.settings.ocr_min_confidence:
                return page
            if vl_available:
                escalated = self.vl.run(image, column_hint=column_hint)
                escalated.escalated_from = OcrEngineName.RAPID
                escalated.notes.insert(
                    0,
                    f"一级 OCR 平均置信度 {page.avg_confidence:.2f} 低于阈值 "
                    f"{self.settings.ocr_min_confidence:.2f}，已升级到 VL 模型",
                )
                return escalated
            page.notes.append("置信度偏低，但未配置 VL 模型，保留一级识别结果")
            return page

        page = self.vl.run(image, column_hint=column_hint)
        page.notes.append("未安装一级 OCR，直接使用 VL 模型")
        return page


def ocr_image(image: Any, settings: Settings, column_hint: Sequence[str] | None = None) -> OcrPage:
    """便捷函数：对单张图片跑一次分层 OCR."""
    return TieredOcrEngine(settings).run(image, column_hint=column_hint)
