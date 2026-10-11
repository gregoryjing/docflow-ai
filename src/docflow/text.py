"""文本归一化与相似度工具：字段映射、表头识别都依赖这里的确定性规则."""

from __future__ import annotations

import re
import unicodedata

# 列名里常见的分隔与修饰符号，归一化时统一剔除
_PUNCT = re.compile(r"[\s_\-./\\()\[\]{}<>:：，,。;；、|+*#@!?？！'\"“”‘’]+")
# 全角括号里的单位说明，如「数量（个）」→ 归一化后只留「数量」
_BRACKET_NOTE = re.compile(r"[（(][^（()）]*[)）]")


def to_halfwidth(text: str) -> str:
    """全角转半角（NFKC），让「数量」和「数量」这类字符可比."""
    return unicodedata.normalize("NFKC", text)


def normalize_key(text: object) -> str:
    """列名/字段名归一化，用于精确匹配.

    规则：全角转半角 → 去括号注释 → 去标点空格 → 转小写。
    「物料编号」「物料 编号」「material_id」「Material-ID」归一化后分别是
    「物料编号」「物料编号」「materialid」「materialid」。
    """
    if text is None:
        return ""
    value = to_halfwidth(str(text)).strip()
    value = _BRACKET_NOTE.sub("", value)
    value = _PUNCT.sub("", value)
    return value.lower()


def is_blank(value: object) -> bool:
    """判断单元格是否为空：None、空串、纯空白、NaN、'-' 占位符都算空."""
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    text = str(value).strip()
    return text == "" or text in {"-", "--", "—", "N/A", "n/a", "NA", "null", "None"}


def clean_cell(value: object) -> str:
    """单元格转字符串：去掉首尾空白与不可见字符，保留原始语义."""
    if is_blank(value):
        return ""
    text = to_halfwidth(str(value))
    return re.sub(r"\s+", " ", text).strip()


def dedupe_columns(columns: list[str]) -> list[str]:
    """重名列去歧义：pandas 会给出 a / a.1，这里统一为 a / a__2，避免后续按名取值踩坑."""
    seen: dict[str, int] = {}
    result: list[str] = []
    for name in columns:
        base = name.strip() or "未命名列"
        if base not in seen:
            seen[base] = 1
            result.append(base)
            continue
        seen[base] += 1
        result.append(f"{base}__{seen[base]}")
    return result
