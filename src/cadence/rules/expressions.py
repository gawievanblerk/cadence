"""A small, safe expression language for clinical rules.

Clinicians write things like::

    observations.confidence <= thresholds.low_confidence
    lapsed_in_last(7) and not client.flags.lapse_plan_agreed
    count(observations.trigger_situation) >= 3

The requirement is that these are readable by a non-programmer, evaluated
identically every time, and incapable of doing anything other than returning a
value. So this is not `eval`. It is a whitelist walk over Python's own parse
tree: the node types below are the entire language. Anything else - a call to
an unlisted function, an import, an attribute on a real object, a lambda - is a
parse-time error that the content validator surfaces before publication.

Keeping it a subset of Python syntax means the clinical team gets editor
highlighting and we get a parser we did not have to write.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from typing import Any

__all__ = ["ExpressionError", "OpenMapping", "evaluate", "static_check", "truthy"]


class ExpressionError(Exception):
    """Raised for a malformed, unsafe, or unresolvable expression."""


_BIN_OPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

_CMP_OPS: dict[type[ast.cmpop], Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.And,
    ast.Or,
    ast.BinOp,
    ast.Compare,
    ast.Call,
    ast.Name,
    ast.Attribute,
    ast.Subscript,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Load,
    ast.In,
    ast.NotIn,
    ast.IfExp,
    *_BIN_OPS,
    *_CMP_OPS,
)


class ExpressionContext(dict):
    """The variables an expression may see.

    A plain dict, with dotted access supported by the evaluator. Nothing in
    here is a live domain object: the orchestrator projects a flat, JSON-safe
    view before evaluation so that an expression can never reach a database
    session or a network client.
    """


class OpenMapping(dict):
    """A context namespace whose keys cannot be known in advance.

    `capture` and `flags` are populated at runtime from whatever the current
    session collected, so an expression referring to a key that is not there
    yet is normal rather than a mistake, and reads as null. Closed namespaces - `observations`, `thresholds`, `episode` - raise instead, because a missing
    key there is a typo the clinical team needs to see.
    """


def _dotted(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        if name in obj:
            return obj[name]
        if isinstance(obj, OpenMapping):
            return None
        raise ExpressionError(f"unknown field {name!r}")
    raise ExpressionError(f"cannot read {name!r} on a {type(obj).__name__}")


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _builtins(ctx: Mapping[str, Any]) -> dict[str, Callable[..., Any]]:
    """Functions available inside expressions.

    Every one of these exists because a clinician asked a question in plain
    English that could not otherwise be written as a comparison.
    """

    def days_since(what: Any) -> float | None:
        """Days since a date, a session completion, or `quit_date`.

        A date that is not set yields null, not a large number, so that the
        comparison it feeds is false. Nothing has been "a long time since" a
        quit date the client has not chosen.
        """
        if isinstance(what, str):
            marks = ctx.get("marks", {}) or {}
            if what in marks:
                what = marks[what]
            else:
                episode = ctx.get("episode", {}) or {}
                what = episode.get(what, what)
        d = _to_date(what)
        if d is None:
            return None
        return (datetime.now(timezone.utc).date() - d).days

    def count(value: Any) -> int:
        return len(_as_list(value))

    def contains(haystack: Any, needle: Any) -> bool:
        items = _as_list(haystack)
        if isinstance(needle, str):
            return any(isinstance(i, str) and needle.lower() in i.lower() for i in items)
        return needle in items

    def latest(value: Any) -> Any:
        items = _as_list(value)
        return items[-1] if items else None

    def mean(value: Any) -> float | None:
        nums = [float(v) for v in _as_list(value) if isinstance(v, (int, float))]
        return sum(nums) / len(nums) if nums else None

    def defined(value: Any) -> bool:
        return value is not None and value != [] and value != ""

    return {
        "len": lambda v: len(_as_list(v)),
        "count": count,
        "min": lambda *a: min(*a),
        "max": lambda *a: max(*a),
        "abs": abs,
        "round": round,
        "sum": lambda v: sum(_as_list(v)),
        "any": lambda v: any(_as_list(v)),
        "all": lambda v: all(_as_list(v)),
        "days_since": days_since,
        "contains": contains,
        "latest": latest,
        "mean": mean,
        "defined": defined,
    }


class _Evaluator(ast.NodeVisitor):
    def __init__(self, ctx: Mapping[str, Any]):
        self.ctx = ctx
        self.funcs = _builtins(ctx)

    def visit(self, node: ast.AST) -> Any:  # noqa: D102
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(f"{type(node).__name__} is not allowed in an expression")
        return super().visit(node)

    def generic_visit(self, node: ast.AST) -> Any:
        raise ExpressionError(f"{type(node).__name__} is not allowed in an expression")

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id in self.ctx:
            return self.ctx[node.id]
        if node.id in self.funcs:
            return self.funcs[node.id]
        if node.id in ("None", "True", "False"):
            return {"None": None, "True": True, "False": False}[node.id]
        raise ExpressionError(f"unknown name {node.id!r}")

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        return _dotted(self.visit(node.value), node.attr)

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        target = self.visit(node.value)
        key = self.visit(node.slice)
        try:
            return target[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExpressionError(f"cannot index with {key!r}: {exc}") from exc

    def visit_List(self, node: ast.List) -> Any:
        return [self.visit(e) for e in node.elts]

    def visit_Tuple(self, node: ast.Tuple) -> Any:
        return tuple(self.visit(e) for e in node.elts)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        val = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            return not val
        if isinstance(node.op, ast.USub):
            return -val
        return +val

    def visit_BoolOp(self, node: ast.BoolOp) -> Any:
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = self.visit(value)
                if not result:
                    return False
            return bool(result)
        for value in node.values:
            if self.visit(value):
                return True
        return False

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ExpressionError(f"operator {type(node.op).__name__} is not allowed")
        return op(self.visit(node.left), self.visit(node.right))

    def visit_IfExp(self, node: ast.IfExp) -> Any:
        return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)

    def visit_Compare(self, node: ast.Compare) -> Any:
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = self.visit(comparator)
            if isinstance(op, ast.In):
                ok = _in(left, right)
            elif isinstance(op, ast.NotIn):
                ok = not _in(left, right)
            else:
                fn = _CMP_OPS.get(type(op))
                if fn is None:
                    raise ExpressionError(f"comparison {type(op).__name__} is not allowed")
                if left is None or right is None:
                    # A missing observation is not a match. Rules must never fire
                    # on absent data by accident.
                    return False
                ok = fn(left, right)
            if not ok:
                return False
            left = right
        return True

    def visit_Call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("only plain function calls are allowed")
        fn = self.funcs.get(node.func.id)
        if fn is None:
            raise ExpressionError(f"unknown function {node.func.id!r}")
        if node.keywords:
            raise ExpressionError("keyword arguments are not allowed")
        return fn(*[self.visit(a) for a in node.args])


def _in(needle: Any, haystack: Any) -> bool:
    if haystack is None:
        return False
    if isinstance(haystack, str):
        return isinstance(needle, str) and needle in haystack
    return needle in _as_list(haystack)


def evaluate(expression: str, context: Mapping[str, Any]) -> Any:
    """Evaluate `expression` against `context`. Raises ExpressionError on anything odd."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"could not parse {expression!r}: {exc.msg}") from exc
    return _Evaluator(context).visit(tree)


def truthy(expression: str | None, context: Mapping[str, Any], *, default: bool = True) -> bool:
    if expression is None:
        return default
    return bool(evaluate(expression, context))


def static_check(expression: str, allowed_roots: set[str]) -> list[str]:
    """Parse-time validation used by the content validator.

    Returns a list of human-readable problems. An empty list means the
    expression is syntactically valid, uses only whitelisted constructs, and
    refers only to roots the engine will actually provide at runtime.
    """
    problems: list[str] = []
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg}"]

    known_funcs = set(_builtins({}))
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            problems.append(f"{type(node).__name__} is not allowed")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id not in known_funcs:
                problems.append(f"unknown function {node.func.id!r}")
        if isinstance(node, ast.Name):
            if node.id in known_funcs or node.id in {"None", "True", "False"}:
                continue
            if node.id not in allowed_roots:
                problems.append(
                    f"unknown name {node.id!r} (available: {', '.join(sorted(allowed_roots))})"
                )
    return problems
