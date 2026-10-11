"""类型转换：把「人写的」单元格值转成标准类型.

原则是**保守**——拿不准就返回 None 并让上层标记为类型不匹配，而不是猜一个可能错的值。
数据质量工具里，一个悄悄猜错的数字比一条明确的告警危险得多。
"""

from __future__ import annotations

import datetime as dt
import re

from docflow.models.schema import FieldType
from docflow.text import to_halfwidth

# 千分位与纯数字
_NUMERIC = re.compile(r"^[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
# 开头的货币符号/空白
_LEADING_NOISE = re.compile(r"^[\s¥￥$€£]*")
# 结尾的单位（中文量词、常见计量单位、货币）
_TRAILING_UNIT = re.compile(
    r"[\s]*(?:个|件|套|台|只|支|根|张|米|平方米|立方米|吨|公斤|千克|克|"
    r"pcs|pc|units?|sets?|m|mm|cm|km|kg|g|t|元|人民币|rmb|cny|usd|\$|¥|￥)?[\s]*$",
    re.IGNORECASE,
)

_TRUE_TOKENS = {"是", "y", "yes", "true", "t", "1", "真", "有"}
_FALSE_TOKENS = {"否", "n", "no", "false", "f", "0", "假", "无"}

_DATE_PATTERNS: list[tuple[re.Pattern[str], bool]] = [
    (re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$"), False),
    (re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日?$"), False),
    (re.compile(r"^(\d{4})(\d{2})(\d{2})$"), False),
]


def to_float(raw: object) -> float | None:
    """字符串/数字 → float。支持千分位、货币符号与常见单位后缀，其余一律返回 None."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = to_halfwidth(str(raw)).strip()
    if not text:
        return None
    text = _LEADING_NOISE.sub("", text)
    text = _TRAILING_UNIT.sub("", text)
    text = text.replace(",", "").replace(" ", "")
    if not _NUMERIC.match(text):
        return None
    try:
        return float(text)
    except ValueError:  # pragma: no cover - 正则已保证可解析
        return None


def to_int(raw: object) -> int | None:
    """→ int。小数会被四舍五入（数量列偶尔写成 100.0 是常态）."""
    value = to_float(raw)
    if value is None:
        return None
    return round(value)


def to_bool(raw: object) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    token = to_halfwidth(str(raw)).strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    return None


def to_date(raw: object) -> str | None:
    """→ ISO 日期字符串 YYYY-MM-DD。不猜歧义格式（如 03/04/2024），猜错比不认更糟."""
    if isinstance(raw, dt.datetime):
        return raw.date().isoformat()
    if isinstance(raw, dt.date):
        return raw.isoformat()
    if raw is None:
        return None
    text = to_halfwidth(str(raw)).strip()
    if not text:
        return None
    for pattern, _ in _DATE_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        year, month, day = (int(match.group(index)) for index in (1, 2, 3))
        try:
            return dt.date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def coerce_value(raw: object, field_type: FieldType) -> tuple[object, str | None]:
    """按字段类型转换。返回 (值, 错误说明)；错误说明非空表示转换失败，值退化为原始文本."""
    if field_type is FieldType.STRING:
        text = to_halfwidth(str(raw)).strip() if raw is not None else ""
        return (text or None), None

    if field_type is FieldType.INTEGER:
        int_value = to_int(raw)
        return (int_value, None) if int_value is not None else (raw, f"无法解析为整数：{raw!r}")

    if field_type is FieldType.FLOAT:
        float_value = to_float(raw)
        return (float_value, None) if float_value is not None else (raw, f"无法解析为数值：{raw!r}")

    if field_type is FieldType.DATE:
        date_value = to_date(raw)
        return (date_value, None) if date_value is not None else (raw, f"无法解析为日期：{raw!r}")

    bool_value = to_bool(raw)
    return (bool_value, None) if bool_value is not None else (raw, f"无法解析为布尔值：{raw!r}")
