"""Flow templates: flows that declare parameters, filled in when a run is scheduled or started.

A template is an ordinary flow with ``"template": true`` and a parameter list::

    "parameters": [
      {"name": "rate", "type": "number", "default": 2, "required": true, "description": "..."},
      {"name": "mode", "type": "choice", "choices": ["SAFE", "NOMINAL"], "default": "SAFE"}
    ]

Each parameter becomes a run variable of the same name, so nodes use it as
``{{ rate }}`` in templates or ``rate`` in expressions.
"""

PARAM_TYPES = ("string", "number", "boolean", "choice")
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
        if param.get("default") not in (None, ""):
            try:
                coerce_value(param, param["default"])
            except ValueError as error:
                messages.append(f"{label} default: {error}")
    return messages


def coerce_value(param, value):
    kind = param.get("type", "string")
    if kind == "number":
        if isinstance(value, bool):
            raise ValueError("must be a number")
        if isinstance(value, (int, float)):
            return value
        try:
            text = str(value).strip()
            return int(text) if text.lstrip("+-").isdigit() else float(text)
        except ValueError:
            raise ValueError(f"'{value}' is not a number") from None
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "yes", "1", "on"):
            return True
        if text in ("false", "no", "0", "off"):
            return False
        raise ValueError(f"'{value}' is not true or false")
    if kind == "choice":
        choices = [str(c) for c in param.get("choices") or []]
        if str(value) not in choices:
            raise ValueError(f"'{value}' is not one of {', '.join(choices)}")
        return str(value)
    return str(value)


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
