"""Conversation boundaries, grounded evidence, persistence and real UI acceptance."""

import asyncio
import copy
import json
import re
import threading

import pytest

from agent.config import AgentSettings
from agent.dev.local_workflow import LocalStore
from agent.dev.test_server import LocalHTTPServer
from agent.review.chat import evidence_context
from agent.review.model import ReviewModel
from agent.schemas.public.errors import ModelFormatError
from agent.tests.test_review_workspace import seeded


class ChatFixture:
    def __init__(self):
        self.calls = []
        self.pause = None

    async def invoke(self, operation, data):
        assert operation == "chat"
        self.calls.append(copy.deepcopy(data))
        if self.pause:
            await self.pause.wait()
        source = next((s for s in data["context"]["sources"] if s.get("field_id")), None)
        return {
            "answer": "자료의 **원문**과 추출값을 확인하고 필요한 항목을 확정해 주세요.",
            "source_ids": ["workflow", source["id"] if source else "requirements", "other-project-field"],
        }, {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}


async def ask(reviews, rid, text="어떤 파일을 올리고 확인해야 해?", request_id="test-message-001"):
    result = await reviews.chat.send(rid, {"message": text, "request_id": request_id})
    await reviews.chat.tasks[result["active_job"]]
    return reviews.chat.view(rid)


async def test_scoped_evidence_read_only_history_sources_and_usage(tmp_path):
    legacy, reviews, review = await seeded(tmp_path)
    other = await reviews.create()
    reviews.records[other["id"]]["title"] = "OTHER_PROJECT_SECRET"
    model = ChatFixture()
    reviews.model = model
    before = copy.deepcopy(reviews.records[review["id"]])
    answer = await ask(reviews, review["id"])
    assert len(answer["messages"]) == 2
    assert not answer["active_job"]
    assert answer["messages"][-1]["mode"] == "model"
    assert len(answer["messages"][-1]["sources"]) == 2
    assert "OTHER_PROJECT_SECRET" not in json.dumps(model.calls, ensure_ascii=False)
    assert all(s["id"] != "other-project-field" for s in answer["messages"][-1]["sources"])
    assert reviews.records[review["id"]] == before
    await reviews.chat.send(review["id"], {"message": "retry", "request_id": "test-message-001"})
    assert len(model.calls) == 1  # Network retries are idempotent.
    await ask(reviews, review["id"], "그 항목을 어디서 확인해?", "test-message-002")
    assert len(model.calls[1]["history"]) == 2
    assert reviews.chat.view(other["id"])["messages"] == []
    execution = reviews.view(review["id"])["execution"]
    assert execution["usage"]["total_tokens"] == 90  # 30 extraction + 2 * 30 chat.
    chats = [j for j in execution["jobs"] if j["kind"] == "chat"]
    assert len(chats) == 2
    assert "chat_context>chat_answer" in chats[-1]["flow"]["edges"]
    assert chats[-1]["flow"]["nodes"]["chat_answer"]["usage"]["total_tokens"] == 30
    restored = LocalStore(AgentSettings(), state=tmp_path)
    assert len(restored.reviews.chat.view(review["id"])["messages"]) == 4


async def test_model_failure_records_usage_and_provides_real_local_guidance(tmp_path):
    _, reviews, review = await seeded(tmp_path)

    class Failure:
        async def invoke(self, *args):
            error = ModelFormatError("SENSITIVE_PROVIDER_BODY")
            error.usage = {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}
            raise error

    reviews.model = Failure()
    answer = await ask(reviews, review["id"], "비교 대조 결과 차이를 알려줘")
    message = answer["messages"][-1]
    assert message["mode"] == "guidance"
    assert "아직 완료된 대조 결과가 없습니다" in message["content"]
    assert "SENSITIVE_PROVIDER_BODY" not in json.dumps(answer)
    job = reviews.view(review["id"])["execution"]["jobs"][-1]
    assert job["usage"]["total_tokens"] == 9
    assert job["flow"]["nodes"]["chat_answer"]["status"] == "failed"
    assert "chat_answer>chat_fallback" in job["flow"]["edges"]


async def test_inflight_scope_revision_and_restart_recovery(tmp_path):
    _, reviews, review = await seeded(tmp_path)
    model = ChatFixture()
    model.pause = asyncio.Event()
    reviews.model = model
    result = await reviews.chat.send(review["id"], {"message": "현재 상태", "request_id": "test-message-003"})
    await asyncio.sleep(0.02)
    with pytest.raises(ValueError, match="작성하고"):
        await reviews.chat.send(review["id"], {"message": "또 질문", "request_id": "test-message-004"})
    await reviews.update(
        review["id"], {"revision": review["revision"], "basic": {"payment_date": "2026-11-15"}}
    )
    model.pause.set()
    await reviews.chat.tasks[result["active_job"]]
    assert reviews.chat.view(review["id"])["messages"][-1]["stale"] is True
    # A persisted in-flight conversation can be retried after process interruption.
    job = reviews.jobs[result["active_job"]]
    job["status"] = "running"
    reviews.save_job(job)
    restored = LocalStore(AgentSettings(), state=tmp_path)
    chat = restored.reviews.chat.view(review["id"])
    assert not chat["active_job"]
    assert chat["last_job"]["status"] == "interrupted"


async def test_context_keeps_comparison_direction_staleness_and_excluded_documents(tmp_path):
    legacy, reviews, review = await seeded(tmp_path)
    record = reviews.records[review["id"]]
    record["requests"].append(
        dict(
            id="request-abc",
            state="completed",
            comparison_id="compare-abc",
            stale=True,
            capability="compare",
            created_at="2026-11-11T00:00:00+00:00",
        )
    )
    legacy.cases["compare-abc"] = {
        "analysis": {
            "comparisons": [
                dict(
                    rule_id="R-06",
                    status="difference",
                    lhs="2000000",
                    rhs="1900000",
                    delta="100000",
                    unit="KRW",
                    reason_codes=[],
                    scope_complete=True,
                )
            ],
            "questions": [],
        }
    }
    # A later 'prepare' request has no result and must not hide the completed comparison.
    record["requests"].append(dict(id="request-def", state="completed", comparison_id="compare-def"))
    legacy.cases["compare-def"] = {"analysis": None}
    record["documents"][0]["excluded"] = True
    context = evidence_context(reviews, record, "입금 차이")
    comparison = next(s for s in context["sources"] if s["id"] == "comparison-0")
    assert comparison["data"]["lhs"] == "2000000"
    assert comparison["data"]["rhs"] == "1900000"
    assert comparison["data"]["delta"] == "100000"
    assert comparison["data"]["stale"] is True
    assert comparison["href"] == "/results?comparison=compare-abc"
    assert context["totals"]["documents"] == 0
    assert not any(s.get("field_id") for s in context["sources"])


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"message": "", "request_id": "a12345678"},
        {"message": "x" * 4001, "request_id": "a12345678"},
        {"message": "hello", "request_id": "../outside"},
        {"message": "hello", "request_id": "a12345678", "review_id": "other"},
    ],
)
async def test_reject_invalid_chat_without_changing_state(tmp_path, payload):
    store = LocalStore(AgentSettings(), state=tmp_path)
    review = await store.reviews.create()
    with pytest.raises(ValueError):
        await store.reviews.chat.send(review["id"], payload)
    assert not store.reviews.chat.view(review["id"])["messages"]


async def test_real_provider_chat_contract_uses_explanation_model(monkeypatch):
    captured = {}

    class Provider:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def with_structured_output(self, schema, **kwargs):
            assert "source_ids" in schema["properties"]
            return self

        async def ainvoke(self, messages):
            captured["system"] = messages[0].content
            return {
                "raw": type("Raw", (), {"usage_metadata": {"input_tokens": 2, "output_tokens": 1}})(),
                "parsed": {"answer": "현재 자료를 확인해 주세요.", "source_ids": ["workflow"]},
            }

    monkeypatch.setattr("langchain_openai.ChatOpenAI", Provider)
    response, usage = await ReviewModel(
        AgentSettings(
            openai_api_key="fixture-only",
            explanation_model="chat-fixture",
            extraction_model="extract-fixture",
        )
    ).invoke("chat", {"question": "어떤 자료가 필요해?"})
    assert captured["model"] == "chat-fixture"
    assert captured["store"] is False
    assert "CURRENT payroll review project" in captured["system"]
    assert response["source_ids"] == ["workflow"]
    assert usage["total_tokens"] == 3


async def test_browser_floating_chat_scope_draft_history_and_graph(tmp_path):
    playwright = pytest.importorskip("playwright.async_api")
    store, reviews, review = await seeded(tmp_path / "state")
    model = ChatFixture()
    reviews.model = model
    other = await reviews.create()
    server = LocalHTTPServer(("127.0.0.1", 0), store, asyncio.get_running_loop())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    errors = []
    try:
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(base + "/review/" + review["id"] + "/materials")
            launcher, panel = page.locator("#chat-launcher"), page.locator("#chat-panel")
            await playwright.expect(launcher).to_be_visible()
            before = await launcher.bounding_box()
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            after = await launcher.bounding_box()
            assert abs(before["y"] - after["y"]) < 2
            await launcher.click()
            await playwright.expect(panel).to_be_visible()
            input_box = page.locator("#chat-input")
            await input_box.fill("작성 중인 질문")
            await launcher.click()
            await playwright.expect(panel).to_be_hidden()
            await launcher.click()
            await playwright.expect(input_box).to_have_value("작성 중인 질문")
            await input_box.fill("인식 안 된 항목을 어떻게 확인해?")
            await input_box.press("Enter")
            await playwright.expect(page.locator(".chat-message.assistant")).to_have_count(1, timeout=15000)
            await playwright.expect(page.locator(".chat-message.assistant strong")).to_have_text("원문")
            await playwright.expect(page.locator(".chat-message.assistant .chat-source")).to_have_count(2)
            await page.reload()
            await page.locator("#chat-launcher").click()
            await playwright.expect(page.locator(".chat-message.assistant")).to_have_count(1)
            source_link = page.locator(".chat-message.assistant .chat-source").last
            href = await source_link.get_attribute("href")
            field_id = href.split("field=")[1]
            await source_link.click()
            await playwright.expect(page.locator(f'.field-card[data-field-ids~="{field_id}"]')).to_have_class(
                re.compile(r".*selected.*")
            )
            await page.get_by_role("button", name="채팅창 닫기", exact=True).click()
            await page.locator("#execution-panel").scroll_into_view_if_needed()
            await playwright.expect(page.locator('[data-flow-node="chat_answer"]')).to_have_attribute(
                "data-flow-state", "completed"
            )
            await playwright.expect(page.locator('[data-flow-node="classify"]')).to_have_attribute(
                "data-flow-state", "completed"
            )
            await page.goto(base + "/review")
            await page.locator("#chat-launcher").click()
            await page.locator("#chat-review-select").select_option(other["id"])
            await playwright.expect(page.locator(".chat-message")).to_have_count(0)
            await page.locator("#chat-review-select").select_option(review["id"])
            await playwright.expect(page.locator(".chat-message.assistant")).to_have_count(1)
            await page.set_viewport_size({"width": 390, "height": 844})
            box = await page.locator("#chat-panel").bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= 390
            assert box["y"] >= 0 and box["y"] + box["height"] <= 844
            await page.screenshot(path=str(tmp_path / "chat-mobile.png"))
            await page.set_viewport_size({"width": 1440, "height": 1000})
            await page.screenshot(path=str(tmp_path / "chat-desktop.png"))
            await browser.close()
        assert not errors
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)
