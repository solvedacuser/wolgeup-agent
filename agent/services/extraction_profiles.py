"""Shared extraction vocabulary for prompts, validation, and the local review UI."""

import re

RATE_UNITS = ("KRW/hour", "KRW/month", "KRW/year", "KRW/day")


def field(label, unit):
    return {"label": label, "unit": unit}


PERIOD_FIELDS = {
    "period_start": field("대상기간 시작일", "date"),
    "period_end": field("대상기간 종료일", "date"),
}
PROFILES = {
    "contract": {
        "label": "근로계약서",
        "fields": {
            "pay_type": field("임금 형태", "category"),
            "contract_rate": field("임금 금액", "rate"),
            "contract_start": field("계약 시작일", "date"),
            "contract_end": field("계약 종료일", "date"),
            "work_start": field("출근 시각", "time"),
            "work_end": field("퇴근 시각", "time"),
            "break_minutes": field("휴게 시간 (분)", "minute"),
            "scheduled_daily_minutes": field("일 소정근로시간 (분)", "minute"),
            "scheduled_weekly_minutes": field("주 소정근로시간 (분)", "minute"),
            "payday": field("지급일", "day_of_month"),
        },
        "rows": {},
    },
    "attendance": {
        "label": "근무기록",
        "fields": {
            **PERIOD_FIELDS,
            "worked_minutes": field("기재된 총 근로시간 (분)", "minute"),
            "overtime_minutes": field("기재된 연장시간 (분)", "minute"),
            "night_minutes": field("기재된 야간시간 (분)", "minute"),
            "holiday_minutes": field("기재된 휴일시간 (분)", "minute"),
            "work_days": field("기재된 근로일수", "count"),
        },
        "rows": {
            "work": {
                "label": "날짜별 근무기록",
                "fields": {
                    "date": field("근무일 (원문)", "text"),
                    "start": field("출근", "time"),
                    "end": field("퇴근", "time"),
                    "break_minutes": field("휴게 (분)", "minute"),
                    "break_interval": field("휴게 구간 (원문)", "text"),
                    "worked_minutes": field("근로 (분)", "minute"),
                    "overtime_minutes": field("연장 (분)", "minute"),
                    "night_minutes": field("야간 (분)", "minute"),
                    "holiday_minutes": field("휴일 (분)", "minute"),
                    "note": field("비고", "text"),
                },
            }
        },
    },
    "payslip": {
        "label": "급여명세서",
        "fields": {
            **PERIOD_FIELDS,
            "payment_date": field("지급일", "date"),
            "slip_rate": field("명세서 기재 단가", "rate"),
            "slip_minutes": field("명세서 기재 시간 (분)", "minute"),
            "gross": field("총지급액 (원)", "KRW"),
            "deduction": field("공제 합계 (원)", "KRW"),
            "net": field("실수령액 (원)", "KRW"),
        },
        "rows": {
            name: {
                "label": label,
                "fields": {
                    "label": field("항목명", "text"),
                    "amount": field("금액 (원)", "KRW"),
                    "calculation": field("기재된 계산 방법", "text"),
                },
            }
            for name, label in (("earning", "지급 항목"), ("deduction", "공제 항목"))
        },
    },
    "deposit": {
        "label": "입금기록",
        "fields": {
            **PERIOD_FIELDS,
            "account_holder": field("예금주", "text"),
            "account_number": field("계좌 표시", "text"),
            "bank_name": field("은행", "text"),
            "deposit_total": field("기재된 입금 합계 (원)", "KRW"),
        },
        "rows": {
            "transaction": {
                "label": "거래 내역",
                "fields": {
                    "date": field("거래일", "date"),
                    "time": field("거래시각", "time"),
                    "description": field("거래 구분", "text"),
                    "sender": field("입금자 표시", "text"),
                    "deposit_amount": field("입금액 (원)", "KRW"),
                    "withdrawal_amount": field("출금액 (원)", "KRW"),
                    "balance": field("거래 후 잔액 (원)", "KRW"),
                    "reference": field("거래 식별자", "text"),
                },
            }
        },
    },
}

PROFILES["payslip_ocr"] = {
    **PROFILES["payslip"],
    "label": "급여명세서 · Python OCR",
}


def validate_document_type(document_type):
    if not isinstance(document_type, str) or document_type not in PROFILES:
        raise ValueError("지원하지 않는 문서 유형입니다.")
    return document_type


def field_definition(document_type, field_id):
    profile = PROFILES.get(document_type, {})
    definition = profile.get("fields", {}).get(field_id)
    if definition:
        return definition
    match = re.fullmatch(r"([a-z]+)_([1-9]\d{0,3})_([a-z_]+)", field_id)
    if match:
        return profile.get("rows", {}).get(match[1], {}).get("fields", {}).get(match[3])
    return None


def expected_unit(document_type, field_id):
    definition = field_definition(document_type, field_id)
    return definition["unit"] if definition and definition["unit"] != "rate" else None


def extraction_instructions(document_type):
    """Keep stable field IDs and preserve table rows in the existing scalar field contract."""
    import json

    profile = PROFILES[document_type]
    instructions = (
        " Extract this document using the following field vocabulary (unit 'rate' means one of "
        "KRW/hour, KRW/month, KRW/year, KRW/day, only when explicit). "
        + json.dumps(profile, ensure_ascii=False)
        + " Return every summary field once; use null raw_value/normalized_value/unit, page 1 and empty "
        "quote when absent. Put summary fields in fields. Put detail rows in tables, not fields. "
        "Each table has prefix (work/earning/deduction/transaction as appropriate), columns (the exact "
        "column keys from this vocabulary), header_quote (the exact printed column/unit heading, "
        "including '기록 단위: 시간(h)' when present), and rows. Each row has a 1-based page, one exact "
        "contiguous row quote, raw_values and normalized_values arrays aligned to columns, and issues. "
        "Use null cells for absent values. Do not repeat the header in each row quote. "
        "The header_quote must occur on the same page as its rows; start a new table for a new page. "
        "Copy the complete contiguous header, including any section title between the unit line and "
        "column names. In raw_values copy each cell exactly: if the cell is 8 under an hours heading, "
        "return raw 8 and normalized 480; never append '시간(h)' to the raw cell. Do not join text cells "
        "with punctuation that is absent from the source. "
        "If a duration's unit cannot be grounded in the row or that page's heading, retain its raw "
        "value with null normalized_value and an issue instead of assuming hours or minutes. "
        "Keep every row separate in source order, including repeated dates and equal amounts. "
        "Never encode rows in JSON strings, merge rows, or use subtotals/totals as detail rows. "
        "Use exact source text for text units. Include duration units in raw_value; normalize hours "
        "to minute. Use exact quotes containing each value. Do not sum rows to create an absent total. "
    )
    if document_type == "attendance":
        instructions += (
            " Preserve explicit work dates, start/end, breaks and any stated work/overtime/night/holiday "
            "durations per row. Do not calculate durations from clock times or classify overtime by law. "
            "An overnight end time may be earlier than its start; do not invent the next date. "
            "Work-row date is source text, so preserve '10/01' without inventing a year. "
            "A break interval such as 12:00-13:00 belongs in break_interval, not break_minutes; "
            "do not calculate a break duration. If a printed table heading explicitly gives hours, "
            "keep raw numeric cells unchanged and normalize those durations to minutes. "
            "Do not use scheduled contract hours as actual attendance. Missing cells remain null."
        )
    elif document_type == "payslip":
        instructions += (
            " Separate earnings and deductions and extract their individual names, amounts and printed "
            "calculation text. Keep gross, total deductions and net separate from detail rows. "
            "Do not infer rates from amounts or missing deductions from tax rates."
        )
    elif document_type == "deposit":
        instructions += (
            " Separate deposits, withdrawals and balances on each transaction row. A balance is never "
            "a deposit amount. Preserve a printed transaction reference if present. Do not decide "
            "whether a transfer is salary or link transactions to payslips. Do not infer missing zeros."
        )
    return instructions
