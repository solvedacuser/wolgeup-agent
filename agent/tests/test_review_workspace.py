"""Acceptance tests for source-preserving review and explicit confirmation."""

import asyncio
import io
import zipfile

import pytest
from PIL import Image
from reportlab.pdfgen.canvas import Canvas

from agent.config import AgentSettings
from agent.dev.local_workflow import LocalStore
from agent.review.reading import locate, png_bytes, probe, read_document
from agent.review.store import RevisionConflict

DATA = {
    "contract": {
        "employee_name": ("Alex", "text"),
        "employer_name": ("Cafe ABC", "text"),
        "pay_type": ("monthly", "category"),
        "contract_rate": ("2300000", "KRW/month"),
        "contract_start": ("2026-01-01", "date"),
    },
    "payslip": {
        "employee_name": ("Alex", "text"),
        "employer_name": ("Cafe ABC", "text"),
        "period_start": ("2026-10-01", "date"),
        "period_end": ("2026-10-31", "date"),
        "gross": ("2300000", "KRW"),
        "deduction": ("300000", "KRW"),
        "net": ("2000000", "KRW"),
        "earning_1_label": ("Base salary", "text"),
        "earning_1_amount": ("2300000", "KRW"),
    },
    "deposit": {
        "employee_name": ("Alex", "text"),
        "employer_name": ("Cafe ABC", "text"),
        "account_holder": ("Alex", "text"),
        "transaction_1_date": ("2026-11-10", "date"),
        "transaction_1_description": ("Salary", "text"),
        "transaction_1_deposit_amount": ("2000000", "KRW"),
    },
}


def sample_pdf(kinds=("payslip",), *, rotate=0):
    buffer = io.BytesIO()
    canvas = Canvas(buffer, invariant=1)
    for kind in kinds:
        y = 780
        canvas.drawString(50, y, "TYPE " + kind)
        for key, (value, _) in DATA[kind].items():
            y -= 25
            canvas.drawString(50, y, key + ": " + value)
        canvas.showPage()
    canvas.save()
    if rotate:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(io.BytesIO(buffer.getvalue()))
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page.rotate(rotate))
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()
    return buffer.getvalue()


class ReviewFixtureModel:
    def __init__(self):
        self.calls = []
        self.pause = None

    async def invoke(self, operation, data):
        self.calls.append(operation)
        if self.pause:
            await self.pause.wait()
        if operation == "classify":
            parts = []
            for page in data["pages"]:
                kind = next((kind for kind in DATA if "TYPE " + kind in page["text"]), "payslip")
                parts.append(
                    dict(
                        document_type=kind,
                        start_page=page["page"],
                        end_page=page["page"],
                        label=kind,
                        needs_review=False,
                        reason="",
                    )
                )
            return {"parts": parts}, {"input_tokens": 6, "output_tokens": 4, "total_tokens": 10}
        kind = data["document_type"]
        page = data.get("pages", data.get("images", [{"page": 1}]))[0]["page"]
        targets = data.get("target_field_ids")
        fields = []
        for key, (value, unit) in DATA[kind].items():
            if targets is not None and key not in targets:
                continue
            fields.append(
                dict(
                    field_id=key,
                    raw_value=value,
                    normalized_value=value,
                    unit=unit,
                    page=page,
                    quote=key + ": " + value,
                    issues=[],
                )
            )
        return {
            "document_type": kind,
            "fields": fields,
            "tables": [],
            "additional_fields": [],
            "issues": [],
        }, {"input_tokens": 6, "output_tokens": 4, "total_tokens": 10}


async def seeded(tmp_path, kinds=("payslip",)):
    legacy = LocalStore(AgentSettings(), state=tmp_path)
    reviews = legacy.reviews
    reviews.model = ReviewFixtureModel()
    review = await reviews.create()
    uploaded = await reviews.upload(review["id"], sample_pdf(kinds), "sample.pdf")
    jid = uploaded["review"]["documents"][0]["job_id"]
    await reviews.tasks[jid]
    return legacy, reviews, reviews.view(review["id"])


def fields(review):
    return [f for d in review["documents"] for p in d["parts"] for f in p["fields"]]


async def test_multidocument_extraction_missing_fields_and_exact_source(tmp_path):
    _, reviews, review = await seeded(tmp_path, ("contract", "payslip"))
    document = review["documents"][0]
    assert document["job"]["status"] == "completed"
    assert [p["document_type"] for p in document["parts"]] == ["contract", "payslip"]
    assert review["basic"]["period_start"]["value"] == "2026-10-01"
    assert review["basic"]["period_start"]["confirmed"] is False
    assert all(f["review_state"] == "unreviewed" for f in fields(review))
    rate = next(f for f in fields(review) if f["field_id"] == "contract_rate")
    assert rate["value"] == "2300000"
    assert rate["evidence"]["boxes"]
    assert rate["evidence"]["page"] == 1
    assert (
        next(f for f in fields(review) if f["field_id"] == "employer_phone")["validation_status"] == "missing"
    )
    assert "verify" in reviews.model.calls
    assert review["capabilities"][0]["status"] != "ready"


async def test_edit_defer_confirm_reextract_and_duplicate_preserve_history(tmp_path):
    _, reviews, review = await seeded(tmp_path)
    rid = review["id"]
    net = next(f for f in fields(review) if f["field_id"] == "net")
    review = await reviews.edit_field(
        rid, dict(revision=review["revision"], field_id=net["id"], value="1900000", unit="KRW")
    )
    review = await reviews.confirm(rid, dict(revision=review["revision"], field_ids=[net["id"]]))
    assert next(f for f in fields(review) if f["id"] == net["id"])["review_state"] == "confirmed"
    with pytest.raises(RevisionConflict):
        await reviews.edit_field(rid, dict(revision=1, field_id=net["id"], value="1", unit="KRW"))
    review = await reviews.document_action(
        rid, review["documents"][0]["id"], dict(revision=review["revision"], action="retry")
    )
    await reviews.tasks[review["documents"][0]["job_id"]]
    review = reviews.view(rid)
    net = next(f for f in fields(review) if f["id"] == net["id"])
    assert net["value"] == "1900000"
    assert net["review_state"] == "unreviewed"
    assert "CONFLICT" in net["issues"]
    assert net["history"]
    review = await reviews.edit_field(
        rid, dict(revision=review["revision"], field_id=net["id"], value="", unit="KRW", defer=True)
    )
    assert next(f for f in fields(review) if f["id"] == net["id"])["value"] == ""
    before = len(reviews.jobs)
    duplicate = await reviews.upload(rid, sample_pdf(), "same.pdf")
    assert duplicate["duplicate"]
    assert len(reviews.jobs) == before
    restored = LocalStore(AgentSettings(), state=tmp_path).reviews.view(rid)
    assert next(f for f in fields(restored) if f["id"] == net["id"])["review_state"] == "deferred"


async def test_manual_input_and_rows_work_after_model_failure(tmp_path):
    legacy = LocalStore(AgentSettings(), state=tmp_path)
    reviews = legacy.reviews

    class Fails:
        async def invoke(self, *args):
            raise RuntimeError("fixture failure")

    reviews.model = Fails()
    review = await reviews.create()
    result = await reviews.upload(review["id"], sample_pdf(), "broken.pdf")
    await reviews.tasks[result["review"]["documents"][0]["job_id"]]
    review = reviews.view(review["id"])
    assert review["documents"][0]["job"]["status"] == "failed"
    review = await reviews.document_action(
        review["id"],
        review["documents"][0]["id"],
        dict(
            revision=review["revision"],
            action="manual",
            parts=[dict(document_type="payslip", start_page=1, end_page=1)],
        ),
    )
    part = review["documents"][0]["parts"][0]
    review = await reviews.add_fields(
        review["id"], dict(revision=review["revision"], part_id=part["id"], row_type="earning")
    )
    review = await reviews.add_fields(
        review["id"], dict(revision=review["revision"], part_id=part["id"], label="추가 근로조건")
    )
    assert any(f["field_id"] == "earning_1_amount" for f in fields(review))
    assert any(f["extra"] for f in fields(review))
    review = await reviews.document_action(
        review["id"],
        review["documents"][0]["id"],
        dict(
            revision=review["revision"],
            action="manual",
            parts=[dict(document_type="contract", start_page=1, end_page=1)],
        ),
    )
    rate = next(f for f in fields(review) if f["field_id"] == "contract_rate")
    review = await reviews.edit_field(
        review["id"],
        dict(revision=review["revision"], field_id=rate["id"], value="", unit="rate", defer=True),
    )
    assert next(f for f in fields(review) if f["id"] == rate["id"])["review_state"] == "deferred"


async def test_waiting_request_resumes_once_only_confirmed_values_reach_engine(tmp_path):
    legacy, reviews, review = await seeded(tmp_path)
    rid = review["id"]
    review = await reviews.request(rid, dict(revision=review["revision"], capability="compare"))
    assert review["requests"][0]["state"] == "waiting"
    waiting = review["execution"]["jobs"][-1]
    assert waiting["flow"]["nodes"]["wait"]["status"] == "waiting"
    assert "snapshot" not in waiting["flow"]["nodes"]
    ids = [f["id"] for f in fields(review) if f["value"] and f["field_id"] != "gross"]
    review = await reviews.confirm(rid, dict(revision=review["revision"], field_ids=ids))
    review = await reviews.update(
        rid, dict(revision=review["revision"], options={"subjects_confirmed": True}, confirm_basic=True)
    )
    request = review["requests"][0]
    await reviews.request_tasks[request["id"]]
    request = reviews.view(rid)["requests"][0]
    assert request["state"] == "completed", request
    execution = reviews.view(rid)["execution"]
    work = next(job for job in execution["jobs"] if job["id"] == request["job_id"])
    assert work["usage"]["total_tokens"] == 0
    assert "wait>ready" in work["flow"]["edges"]
    assert work["flow"]["nodes"]["wait"]["status"] == "completed"
    assert "snapshot>supervisor" in work["flow"]["edges"]
    for worker in ("worker_wage", "worker_terms", "worker_statement", "worker_payment"):
        assert work["flow"]["nodes"][worker]["status"] == "completed"
        assert f"supervisor>{worker}" in work["flow"]["edges"]
        assert f"{worker}>aggregate" in work["flow"]["edges"]
    comparison = legacy.comparisons[request["comparison_id"]]
    assert comparison["analysis"]
    assert comparison["draft"]["manual"].get("gross") is None
    count = len(legacy.comparisons)
    await reviews.wake(rid)
    assert len(legacy.comparisons) == count


async def test_nonoverlapping_mixed_pdf_parts_can_be_compared(tmp_path):
    legacy, reviews, review = await seeded(tmp_path, ("contract", "payslip"))
    rid = review["id"]
    ids = [f["id"] for f in fields(review) if f["value"]]
    review = await reviews.confirm(rid, dict(revision=review["revision"], field_ids=ids))
    review = await reviews.update(
        rid, dict(revision=review["revision"], confirm_basic=True, options={"subjects_confirmed": True})
    )
    review = await reviews.request(rid, dict(revision=review["revision"], capability="compare"))
    request = review["requests"][0]
    await reviews.request_tasks[request["id"]]
    request = reviews.view(rid)["requests"][0]
    assert request["state"] == "completed", request
    assert len(legacy.comparisons[request["comparison_id"]]["materials"]["sources"]) == 2


def test_image_probe_and_rotated_pdf_coordinates():
    data = sample_pdf(rotate=90)
    reading = read_document(data, "application/pdf", AgentSettings())
    assert reading["pages"][0]["text"]
    with Image.open(io.BytesIO(png_bytes(data, "application/pdf"))) as image:
        assert image.width > image.height
    buffer = io.BytesIO()
    Image.new("RGB", (400, 200), "white").save(buffer, "PNG")
    assert probe(buffer.getvalue(), AgentSettings())["media_type"] == "image/png"
    assert locate({"pages": []}, 1, "unknown", "unknown") == []
    with pytest.raises(Exception):
        probe(b"not a document", AgentSettings())


async def test_cancellation_and_restart_preserve_unfinished_job(tmp_path):
    legacy = LocalStore(AgentSettings(), state=tmp_path)
    model = ReviewFixtureModel()
    model.pause = asyncio.Event()
    legacy.reviews.model = model
    review = await legacy.reviews.create()
    result = await legacy.reviews.upload(review["id"], sample_pdf(), "sample.pdf")
    job = result["review"]["documents"][0]["job_id"]
    for _ in range(100):
        if model.calls:
            break
        await asyncio.sleep(0.01)
    restored = LocalStore(AgentSettings(), state=tmp_path)
    assert restored.reviews.jobs[job]["status"] == "interrupted"
    await legacy.reviews.control_job(job, "cancel")
    await legacy.reviews.tasks[job]
    assert legacy.reviews.jobs[job]["status"] == "cancelled"


async def test_basic_edit_while_processing_and_verified_candidates_do_not_overwrite(tmp_path):
    legacy = LocalStore(AgentSettings(), state=tmp_path)
    reviews = legacy.reviews
    model = ReviewFixtureModel()
    model.pause = asyncio.Event()
    reviews.model = model
    review = await reviews.create()
    uploaded = await reviews.upload(review["id"], sample_pdf(), "sample.pdf")
    review = await reviews.update(
        review["id"],
        dict(
            revision=uploaded["review"]["revision"],
            basic={"employer_name": "직접 입력한 근무지", "payment_date": ""},
        ),
    )
    model.pause.set()
    await reviews.tasks[uploaded["review"]["documents"][0]["job_id"]]
    review = reviews.view(review["id"])
    assert review["basic"]["employer_name"]["value"] == "직접 입력한 근무지"
    assert review["basic"]["payment_date"]["value"] == ""
    net = next(f for f in fields(review) if f["field_id"] == "net")
    review = await reviews.confirm(review["id"], dict(revision=review["revision"], field_ids=[net["id"]]))
    calls = len(model.calls)
    before = len(net["candidates"])
    review = await reviews.document_action(
        review["id"],
        review["documents"][0]["id"],
        dict(revision=review["revision"], action="verify", field_ids=[net["id"]]),
    )
    await reviews.tasks[review["documents"][0]["job_id"]]
    review = reviews.view(review["id"])
    current = next(f for f in fields(review) if f["id"] == net["id"])
    assert model.calls[calls:] == ["verify"]
    assert current["value"] == "2000000"
    assert current["review_state"] == "confirmed"
    assert len(current["candidates"]) == before + 1


async def test_image_corrections_compare_and_report_keep_original_png(tmp_path):
    legacy = LocalStore(AgentSettings(), state=tmp_path)
    reviews = legacy.reviews

    class Fails:
        async def invoke(self, *args):
            raise RuntimeError("manual-only fixture")

    reviews.model = Fails()
    review = await reviews.create()
    image = png_bytes(sample_pdf(), "application/pdf", max_side=1200)
    uploaded = await reviews.upload(review["id"], image, "명세서.png")
    await reviews.tasks[uploaded["review"]["documents"][0]["job_id"]]
    review = reviews.view(review["id"])
    review = await reviews.document_action(
        review["id"],
        review["documents"][0]["id"],
        dict(
            revision=review["revision"],
            action="manual",
            parts=[dict(document_type="payslip", start_page=1, end_page=1)],
        ),
    )
    rid = review["id"]
    for key, (value, unit) in DATA["payslip"].items():
        field = next((f for f in fields(review) if f["field_id"] == key), None)
        if field:
            review = await reviews.edit_field(
                rid, dict(revision=review["revision"], field_id=field["id"], value=value, unit=unit)
            )
    review = await reviews.confirm(
        rid, dict(revision=review["revision"], field_ids=[f["id"] for f in fields(review) if f["value"]])
    )
    review = await reviews.update(
        rid, dict(revision=review["revision"], confirm_basic=True, options={"subjects_confirmed": True})
    )
    review = await reviews.request(rid, dict(revision=review["revision"], capability="compare"))
    request = review["requests"][0]
    await reviews.request_tasks[request["id"]]
    request = reviews.view(rid)["requests"][0]
    assert request["state"] == "completed", request
    cid = request["comparison_id"]
    job = await legacy.create_job(cid, "report", include_original=True)
    await legacy.tasks[job["id"]]
    assert legacy.jobs[job["id"]]["status"] == "completed"
    report = legacy.comparisons[cid]["report"]
    zipped = next(a for a in report["artifacts"] if a["mime_type"] == "application/zip")
    content, _ = await legacy.artifact(zipped["artifact_id"])
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".png"))
        assert archive.read(name) == image
