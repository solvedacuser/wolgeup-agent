"""Real Chromium acceptance flow against the shipped frontend and local API."""

import asyncio
import threading

import pytest

from agent.config import AgentSettings
from agent.dev.local_workflow import LocalStore
from agent.dev.test_server import LocalHTTPServer
from agent.tests.test_review_workspace import ReviewFixtureModel, sample_pdf


async def test_browser_upload_edit_source_confirm_and_compare(tmp_path):
    playwright = pytest.importorskip("playwright.async_api")
    store = LocalStore(AgentSettings(), state=tmp_path / "state")
    store.reviews.model = ReviewFixtureModel()
    server = LocalHTTPServer(("127.0.0.1", 0), store, asyncio.get_running_loop())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    errors = []
    try:
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 1100})
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(base + "/review/new")
            await playwright.expect(page.get_by_role("heading", name="근무지와 급여 기간")).to_be_visible()
            await page.locator("#review-file-input").set_input_files(
                [
                    {"name": "pay.pdf", "mimeType": "application/pdf", "buffer": sample_pdf()},
                    {
                        "name": "contract.pdf",
                        "mimeType": "application/pdf",
                        "buffer": sample_pdf(("contract",)),
                    },
                ]
            )
            await playwright.expect(page.locator("#basic-period_start")).to_have_value(
                "2026-10-01", timeout=25000
            )
            await playwright.expect(page.locator("#basic-employer_name")).to_have_value("Cafe ABC")
            panel = page.locator("#execution-panel")
            await playwright.expect(
                panel.get_by_role("heading", name="에이전트 실행·토큰 사용량")
            ).to_be_visible()
            await playwright.expect(panel.locator('[data-token-metric="total_tokens"]')).to_have_text(
                "60", timeout=10000
            )
            await playwright.expect(panel.locator(".flow-group")).to_have_count(5)
            await playwright.expect(panel.locator('[data-selected-metric="total_tokens"]')).to_have_text("30")
            await playwright.expect(panel.locator('[data-flow-node="classify"]')).to_have_attribute(
                "data-flow-state", "completed"
            )
            await playwright.expect(panel.locator('[data-flow-node="classify"]')).to_contain_text("10 토큰")
            await playwright.expect(panel.locator('[data-flow-node="ocr"]')).to_have_attribute(
                "data-flow-state", "idle"
            )
            await playwright.expect(panel.locator('[data-flow-node="worker_wage"]')).to_have_attribute(
                "data-flow-state", "idle"
            )
            await playwright.expect(panel.locator('[data-flow-edge="quality>native"]')).to_have_class(
                "flow-edge edge-completed"
            )
            await playwright.expect(panel.locator('[data-flow-edge="quality>render_ocr"]')).to_have_class(
                "flow-edge edge-idle"
            )
            await panel.locator(".flow-group").first.screenshot(path=str(tmp_path / "document-flow.png"))
            await playwright.expect(panel).to_contain_text("pdf.read_native")
            await playwright.expect(panel).to_contain_text("model.classify")
            material = page.locator(".file-card").filter(
                has=page.get_by_role("heading", name="pay.pdf", exact=True)
            )
            part = material.locator("details.part-summary").first
            await part.locator("summary").first.click()
            await playwright.expect(part).not_to_have_attribute("open", "")
            # Wait for two real automatic refreshes, not an implementation-level render call.
            for _ in range(2):
                previous = await part.element_handle()
                await page.wait_for_function("node => !node.isConnected", arg=previous, timeout=10000)
                await playwright.expect(part).not_to_have_attribute("open", "")
            await page.reload()
            await playwright.expect(part).not_to_have_attribute("open", "")
            await part.locator("summary").first.click()
            candidates = part.locator("details.field-candidates").first
            await candidates.locator("summary").click()
            previous = await candidates.element_handle()
            await page.wait_for_function("node => !node.isConnected", arg=previous, timeout=10000)
            await playwright.expect(candidates).to_have_attribute("open", "")
            monitor_job = page.locator("#execution-panel details.execution-job").first
            await monitor_job.locator("summary").first.click()
            previous = await monitor_job.element_handle()
            await page.wait_for_function("node => !node.isConnected", arg=previous, timeout=10000)
            await playwright.expect(monitor_job).not_to_have_attribute("open", "")
            await page.locator("#execution-panel").screenshot(path=str(tmp_path / "execution-monitor.png"))
            await page.locator("#basic-payment_date").fill("2026-11-10")
            await page.locator("h1").click()
            await playwright.expect(page.locator("#basic-payment_date")).to_have_value("2026-11-10")
            await page.get_by_role("button", name="사례 생성 후 자료 등록 →").click()
            await playwright.expect(page.get_by_role("heading", name="자료 등록", exact=True)).to_be_visible()
            await playwright.expect(page.locator(".file-card")).to_have_count(2)
            await page.get_by_role("link", name="내용 확인 →").last.click()
            await playwright.expect(page.get_by_role("heading", name="내용 확인", exact=True)).to_be_visible()
            await page.locator(".document-tabs").get_by_role("button", name="pay.pdf", exact=True).click()
            await playwright.expect(page.locator("#source-overlay rect").first).to_be_visible()
            # Filter: only cards whose extraction found nothing; the extracted net pay is hidden.
            net_input = page.get_by_role("textbox", name="실수령액 (원)", exact=True)
            await page.locator('.filter-chip[data-filter="missing"]').click()
            await playwright.expect(page.locator('.filter-chip[data-filter="missing"]')).to_have_attribute(
                "aria-pressed", "true"
            )
            await playwright.expect(net_input).to_have_count(0)
            assert await page.locator(".field-card").count() > 0
            for badge_text in await page.locator(".field-card .field-header .badge").all_inner_texts():
                assert badge_text == "확인 필요"
            await page.locator(".filter-note").get_by_role("button", name="전체 보기").click()
            await playwright.expect(net_input).to_have_count(1)
            card = page.locator(".field-card").filter(
                has=page.get_by_role("textbox", name="실수령액 (원)", exact=True)
            )
            await card.get_by_role("button", name="원문 위치", exact=True).click()
            await playwright.expect(page.locator("#source-note")).to_contain_text("강조 영역")
            rectangle = page.locator("#source-overlay rect.selected").first
            before = await rectangle.bounding_box()
            assert before and before["width"] > 0
            await page.set_viewport_size({"width": 950, "height": 1000})
            after = await rectangle.bounding_box()
            assert after and after["width"] > 0
            await page.locator("#source-page").evaluate("image => image.decode()")
            image_box = await page.locator("#source-page").bounding_box()
            overlay_box = await page.locator("#source-overlay").bounding_box()
            assert abs(image_box["height"] - overlay_box["height"]) < 2
            assert abs(image_box["width"] - overlay_box["width"]) < 2
            await page.set_viewport_size({"width": 1440, "height": 1100})
            await card.get_by_role("textbox", name="실수령액 (원)", exact=True).fill("1990000")
            await page.locator("h1").click()
            await playwright.expect(
                card.get_by_role("textbox", name="실수령액 (원)", exact=True)
            ).to_have_value("1990000")
            await page.get_by_role("button", name="값 있는 항목 선택", exact=True).click()
            await page.get_by_role("button", name="선택 항목 확정", exact=True).click()
            await playwright.expect(card.locator(".badge")).to_have_text("확인 완료")
            await (
                page.locator(".document-tabs").get_by_role("button", name="contract.pdf", exact=True).click()
            )
            await page.get_by_role("button", name="값 있는 항목 선택", exact=True).click()
            await page.get_by_role("button", name="선택 항목 확정", exact=True).click()
            await page.get_by_role("button", name="입력한 기본 정보 확인·확정", exact=True).click()
            await page.get_by_label(
                "선택한 자료가 같은 근로자·사업장의 자료임을 확인했습니다.", exact=True
            ).check()
            comparison = page.locator(".request-row").filter(
                has=page.get_by_role("heading", name="확정한 값으로 문서 대조", exact=True)
            )
            await playwright.expect(
                comparison.get_by_role("button", name="실행하기", exact=True)
            ).to_be_visible()
            await comparison.get_by_role("button", name="실행하기", exact=True).click()
            await playwright.expect(page.get_by_role("link", name="문서 대조 결과 보기 →")).to_be_visible(
                timeout=20000
            )
            await playwright.expect(panel.locator('[data-flow-node="worker_wage"]')).to_have_attribute(
                "data-flow-state", "completed"
            )
            # A later business run must not blank the document graph; it follows the open document tab.
            await playwright.expect(panel.locator('[data-flow-node="classify"]')).to_have_attribute(
                "data-flow-state", "completed"
            )
            await playwright.expect(panel.locator(".flow-group-job").first).to_contain_text(
                "표시 중: contract.pdf"
            )
            await playwright.expect(panel.locator('[data-selected-metric="total_tokens"]')).to_have_text("0")
            history = page.locator(".request-history").first
            await playwright.expect(history).to_contain_text("확정한 값으로 문서 대조")
            await playwright.expect(history).to_contain_text("최신 결과")
            await playwright.expect(history).to_contain_text("확정값")
            await panel.locator(".flow-group").nth(1).screenshot(path=str(tmp_path / "work-flow.png"))
            rid = next(iter(store.reviews.records))
            doc_job = store.reviews.records[rid]["documents"][0]["job_id"]
            await page.locator("#execution-job-select").select_option(doc_job)
            await playwright.expect(panel.locator('[data-flow-node="classify"]')).to_have_attribute(
                "data-flow-state", "completed"
            )
            previous = await panel.element_handle()
            await page.wait_for_function("node => !node.isConnected", arg=previous, timeout=10000)
            await playwright.expect(page.locator("#execution-job-select")).to_have_value(doc_job)
            await playwright.expect(panel.locator('[data-selected-metric="total_tokens"]')).to_have_text("30")
            await page.set_viewport_size({"width": 600, "height": 950})
            await panel.locator(".flow-canvas-scroll").first.evaluate("element => element.scrollLeft = 240")
            assert (
                await panel.locator(".flow-canvas-scroll").first.evaluate("element => element.scrollLeft") > 0
            )
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            await page.set_viewport_size({"width": 1440, "height": 1100})
            await page.screenshot(path=str(tmp_path / "review-verify.png"), full_page=True)
            rid = next(iter(store.reviews.records))
            await page.reload()
            await playwright.expect(page.get_by_role("link", name="문서 대조 결과 보기 →")).to_be_visible()
            assert store.reviews.records[rid]["state"] == "active"
            await page.get_by_role("link", name="문서 대조 결과 보기 →").click()
            await playwright.expect(page.locator("body")).to_contain_text("차이")
            await playwright.expect(page.locator("#chat-launcher")).to_be_visible()
            await page.locator("#chat-launcher").click()
            await playwright.expect(page.locator("#chat-panel")).to_be_visible()
            await playwright.expect(page.locator("#chat-review-select")).to_have_value(rid)
            await page.get_by_role("button", name="채팅창 닫기", exact=True).click()
            # A pre-instrumentation job retains its real total; missing details are not zero/failure.
            historical = store.reviews.jobs[doc_job]
            historical.pop("execution_trace")
            historical.pop("route")
            for call in historical["agent_calls"]:
                call.pop("usage", None)
            await page.goto(base + f"/review/{rid}/verify")
            await page.locator("#execution-job-select").select_option(doc_job)
            await playwright.expect(panel.locator('[data-selected-metric="total_tokens"]')).to_have_text("30")
            await playwright.expect(panel.locator('[data-flow-node="classify"]')).to_contain_text(
                "호출 확인 · 당시 토큰 미수집"
            )
            await playwright.expect(panel.locator(".execution-selected")).to_contain_text(
                "0토큰이나 실행 실패를 의미하지 않습니다"
            )
            await playwright.expect(panel.locator('[data-flow-edge="native>classify"]')).to_have_class(
                "flow-edge edge-idle"
            )
            assert not errors, errors
            await browser.close()
    finally:
        tasks = [*store.reviews.tasks.values(), *store.reviews.request_tasks.values(), *store.tasks.values()]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
