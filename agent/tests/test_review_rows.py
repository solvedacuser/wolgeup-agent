"""Row grouping: a printed table row may be reviewed in one card, never confirmed by it."""

from agent.review.pipeline import build_fields
from agent.review.rows import label_amount_adjacent, link_rows
from agent.tests.test_review_workspace import seeded

TABLE_TEXT = (
    "1. 지급 및 공제 내역 (단위: 원) 지급 항목 금액 공제 항목 금액 "
    "월 기본급 3,000,000 국민연금 130,000 상여금 0 소득세 10,000"
)


def cell(field_id, raw, quote, *, page=1, issues=(), row_quote=None):
    evidence = {"page": page, "quote": quote}
    if row_quote:
        evidence["row_quote"] = row_quote
    return {
        "id": "field-" + field_id,
        "field_id": field_id,
        "raw_value": raw,
        "value": raw or "",
        "issues": list(issues),
        "evidence": evidence if raw else None,
    }


def test_label_amount_adjacency_requires_the_amount_right_after_the_label():
    assert label_amount_adjacent("상여금", "0", TABLE_TEXT)
    assert label_amount_adjacent("월 기본급", "3,000,000", TABLE_TEXT)
    assert label_amount_adjacent("월기본급", "3,000,000", "월 기 본 급  3,000,000")
    # A different row's amount, or a longer number starting with the same digits.
    assert not label_amount_adjacent("상여금", "130,000", TABLE_TEXT)
    assert not label_amount_adjacent("상여금", "1", "상여금 10,000")
    # Column layouts print every label first, so pairing cannot be read from the text.
    assert not label_amount_adjacent("상여금", "100,000", "상여금 식대 0 100,000")


def test_link_rows_groups_only_rows_proven_by_the_source_text():
    part = {
        "fields": [
            cell("gross", "3,000,000", TABLE_TEXT),
            cell("earning_1_label", "상여금", TABLE_TEXT),
            cell("earning_1_amount", "0", TABLE_TEXT),
            cell("earning_1_calculation", None, ""),
            cell("earning_2_label", "식대", "상여금 식대 0 100,000"),
            cell("earning_2_amount", "100,000", "상여금 식대 0 100,000"),
            cell("earning_3_label", "연장근로수당", "연장근로수당 172,249"),
            cell("earning_3_amount", None, ""),
            cell("earning_4_label", "야간수당", "야간수당 14,354", page=1),
            cell("earning_4_amount", "14,354", "야간수당 14,354", page=2),
            cell(
                "deduction_1_label",
                "소득세",
                "x",
                issues=["SOURCE_QUOTE_MISMATCH"],
                row_quote="소득세 10,000",
            ),
            cell("deduction_1_amount", "10,000", "x", row_quote="소득세 10,000"),
        ]
    }
    part["fields"].append({**cell("deduction_2_label", None, ""), "issues": ["MANUAL"]})
    part["fields"].append({**cell("deduction_2_amount", None, ""), "issues": ["MANUAL"]})
    rows = {row["id"]: row for row in link_rows(part)}
    assert list(rows) == [
        "earning_1",
        "earning_2",
        "earning_3",
        "earning_4",
        "deduction_1",
        "deduction_2",
    ]
    assert rows["earning_1"]["link"] == "linked"
    assert rows["earning_1"]["primary"] == ["label", "amount"]
    assert rows["earning_1"]["cells"] == {
        "label": "field-earning_1_label",
        "amount": "field-earning_1_amount",
        "calculation": "field-earning_1_calculation",
    }
    assert rows["earning_2"]["link"] == "unlinked"
    assert rows["earning_3"]["link"] == "partial"
    assert rows["earning_4"]["link"] == "unlinked"
    assert rows["deduction_1"]["link"] == "unlinked"
    assert rows["deduction_2"]["link"] == "manual"


def test_row_quote_is_used_for_pairing_and_value_mismatch_cells_keep_row_membership():
    expanded = "header 연장근로수당 172,249 상여금 0"
    linked = link_rows(
        {
            "fields": [
                cell("earning_1_label", "연장근로수당", expanded, row_quote="연장근로수당 172,249"),
                cell("earning_1_amount", "172,249", expanded, row_quote="연장근로수당 172,249"),
            ]
        }
    )
    assert linked[0]["link"] == "linked"
    quote = "04/01 수 정상근무 09:00 18:00 8 0"
    work = link_rows(
        {
            "fields": [
                cell("work_1_date", "04/01", quote),
                cell("work_1_start", "09:00", quote),
                cell("work_1_worked_minutes", "8시간", quote, issues=["SOURCE_VALUE_MISMATCH"]),
                cell("work_1_overtime_minutes", None, ""),
            ]
        }
    )
    assert work[0]["link"] == "linked"
    other = link_rows(
        {
            "fields": [
                cell("work_1_date", "04/01", quote),
                cell("work_1_start", "10:00", quote),
            ]
        }
    )
    assert other[0]["link"] == "unlinked"


def test_build_fields_keeps_the_model_row_quote_for_table_rows():
    text = "지급 항목 금액\n월 기본급 3,000,000\n상여금 0"
    reading = {"pages": [{"page": 1, "text": text, "method": "pdf_text", "blocks": []}]}
    response = {
        "document_type": "payslip",
        "fields": [],
        "issues": [],
        "additional_fields": [],
        "tables": [
            {
                "prefix": "earning",
                "columns": ["label", "amount"],
                "header_quote": "지급 항목 금액",
                "rows": [
                    {
                        "page": 1,
                        "quote": "상여금 0",
                        "raw_values": ["상여금", "0"],
                        "normalized_values": ["상여금", "0"],
                        "issues": [],
                    }
                ],
            }
        ],
    }
    part = {"document_type": "payslip", "start_page": 1, "end_page": 1}
    fields = {f["field_id"]: f for f in build_fields(response, part, reading)}
    label, amount = fields["earning_1_label"], fields["earning_1_amount"]
    assert label["evidence"]["row_quote"] == "상여금 0"
    assert "월 기본급" in label["evidence"]["quote"]
    rows = link_rows({"fields": list(fields.values())})
    assert rows[0]["link"] == "linked"
    assert label["review_state"] == amount["review_state"] == "unreviewed"


async def test_view_exposes_rows_and_hides_request_snapshots(tmp_path):
    _, reviews, view = await seeded(tmp_path)
    part = view["documents"][0]["parts"][0]
    assert [row["id"] for row in part["rows"]] == ["earning_1"]
    # The fixture quotes each cell on its own line, so the pair is not proven.
    assert part["rows"][0]["link"] == "unlinked"
    rid = view["id"]
    reviews.records[rid]["requests"].append(
        {
            "id": "request-x",
            "capability": "compare",
            "state": "completed",
            "created_at": "2026-11-10T00:00:00+00:00",
            "snapshot": {"documents": reviews.records[rid]["documents"]},
        }
    )
    request = reviews.view(rid)["requests"][-1]
    assert "snapshot" not in request
    assert request["confirmed_field_count"] == 0
    assert "snapshot" in reviews.records[rid]["requests"][-1]
