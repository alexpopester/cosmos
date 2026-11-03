import pytest
from flow_automator.cron import Cron, check_cron
from flow_automator.expressions import Evaluator, ExpressionError, check_syntax, rewrite_triplets, telemetry_refs


def lookup(values):
    return lambda item, value_type: values[(item, value_type)]


def test_cosmos_check_syntax_reads_telemetry():
    ev = Evaluator({}, lookup({("INST HEALTH_STATUS TEMP1", "CONVERTED"): 55.0}))
    assert ev.truthy("INST HEALTH_STATUS TEMP1 > 50")
    assert not ev.truthy("INST HEALTH_STATUS TEMP1 < 50")


def test_multiple_checks_and_variables():
    values = {("INST HEALTH_STATUS TEMP1", "CONVERTED"): 55, ("INST HEALTH_STATUS TEMP2", "CONVERTED"): 5}
    ev = Evaluator({"limit": 50}, lookup(values))
    assert ev.truthy("INST HEALTH_STATUS TEMP1 > limit and INST HEALTH_STATUS TEMP2 < 10")


def test_strings_are_not_rewritten():
    assert rewrite_triplets("X == 'A B C'") == "X == 'A B C'"
    assert rewrite_triplets("INST HEALTH_STATUS COLLECT_TYPE == 'NORMAL'") == (
        "tlm('INST HEALTH_STATUS COLLECT_TYPE') == 'NORMAL'"
    )


def test_tlm_functions_and_types():
    values = {("INST HEALTH_STATUS TEMP1", "RAW"): 1234}
    ev = Evaluator({}, lookup(values))
    assert ev.evaluate("tlm_raw('inst health_status temp1')") == 1234
    assert ev.evaluate("tlm('INST HEALTH_STATUS TEMP1', 'raw')") == 1234


def test_telemetry_refs():
    refs = telemetry_refs("INST HEALTH_STATUS TEMP1 > 5 or tlm_raw('INST ADCS POSX') == 0")
    assert refs == {("INST HEALTH_STATUS TEMP1", "CONVERTED"), ("INST ADCS POSX", "RAW")}


@pytest.mark.parametrize(
    "source",
    [
        "__import__('os')",
        "(1).__class__",
        "open('/etc/passwd')",
        "[x for x in range(3)]",
        "lambda: 1",
        "print(1)",
    ],
)
def test_unsafe_expressions_are_rejected(source):
    assert check_syntax(source) is not None
    with pytest.raises(ExpressionError):
        Evaluator().evaluate(source)


def test_resource_limits():
    with pytest.raises(ExpressionError):
        Evaluator().evaluate("10 ** 100000")
    with pytest.raises(ExpressionError):
        Evaluator().evaluate("'a' * 10000000")


def test_unknown_variable():
    with pytest.raises(ExpressionError, match="Unknown variable 'nope'"):
        Evaluator().evaluate("nope + 1")


def test_render_templates():
    ev = Evaluator({"n": 3, "name": "NORMAL"})
    assert (
        ev.render("INST COLLECT with TYPE {{name}}, DURATION {{ n * 2 }}")
        == "INST COLLECT with TYPE NORMAL, DURATION 6"
    )
    assert ev.render("no templates") == "no templates"


def test_var_function_and_misc():
    ev = Evaluator({"a": [1, 2, 3]})
    assert ev.evaluate("var('missing', 7)") == 7
    assert ev.evaluate("a[1] + len(a)") == 5
    assert ev.evaluate("'yes' if a else 'no'") == "yes"
    assert ev.evaluate("max(a) - min(a)") == 2


def test_cron():
    import datetime

    cron = Cron("*/15 9-17 * * 1-5")
    monday_930 = datetime.datetime(2026, 11, 2, 9, 30)
    sunday_930 = datetime.datetime(2026, 11, 1, 9, 30)
    assert cron.matches(monday_930)
    assert not cron.matches(monday_930.replace(minute=31))
    assert not cron.matches(sunday_930)
    assert Cron("0 0 * * 7").matches(datetime.datetime(2026, 11, 1, 0, 0))  # 7 is Sunday
    assert check_cron("* * *") is not None
    assert check_cron("61 * * * *") is not None
    assert check_cron("0 12 1 * *") is None
