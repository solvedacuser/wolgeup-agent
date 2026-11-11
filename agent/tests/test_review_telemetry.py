"""Token accounting must follow provider usage, including retries and failures."""

import copy
from types import SimpleNamespace

import pytest

from agent.config import AgentSettings
from agent.dev.local_workflow import LocalStore
from agent.review.model import ReviewModel
from agent.review.telemetry import execution_summary
from agent.schemas.public.errors import ModelFormatError
from agent.tests.test_review_workspace import fields, seeded


async def test_stage_usage_and_chronological_tools_sum_once_across_retries(tmp_path):
    legacy, reviews, review = await seeded(tmp_path)
    execution = review["execution"]
    assert execution["usage"]["total_tokens"] == 30
    stages = {s["id"]: s for s in execution["stages"]}
    assert {key: stages[key]["usage"]["total_tokens"] for key in ("classify", "extract", "verify")} == {
        "classify": 10,
        "extract": 10,
        "verify": 10,
    }
    assert not stages["reader"]["usage"]
    assert execution["model_calls"] == 3
    flow = execution["jobs"][0]["flow"]
    assert "format>quality" in flow["edges"]
    assert "quality>native" in flow["edges"]
    assert "native>classify" in flow["edges"]
    assert "verify>validate_again" in flow["edges"]
    assert "ocr" not in flow["nodes"]
    assert "supervisor" not in flow["nodes"]
    assert flow["nodes"]["classify"]["usage"]["total_tokens"] == 10
    assert flow["nodes"]["review"]["status"] == "waiting"
    trace = execution["jobs"][0]["trace"]
    names = [event["name"] for event in trace]
    assert names.index("document.read") < names.index("pdf.read_native") < names.index("model.classify")
    assert names.index("page.render") < names.index("model.verify")
    assert names.count("fields.validate") == 2
    assert all(event.get("duration_ms") is not None for event in trace)
    assert any(event.get("parent_id") for event in trace if event["name"] == "pdf.read_native")
    for event in trace:
        assert not {"data", "settings", "images", "raw_value", "quote"} & set(event.get("context", {}))
    net = next(f for f in fields(review) if f["field_id"] == "net")
    review = await reviews.document_action(
        review["id"],
        review["documents"][0]["id"],
        dict(revision=review["revision"], action="verify", field_ids=[net["id"]]),
    )
    await reviews.tasks[review["documents"][0]["job_id"]]
    review = reviews.view(review["id"])
    assert review["execution"]["usage"]["total_tokens"] == 40
    assert review["execution"]["model_calls"] == 4
    recheck = review["execution"]["jobs"][-1]
    assert recheck["usage"]["total_tokens"] == 10
    assert "classify" not in recheck["flow"]["nodes"]
    assert "native" not in recheck["flow"]["nodes"]
    assert "cache>recheck" in recheck["flow"]["edges"]
    review = await reviews.document_action(
        review["id"], review["documents"][0]["id"], dict(revision=review["revision"], action="exclude")
    )
    assert review["execution"]["usage"]["total_tokens"] == 40
    assert all(job["excluded"] for job in review["execution"]["jobs"])
    restored = LocalStore(legacy.settings, state=tmp_path).reviews.view(review["id"])
    assert restored["execution"]["usage"]["total_tokens"] == 40


def test_historical_totals_remain_unattributed_without_inventing_call_order():
    review = {
        "id": "review-old",
        "documents": [{"id": "doc", "title": "old.pdf", "job_id": "old-job"}],
        "requests": [],
    }
    job = {
        "id": "old-job",
        "review_id": "review-old",
        "document_id": "doc",
        "status": "completed",
        "created_at": "2026-11-09T00:00:00Z",
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        "agent_calls": [
            {"agent": "classify", "status": "completed"},
            {"agent": "extract", "status": "completed"},
        ],
        "tool_calls": [{"tool": "document.read", "role": "reader", "status": "completed", "duration_ms": 20}],
    }
    original = copy.deepcopy(job)
    execution = execution_summary(review, {job["id"]: job}, {})
    assert execution["usage"]["total_tokens"] == 120
    assert execution["historical_jobs"] == 1
    stages = {s["id"]: s for s in execution["stages"]}
    assert stages["unattributed"]["usage"]["total_tokens"] == 120
    assert not stages["classify"]["usage"]
    assert stages["classify"]["not_recorded_calls"] == 1
    assert execution["jobs"][0]["trace_order"] == "not_recorded"
    assert all(event["started_at"] is None for event in execution["jobs"][0]["trace"])
    assert job == original
    assert execution["jobs"][0]["flow"]["edges"] == []
    assert execution["jobs"][0]["flow"]["nodes"]["classify"]["status"] == "completed"
    assert execution["jobs"][0]["flow"]["nodes"]["classify"]["usage"] == {}


async def test_failed_response_usage_is_counted_and_missing_usage_is_unknown(tmp_path):
    legacy, reviews, review = await seeded(tmp_path)

    class Broken:
        async def invoke(self, operation, data):
            error = ModelFormatError("fixture malformed response")
            error.usage = {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}
            raise error

    reviews.model = Broken()
    review = await reviews.document_action(
        review["id"], review["documents"][0]["id"], dict(revision=review["revision"], action="retry")
    )
    await reviews.tasks[review["documents"][0]["job_id"]]
    review = reviews.view(review["id"])
    assert review["execution"]["usage"]["total_tokens"] == 80
    assert review["execution"]["unavailable_calls"] == 0

    class Unknown:
        async def invoke(self, *args):
            raise TimeoutError("fixture response unavailable")

    reviews.model = Unknown()
    review = await reviews.document_action(
        review["id"], review["documents"][0]["id"], dict(revision=review["revision"], action="retry")
    )
    await reviews.tasks[review["documents"][0]["job_id"]]
    review = reviews.view(review["id"])
    assert review["execution"]["usage"]["total_tokens"] == 80
    assert review["execution"]["unavailable_calls"] == 1
    failed = review["execution"]["jobs"][-1]["flow"]
    assert failed["nodes"]["classify"]["status"] == "failed"
    assert "extract" not in failed["nodes"]


async def test_image_route_highlights_ocr_but_not_native_pdf(tmp_path, monkeypatch):
    import io

    from PIL import Image

    from agent.tests.test_review_workspace import ReviewFixtureModel

    monkeypatch.setattr(
        "agent.review.reading._ocr_blocks",
        lambda *args: [{"text": "TYPE payslip", "bbox": [0.1, 0.1, 0.6, 0.2], "confidence": 99}],
    )
    buffer = io.BytesIO()
    Image.new("RGB", (300, 300), "white").save(buffer, "PNG")
    reviews = LocalStore(AgentSettings(), state=tmp_path).reviews
    reviews.model = ReviewFixtureModel()
    review = await reviews.create()
    result = await reviews.upload(review["id"], buffer.getvalue(), "fixture.png")
    await reviews.tasks[result["review"]["documents"][0]["job_id"]]
    flow = reviews.view(review["id"])["execution"]["jobs"][0]["flow"]
    assert {"format>image", "image>ocr", "ocr>classify"} <= set(flow["edges"])
    assert not {"native", "quality", "render_ocr"} & flow["nodes"].keys()
    monkeypatch.setattr("agent.review.reading._ocr_blocks", lambda *args: [])
    current = reviews.view(review["id"])
    current = await reviews.document_action(
        current["id"], current["documents"][0]["id"], dict(revision=current["revision"], action="retry")
    )
    await reviews.tasks[current["documents"][0]["job_id"]]
    empty = reviews.view(review["id"])["execution"]["jobs"][-1]
    assert empty["usage"]["total_tokens"] == 0
    assert "classify" not in empty["flow"]["nodes"]
    assert "classify>type_gate" not in empty["flow"]["edges"]
    assert "ocr>type_wait" in empty["flow"]["edges"]


async def test_provider_keeps_usage_even_when_structured_response_is_invalid(monkeypatch):
    class Chat:
        def __init__(self, **kwargs):
            pass

        def with_structured_output(self, *args, **kwargs):
            return self

        async def ainvoke(self, *args):
            return {
                "raw": SimpleNamespace(
                    usage_metadata={
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "total_tokens": 120,
                        "input_token_details": {"cache_read": 30},
                        "output_token_details": {"reasoning": 5},
                    }
                ),
                "parsed": None,
                "parsing_error": ValueError("bad schema"),
            }

    monkeypatch.setattr("langchain_openai.ChatOpenAI", Chat)
    model = ReviewModel(AgentSettings(openai_api_key="fixture-only"))
    with pytest.raises(ModelFormatError) as caught:
        await model.invoke("classify", {"pages": []})
    assert caught.value.usage == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "cached_input_tokens": 30,
        "reasoning_tokens": 5,
    }
