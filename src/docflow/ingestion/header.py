"""表头识别与「行列表 → RawTable」的通用构造逻辑.

Excel / CSV / PDF 提取出来的都是「二维行列表」，脏数据的差异主要在表头位置和空行分布，
所以统一在这里处理，接入层各分支只负责把文件读成二维列表。
"""

from __future__ import annotations

from typing import Any

from docflow.models.table import RawTable, SourceRef
from docflow.text import clean_cell, dedupe_columns, is_blank


def _non_blank_count(row: list[Any]) -> int:
    return sum(1 for cell in row if not is_blank(cell))


def detect_header_index(rows: list[list[Any]], max_scan_rows: int, min_fill_ratio: float) -> int | None:
    """在前若干行里找出最像表头的一行.

    判据（按顺序）：
    1. 非空单元格 ≥ 2——排除「报价单」这类跨列标题；
    2. 非空占比 ≥ min_fill_ratio——排除半截行；
    3. 列数与下方数据行量级相当——排除「编制：工程部　日期：2024-03-01」这类
       恰好有两个单元格、但明显不是表头的说明行。
    """
    for index in range(min(len(rows), max_scan_rows)):
        row = rows[index]
        non_blank = _non_blank_count(row)
        if non_blank < 2:
            continue
        width = max(len(row), 1)
        if non_blank / width < min_fill_ratio:
            continue

        following = [_non_blank_count(r) for r in rows[index + 1 : index + 4]]
        following = [count for count in following if count > 0]
        if following:
            average = sum(following) / len(following)
            # 双向比较：表头列数不该与数据行列数相差一个量级
            if non_blank < average * 0.5 or average < non_blank * 0.5:
                continue
        return index
    return None


def _trim_empty_edges(rows: list[list[Any]], notes: list[str]) -> list[list[Any]]:
    leading = 0
    while rows and _non_blank_count(rows[0]) == 0:
        rows = rows[1:]
        leading += 1
    trailing = 0
    while rows and _non_blank_count(rows[-1]) == 0:
        rows = rows[:-1]
        trailing += 1
    if leading:
        notes.append(f"跳过开头 {leading} 行空行")
    if trailing:
        notes.append(f"跳过结尾 {trailing} 行空行")
    return rows


def _drop_trailing_auto_columns(columns: list[str], data: list[dict[str, Any]]) -> list[str]:
    """丢掉「自动命名 + 整列全空」的尾巴列——Excel 常见的幽灵列."""
    keep = len(columns)
    while keep > 1:
        name = columns[keep - 1]
        if not name.startswith("未命名列"):
            break
        if any(not is_blank(row.get(name)) for row in data):
            break
        keep -= 1
    return columns[:keep]


def build_table(
    rows: list[list[Any]],
    source: SourceRef,
    *,
    max_scan_rows: int = 10,
    min_fill_ratio: float = 0.6,
    header_index: int | None = None,
) -> RawTable | None:
    """把二维行列表构造成 RawTable。表头识别失败或整表为空时返回 None."""
    notes: list[str] = []
    rows = _trim_empty_edges(list(rows), notes)
    if not rows:
        return None

    if header_index is None:
        header_index = detect_header_index(rows, max_scan_rows, min_fill_ratio)
        if header_index is None:
            return None
        if header_index > 0:
            notes.append(f"表头位于第 {header_index + 1} 行，已跳过前 {header_index} 行说明内容")

    header = rows[header_index]
    columns = [clean_cell(cell) or f"未命名列{position + 1}" for position, cell in enumerate(header)]
    columns = dedupe_columns(columns)

    width = len(columns)
    data: list[dict[str, Any]] = []
    blank_rows = 0
    for raw in rows[header_index + 1 :]:
        padded = list(raw[:width]) + [None] * max(0, width - len(raw))
        if all(is_blank(cell) for cell in padded):
            blank_rows += 1
            continue
        data.append({name: (None if is_blank(cell) else cell) for name, cell in zip(columns, padded, strict=True)})

    if blank_rows:
        notes.append(f"跳过中间 {blank_rows} 行空行")

    columns = _drop_trailing_auto_columns(columns, data)
    if len(columns) < width:
        data = [{name: row.get(name) for name in columns} for row in data]
        notes.append(f"丢弃 {width - len(columns)} 个整列为空的尾部列")

    return RawTable(
        source=source,
        columns=columns,
        rows=data,
        header_row_index=header_index,
        notes=notes,
    )
