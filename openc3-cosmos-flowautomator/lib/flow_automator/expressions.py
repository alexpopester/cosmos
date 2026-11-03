"""Safe expression evaluation for conditions, variables and templates.

Expressions are a small, side-effect free subset of Python:

* literals, arithmetic, comparisons, ``and`` / ``or`` / ``not``, ``x if c else y``
* variables by bare name (``count > 3``)
* whitelisted functions: ``tlm('INST HEALTH_STATUS TEMP1')``, ``tlm_raw``,
  ``tlm_formatted``, ``abs``, ``min``, ``max``, ``round``, ``int``, ``float``,
  ``str``, ``bool``, ``len``, ``now``, ``var('name', default)``

COSMOS check syntax is also accepted: any three upper case words in a row,
``INST HEALTH_STATUS TEMP1``, are read as ``tlm('INST HEALTH_STATUS TEMP1')``,
so ``INST HEALTH_STATUS TEMP1 > 50 and INST HEALTH_STATUS TEMP2 < 10`` works.

Nothing else is allowed: no attribute access, imports, lambdas or
comprehensions, so a condition can never run arbitrary code in the engine.
"""

import ast
import operator
import re
import time


MAX_RESULT_LENGTH = 100_000
MAX_POWER_EXPONENT = 1000

TLM_FUNCTIONS = {"tlm": "CONVERTED", "tlm_raw": "RAW", "tlm_formatted": "FORMATTED", "tlm_with_units": "WITH_UNITS"}

# String literals are left alone when rewriting COSMOS item triplets
_STRING_RE = re.compile(r"""("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""")
# Three upper case words in a row: TARGET PACKET ITEM
_TRIPLET_RE = re.compile(r"(?<![\w'\"])([A-Z][A-Z0-9_]*)\s+([A-Z][A-Z0-9_]*)\s+([A-Z][A-Z0-9_]*)(?![\w(])")
_TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}")


class ExpressionError(Exception):
    pass


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
}
_UNARY_OPS = {ast.Not: operator.not_, ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Invert: operator.invert}
_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

_SAFE_FUNCTIONS = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "len": len,
    "now": time.time,
}


def rewrite_triplets(source):
    """Turn ``TGT PKT ITEM`` triplets outside string literals into ``tlm('TGT PKT ITEM')``."""
    parts = _STRING_RE.split(source)
    for index in range(0, len(parts), 2):  # even indexes are outside strings
        parts[index] = _TRIPLET_RE.sub(lambda m: f"tlm('{m.group(1)} {m.group(2)} {m.group(3)}')", parts[index])
    return "".join(parts)


_PARSE_CACHE = {}


def parse(source):
    """Parse an expression into an AST, raising ExpressionError on bad syntax or disallowed constructs."""
    if not isinstance(source, str) or not source.strip():
        raise ExpressionError("Expression is empty")
    cached = _PARSE_CACHE.get(source)
    if cached is not None:
        return cached
    rewritten = rewrite_triplets(source.strip())
    try:
        tree = ast.parse(rewritten, mode="eval")
    except SyntaxError as error:
        raise ExpressionError(f"Invalid expression '{source}': {error.msg}") from None
    _check_nodes(tree, source)
    if len(_PARSE_CACHE) > 5000:
        _PARSE_CACHE.clear()
    _PARSE_CACHE[source] = tree
    return tree


_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Subscript,
    ast.Slice,
    ast.And,
    ast.Or,
    *_BIN_OPS.keys(),
    *_UNARY_OPS.keys(),
    *_CMP_OPS.keys(),
)


def _check_nodes(tree, source):
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(f"'{type(node).__name__}' is not allowed in expression '{source}'")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExpressionError(f"Only simple function calls are allowed in '{source}'")
            name = node.func.id
            if name not in _SAFE_FUNCTIONS and name not in TLM_FUNCTIONS and name != "var":
                raise ExpressionError(f"Unknown function '{name}' in '{source}'")
            if node.keywords:
                raise ExpressionError(f"Keyword arguments are not supported in '{source}'")


def telemetry_refs(source):
    """Return the set of (item, value_type) telemetry references in an expression.

    Only literal references (``tlm('INST HEALTH_STATUS TEMP1')``) are found; the
    trigger poller batches these into one request per tick.
    """
    refs = set()
    for node in ast.walk(parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in TLM_FUNCTIONS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            refs.add((normalize_item(node.args[0].value), TLM_FUNCTIONS[node.func.id]))
    return refs


def normalize_item(item):
    """Normalize 'inst health_status temp1' to 'INST HEALTH_STATUS TEMP1', validating there are three parts."""
    parts = str(item).replace("__", " ").split()
    if len(parts) != 3:
        raise ExpressionError(f"Telemetry item must be 'TARGET PACKET ITEM', got '{item}'")
    return " ".join(part.upper() for part in parts)


class Evaluator:
    """Evaluates parsed expressions against variables and a telemetry lookup.

    ``tlm_lookup(item, value_type)`` receives a normalized 'TGT PKT ITEM' string
    and one of CONVERTED/RAW/FORMATTED/WITH_UNITS.
    """

    def __init__(self, variables=None, tlm_lookup=None):
        self.variables = variables if variables is not None else {}
        self.tlm_lookup = tlm_lookup

    def evaluate(self, source):
        return self._eval(parse(source).body)

    def truthy(self, source):
        return bool(self.evaluate(source))

    def render(self, template):
        """Replace each ``{{ expression }}`` in a string with its value."""
        if not isinstance(template, str) or "{{" not in template:
            return template

        def replace(match):
            value = self.evaluate(match.group(1))
            return "" if value is None else str(value)

        return _TEMPLATE_RE.sub(replace, template)

    def _eval(self, node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self.variables:
                return self.variables[node.id]
            raise ExpressionError(f"Unknown variable '{node.id}'")
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                result = True
                for value in node.values:
                    result = self._eval(value)
                    if not result:
                        return result
                return result
            result = False
            for value in node.values:
                result = self._eval(value)
                if result:
                    return result
            return result
        if isinstance(node, ast.BinOp):
            left = self._eval(node.left)
            right = self._eval(node.right)
            if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) and abs(right) > MAX_POWER_EXPONENT:
                raise ExpressionError("Exponent too large")
            if isinstance(node.op, ast.Mult):
                for seq, count in ((left, right), (right, left)):
                    sized = isinstance(seq, (str, list, tuple)) and isinstance(count, int)
                    if sized and len(seq) * count > MAX_RESULT_LENGTH:
                        raise ExpressionError("Result too large")
            try:
                return _BIN_OPS[type(node.op)](left, right)
            except Exception as error:
                raise ExpressionError(f"{type(error).__name__}: {error}") from None
        if isinstance(node, ast.UnaryOp):
            return _UNARY_OPS[type(node.op)](self._eval(node.operand))
        if isinstance(node, ast.Compare):
            left = self._eval(node.left)
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                right = self._eval(comparator)
                try:
                    if not _CMP_OPS[type(op)](left, right):
                        return False
                except TypeError as error:
                    raise ExpressionError(f"Cannot compare {left!r} and {right!r}: {error}") from None
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return self._eval(node.body) if self._eval(node.test) else self._eval(node.orelse)
        if isinstance(node, (ast.List, ast.Tuple)):
            values = [self._eval(element) for element in node.elts]
            return values if isinstance(node, ast.List) else tuple(values)
        if isinstance(node, ast.Dict):
            return {self._eval(k): self._eval(v) for k, v in zip(node.keys, node.values, strict=True)}
        if isinstance(node, ast.Subscript):
            container = self._eval(node.value)
            index = self._eval(node.slice) if not isinstance(node.slice, ast.Slice) else self._slice(node.slice)
            try:
                return container[index]
            except (KeyError, IndexError, TypeError) as error:
                raise ExpressionError(f"{type(error).__name__}: {error}") from None
        if isinstance(node, ast.Call):
            return self._call(node)
        raise ExpressionError(f"Unsupported expression element {type(node).__name__}")

    def _slice(self, node):
        return slice(
            self._eval(node.lower) if node.lower else None,
            self._eval(node.upper) if node.upper else None,
            self._eval(node.step) if node.step else None,
        )

    def _call(self, node):
        name = node.func.id
        args = [self._eval(arg) for arg in node.args]
        if name in TLM_FUNCTIONS:
            if not self.tlm_lookup:
                raise ExpressionError("Telemetry is not available here")
            if len(args) not in (1, 2):
                raise ExpressionError(f"{name}() takes 'TARGET PACKET ITEM' and an optional type")
            value_type = TLM_FUNCTIONS[name] if len(args) == 1 else str(args[1]).upper()
            return self.tlm_lookup(normalize_item(args[0]), value_type)
        if name == "var":
            if len(args) not in (1, 2):
                raise ExpressionError("var() takes a name and an optional default")
            return self.variables.get(args[0], args[1] if len(args) == 2 else None)
        try:
            result = _SAFE_FUNCTIONS[name](*args)
        except Exception as error:
            raise ExpressionError(f"{name}(): {error}") from None
        if isinstance(result, str) and len(result) > MAX_RESULT_LENGTH:
            raise ExpressionError("Result too large")
        return result


def check_syntax(source):
    """Return None if the expression parses, else the error message."""
    try:
        parse(source)
        return None
    except ExpressionError as error:
        return str(error)


def check_template(template):
    """Return None if every ``{{ }}`` in a template parses, else the first error."""
    if not isinstance(template, str):
        return None
    for match in _TEMPLATE_RE.finditer(template):
        error = check_syntax(match.group(1))
        if error:
            return error
    return None
