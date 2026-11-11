"""Source-preserving recommendations, real conflict acknowledgement and UI acceptance."""

import asyncio
import copy
import threading

import pytest

from agent.config import AgentSettings
from agent.dev.local_workflow import LocalStore
from agent.dev.test_server import LocalHTTPServer
from agent.review.chat import evidence_context, validated_steps
from agent.review.pipeline import field_seed, identifier
from agent.review.recommendations import assess, input_hash
from agent.review.work import bridge
from agent.services.identity import identity_key
from agent.tests.test_review_workspace import fields, seeded


def replace_source(field, raw, value=None, *, method="text_match", append=False):
    candidate = dict(
        id=identifier("candidate"),
        raw_value=raw,
        value=value if value is not None else raw,
        unit=field["unit"],
        validation_status="valid",
        issues=[],
        evidence=dict(
            page=1, quote=field["label"] + ": " + raw, method=method, boxes=[], location_status="unavailable"
        ),
    )
    if append:
        field["candidates"].append(candidate)
    else:
        field.update(
            raw_value=raw,
            value=value if value is not None else raw,
            candidates=[candidate],
            evidence=copy.deepcopy(candidate["evidence"]),
            validation_status="valid",
            issues=[],
        )
    return candidate


def find(record, key):
    return next(f for f in fields(record) if f["field_id"] == key)


async def analyze(reviews, record):
    await reviews.recommendations.start(record["id"], {"revision": record["revision"]})
    await asyncio.gather(*reviews.recommendations.tasks.values())
    return reviews.view(record["id"])


@pytest.mark.parametrize(
    "name", ["주식회사 한빛정밀 (가상)", "한빛정밀", "주식회사 한 빛 정밀", "㈜ 한빛정밀", "(주)한빛정밀"]
)
def test_company_comparison_key_preserves_branches_and_person_names(name):
    assert identity_key(name, company=True) == "한빛정밀"
    assert identity_key(name) != identity_key("한빛정밀") or name == "한빛정밀"
    assert identity_key("한빛정밀 부산지점", company=True) != identity_key(name, company=True)


async def test_aliases_are_proposed_without_filling_then_explicitly_applied(tmp_path):
    _, reviews, view = await seeded(tmp_path, ("contract", "payslip"))
    record = reviews.records[view["id"]]
    names = [f for f in fields(record) if f["field_id"] == "employer_name"]
    replace_source(names[0], "주식회사 한빛정밀 (가상)")
    replace_source(names[1], "주식회사 한 빛 정밀")
    reviews.autofill(record)
    before = copy.deepcopy(record)
    result = await analyze(reviews, record)
    assert record["basic"]["employer_name"]["value"] == ""
    assert not result["recommendation_review"]["conflicts"]
    assert all(result["recommendation_review"]["fields"][f["id"]]["kind"] == "equivalent" for f in names)
    assert names == [f for f in fields(before) if f["field_id"] == "employer_name"]
    proposal = next(
        p for p in result["recommendation_review"]["proposals"] if p["target_id"] == "basic:employer_name"
    )
    applied = await reviews.recommendations.apply(
        record["id"], {"revision": record["revision"], "recommendation_ids": [proposal["id"]]}
    )
    assert applied["basic"]["employer_name"]["value"] == proposal["value"]
    assert applied["basic"]["employer_name"]["source_kind"] == "ai_provisional"
    assert applied["basic"]["employer_name"]["confirmed"] is False
    assert names[0]["raw_value"] == "주식회사 한빛정밀 (가상)"


async def test_different_business_identifiers_override_similar_company_names(tmp_path):
    _, reviews, view = await seeded(tmp_path, ("contract", "payslip"))
    record = reviews.records[view["id"]]
    parts = record["documents"][0]["parts"]
    for part, number in zip(parts, ("123-45-67890", "987-65-43210"), strict=True):
        extra = field_seed("business_registration_number", {"label": "사업자번호", "unit": "text"})
        replace_source(extra, number)
        part["fields"].append(extra)
    result = assess(record)
    assert all(
        result["fields"][f["id"]]["kind"] == "conflict"
        for f in fields(record)
        if f["field_id"] == "employer_name"
    )
    assert result["fields"]["basic:employer_name"]["suggested_value"] is None


async def test_money_recommendation_needs_actual_visual_support_and_keeps_provenance(tmp_path):
    legacy, reviews, view = await seeded(tmp_path)
    record = reviews.records[view["id"]]
    net = find(record, "net")
    replace_source(net, "3000.000", "3000")
    net.update(value="", validation_status="needs_review", issues=["AMBIGUOUS_NUMBER"])
    replace_source(net, "3,000,000", "3000000", method="vision_unverified", append=True)
    result = await analyze(reviews, record)
    proposal = next(p for p in result["recommendation_review"]["proposals"] if p["target_id"] == net["id"])
    assert proposal["value"] == "3000000"
    assert net["value"] == ""
    await reviews.recommendations.apply(
        record["id"], {"revision": record["revision"], "recommendation_ids": [proposal["id"]]}
    )
    assert net["value"] == "3000000" and net["review_state"] == "unreviewed"
    assert net["raw_value"] == "3000.000"
    cid = await bridge(reviews, copy.deepcopy(record))
    assert all(
        f["normalized_value"] is None for case in legacy.cases.values() for f in case["extraction"]["fields"]
    )
    await reviews.confirm(record["id"], {"revision": record["revision"], "field_ids": [net["id"]]})
    cid = await bridge(reviews, copy.deepcopy(record))
    case = next(c for c in reversed(list(legacy.cases.values())) if c.get("review_manual_sources"))
    assert any(
        e["quote"].startswith("AI 추천 후 사용자 확인:") for e in case["extraction"]["evidence_catalog"]
    )
    assert net["history"][-2]["event"] == "ai_recommendation_applied"
    assert legacy.comparisons[cid]


async def test_model_cannot_invent_money_from_ambiguous_punctuation(tmp_path):
    _, reviews, view = await seeded(tmp_path)
    record = reviews.records[view["id"]]
    net = find(record, "net")
    candidate = replace_source(net, "3000.000", "3000")
    net.update(value="", validation_status="needs_review", issues=["AMBIGUOUS_NUMBER"])

    class UnsupportedGuess:
        async def invoke(self, operation, data):
            assert operation == "recommend"
            return {
                "recommendations": [
                    {
                        "target_id": net["id"],
                        "value": "3000000",
                        "reason": "추측",
                        "source_ids": [candidate["id"]],
                    }
                ]
            }, {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}

    reviews.model = UnsupportedGuess()
    result = await analyze(reviews, record)
    assert not any(p["target_id"] == net["id"] for p in result["recommendation_review"]["proposals"])
    assert net["value"] == ""
    job = next(j for j in result["execution"]["jobs"] if j["kind"] == "recommendation")
    assert job["usage"]["total_tokens"] == 14
    assert job["flow"]["nodes"]["recommend_model"]["status"] == "completed"


async def test_actual_conflict_requires_choice_ack_and_individual_confirmation(tmp_path):
    _, reviews, view = await seeded(tmp_path)
    record = reviews.records[view["id"]]
    net = find(record, "net")
    replace_source(net, "3,000,000", "3000000")
    selected = replace_source(net, "3,200,000", "3200000", method="vision_unverified", append=True)
    result = reviews.view(record["id"])
    assert find(result, "net")["value"] == ""
    assert net["value"] == "3000000"  # Projection never destroys stored source values.
    conflict = result["recommendation_review"]["fields"][net["id"]]
    with pytest.raises(ValueError, match="개별"):
        await reviews.confirm(record["id"], {"revision": record["revision"], "field_ids": [net["id"]]})
    await reviews.edit_field(
        record["id"], {"revision": record["revision"], "field_id": net["id"], "candidate_id": selected["id"]}
    )
    with pytest.raises(ValueError, match="개별"):
        await reviews.confirm(
            record["id"],
            {
                "revision": record["revision"],
                "field_ids": [net["id"], find(record, "gross")["id"]],
                "conflict_acknowledgement": conflict["conflict_id"],
            },
        )
    with pytest.raises(ValueError, match="개별"):
        await reviews.confirm(
            record["id"],
            {
                "revision": record["revision"],
                "field_ids": [net["id"]],
                "conflict_acknowledgement": "old-conflict",
            },
        )
    result = await reviews.confirm(
        record["id"],
        {
            "revision": record["revision"],
            "field_ids": [net["id"]],
            "conflict_acknowledgement": conflict["conflict_id"],
        },
    )
    assert find(result, "net")["value"] == "3200000"
    assert result["recommendation_review"]["fields"][net["id"]]["resolved"]
    assert len(net["candidates"]) == 2


async def test_stale_or_confirmed_targets_cannot_be_overwritten_and_restart_does_not_run_models(tmp_path):
    _, reviews, view = await seeded(tmp_path)
    record = reviews.records[view["id"]]
    net = find(record, "net")
    replace_source(net, "3000.000", "3000")
    net.update(value="", validation_status="needs_review")
    replace_source(net, "3,000,000", "3000000", method="vision_unverified", append=True)
    result = await analyze(reviews, record)
    proposal = next(p for p in result["recommendation_review"]["proposals"] if p["target_id"] == net["id"])
    before_hash = input_hash(record)
    await reviews.edit_field(
        record["id"],
        {"revision": record["revision"], "field_id": net["id"], "value": "", "unit": "KRW", "defer": True},
    )
    assert input_hash(record) != before_hash
    with pytest.raises(ValueError, match="만료"):
        await reviews.recommendations.apply(
            record["id"], {"revision": record["revision"], "recommendation_ids": [proposal["id"]]}
        )
    restored = LocalStore(AgentSettings(), state=tmp_path)
    assert not restored.reviews.recommendations.tasks
    assert find(restored.reviews.view(record["id"]), "net")["value"] == ""


async def test_chat_steps_use_server_order_status_and_scoped_links(tmp_path):
    _, reviews, view = await seeded(tmp_path)
    context = evidence_context(reviews, reviews.records[view["id"]], "다음 단계")
    result = validated_steps(
        [
            dict(
                stage="compare",
                title="대조 실행",
                description="확정 후 실행",
                reason="결과 확인",
                source_ids=["requirements", "outside-project"],
            ),
            dict(
                stage="prepare",
                title="자료 준비",
                description="필요한 자료",
                reason="자료 확인",
                source_ids=["workflow"],
            ),
        ],
        context,
    )
    assert [s["stage"] for s in result] == ["prepare", "compare"]
    assert result[0]["status"] == "completed"
    assert result[1]["status"] == "waiting"
    assert len(result[1]["sources"]) == 1


@pytest.mark.parametrize(
    "change,supported",
    [("same", True), ("period", False), ("unit", False), ("subject", False), ("different_amount", False)],
)
async def test_corresponding_money_evidence_requires_same_role_period_subject_unit_and_digits(
    tmp_path, change, supported
):
    _, reviews, view = await seeded(tmp_path)
    record = reviews.records[view["id"]]
    target = find(record, "net")
    replace_source(target, "3000.000", "3000")
    target.update(value="", validation_status="needs_review")
    other = copy.deepcopy(record["documents"][0])
    other["id"] = identifier("upload")
    for part in other["parts"]:
        part["id"] = identifier("part")
        for field in part["fields"]:
            field["id"] = identifier("field")
            for candidate in field["candidates"]:
                candidate["id"] = identifier("candidate")
    other_record = {"documents": [other]}
    counterpart = find(other_record, "net")
    replace_source(counterpart, "3,000,000", "3000000")
    if change == "period":
        replace_source(find(other_record, "period_start"), "2026-09-01")
    elif change == "unit":
        counterpart["unit"] = "KRW/hour"
        replace_source(counterpart, "3,000,000", "3000000")
    elif change == "subject":
        replace_source(find(other_record, "employee_name"), "Different Person")
    elif change == "different_amount":
        replace_source(counterpart, "1,000,000", "1000000")
    record["documents"].append(other)
    item = assess(record)["fields"][target["id"]]
    assert (item["suggested_value"] == "3000000") is supported
    if change == "different_amount":
        assert item["kind"] == "conflict"


async def test_recommendation_results_from_an_old_snapshot_are_not_published(tmp_path):
    _, reviews, view = await seeded(tmp_path)
    record = reviews.records[view["id"]]
    target = find(record, "net")
    replace_source(target, "3000.000", "3000")
    target.update(value="", validation_status="needs_review")
    paused, release = asyncio.Event(), asyncio.Event()

    class SlowModel:
        async def invoke(self, *args):
            paused.set()
            await release.wait()
            return {"recommendations": []}, {"total_tokens": 5}

    reviews.model = SlowModel()
    started = await reviews.recommendations.start(record["id"], {"revision": record["revision"]})
    await paused.wait()
    await reviews.edit_field(
        record["id"],
        {"revision": record["revision"], "field_id": target["id"], "value": "4000000", "unit": "KRW"},
    )
    release.set()
    await reviews.recommendations.tasks[started["active_job"]]
    assert reviews.jobs[started["active_job"]]["status"] == "superseded"
    assert target["value"] == "4000000"
    assert not reviews.recommendations.view(record)["proposals"]


async def test_browser_recommendation_preview_apply_critical_queue_and_chat_steps(tmp_path):
    playwright = pytest.importorskip("playwright.async_api")
    store, reviews, view = await seeded(tmp_path / "state")
    record = reviews.records[view["id"]]
    net, gross = find(record, "net"), find(record, "gross")
    replace_source(net, "3000.000", "3000")
    net.update(value="", validation_status="needs_review", issues=["AMBIGUOUS_NUMBER"])
    replace_source(net, "3,000,000", "3000000", method="vision_unverified", append=True)
    replace_source(gross, "3,000,000", "3000000")
    replace_source(gross, "3,200,000", "3200000", method="vision_unverified", append=True)
    await analyze(reviews, record)

    class Chat:
        async def invoke(self, operation, data):
            assert operation == "chat"
            return {
                "answer": "필수 확인 항목부터 진행해 주세요.",
                "source_ids": ["recommendations"],
                "steps": [
                    dict(
                        stage="review",
                        title="충돌 원문 확인",
                        description="최상단에서 기준값을 선택하세요.",
                        reason="후보가 서로 다릅니다.",
                        source_ids=["recommendations"],
                    ),
                    dict(
                        stage="confirm",
                        title="선택한 값 확정",
                        description="원문 확인 표시 후 개별 확정하세요.",
                        reason="미확정 값은 계산에 사용하지 않습니다.",
                        source_ids=["requirements"],
                    ),
                ],
            }, {"total_tokens": 10}

    reviews.model = Chat()
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
            await page.goto(base + "/review/" + record["id"] + "/verify")
            await playwright.expect(page.locator("#critical-review")).to_be_visible()
            await page.locator("#critical-review").screenshot(path=str(tmp_path / "review-critical.png"))
            await playwright.expect(page.locator('.filter-chip[data-filter="critical"]')).to_contain_text("1")
            net_input = page.get_by_role("textbox", name="실수령액 (원)", exact=True)
            await playwright.expect(net_input).to_have_value("")
            card = page.locator(f'.field-card[data-field-id="{net["id"]}"]')
            await card.get_by_role("button", name="이 추천 적용", exact=True).click()
            await playwright.expect(net_input).to_have_value("3000000")
            await playwright.expect(card).to_contain_text("AI 임시 추천 · 확인 필요")
            assert net["review_state"] == "unreviewed"
            await page.get_by_role("button", name="값 있는 항목 선택", exact=True).click()
            assert not await page.locator(
                f'.field-card[data-field-id="{gross["id"]}"] .field-header input[type="checkbox"]'
            ).is_checked()
            critical_card = page.locator(".critical-conflict").first
            await playwright.expect(
                critical_card.get_by_role("button", name="원문 확인 후 이 값 개별 확정")
            ).to_be_disabled()
            await critical_card.locator('input[type="radio"]').last.check()
            await critical_card.get_by_label("원문과 선택한 기준값을 확인했습니다.").check()
            await critical_card.get_by_role("button", name="원문 확인 후 이 값 개별 확정").click()
            await playwright.expect(page.locator("#critical-review")).to_have_count(0)
            assert gross["value"] == "3200000" and gross["review_state"] == "confirmed"
            await page.locator("#chat-launcher").click()
            await page.locator("#chat-input").fill("다음에는 어떤 단계를 진행해?")
            await page.locator("#chat-input").press("Enter")
            await playwright.expect(page.locator(".chat-step")).to_have_count(2, timeout=15000)
            await playwright.expect(page.locator(".chat-step").first).to_contain_text("원문·충돌 확인")
            await page.screenshot(path=str(tmp_path / "review-chat-steps.png"))
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.screenshot(path=str(tmp_path / "review-chat-steps-mobile.png"))
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            await browser.close()
        assert not errors, errors
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)
