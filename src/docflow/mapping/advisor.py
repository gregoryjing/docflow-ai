"""L3 字段映射：前两级（精确 / 模糊）都拿不准时，才把列名 + 样本交给 LLM 判断.

设计原则是「能用规则解决的不浪费 token」——只有相似度低于建议阈值的列才会走到这里，
且默认关闭（DOCFLOW_LLM_MAPPING_ENABLED=false）。L3 是**尽力而为**：
模型服务不可用、返回非法 JSON、推断出未知字段，都只会退回「未映射」，绝不拖垮整条链路。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from docflow.config import Settings
from docflow.errors import DocFlowError
from docflow.models.schema import FieldDefinition

# 约束逐条对应一种常见的「LLM 乱猜」失败模式：
# 1 输出解释/寒暄 → 解析器要面对噪声；
# 2 用代码块包裹 → 需要能容忍；
# 3 硬猜一个字段 → 必须允许 null 表示「不确定」；
# 4 输出不存在的字段名 → 由调用方校验并丢弃。
_SYSTEM_PROMPT = (
    "你是数据工程里的字段映射助手。给定源表的列名、若干样本值和目标字段清单，"
    "判断该列最可能对应哪个目标字段。\n"
    "只输出一个 JSON 对象，不要任何解释文字或代码块围栏，格式为："
    '{"target_field": "<字段 key 或 null>", "confidence": <0 到 1 的小数>, "reason": "<一句话理由>"}。\n'
    "判断依据：列名语义优先，样本值辅助；拿不准时 target_field 用 null，不要硬猜。"
)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_FENCE = re.compile(r"```[a-zA-Z]*\n?|\n?```")


class FieldMappingAdvice(BaseModel):
    """L3 的推断结论."""

    target_field: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""


class FieldMappingAdvisor(Protocol):
    """L3 映射顾问接口：便于在测试中注入假实现，不依赖真实模型."""

    def advise(
        self,
        column: str,
        samples: list[str],
        fields: list[FieldDefinition],
    ) -> FieldMappingAdvice | None: ...


def _extract_json(content: str) -> dict[str, Any] | None:
    """从模型回复里抠出 JSON 对象，容忍代码块围栏与前后说明."""
    text = _FENCE.sub("", content).strip()
    match = _JSON_BLOCK.search(text)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


class LlmFieldMappingAdvisor:
    """走 OpenAI 兼容的 chat/completions 接口做字段推断."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def available(self) -> bool:
        return self.settings.llm_configured

    def advise(
        self,
        column: str,
        samples: list[str],
        fields: list[FieldDefinition],
    ) -> FieldMappingAdvice | None:
        if not self.available():
            return None
        try:
            content = self._post(self._payload(column, samples, fields))
        except DocFlowError:
            return None  # L3 尽力而为：模型不可用就退回「未映射」
        return self._parse(content, fields)

    def _payload(self, column: str, samples: list[str], fields: list[FieldDefinition]) -> dict[str, Any]:
        catalog = "\n".join(f"- {field.key}（{'、'.join(field.all_names())}）" for field in fields)
        user = (
            f"源列名：{column}\n"
            f"样本值：{'、'.join(samples) if samples else '（无）'}\n"
            f"目标字段清单：\n{catalog}\n"
            "请判断该列应映射到哪个字段。"
        )
        return {
            "model": self.settings.llm_model,
            "temperature": 0,
            "stream": False,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
        }

    @staticmethod
    def _parse(content: str, fields: list[FieldDefinition]) -> FieldMappingAdvice | None:
        payload = _extract_json(content)
        if payload is None:
            return None
        try:
            advice = FieldMappingAdvice.model_validate(payload)
        except ValidationError:
            return None
        if advice.target_field is not None and advice.target_field not in {field.key for field in fields}:
            return None  # 模型编造了不存在的字段，丢弃
        return advice

    def _endpoint(self) -> str:
        base = (self.settings.llm_base_url or "").rstrip("/")
        if not base:
            raise DocFlowError("未配置 llm_base_url，无法调用 L3 字段映射")
        if base.endswith("/chat/completions"):
            return base
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        return f"{base}/chat/completions"

    def _post(self, payload: dict[str, Any]) -> str:
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.llm_timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise DocFlowError(f"L3 模型返回 HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DocFlowError(f"无法连接 L3 模型服务：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise DocFlowError(f"L3 模型返回的不是合法 JSON：{exc}") from exc

        try:
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise DocFlowError(f"L3 模型响应结构异常：{body}") from exc
