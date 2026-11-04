import asyncio
import io
import json
import zipfile
from html import escape

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from agent.schemas.public.errors import IntegrityError
from agent.schemas.public.results import ReportCandidate
from agent.services.hashing import content_hash
from agent.services.messages import RULE_LABELS, reason_text


def render_pdf(result, explanation, settings, generated_at):
    if not settings.font_path.is_file():
        raise IntegrityError("A licensed Hangul TrueType font must be configured")
    # Registering happens only when generating a PDF, never on import.
    font_name = "AgentHangul-" + content_hash(str(settings.font_path.resolve()).encode())[:10]
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name, str(settings.font_path)))
    body = ParagraphStyle("body", fontName=font_name, fontSize=9, leading=15, wordWrap="CJK")
    heading = ParagraphStyle(
        "heading", parent=body, fontSize=20, leading=28, textColor=colors.HexColor("#174f42")
    )
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=40)

    def p(text):
        return Paragraph(escape(str(text)).replace("\n", "<br/>"), body)

    story = [
        Paragraph("월급잇다 상담 자료", heading),
        Spacer(1, 12),
        p(
            f"결과 {result.result_id} / 버전 {result.result_version} / {'최신 결과' if result.freshness == 'current' else '이전 입력 결과'}"
        ),
        p(f"생성 기준 시각 {generated_at.isoformat()}"),
        p("이 자료는 확인된 입력의 대조 결과입니다. 법정 임금 계산 규칙 검증은 완료되지 않았습니다."),
        Spacer(1, 12),
    ]
    story.extend(p(text) for text in explanation.paragraphs)
    story.append(Spacer(1, 12))
    rows = [[p(x) for x in ["항목", "상태", "왼쪽 값", "오른쪽 값", "차이", "확인 사유"]]]
    labels = {
        "match": "자료상 일치",
        "difference": "차이 발견",
        "insufficient": "정보 부족",
        "needs_review": "검토 필요",
    }

    def number(value, unit):
        if value is None:
            return "미확인"
        label = {"KRW": "원", "KRW/hour": "원/시간", "KRW/month": "원/월", "minute": "분"}.get(unit, unit)
        return f"{value:,}{label}"

    for item in result.comparisons:
        rows.append(
            [
                p(v)
                for v in [
                    RULE_LABELS.get(item.rule_id, item.rule_id),
                    labels[item.status],
                    number(item.lhs, item.unit),
                    number(item.rhs, item.unit),
                    "비교 보류" if item.delta is None else number(item.delta, item.unit),
                    " ".join(reason_text(code) for code in item.reason_codes) or "-",
                ]
            ]
        )
    table = Table(rows, colWidths=[58, 70, 85, 85, 75, A4[0] - 72 - 373], repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2eee8")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#bdccc3")),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.extend([table, Spacer(1, 14), p("보완 질문")])
    for question in result.questions:
        story.append(p(question.text))
    story.extend([Spacer(1, 12), p("출처")])
    for ev in result.evidence_catalog:
        story.append(p(f"{ev.evidence_id[:12]} / {ev.source_id} / {ev.page or '수기'}쪽 / {ev.quote}"))

    def footer(canvas, _):
        canvas.setFont(font_name, 8)
        canvas.drawRightString(A4[0] - 36, 22, f"월급잇다 | {canvas.getPageNumber()}")

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    data = buffer.getvalue()
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if not reader.pages or "월급잇다" not in text or result.result_id not in text:
        raise IntegrityError("Generated PDF failed content validation")
    return data


async def build_report(runtime):
    payload = runtime.task.payload
    result, explanation = payload.result, payload.explanation
    if (
        explanation.result_id != result.result_id
        or explanation.result_version != result.result_version
        or explanation.result_hash != result.result_hash
    ):
        raise IntegrityError("Explanation belongs to another result")
    if payload.template_version != "report-v1":
        raise IntegrityError("Unsupported report template")
    if len({s.source_id for s in payload.selected_sources}) != len(payload.selected_sources):
        raise IntegrityError("Duplicate selected source")
    originals = {}
    for index, source in enumerate(payload.selected_sources, 1):
        if source.workplace_id != result.workplace_id:
            raise IntegrityError("Selected original belongs to another workplace")
        dependency = f"source:{source.source_id}:{source.document_version}:{source.link_version}"
        if dependency not in result.dependencies:
            raise IntegrityError("Selected source version is not in result")
        await runtime.control()
        content = await runtime.ports.read_source(runtime.context, source)
        if (
            content.document_version != source.document_version
            or content.content_hash != source.content_hash
            or content_hash(content.data) != source.content_hash
        ):
            raise IntegrityError("Original source changed")
        if len(content.data) > runtime.settings.max_file_bytes or content.mime_type != "application/pdf":
            raise IntegrityError("Selected original exceeds limits or is not PDF")
        originals[f"originals/{index:03d}-{content_hash(source.source_id.encode())[:12]}.pdf"] = content.data
    await runtime.control()
    pdf = await asyncio.to_thread(render_pdf, result, explanation, runtime.settings, payload.generated_at)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("summary.pdf", pdf)
        archive.writestr("result.json", result.model_dump_json(indent=2))
        archive.writestr("explanation.json", explanation.model_dump_json(indent=2))
        archive.writestr(
            "manifest.json", json.dumps({name: content_hash(data) for name, data in originals.items()})
        )
        for name, data in originals.items():
            archive.writestr(name, data)
    zip_data = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(zip_data)) as archive:
        if archive.testzip() is not None or archive.read("summary.pdf") != pdf:
            raise IntegrityError("Generated ZIP failed validation")
    await runtime.control()
    pdf_ref = await runtime.ports.stage_artifact(runtime.context, pdf, "application/pdf")
    zip_ref = await runtime.ports.stage_artifact(runtime.context, zip_data, "application/zip")
    for ref, data, mime in [(pdf_ref, pdf, "application/pdf"), (zip_ref, zip_data, "application/zip")]:
        if ref.content_hash != content_hash(data) or ref.size_bytes != len(data) or ref.mime_type != mime:
            raise IntegrityError("Staged artifact metadata mismatch")
    return ReportCandidate(
        result_id=result.result_id,
        result_version=result.result_version,
        explanation_version=payload.explanation_version,
        artifacts=[pdf_ref, zip_ref],
        included_source_ids=[s.source_id for s in payload.selected_sources],
    ).model_dump(mode="json")
