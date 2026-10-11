"""轻量切词：英文/数字按词，中文按字符二元组.

不引入 jieba 之类的分词依赖——本项目的检索对象是业务字段与编号，
「字符二元组 + 英文数字词」足以覆盖「物料编号」「PO-2024-001」这类查询，
且完全离线、确定性、可解释。
"""

from __future__ import annotations

import re

from docflow.text import to_halfwidth

_ASCII_WORD = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    normalized = to_halfwidth(text or "").lower()
    tokens = _ASCII_WORD.findall(normalized)
    for run in _CJK_RUN.findall(normalized):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens
