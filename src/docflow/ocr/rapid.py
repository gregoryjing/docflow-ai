"""一级 OCR：RapidOCR（ONNX Runtime）.

选它的理由：纯本地、CPU 可跑、无需 GPU、模型体积小，适合把「大部分正常扫描件」
快速吃掉；识别质量差的手写件/低质扫描件再交给二级 VL 模型。
"""

from __future__ import annotations

from typing import Any

from docflow.errors import OcrUnavailableError
from docflow.ocr.image import to_bgr_array
from docflow.ocr.models import OcrEngineName, OcrLine, OcrPage


def _load_rapidocr() -> Any:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise OcrUnavailableError('一级 OCR 需要 rapidocr-onnxruntime，请执行：pip install "docflow-ai[ocr]"') from exc
    return RapidOCR


def _polygon_to_box(polygon: Any) -> tuple[float, float, float, float]:
    """RapidOCR 给的是四点多边形，转成外接矩形."""
    xs = [float(point[0]) for point in polygon]
    ys = [float(point[1]) for point in polygon]
    return min(xs), min(ys), max(xs), max(ys)


class RapidOcrEngine:
    """RapidOCR 的薄封装，输出统一的 OcrPage."""

    name = OcrEngineName.RAPID

    def __init__(self) -> None:
        self._engine: Any | None = None

    @property
    def engine(self) -> Any:
        if self._engine is None:
            rapid_ocr = _load_rapidocr()
            self._engine = rapid_ocr()
        return self._engine

    def available(self) -> bool:
        try:
            _load_rapidocr()
        except OcrUnavailableError:
            return False
        return True

    def run(self, image: Any) -> OcrPage:
        """image：图片路径 / 图片字节 / RGB numpy 数组."""
        raw, _elapsed = self.engine(to_bgr_array(image))
        lines: list[OcrLine] = []
        for item in raw or []:
            polygon, text, score = item[0], item[1], item[2]
            if not str(text).strip():
                continue
            lines.append(
                OcrLine(
                    text=str(text).strip(),
                    confidence=float(score),
                    box=_polygon_to_box(polygon),
                )
            )
        return OcrPage(lines=lines, engine=OcrEngineName.RAPID)
