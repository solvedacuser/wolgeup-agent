"""Opt-in smoke check using synthetic Korean data; never opens user documents."""

import argparse
import asyncio
import io
import json
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from agent.config import AgentSettings
from agent.dev.local_workflow import LocalStore
from agent.review.reading import png_bytes, read_document


async def main(live_model):
    settings = AgentSettings.from_env(Path("agent/.env"))
    pdfmetrics.registerFont(TTFont("ReviewSmoke", str(settings.font_path)))
    buffer = io.BytesIO()
    canvas = Canvas(buffer, invariant=1)
    canvas.setFont("ReviewSmoke", 14)
    lines = [
        "급여명세서",
        "사업장명: 테스트카페",
        "근로자 성명: 홍길동",
        "급여 산정기간: 2026-10-01 ~ 2026-10-31",
        "기본급: 2,300,000원",
        "총지급액: 2,300,000원",
        "총공제액: 300,000원",
        "실수령액: 2,000,000원",
        "지급일: 2026-11-10",
    ]
    for i, text in enumerate(lines):
        canvas.drawString(60, 760 - i * 40, text)
    canvas.save()
    pdf = buffer.getvalue()
    image = png_bytes(pdf, "application/pdf", max_side=2400)
    reading = await asyncio.to_thread(read_document, image, "image/png", settings)
    text = reading["pages"][0]["text"]
    result = {
        "ocr_status": reading["status"],
        "korean_recognized": "급여" in text,
        "amount_recognized": "2,000,000" in text,
        "source_blocks": len(reading["pages"][0]["blocks"]),
    }
    if live_model:
        legacy = LocalStore(settings, state=Path("output/review-live-smoke"))
        review = await legacy.reviews.create()
        uploaded = await legacy.reviews.upload(review["id"], pdf, "합성_급여명세서.pdf")
        jid = uploaded["review"]["documents"][0]["job_id"]
        await legacy.reviews.tasks[jid]
        job = legacy.reviews.jobs[jid]
        current = legacy.reviews.view(review["id"])
        result.update(
            job_status=job["status"],
            model=settings.extraction_model,
            model_calls=job.get("agent_calls", []),
            usage=job.get("usage", {}),
            document_types=[p["document_type"] for p in current["documents"][0]["parts"]],
            fields=[
                {
                    "key": f["field_id"],
                    "value": f["value"],
                    "state": f["review_state"],
                    "located": bool((f.get("evidence") or {}).get("boxes")),
                }
                for p in current["documents"][0]["parts"]
                for f in p["fields"]
                if f["value"]
            ],
            error=job.get("error"),
        )
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--live-model", action="store_true", help="Calls the configured paid model on synthetic data"
    )
    args = parser.parse_args()
    outcome = asyncio.run(main(args.live_model))
    if not outcome["korean_recognized"] or args.live_model and outcome.get("job_status") != "completed":
        raise SystemExit(1)
