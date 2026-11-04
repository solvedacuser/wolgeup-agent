"""Source-preserving scalar normalization; no salary/time inference."""

import re
from datetime import date
from decimal import Decimal

CANONICAL_UNITS = {
    "pay_type": "category",
    "contract_start": "date",
    "contract_end": "date",
    "work_start": "time",
    "work_end": "time",
    "payday": "day_of_month",
    "break_minutes": "minute",
    "scheduled_daily_minutes": "minute",
    "scheduled_weekly_minutes": "minute",
}


def decimal_string(value):
    return format(value.normalize(), "f")


def money_number(raw):
    text = re.sub(r"[\s,₩￦]", "", raw).replace("KRW", "").replace("원정", "").replace("원", "")
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return Decimal(text)
    # Multipliers are only applied when explicitly present in the source scalar.
    tokens = list(re.finditer(r"(\d+(?:\.\d+)?)(억|천만|백만|십만|만|천|백|십)?", text))
    if not tokens or "".join(t.group() for t in tokens) != text:
        raise ValueError("Ambiguous currency scalar")
    scales = {
        None: 1,
        "억": 100000000,
        "천만": 10000000,
        "백만": 1000000,
        "십만": 100000,
        "만": 10000,
        "천": 1000,
        "백": 100,
        "십": 10,
    }
    return sum((Decimal(t[1]) * scales[t[2]] for t in tokens), Decimal(0))


def normalize_scalar(raw, unit):
    text = raw.strip()
    if unit == "text":
        return text
    if unit == "count":
        match = re.fullmatch(r"(\d+)\s*(?:일|days?)?", text, re.I)
        if not match:
            raise ValueError("Not an explicit count")
        return str(int(match[1]))
    if unit in ("KRW", "KRW/hour", "KRW/month", "KRW/year", "KRW/day"):
        return decimal_string(money_number(text))
    if unit == "minute":
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*(?:분|minutes?|min)?", text, re.I):
            text = text.replace(",", "")
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            return decimal_string(Decimal(text))
        match = re.fullmatch(
            r"\s*(?:(\d+(?:\.\d+)?)\s*(?:시간|hours?|h))?\s*(?:(\d+(?:\.\d+)?)\s*(?:분|minutes?|min))?\s*",
            text,
            re.I,
        )
        if match and any(match.groups()):
            return decimal_string(Decimal(match[1] or 0) * 60 + Decimal(match[2] or 0))
        raise ValueError("Not an explicit duration")
    if unit == "date":
        match = re.fullmatch(r"(\d{4})\s*(?:년|[-./])\s*(\d{1,2})\s*(?:월|[-./])\s*(\d{1,2})\s*일?\.?", text)
        if not match:
            raise ValueError("Incomplete date")
        return date(*map(int, match.groups())).isoformat()
    if unit == "time":
        seconds = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})", text)
        if seconds:
            hour, minute, second = map(int, seconds.groups())
            if hour > 23 or minute > 59 or second > 59:
                raise ValueError("Invalid clock time")
            return f"{hour:02}:{minute:02}:{second:02}"
        match = re.fullmatch(r"(오전|오후)?\s*(\d{1,2})\s*(?::|시)\s*(\d{1,2})?\s*분?", text)
        if not match:
            raise ValueError("Incomplete clock time")
        part, hour, minute = match.groups()
        hour, minute = int(hour), int(minute or 0)
        if part:
            if not 1 <= hour <= 12:
                raise ValueError("Invalid 12 hour clock")
            hour = hour % 12 + (12 if part == "오후" else 0)
        if hour > 23 or minute > 59:
            raise ValueError("Invalid clock time")
        return f"{hour:02}:{minute:02}"
    if unit == "day_of_month":
        match = re.fullmatch(r"(?:매월|익월|당월)?\s*(\d{1,2})\s*일?", text)
        if match and 1 <= int(match[1]) <= 31:
            return str(int(match[1]))
        if text in ("말일", "매월 말일", "last day"):
            return "last_day"
        raise ValueError("Not an explicit payment day")
    if unit == "category":
        text = re.sub(r"\s+", "", text).lower()
        choices = {
            "hourly": ("시급", "시간급", "시간급제", "시간당", "hourly", "hourlywage"),
            "monthly": ("월급", "월급제", "월급여", "monthly"),
            "annual": ("연봉", "연봉제", "연간지급제", "annual"),
            "daily": ("일급", "일급제", "daily"),
        }
        for value, forms in choices.items():
            if text.lower() in forms:
                return value
        raise ValueError("Ambiguous wage category")
    return None


def normalize_evidenced_scalar(raw, unit, quote, *, field_id=None):
    """Use explicit units beside the source scalar, never inferred work hours."""
    text = raw.strip()
    # Strip only a unit-compatible rate label, never a second amount or a sentence.
    rate_labels = {
        "KRW/month": r"월(?:급|급여)?",
        "KRW/year": r"연(?:봉)?",
        "KRW/day": r"일(?:급|당)?",
        "KRW/hour": r"(?:시급|시간급|시간당)",
    }
    if unit in rate_labels:
        labelled = re.fullmatch(rf"{rate_labels[unit]}\s*[:：]?\s*(.+)", text)
        if labelled:
            return normalize_scalar(labelled[1], unit)
    if unit == "minute":
        period_names = {"일": "day", "하루": "day", "day": "day", "주": "week", "week": "week"}
        expected_period = {
            "scheduled_daily_minutes": "day",
            "scheduled_weekly_minutes": "week",
            "break_minutes": "day",
        }.get(field_id)
        prefixed = re.fullmatch(r"(?:1\s*)?(일|하루|주)\s*[:：]?\s*(.+)", text)
        suffixed = re.fullmatch(r"(.+?)\s*(?:per\s+|/\s*)(day|week|일|주)", text, re.I)
        if prefixed or suffixed:
            period, duration = (prefixed[1], prefixed[2]) if prefixed else (suffixed[2], suffixed[1])
            if period_names[period.lower()] != expected_period:
                raise ValueError("Source duration period does not match field")
            # E.g. '1일 8시간' is an explicit daily duration, not 1 day plus 8 hours.
            if re.fullmatch(r"\d+(?:\.\d+)?", duration.strip()):
                raise ValueError("Source duration unit is missing")
            return normalize_scalar(duration, unit)
    if unit == "minute" and re.fullmatch(r"\d+(?:\.\d+)?", text):
        # Models sometimes return the number alone while keeping the source unit in the quote.
        matches = re.finditer(
            rf"(?<![\d.]){re.escape(text)}\s*\)?\s*(시간|hours?|h|분|minutes?|min)(?![a-z가-힣])",
            quote,
            re.I,
        )
        values = set()
        for match in matches:
            source_period = re.search(r"(?:1\s*)?(일|하루|주)\s*$", quote[: match.start()])
            if source_period and expected_period and period_names[source_period[1]] != expected_period:
                raise ValueError("Source duration period does not match field")
            if re.match(r"\s*\d+(?:\.\d+)?\s*(분|minutes?|min)\b", quote[match.end() :], re.I):
                raise ValueError("Source duration includes additional components")
            values.add(normalize_scalar(text + match[1], "minute"))
        if len(values) > 1:
            raise ValueError("Conflicting source duration units")
        if values:
            return values.pop()
        if field_id and re.fullmatch(r"work_[1-9]\d*_(?:worked|overtime|night|holiday)_minutes", field_id):
            if re.search(
                r"(?:기록\s*단위|시간\s*단위|time\s*unit)\s*[:：]\s*(?:시간|hours?|h\b)", quote, re.I
            ):
                return decimal_string(Decimal(text) * 60)
    if unit == "category" and re.sub(r"\s+", "", text) in ("월", "월통상임금"):
        # A month number/date or an unselected pay-type option is not a monthly wage.
        if re.search(r"월\s*(?:통상임금|기본급|급여|임금)?\s*[:：]?\s*(?:월\s*)?\d[\d,.]*\s*원", quote):
            return "monthly"
    return normalize_scalar(raw, unit)
