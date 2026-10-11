"""图片加载与编码的小工具，供两级 OCR 引擎与接入层共用."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

from docflow.errors import ParseError


def _to_rgb_array(source: Any) -> Any:
    """把各种输入统一成 RGB 的 numpy 数组（本模块约定：内部一律 RGB）.

    支持：numpy 数组（视为 RGB）、PIL.Image、文件路径、图片字节。
    """
    import numpy as np

    if isinstance(source, np.ndarray):
        if source.ndim == 2:  # 灰度 → 复制成三通道
            return np.stack([source] * 3, axis=-1)
        if source.shape[2] == 4:  # RGBA → RGB
            return source[:, :, :3]
        if source.shape[2] == 3:
            return source
        raise ParseError(f"不支持的图像通道数：{source.shape[2]}")

    if isinstance(source, (bytes, bytearray)):
        return _to_rgb_array(_open_pillow(io.BytesIO(source)))

    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.exists():
            raise ParseError(f"图片不存在：{path}")
        return _to_rgb_array(_open_pillow(path))

    # PIL.Image 或其它带 convert 的对象
    if hasattr(source, "convert"):
        return np.asarray(source.convert("RGB"))
    raise ParseError(f"无法识别的图像输入类型：{type(source)!r}")


def _open_pillow(handle: Any) -> Any:
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise ParseError('图片解析需要 Pillow，请执行：pip install "docflow-ai[ocr]"') from exc
    try:
        return Image.open(handle)
    except Exception as exc:
        raise ParseError(f"图片打开失败：{exc}") from exc


def to_rgb_array(source: Any) -> Any:
    return _to_rgb_array(source)


def to_bgr_array(source: Any) -> Any:
    """RapidOCR/OpenCV 习惯 BGR，这里把内部 RGB 转过去."""
    return _to_rgb_array(source)[:, :, ::-1]


def to_png_bytes(source: Any) -> bytes:
    """转成 PNG 字节，用于发给 VL 模型（data URI）."""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise ParseError('图片编码需要 Pillow，请执行：pip install "docflow-ai[ocr]"') from exc

    array = _to_rgb_array(source)
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="PNG")
    return buffer.getvalue()


def to_data_uri(source: Any) -> str:
    encoded = base64.b64encode(to_png_bytes(source)).decode("ascii")
    return f"data:image/png;base64,{encoded}"
