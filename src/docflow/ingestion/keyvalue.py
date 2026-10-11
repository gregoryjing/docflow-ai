"""从自由文本里抽取「键：值」对.

技术说明书、合同、发票这类文档没有规整表格，但有大量「字段：值」行。
抽出来就是一张两列表，能直接进统一数据模型——这让 PDF 不再只是「一段文字」。
"""

from __future__ import annotations

import re

# key 不超过 25 字符、不含冒号；value 至少 1 个非空字符
_KV_PATTERN = re.compile(r"^\s*([^：:\s][^：:]{0,24}?)\s*[：:]\s*(\S.*?)\s*$")
# 「http://x」这类会被误判成键值对，按前缀直接排除
_URL_LIKE_KEYS = {"http", "https", "ftp", "file", "mailto"}


def extract_key_value_rows(lines: list[str]) -> list[list[str]]:
    """从文本行里抽键值对，返回 [key, value] 列表，保持原文顺序."""
    rows: list[list[str]] = []
    for line in lines:
        match = _KV_PATTERN.match(line)
        if not match:
            continue
        key, value = match.group(1).strip(), match.group(2).strip()
        if key.lower() in _URL_LIKE_KEYS:
            continue
        rows.append([key, value])
    return rows


def key_value_table(pairs: list[list[str]]) -> list[list[str]]:
    """把 [键, 值] 列表转成「键作表头、值作数据行」的二维表.

    直接把键值对摊成两列 [字段, 值] 是行不通的：列名「字段」「值」不可能与任何业务
    Schema 对齐，映射阶段会把它们全部判为未映射，最终产出一堆字段全空的记录——
    数据明明抽出来了，却整条丢失。转置成「一个键一列、一行一条实体」后，
    键名（如「供应商」「数量」）就能正常参与字段映射，键值型 PDF/Word 才真正可用。

    同一键重复出现有两种截然不同的含义，必须分开处理：
    - **周期性重复**（如采购单里「物料/数量/单价」成组重复 N 次）：这是「一个文档装多条
      明细」，按重复周期拆成 N 行。仅当每一块的键都严格等于首块表头时才拆，否则退回单行，
      避免猜错周期把不同实体的值错位拼到一起。
    - **零散重复**（键只是碰巧出现两次，不构成规整周期）：不能静默丢弃，给后出现的键加
      `#2`、`#3` 后缀另立一列，值全部保留；多出来的列会因映射不到标准字段而以
      「未映射列」提示出现在结果里，用户看得见。
    """
    if not pairs:
        return [[], []]

    cycle = _repeat_cycle(pairs)
    if cycle is not None:
        split = _split_regular_blocks(pairs, cycle)
        if split is not None:
            return split

    header: list[str] = []
    values: list[str] = []
    used: dict[str, int] = {}
    for key, value in pairs:
        count = used.get(key, 0) + 1
        used[key] = count
        header.append(key if count == 1 else f"{key}#{count}")
        values.append(value)
    return [header, values]


def _repeat_cycle(pairs: list[list[str]]) -> int | None:
    """首个重复键出现的位置，即「一个实体的键个数」；没有重复则返回 None."""
    seen: dict[str, int] = {}
    for index, (key, _) in enumerate(pairs):
        if key in seen:
            return index
        seen[key] = index
    return None


def _split_regular_blocks(pairs: list[list[str]], cycle: int) -> list[list[str]] | None:
    """按固定块长把键值对拆成多行；只要有一块的键与首块表头不一致就放弃（返回 None）."""
    if cycle < 2 or cycle >= len(pairs) or len(pairs) % cycle != 0:
        return None
    header = [key for key, _ in pairs[:cycle]]
    rows = [header]
    for start in range(cycle, len(pairs), cycle):
        block = pairs[start : start + cycle]
        if [key for key, _ in block] != header:
            return None
        rows.append([value for _, value in block])
    return rows
