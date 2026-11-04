"""Safe expression evaluation for conditions, variables and templates.

Expressions are a small, side-effect free subset of Python:

* literals, arithmetic, comparisons, ``and`` / ``or`` / ``not``, ``x if c else y``
* variables by bare name (``count > 3``)
* whitelisted functions: ``tlm('INST HEALTH_STATUS TEMP1')``, ``tlm_raw``,
  ``tlm_formatted``, ``abs``, ``min``, ``max``, ``round``, ``int``, ``float``,
  ``str``, ``bool``, ``len``, ``now``, ``var('name', default)``
* text: ``upper``, ``lower``, ``strip``, ``replace``, ``split``, ``join(sep, list)``,
  ``startswith``, ``endswith``, ``contains``, ``format(value, '.2f')``, ``hex``,
  ``utc(seconds)`` (ISO time)
* collections and math: ``sum``, ``any``, ``all``, ``sorted``, ``reversed``,
  ``range`` (up to 10,000), ``list``, ``keys``, ``values``, ``get(container, key, default)``,
  ``sqrt``, ``floor``, ``ceil``, ``log10``
* in runs of scheduled activities: ``time_to_aos()``, ``time_to_tca()``,
  ``time_to_los()`` (seconds, negative once passed)

COSMOS check syntax is also accepted: any three upper case words in a row,
``INST HEALTH_STATUS TEMP1``, are read as ``tlm('INST HEALTH_STATUS TEMP1')``,
so ``INST HEALTH_STATUS TEMP1 > 50 and INST HEALTH_STATUS TEMP2 < 10`` works.

Nothing else is allowed: no attribute access, imports, lambdas or
comprehensions, so a condition can never run arbitrary code in the engine.

Expressions are also bounded so one can't stall or exhaust the engine: the
source length and syntax tree size are capped, and every operation that could
build a huge value (``*``, ``+``, ``**``, ``<<``, ``%`` on text, ``sum``,
``replace``, ``join``, ``format``, ``round``, ``str``) checks the size first.
"""

import ast
import datetime
import math
import operator
import re
import time


MAX_RESULT_LENGTH = 100_000  # characters of text, or elements (counting nested ones) of a list
MAX_POWER_EXPONENT = 1000
MAX_INT_BITS = 64_000  # integers bigger than this (about 19,000 digits) are refused
MAX_SOURCE_LENGTH = 10_000  # characters in one expression
MAX_AST_NODES = 2_000  # syntax tree nodes in one expression
MAX_FORMAT_WIDTH = 200  # widths and precisions in format() specs
MAX_ROUND_DIGITS = 100
_CACHE_SOURCE_LENGTH = 1_000  # only short expressions are cached
_CACHE_ENTRIES = 5_000

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

# Seconds until a point of the run's contact, from the contact_* run variables
CONTACT_FUNCTIONS = {"time_to_aos": "contact_aos", "time_to_tca": "contact_tca", "time_to_los": "contact_los"}

MAX_RANGE = 10_000


def _text(value):
    if not isinstance(value, str):
        raise ExpressionError(f"expected text, got {type(value).__name__}")
    return value


def size_of(value, budget=MAX_RESULT_LENGTH):
    """Rough size of a value: characters of text, plus elements of (nested) containers. Stops past budget."""
    stack = [value]
    total = 0
    while stack:
        item = stack.pop()
        if isinstance(item, (str, bytes)):
            total += len(item)
        elif isinstance(item, dict):
            total += len(item)
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            total += len(item)
            stack.extend(item)
        elif isinstance(item, int) and not isinstance(item, bool) and item.bit_length() > 64:
            total += item.bit_length() // 3  # about as many characters as it has digits
        else:
            total += 1
        if total > budget:
            return total
    return total


def _too_big(value):
    if size_of(value) > MAX_RESULT_LENGTH:
        raise ExpressionError("Result too large")
    return value


def _format(value, spec=""):
    # Only format specs, never format strings with fields: '.2f', '>8', '04d', ',', 'x'
    spec = str(spec)
    if len(spec) > 20 or "{" in spec or "}" in spec:
        raise ExpressionError("format() takes a short format spec like '.2f' or '04d'")
    if any(int(number) > MAX_FORMAT_WIDTH for number in re.findall(r"\d+", spec)):
        raise ExpressionError(f"format() widths and precisions are limited to {MAX_FORMAT_WIDTH}")
    return format(value, spec)


def _round(value, digits=None):
    if digits is not None and abs(int(digits)) > MAX_ROUND_DIGITS:
        raise ExpressionError(f"round() takes at most {MAX_ROUND_DIGITS} digits")
    return round(value) if digits is None else round(value, int(digits))


def _sum(values, start=0):
    # Numbers only: summing lists or text grows the result on every step (and quadratically)
    if not isinstance(start, (int, float)) or isinstance(start, bool):
        raise ExpressionError("sum() adds numbers; use join() for text")
    total = start
    for value in values:
        if not isinstance(value, (int, float)):
            raise ExpressionError("sum() adds numbers; use join() for text")
        total = total + value
    return total


def _replace(text, old, new):
    text, old, new = _text(text), str(old), str(new)
    count = len(text) + 1 if old == "" else text.count(old)
    if len(text) + count * (len(new) - len(old)) > MAX_RESULT_LENGTH:
        raise ExpressionError("Result too large")
    return text.replace(old, new)


def _range(*args):
    span = range(*[int(a) for a in args])
    if len(span) > MAX_RANGE:
        raise ExpressionError(f"range() is limited to {MAX_RANGE} values")
    return list(span)


def _join(separator, values):
    separator = _text(separator)
    parts = []
    length = 0
    for value in values:
        part = _str(value)
        length += len(part) + len(separator)
        if length > MAX_RESULT_LENGTH:
            raise ExpressionError("Result too large")
        parts.append(part)
    return separator.join(parts)


def _str(value):
    _too_big(value)  # str() of a container can be several times its size; refuse the big ones first
    return str(value)


def _get(container, key, default=None):
    try:
        return container[key]
    except (KeyError, IndexError, TypeError):
        return default


def _utc(seconds=None, fmt="%Y-%m-%dT%H:%M:%S.%fZ"):
    moment = time.time() if seconds is None else float(seconds)
    text = datetime.datetime.fromtimestamp(moment, tz=datetime.timezone.utc).strftime(str(fmt))
    return text[:-4] + "Z" if fmt.endswith(".%fZ") else text


_SAFE_FUNCTIONS = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": _round,
    "int": int,
    "float": float,
    "str": _str,
    "bool": bool,
    "len": len,
    "now": time.time,
    # text
    "upper": lambda s: _text(s).upper(),
    "lower": lambda s: _text(s).lower(),
    "strip": lambda s, chars=None: _text(s).strip(chars),
    "replace": _replace,
    "split": lambda s, sep=None, limit=-1: _text(s).split(sep, int(limit)),
    "join": _join,
    "startswith": lambda s, prefix: _text(s).startswith(prefix),
    "endswith": lambda s, suffix: _text(s).endswith(suffix),
    "contains": lambda container, item: item in container,
    "format": _format,
    "hex": hex,
    "utc": _utc,
    # collections
    "sum": _sum,
    "any": any,
    "all": all,
    "sorted": sorted,
    "reversed": lambda values: list(reversed(values)),
    "range": _range,
    "list": list,
    "keys": lambda d: list(d.keys()),
    "values": lambda d: list(d.values()),
    "get": _get,
    # math
    "sqrt": math.sqrt,
    "floor": math.floor,
    "ceil": math.ceil,
    "log10": math.log10,
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
    if len(source) > MAX_SOURCE_LENGTH:
        raise ExpressionError(f"Expression is longer than {MAX_SOURCE_LENGTH} characters")
    rewritten = rewrite_triplets(source.strip())
    try:
        tree = ast.parse(rewritten, mode="eval")
    except SyntaxError as error:
        raise ExpressionError(f"Invalid expression '{_short(source)}': {error.msg}") from None
    except (RecursionError, MemoryError):
        raise ExpressionError(f"Expression is nested too deeply: '{_short(source)}'") from None
    _check_nodes(tree, source)
    if len(source) <= _CACHE_SOURCE_LENGTH:
        if len(_PARSE_CACHE) >= _CACHE_ENTRIES:
            _PARSE_CACHE.clear()
        _PARSE_CACHE[source] = tree
    return tree


def _short(source, length=80):
    return source if len(source) <= length else source[:length] + "…"


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
    for count, node in enumerate(ast.walk(tree)):
        if count >= MAX_AST_NODES:
            raise ExpressionError(f"Expression is too complex (more than {MAX_AST_NODES} parts): '{_short(source)}'")
        if isinstance(node, ast.Constant) and isinstance(node.value, (bytes, complex)):
            raise ExpressionError(f"Byte strings and complex numbers are not supported in '{_short(source)}'")
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(f"'{type(node).__name__}' is not allowed in expression '{source}'")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ExpressionError(f"Only simple function calls are allowed in '{source}'")
            name = node.func.id
            known = name in _SAFE_FUNCTIONS or name in TLM_FUNCTIONS or name in CONTACT_FUNCTIONS
            if not known and name != "var":
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
        try:
            return self._eval(parse(source).body)
        except (RecursionError, MemoryError):
            raise ExpressionError(f"Expression too large to evaluate: '{_short(source)}'") from None

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
            _check_binop(node.op, left, right)
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
        if name in CONTACT_FUNCTIONS:
            if args:
                raise ExpressionError(f"{name}() takes no arguments")
            moment = self.variables.get(CONTACT_FUNCTIONS[name])
            if not isinstance(moment, (int, float)):
                raise ExpressionError(f"{name}(): this run has no contact (it wasn't started by a scheduled activity)")
            return moment - time.time()
        if name == "var":
            if len(args) not in (1, 2):
                raise ExpressionError("var() takes a name and an optional default")
            return self.variables.get(args[0], args[1] if len(args) == 2 else None)
        try:
            result = _SAFE_FUNCTIONS[name](*args)
        except ExpressionError as error:
            raise ExpressionError(f"{name}(): {error}") from None
        except Exception as error:
            raise ExpressionError(f"{name}(): {error}") from None
        return _too_big(result) if isinstance(result, (str, list, tuple, dict)) else result


def _int_bits(value):
    return value.bit_length() if isinstance(value, int) else 0


def _check_binop(op, left, right):
    """Refuse an operation whose result would be huge, before computing it."""
    if isinstance(op, ast.Pow) and isinstance(right, (int, float)):
        if abs(right) > MAX_POWER_EXPONENT:
            raise ExpressionError("Exponent too large")
        if isinstance(left, int) and isinstance(right, int) and right > 0 and _int_bits(left) * right > MAX_INT_BITS:
            raise ExpressionError("Result too large")
    elif isinstance(op, ast.LShift) and isinstance(right, int):
        if right > MAX_INT_BITS or _int_bits(left) + right > MAX_INT_BITS:
            raise ExpressionError("Result too large")
    elif isinstance(op, ast.Mult):
        if isinstance(left, int) and isinstance(right, int) and _int_bits(left) + _int_bits(right) > MAX_INT_BITS:
            raise ExpressionError("Result too large")
        for seq, count in ((left, right), (right, left)):
            repeats = isinstance(seq, (str, bytes, list, tuple)) and isinstance(count, int) and count > 0
            if repeats and size_of(seq) * count > MAX_RESULT_LENGTH:
                raise ExpressionError("Result too large")
    elif isinstance(op, ast.Add):
        sequences = isinstance(left, (str, bytes, list, tuple)) and isinstance(right, (str, bytes, list, tuple))
        if sequences and size_of(left) + size_of(right) > MAX_RESULT_LENGTH:
            raise ExpressionError("Result too large")
    elif isinstance(op, ast.Mod) and isinstance(left, (str, bytes)):
        raise ExpressionError("'%' formatting of text is not supported; use format() or a template")


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
