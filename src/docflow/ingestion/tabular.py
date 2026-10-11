"""表格类文件接入：Excel（多 Sheet）与 CSV/TSV."""

from __future__ import annotations

import csv
import io
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from docflow.config import Settings
from docflow.errors import ParseError
from docflow.ingestion.header import build_table
from docflow.models.table import RawTable, SourceKind, SourceRef

# 中文环境常见的 CSV 编码，按命中概率排序；latin-1 兜底保证不抛 UnicodeDecodeError
_CSV_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1")
_CSV_DELIMITERS = (",", ";", "\t", "|")


def _split_counts(text: str, delimiter: str) -> list[int]:
    """按分隔符逐行切分，返回每行的字段数（忽略空行）."""
    return [len(next(csv.reader([line], delimiter=delimiter))) for line in text.splitlines() if line.strip()]


def _sniff_delimiter(text: str) -> str:
    """在候选分隔符里挑最像的那一个.

    pandas 的 sep=None 走 csv.Sniffer，遇到「标题行 + 空格分隔的说明行」会误判成空格，
    把整行当成一列。这里改成对每个候选分隔符统计各行切出的列数，取
    「列数众数出现次数最多、众数列数最大」的那个——数据行的列数总是一致的，说明行的列数则七零八落。
    """
    best_delimiter = ","
    best_score = (0, 1)  # (众数出现次数, 众数列数)
    for delimiter in _CSV_DELIMITERS:
        counts = _split_counts(text, delimiter)
        if not counts:
            continue
        mode, matches = Counter(counts).most_common(1)[0]
        if mode < 2:
            continue
        score = (matches, mode)
        if score > best_score:
            best_score = score
            best_delimiter = delimiter
    return best_delimiter


def _read_csv(path: Path) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in _CSV_ENCODINGS:
        try:
            text = path.read_text(encoding=encoding)
        except (UnicodeDecodeError, OSError) as exc:
            last_error = exc
            continue
        delimiter = _sniff_delimiter(text)
        # 先算出整表最大列数并显式声明列名，否则 pandas 会拿第一行（标题行，往往只有 1 列）
        # 当基准，遇到后面的宽数据行直接报 "Expected 1 fields ... saw N"。
        width = max(_split_counts(text, delimiter) or [1])
        try:
            return pd.read_csv(
                io.StringIO(text),
                header=None,
                names=list(range(width)),
                dtype=object,
                keep_default_na=False,
                sep=delimiter,
                engine="python",  # C 引擎遇到宽窄不一的行会直接报错，Python 引擎按声明宽度补齐
                skip_blank_lines=False,
            )
        except pd.errors.ParserError as exc:
            last_error = exc
    raise ParseError(f"{path.name} 解析失败，已尝试 {len(_CSV_ENCODINGS)} 种编码：{last_error}")


def _read_excel(path: Path) -> dict[str, pd.DataFrame]:
    try:
        sheets = pd.read_excel(
            path,
            header=None,
            dtype=object,
            keep_default_na=False,
            sheet_name=None,  # 读全部 Sheet
        )
    except Exception as exc:  # openpyxl 的异常类型不稳定，这里统一收敛
        raise ParseError(f"{path.name} 解析失败：{exc}") from exc
    if not isinstance(sheets, dict):  # pragma: no cover - sheet_name=None 时必为 dict
        raise ParseError(f"{path.name} 未返回任何 Sheet")
    return sheets


def _frame_to_rows(frame: pd.DataFrame) -> list[list[Any]]:
    """DataFrame → 二维列表，顺手把 NaN 统一成 None."""
    rows: list[list[Any]] = []
    for record in frame.itertuples(index=False, name=None):
        rows.append([None if (value is None or value != value) else value for value in record])
    return rows


def parse_tabular(path: Path, kind: SourceKind, settings: Settings) -> list[RawTable]:
    """解析 Excel / CSV，返回每个 Sheet（CSV 视为单 Sheet）的 RawTable."""
    frames = {"": _read_csv(path)} if kind is SourceKind.CSV else _read_excel(path)

    tables: list[RawTable] = []
    for sheet_name, frame in frames.items():
        if frame.empty:
            continue
        source = SourceRef(
            file_name=path.name,
            kind=kind,
            file_path=str(path),
            location=str(sheet_name),
        )
        table = build_table(
            _frame_to_rows(frame),
            source,
            max_scan_rows=settings.max_header_scan_rows,
            min_fill_ratio=settings.header_min_fill_ratio,
        )
        if table is not None and table.row_count > 0:
            tables.append(table)
    return tables
