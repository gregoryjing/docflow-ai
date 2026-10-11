"""二级 OCR：本地 VL（视觉语言）模型，走 OpenAI 兼容的 chat/completions 接口.

定位是「选配的兜底链路」：一级 RapidOCR 平均置信度低于阈值时才会调用，
所以这里不追求吞吐，追求把版面读对。Ollama / vLLM / LM Studio 都提供兼容接口。

提示词工程是本模块的重点：7B 级 VL 模型只要约束给足，表格转写质量会有量级提升。
关键约束写在 `_SYSTEM_PROMPT` 里，逐条对应一种常见的转写失败模式（见各行注释）。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any

from docflow.config import Settings
from docflow.errors import OcrUnavailableError
from docflow.ocr.image import to_data_uri
from docflow.ocr.models import OcrEngineName, OcrPage

# 逐条对应一类常见的 VL 转写失败模式，而不是泛泛地「请准确识别」：
# 1 代码块围栏 / 寒暄前言    → 下游解析器要面对噪声
# 2 行列错位、合并表头       → 表头识别直接失败
# 3 列数不一致               → 二维矩阵参差不齐，映射时串列
# 4 合并单元格留空           → 该行关键字段凭空消失
# 5 单元格内换行 / 竖线      → 撑破 Markdown 表格
# 6 「顺手纠正」错别字 / 翻译 → 与原文不符，业务上不可接受
# 7 重算数字 / 补千分位       → 金额、单价被篡改
# 8 图非表格时瞎编表格        → 应退化为键值对
_SYSTEM_PROMPT = (
    "你是专业的文档表格结构化助手，任务是把图片中的表格精确转写成 Markdown 表格。\n"
    "必须严格遵守以下规则：\n"
    "1. 只输出 Markdown 表格本身，不要任何解释、前言或代码块围栏（不要出现 ```）。\n"
    "2. 第一行必须是表头；行列的数量与顺序和图片完全一致，不要合并或拆分行列。\n"
    "3. 每一行的列数都要与表头相同；没有内容的单元格留空，不要臆造或补全。\n"
    "4. 合并单元格：把该值填进它覆盖的每一个单元格。\n"
    "5. 单元格内原本换行的，用空格连接成一行；单元格内出现竖线的，写成 \\| 转义。\n"
    "6. 原文照抄：不翻译、不改写、不纠正错别字、不统一单位与大小写。\n"
    "7. 数字保持原样（含千分位、小数点、货币符号与单位），不要重算、不要补位。\n"
    "8. 如果图片里不是表格，就按「字段：值」逐行输出，每行一个字段，不要硬凑成表格。"
)

_SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")


def _split_row(line: str) -> list[str]:
    """按「未转义的竖线」切分一行，并把 `\\|` 还原成 `|`.

    直接用 `split("|")` 会把单元格里转义过的竖线也切开，导致列数错乱。
    """
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in body:
        if escaped:
            if char == "|":
                current.append("|")
            else:
                current.append("\\")
                current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if escaped:  # 行尾孤立的转义符，原样保留
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def parse_markdown_table(text: str) -> list[list[str]]:
    """从模型回复里抠出 Markdown 表格，转成二维矩阵.

    容错：模型偶尔会加代码块围栏或前后说明，这里按行扫描，只取以 `|` 开头的行，
    跳过 `|---|---|` 分隔行，并把各行补齐到相同的列数。
    """
    rows: list[list[str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = _split_row(line)
        if cells and all(_SEPARATOR_CELL.match(cell) for cell in cells):
            continue  # 分隔行 |---|---|
        rows.append(cells)
    if not rows:
        return []
    width = max(len(row) for row in rows)
    return [row + [""] * (width - len(row)) for row in rows]


def _fallback_keyvalue(text: str) -> list[list[str]]:
    """不是表格时，退化成两列「字段,值」."""
    rows: list[list[str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("-*").strip()
        if not line:
            continue
        for separator in ("：", ":"):
            if separator in line:
                key, _, value = line.partition(separator)
                rows.append([key.strip(), value.strip()])
                break
        else:
            rows.append([line, ""])
    return rows


def _user_prompt(column_hint: Sequence[str] | None) -> str:
    """把目标 Schema 的列名作为「软提示」给模型，提升表头与下游字段的对齐率.

    只是参考——图片里实际有什么就写什么，不强制模型照抄提示列。
    """
    if not column_hint:
        return "请转写这张图片中的表格。"
    columns = "、".join(str(column) for column in column_hint)
    return (
        "请转写这张图片中的表格。"
        f"若图中表头与下列列名对应，请优先采用这些写法：{columns}。"
        "若图片实际列与此不同，以图片内容为准。"
    )


class VlOcrEngine:
    """本地 VL 模型的调用封装."""

    name = OcrEngineName.VL

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def available(self) -> bool:
        return self.settings.vl_configured

    def _endpoint(self) -> str:
        base = (self.settings.vl_base_url or "").rstrip("/")
        if not base:
            raise OcrUnavailableError("未配置 vl_base_url，无法调用二级 VL 模型")
        # 允许用户填 http://host:port 或 http://host:port/v1，统一补成 /chat/completions
        if base.endswith("/chat/completions"):
            return base
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        return f"{base}/chat/completions"

    def run(self, image: Any, column_hint: Sequence[str] | None = None) -> OcrPage:
        if not self.available():
            raise OcrUnavailableError("未配置本地 VL 模型（vl_base_url / vl_model）")

        payload = {
            "model": self.settings.vl_model,
            "temperature": 0,
            "stream": False,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _user_prompt(column_hint)},
                        {"type": "image_url", "image_url": {"url": to_data_uri(image)}},
                    ],
                },
            ],
        }
        content = self._post(payload)
        matrix = parse_markdown_table(content)
        notes: list[str] = []
        if matrix:
            notes.append("VL 模型直接给出表格结构，已跳过坐标重建")
        else:
            matrix = _fallback_keyvalue(content)
            notes.append("VL 模型未返回表格，已按「字段：值」解析")
        return OcrPage(
            engine=OcrEngineName.VL,
            matrix=matrix,
            notes=notes,
        )

    def _post(self, payload: dict[str, Any]) -> str:
        headers = {"Content-Type": "application/json"}
        if self.settings.vl_api_key:
            headers["Authorization"] = f"Bearer {self.settings.vl_api_key}"
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.vl_timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise OcrUnavailableError(f"VL 模型返回 HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OcrUnavailableError(f"无法连接 VL 模型服务：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise OcrUnavailableError(f"VL 模型返回的不是合法 JSON：{exc}") from exc

        try:
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise OcrUnavailableError(f"VL 模型响应结构异常：{body}") from exc
