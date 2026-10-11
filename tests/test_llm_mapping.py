"""L3 字段映射：模糊匹配失败时由 advisor 兜底（用假实现，不联网）."""

from __future__ import annotations

from docflow.mapping.advisor import FieldMappingAdvice, LlmFieldMappingAdvisor
from docflow.mapping.matcher import ColumnMatcher
from docflow.models.mapping import MappingMethod
from docflow.models.record import ReviewStatus
from docflow.models.schema import FieldDefinition
from docflow.pipeline import run_pipeline


class _FakeAdvisor:
    """记录调用参数的假顾问，验证「何时调用」与「传入什么样本」."""

    def __init__(self, advice: FieldMappingAdvice | None) -> None:
        self.advice = advice
        self.calls: list[tuple[str, list[str]]] = []

    def advise(self, column: str, samples: list[str], fields: list[FieldDefinition]) -> FieldMappingAdvice | None:
        self.calls.append((column, list(samples)))
        return self.advice


def test_unmatched_column_is_unmapped_without_advisor(settings, purchase_order):
    matcher = ColumnMatcher(purchase_order, settings)
    assert matcher.match_column("栏位A1", ["x"]).target_field is None


def test_l3_advice_maps_unmatched_column(settings, purchase_order):
    advisor = _FakeAdvisor(FieldMappingAdvice(target_field="material_id", confidence=0.8, reason="样本像物料编号"))
    matcher = ColumnMatcher(purchase_order, settings, advisor)

    mapping = matcher.match_column("栏位A1", ["A001", "A002"])

    assert mapping.target_field == "material_id"
    assert mapping.method is MappingMethod.LLM
    assert mapping.confidence == 0.8
    assert advisor.calls == [("栏位A1", ["A001", "A002"])]


def test_l3_not_called_when_l1_matches(settings, purchase_order):
    advisor = _FakeAdvisor(FieldMappingAdvice(target_field="supplier", confidence=0.9))
    matcher = ColumnMatcher(purchase_order, settings, advisor)

    mapping = matcher.match_column("物料编号", ["A001"])

    assert mapping.method is MappingMethod.EXACT
    assert advisor.calls == []


def test_l3_unknown_field_is_rejected(settings, purchase_order):
    advisor = _FakeAdvisor(FieldMappingAdvice(target_field="not_a_field", confidence=0.9))
    matcher = ColumnMatcher(purchase_order, settings, advisor)

    mapping = matcher.match_column("栏位A1", ["x"])

    assert mapping.target_field is None
    assert mapping.method is MappingMethod.NONE


def test_pipeline_uses_injected_advisor_and_routes_to_review(settings, purchase_order, write_csv):
    path = write_csv(
        "orders.csv",
        "订单编号,供应商,栏位A1,物料名称,数量,单价,下单日期\nPO-2024-001,华信机械,A001,螺栓,10,1,2024-03-01\n",
    )
    advisor = _FakeAdvisor(FieldMappingAdvice(target_field="material_id", confidence=0.8, reason="样本像物料编号"))

    result = run_pipeline(purchase_order, settings, [path], advisor=advisor)

    record = result.records[0]
    assert record.get("material_id") == "A001"
    # LLM 推断置信度 0.8 低于自动通过阈值 0.9 → 进人工复核
    assert record.review_status is ReviewStatus.PENDING


def test_llm_advisor_parses_fenced_json(settings, monkeypatch):
    advisor = LlmFieldMappingAdvisor(
        settings.model_copy(
            update={"llm_mapping_enabled": True, "llm_base_url": "http://localhost:11434", "llm_model": "qwen"}
        )
    )
    monkeypatch.setattr(
        advisor,
        "_post",
        lambda payload: '```json\n{"target_field": "supplier", "confidence": 0.7, "reason": "ok"}\n```',
    )

    advice = advisor.advise("供货单位", ["华信机械"], [FieldDefinition(key="supplier", label="供应商")])

    assert advice is not None
    assert advice.target_field == "supplier"
    assert advice.confidence == 0.7


def test_llm_advisor_returns_none_on_garbage(settings, monkeypatch):
    advisor = LlmFieldMappingAdvisor(
        settings.model_copy(
            update={"llm_mapping_enabled": True, "llm_base_url": "http://localhost:11434", "llm_model": "qwen"}
        )
    )
    monkeypatch.setattr(advisor, "_post", lambda payload: "抱歉，我无法判断。")

    advice = advisor.advise("栏位A1", ["x"], [FieldDefinition(key="supplier", label="供应商")])

    assert advice is None


def test_llm_advisor_unavailable_without_config(settings):
    advisor = LlmFieldMappingAdvisor(settings)
    assert not advisor.available()
    assert advisor.advise("栏位A1", ["x"], [FieldDefinition(key="supplier", label="供应商")]) is None
