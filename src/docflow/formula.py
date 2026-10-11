"""安全算术表达式求值：供质检的 formula 规则使用.

只用 ast 白名单解析「字段名 + 数字常量 + 加减乘除」，杜绝 eval 带来的任意代码执行风险。
表达式在 Schema 加载时即完成语法与安全性校验，运行期不再承担解析风险。
"""

from __future__ import annotations

import ast
import operator
from typing import Final

_BIN_OPS: Final[dict[type[ast.operator], object]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY_OPS: Final[dict[type[ast.unaryop], object]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


class FormulaError(ValueError):
    """表达式非法：语法错误、含不允许的节点、或引用了非字段名."""


def parse(expression: str) -> ast.Expression:
    """解析并校验表达式，返回可直接求值的 AST."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"算术表达式语法错误：{expression!r}") from exc
    _ensure_safe(tree)
    return tree


def _ensure_safe(tree: ast.Expression) -> None:
    for node in ast.walk(tree):
        # ast.walk 会把运算符、上下文等辅助节点也吐出来，它们由父节点（BinOp/UnaryOp）校验，这里跳过
        if isinstance(node, (ast.Expression, ast.operator, ast.unaryop, ast.expr_context)):
            continue
        if isinstance(node, ast.BinOp):
            if type(node.op) not in _BIN_OPS:
                raise FormulaError(f"算术表达式只允许 + - * /，出现：{type(node.op).__name__}")
            continue
        if isinstance(node, ast.UnaryOp):
            if type(node.op) not in _UNARY_OPS:
                raise FormulaError(f"算术表达式不允许一元运算：{type(node.op).__name__}")
            continue
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise FormulaError(f"算术表达式只允许数字常量，出现：{node.value!r}")
            continue
        if isinstance(node, ast.Name):
            continue
        raise FormulaError(f"算术表达式包含不允许的语法：{type(node).__name__}")


def referenced_names(tree: ast.Expression) -> set[str]:
    """表达式里引用到的所有字段名（用于校验字段是否存在）."""
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def evaluate(tree: ast.Expression, values: dict[str, float]) -> float:
    """按给定字段取值求值；缺字段由调用方保证（调用前应已确认 names ⊆ values）."""
    return _eval_node(tree.body, values)


def _eval_node(node: ast.expr, values: dict[str, float]) -> float:
    if isinstance(node, ast.Constant):
        return float(node.value)  # type: ignore[arg-type]
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS[type(node.op)]
        return float(op(_eval_node(node.left, values), _eval_node(node.right, values)))  # type: ignore[operator]
    if isinstance(node, ast.UnaryOp):
        unary = _UNARY_OPS[type(node.op)]
        return float(unary(_eval_node(node.operand, values)))  # type: ignore[operator]
    raise FormulaError(f"无法求值的节点：{type(node).__name__}")  # pragma: no cover - parse 已拦截
