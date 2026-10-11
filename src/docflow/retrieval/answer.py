"""RAG 问答：把检索到的片段交给本地文本 LLM，生成「带引用的回答」.

未配置文本 LLM 时退化为**抽取式**：直接给出最相关的片段，不做生成。
生成只是锦上添花，检索与引用才是这个能力的主体——所以模型不可用时绝不报错，
而是把原始证据原样交给用户。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence

from docflow.config import Settings
from docflow.errors import DocFlowError
from docflow.retrieval.models import Answer, SearchHit

# 约束逐条对应 RAG 常见的失败模式：编造资料外内容、不标引用、过度自信
_SYSTEM_PROMPT = (
    "你是企业文档数据的问答助手。你只能依据「资料片段」回答，绝不能编造片段之外的信息。\n"
    "规则：\n"
    "1. 每个结论后用 [编号] 标注它来自哪条片段，可标注多个。\n"
    "2. 资料不足或与问题无关时，直接说明「资料中没有相关信息」，不要猜测。\n"
    "3. 数字、编号、单位必须与片段完全一致，不要改写或换算。\n"
    "4. 用简体中文、直接给结论，不要复述问题，不要输出代码块。"
)


class RagAnswerer:
    """基于检索片段回答问题；模型不可用时退回抽取式."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def available(self) -> bool:
        return self.settings.rag_llm_configured

    def answer(self, question: str, hits: Sequence[SearchHit]) -> Answer:
        if not hits:
            return Answer(question=question, answer="没有检索到相关资料。", citations=[], generated=False)
        if not self.available():
            return self._extractive(question, hits)
        try:
            content = self._post(question, hits)
        except DocFlowError:
            return self._extractive(question, hits)  # 生成失败也把证据给用户
        return Answer(question=question, answer=content.strip(), citations=list(hits), generated=True)

    def _extractive(self, question: str, hits: Sequence[SearchHit]) -> Answer:
        blocks = [f"[{index}] {hit.chunk.text}（来源：{hit.chunk.citation()}）" for index, hit in enumerate(hits, 1)]
        body = "（未配置生成模型，以下为最相关的资料片段）\n\n" + "\n\n".join(blocks)
        return Answer(question=question, answer=body, citations=list(hits), generated=False)

    def _post(self, question: str, hits: Sequence[SearchHit]) -> str:
        payload = {
            "model": self.settings.llm_model,
            "temperature": 0,
            "stream": False,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": self._user_prompt(question, hits)},
            ],
        }
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
            raise DocFlowError(f"RAG 模型返回 HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DocFlowError(f"无法连接 RAG 模型服务：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise DocFlowError(f"RAG 模型返回的不是合法 JSON：{exc}") from exc
        try:
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise DocFlowError(f"RAG 模型响应结构异常：{body}") from exc

    @staticmethod
    def _user_prompt(question: str, hits: Sequence[SearchHit]) -> str:
        passages = "\n\n".join(
            f"[{index}] 来源：{hit.chunk.citation()}\n{hit.chunk.text}" for index, hit in enumerate(hits, 1)
        )
        return f"资料片段：\n{passages}\n\n问题：{question}\n请依据上述片段回答。"

    def _endpoint(self) -> str:
        base = (self.settings.llm_base_url or "").rstrip("/")
        if not base:
            raise DocFlowError("未配置 llm_base_url，无法生成 RAG 回答")
        if base.endswith("/chat/completions"):
            return base
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        return f"{base}/chat/completions"
