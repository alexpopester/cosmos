"""Flow templates: flows that declare parameters, filled in when a run is scheduled or started.

A template is an ordinary flow with ``"template": true`` and a parameter list::

    "parameters": [
      {"name": "rate", "type": "number", "default": 2, "required": true, "description": "..."},
      {"name": "mode", "type": "choice", "choices": ["SAFE", "NOMINAL"], "default": "SAFE"}
    ]

Each parameter becomes a run variable of the same name, so nodes use it as
``{{ rate }}`` in templates or ``rate`` in expressions.

Constraints, checked whenever a run or activity is started or scheduled:

* string: ``pattern`` (a regular expression the whole value must match) and
  ``max_length``
* number: ``min``, ``max`` and ``integer``

The ``item`` type is a telemetry item ("TGT PKT ITEM", picked in the tool),
``command`` a whole command ("TGT CMD with ..."), and ``list`` a list of
strings (entered comma separated). Use an item parameter in a condition as
``tlm(item)``, a command parameter in a Send command node as
``{{ command }}`` with Allow raw values on.

Prefer choice or number parameters for anything that goes into a command; a
command also refuses values that would change it (see runner.render_command).
"""

import re


PARAM_TYPES = ("string", "number", "boolean", "choice", "item", "command", "list")
# Set by the contact planner for runs of scheduled activities. Other runs of a
# template get them as None, so templates fail with "this run has no contact"
# rather than "unknown variable".
CONTACT_VARIABLES = (
    "contact_id",
    "contact_satellite",
    "contact_target",
    "contact_station",
    "contact_aos",
    "contact_tca",
    "contact_los",
    "contact_duration",
    "contact_max_elevation",
    "activity_id",
    "activity_label",
)


def check_parameters(parameters):
    """Problems with a template's parameter declarations, as messages."""
    if parameters in (None, []):
        return []
    if not isinstance(parameters, list):
        return ["Parameters must be a list"]
    messages = []
    seen = set()
    for index, param in enumerate(parameters):
        if not isinstance(param, dict):
            messages.append(f"Parameter {index + 1} must be an object")
            continue
        name = str(param.get("name") or "")
        label = f"Parameter '{name or index + 1}'"
        if not name.isidentifier():
            messages.append(f"{label}: name must be a valid identifier")
        elif name in seen:
            messages.append(f"{label} is declared twice")
        elif name.startswith("contact_"):
            messages.append(f"{label}: names starting with contact_ are set by the scheduler")
        seen.add(name)
        kind = param.get("type", "string")
        if kind not in PARAM_TYPES:
            messages.append(f"{label}: type must be one of {', '.join(PARAM_TYPES)}")
            continue
        if kind == "choice" and not [c for c in param.get("choices") or [] if str(c) != ""]:
            messages.append(f"{label}: a choice needs at least one option")
        if param.get("pattern") not in (None, ""):
            try:
                re.compile(str(param["pattern"]))
            except re.error as error:
                messages.append(f"{label}: pattern is not a valid regular expression ({error})")
        for key in ("min", "max", "max_length"):
            if param.get(key) not in (None, ""):
                try:
                    float(param[key])
                except (TypeError, ValueError):
                    messages.append(f"{label}: {key} must be a number")
        try:
            low, high = param.get("min"), param.get("max")
            if low not in (None, "") and high not in (None, "") and float(low) > float(high):
                messages.append(f"{label}: min is more than max")
        except (TypeError, ValueError):
            pass
        if param.get("default") not in (None, ""):
            try:
                coerce_value(param, param["default"])
            except ValueError as error:
                messages.append(f"{label} default: {error}")
    return messages


def _limit(param, key):
    value = param.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def coerce_value(param, value):
    kind = param.get("type", "string")
    if kind == "number":
        if isinstance(value, bool):
            raise ValueError("must be a number")
        if not isinstance(value, (int, float)):
            try:
                text = str(value).strip()
                value = int(text) if text.lstrip("+-").isdigit() else float(text)
            except ValueError:
                raise ValueError(f"'{value}' is not a number") from None
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("must be a finite number")
        if param.get("integer") and float(value) != int(value):
            raise ValueError(f"{value} must be a whole number")
        low, high = _limit(param, "min"), _limit(param, "max")
        if low is not None and value < low:
            raise ValueError(f"{value} is below the minimum {low:g}")
        if high is not None and value > high:
            raise ValueError(f"{value} is above the maximum {high:g}")
        return int(value) if param.get("integer") else value
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "yes", "1", "on"):
            return True
        if text in ("false", "no", "0", "off"):
            return False
        raise ValueError(f"'{value}' is not true or false")
    if kind == "item":
        parts = str(value).replace("__", " ").split()
        if len(parts) != 3:
            raise ValueError(f"'{value}' is not a telemetry item (TARGET PACKET ITEM)")
        return " ".join(p.upper() for p in parts)
    if kind == "command":
        text = str(value).strip()
        if len(text.split()) < 2 or "\n" in text:
            raise ValueError(f"'{value}' is not a command (TARGET COMMAND with ...)")
        return text
    if kind == "list":
        items = value if isinstance(value, list) else str(value).split(",")
        return [str(v).strip() for v in items if str(v).strip() != ""]
    if kind == "choice":
        choices = [str(c) for c in param.get("choices") or []]
        if str(value) not in choices:
            raise ValueError(f"'{value}' is not one of {', '.join(choices)}")
        return str(value)
    text = str(value)
    max_length = _limit(param, "max_length")
    if max_length is not None and len(text) > max_length:
        raise ValueError(f"is longer than {max_length:g} characters")
    if param.get("pattern") not in (None, "") and not re.fullmatch(str(param["pattern"]), text):
        raise ValueError(f"'{text}' doesn't match the pattern {param['pattern']}")
    return text


def fill_parameters(parameters, values):
    """Validate values against declarations. Returns (variables, problems).

    Missing values take the default; a required parameter with neither is a problem.
    Values for undeclared names are passed through unchanged.
    """
    values = dict(values or {})
    variables = {}
    problems = []
    for param in parameters or []:
        name = param.get("name")
        if not name:
            continue
        value = values.pop(name, None)
        if value in (None, ""):
            value = param.get("default")
        if value in (None, ""):
            if param.get("required"):
                problems.append(f"'{name}' is required")
            elif param.get("type", "string") == "string":
                variables[name] = ""
            continue
        try:
            variables[name] = coerce_value(param, value)
        except ValueError as error:
            problems.append(f"'{name}': {error}")
    variables.update(values)
    return variables, problems
