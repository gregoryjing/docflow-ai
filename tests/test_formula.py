"""算术表达式求值的安全边界：只允许字段名 + 数字 + 加减乘除."""

from __future__ import annotations

import pytest

from docflow.formula import FormulaError, evaluate, parse, referenced_names


def test_evaluate_basic_arithmetic() -> None:
    tree = parse("quantity * unit_price")
    assert evaluate(tree, {"quantity": 10.0, "unit_price": 2.5}) == 25.0


def test_evaluate_supports_parentheses_and_unary() -> None:
    tree = parse("(a + b) * 2 - -c")
    assert evaluate(tree, {"a": 1.0, "b": 2.0, "c": 3.0}) == 9.0


def test_referenced_names_are_collected() -> None:
    assert referenced_names(parse("a * b + c")) == {"a", "b", "c"}


@pytest.mark.parametrize(
    "expression",
    [
        "a ** b",  # 幂运算不允许
        "a // b",  # 整除不允许
        "a % b",  # 取模不允许
        "a if b else c",  # 条件表达式不允许
        "__import__('os')",  # 函数调用不允许
        "a.b",  # 属性访问不允许
        "[a, b]",  # 列表不允许
        "'text'",  # 字符串常量不允许
        "a and b",  # 布尔运算不允许
    ],
)
def test_unsafe_expressions_are_rejected(expression: str) -> None:
    with pytest.raises(FormulaError):
        parse(expression)


def test_syntax_error_is_wrapped() -> None:
    with pytest.raises(FormulaError):
        parse("a * * b")
