"""Minimal 5-field cron matcher (minute hour day-of-month month day-of-week).

Supports ``*``, numbers, ranges ``a-b``, lists ``a,b`` and steps ``*/n`` /
``a-b/n``. Day-of-week is 0-6 with 0 = Sunday (7 is also Sunday). Like
standard cron, when both day fields are restricted a time matches if either
one does. Times are evaluated in UTC, matching COSMOS.
"""

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
_NAMES = ["minute", "hour", "day of month", "month", "day of week"]


class CronError(ValueError):
    pass


def _parse_field(text, low, high, name):
    values = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) == 0:
                raise CronError(f"Bad step '{step_text}' in {name}")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_text, end_text = part.split("-", 1)
            if not (start_text.isdigit() and end_text.isdigit()):
                raise CronError(f"Bad range '{part}' in {name}")
            start, end = int(start_text), int(end_text)
        elif part.isdigit():
            start = end = int(part)
            if step != 1:
                end = high
        else:
            raise CronError(f"Bad value '{part}' in {name}")
        if start < low or end > high or start > end:
            raise CronError(f"{name} value '{part}' out of range {low}-{high}")
        values.update(range(start, end + 1, step))
    return values


class Cron:
    def __init__(self, expression):
        fields = str(expression).split()
        if len(fields) != 5:
            raise CronError("Cron needs 5 fields: minute hour day-of-month month day-of-week")
        parsed = [_parse_field(f, lo, hi, n) for f, (lo, hi), n in zip(fields, _RANGES, _NAMES, strict=True)]
        self.minutes, self.hours, self.days, self.months, weekdays = parsed
        self.weekdays = {0 if day == 7 else day for day in weekdays}
        self.day_restricted = fields[2] != "*"
        self.weekday_restricted = fields[4] != "*"

    def matches(self, dt):
        """True if the datetime (to the minute) matches this cron expression."""
        if dt.minute not in self.minutes or dt.hour not in self.hours or dt.month not in self.months:
            return False
        day_ok = dt.day in self.days
        weekday_ok = (dt.isoweekday() % 7) in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok


def check_cron(expression):
    try:
        Cron(expression)
        return None
    except CronError as error:
        return str(error)
