"""OCR 链路的中间数据结构.

两级引擎（RapidOCR / VL 模型）产出同一种结构，下游的表格重建与接入层
只认这里的类型，不关心文本是哪个引擎识出来的。
"""

from __future__ import annotations

from enum import StrEnum
from itertools import pairwise

from pydantic import BaseModel, Field

Box = tuple[float, float, float, float]  # x0, y0, x1, y1（像素坐标，左上角为原点）


class OcrEngineName(StrEnum):
    RAPID = "rapidocr"  # 一级：本地轻量 OCR，快但对手写/低质扫描件不稳
    VL = "vl_model"  # 二级：本地 VL 大模型，慢但能读懂版面
    NONE = "none"  # 未启用任何引擎（依赖缺失或开关关闭）


class OcrLine(BaseModel):
    """一行识别结果：文本 + 置信度 + 外接框."""

    text: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    box: Box = (0.0, 0.0, 0.0, 0.0)

    @property
    def x_center(self) -> float:
        return (self.box[0] + self.box[2]) / 2

    @property
    def y_center(self) -> float:
        return (self.box[1] + self.box[3]) / 2

    @property
    def height(self) -> float:
        return max(self.box[3] - self.box[1], 1.0)

    @property
    def width(self) -> float:
        return max(self.box[2] - self.box[0], 1.0)


class OcrPage(BaseModel):
    """一页（或一张图）的识别结果."""

    lines: list[OcrLine] = Field(default_factory=list)
    engine: OcrEngineName = OcrEngineName.NONE
    width: float = 0.0
    height: float = 0.0
    # VL 模型直接给出结构化的二维矩阵（它本来就在按版面读），此时不必再从坐标重建
    matrix: list[list[str]] | None = None
    # 因为一级置信度不足而升级到 VL 时，记下原始引擎，便于在报告里解释「为什么慢」
    escalated_from: OcrEngineName | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def avg_confidence(self) -> float:
        if not self.lines:
            return 1.0 if self.matrix else 0.0
        return sum(line.confidence for line in self.lines) / len(self.lines)

    @property
    def text(self) -> str:
        if self.matrix is not None:
            return "\n".join(" ".join(cell for cell in row if cell) for row in self.matrix)
        return "\n".join(line.text for line in self.lines)

    def to_matrix(self) -> list[list[str]]:
        """优先用引擎直接给出的矩阵，否则按坐标重建."""
        if self.matrix is not None:
            return [row for row in self.matrix if any(cell.strip() for cell in row)]
        return lines_to_matrix(self.lines)

    def markdown_table(self) -> str:
        """把识别结果按版面还原成 Markdown 表格，供人工复核时快速比对."""
        matrix = self.to_matrix()
        if not matrix:
            return ""
        width = max(len(row) for row in matrix)
        padded = [row + [""] * (width - len(row)) for row in matrix]
        header, *body = padded
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]
        lines.extend("| " + " | ".join(row) + " |" for row in body)
        return "\n".join(lines)


def _cluster_rows(lines: list[OcrLine], tolerance_ratio: float = 0.6) -> list[list[OcrLine]]:
    """按纵坐标把文本行聚成「表格行」.

    同一行的文字 y 中心应该接近；容差取行高的一定比例，避免把相邻两行粘在一起。
    """
    ordered = sorted(lines, key=lambda line: line.y_center)
    rows: list[list[OcrLine]] = []
    centers: list[float] = []
    for line in ordered:
        if rows:
            tolerance = max(line.height, rows[-1][0].height) * tolerance_ratio
            if abs(line.y_center - centers[-1]) <= tolerance:
                rows[-1].append(line)
                centers[-1] = sum(item.y_center for item in rows[-1]) / len(rows[-1])
                continue
        rows.append([line])
        centers.append(line.y_center)
    return rows


def lines_to_matrix(lines: list[OcrLine]) -> list[list[str]]:
    """文本行 → 二维字符串矩阵（表格重建）.

    做法：
    1. 按 y 聚类成行，行内按 x 排序；
    2. 取「单元格最多的一行」当列模板，用它的中心点定义列位置；
    3. 其余行按 x 就近归列，落在所有模板列之外的另起一列（避免丢数据）。

    OCR 版面千奇百怪，这里的目标是「尽量不丢字、大致对齐」，而不是像素级还原。
    """
    if not lines:
        return []

    rows = _cluster_rows(lines)
    cells_per_row = [sorted(row, key=lambda line: line.x_center) for row in rows]

    template = max(cells_per_row, key=len)
    centers = [cell.x_center for cell in template]

    gaps = [right - left for left, right in pairwise(centers) if right > left]
    if gaps:
        tolerance = min(gaps) / 2
    else:
        span = max(line.box[2] for line in lines) - min(line.box[0] for line in lines)
        tolerance = max(span, 1.0) * 0.03

    matrix: list[list[str]] = []
    for cells in cells_per_row:
        slots: list[tuple[float, str]] = [(center, "") for center in centers]
        for cell in cells:
            position = min(range(len(slots)), key=lambda i: abs(slots[i][0] - cell.x_center))
            if abs(slots[position][0] - cell.x_center) <= tolerance:
                if slots[position][1]:
                    # 同一列挤进两个单元格：拼起来，宁可多字也不丢
                    slots[position] = (slots[position][0], f"{slots[position][1]} {cell.text}")
                else:
                    slots[position] = (slots[position][0], cell.text)
            else:
                slots.append((cell.x_center, cell.text))
        slots.sort(key=lambda item: item[0])
        matrix.append([text for _, text in slots])

    # 去掉整行空白（OCR 常把噪声识别成空串）
    return [row for row in matrix if any(cell.strip() for cell in row)]
