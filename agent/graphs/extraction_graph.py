import asyncio
import io
import re
import unicodedata
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pypdf import PdfReader

from agent.providers.base import ExtractionResponse
from agent.schemas.public.errors import DocumentUnreadable, IntegrityError, ModelFormatError, OCRFailed
from agent.schemas.public.inputs import EvidenceRef
from agent.schemas.public.results import ExtractedField, ExtractionCandidate
from agent.services.extraction_profiles import RATE_UNITS, expected_unit, field_definition
from agent.services.extraction_tables import expand_tables
from agent.services.hashing import content_hash, digest
from agent.services.normalization import CANONICAL_UNITS, normalize_evidenced_scalar


def normalized_text(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def verify_response(response, pages, expected_type, image_pages=None):
    response = expand_tables(response, pages, image_pages)
    try:
        response = ExtractionResponse.model_validate(response)
    except Exception as exc:
        raise ModelFormatError("Invalid extraction schema") from exc
    if response.document_type != expected_type:
        raise ModelFormatError(
            "Document type conflict; user confirmation required", reason="DOCUMENT_TYPE_MISMATCH"
        )
    if len({f.field_id for f in response.fields}) != len(response.fields):
        raise ModelFormatError("Duplicate extracted field IDs")
    for item in response.fields:
        if not 1 <= item.page <= len(pages):
            raise ModelFormatError("Evidence page outside document")
        if item.raw_value is None:
            if item.normalized_value is not None:
                raise ModelFormatError("Absent raw value cannot have normalized value")
            continue
        if not item.quote or normalized_text(item.raw_value) not in normalized_text(item.quote):
            raise ModelFormatError("Value missing from evidence quote")
        if (
            pages[item.page - 1].strip()
            and not (image_pages or [False] * len(pages))[item.page - 1]
            and normalized_text(item.quote) not in normalized_text(pages[item.page - 1])
        ):
            raise ModelFormatError("Quote does not match page text")
        if item.normalized_value is not None:
            try:
                definition = field_definition(expected_type, item.field_id)
                source_unit = (
                    item.unit
                    or expected_unit(expected_type, item.field_id)
                    or CANONICAL_UNITS.get(item.field_id)
                )
                if definition and (
                    definition["unit"] == "rate"
                    and source_unit not in RATE_UNITS
                    or definition["unit"] != "rate"
                    and source_unit != definition["unit"]
                ):
                    raise ValueError("Field unit does not match document vocabulary")
                expected = normalize_evidenced_scalar(
                    item.raw_value,
                    source_unit,
                    item.quote,
                    field_id=item.field_id,
                )
                if expected is not None and expected != item.normalized_value:
                    raise ValueError("Normalized value differs from explicit source scalar")
            except (ValueError, ArithmeticError) as exc:
                raise ModelFormatError(
                    "Invalid source-preserving normalization",
                    reason="SOURCE_NORMALIZATION_INVALID",
                    field_id=item.field_id,
                ) from exc


class ExtractionState(TypedDict, total=False):
    inspected: dict[str, Any]
    candidates: dict[str, Any]
    verified: bool
    result: dict[str, Any]


def build_extraction_graph(runtime):
    payload = runtime.task.payload

    async def read():
        content = await runtime.ports.read_source(runtime.context, payload.source)
        if (
            content.document_version != payload.source.document_version
            or content.content_hash != payload.source.content_hash
            or content_hash(content.data) != payload.source.content_hash
        ):
            raise IntegrityError("Source version or content hash mismatch")
        if len(content.data) > runtime.settings.max_file_bytes or content.mime_type != "application/pdf":
            raise DocumentUnreadable("Document size or MIME is unsupported")
        return content.data

    async def inspect_document(state):
        async def inspect_pdf():
            data = await read()
            try:
                reader = PdfReader(io.BytesIO(data), strict=True)
                if reader.is_encrypted:
                    raise DocumentUnreadable("Encrypted PDFs are not supported")
                if not 1 <= len(reader.pages) <= runtime.settings.max_pages:
                    raise DocumentUnreadable("Page count exceeds configured limit")
                pages = [page.extract_text() or "" for page in reader.pages]
                image_pages = [bool(page.images) for page in reader.pages]
            except DocumentUnreadable:
                raise
            except Exception as exc:
                raise DocumentUnreadable("PDF could not be inspected") from exc
            ocr_pages = []
            if payload.document_type == "payslip_ocr":
                from agent.services.local_ocr import recognize_pdf

                for index in range(len(pages)):
                    await runtime.control()
                    ocr_pages.extend(await asyncio.to_thread(recognize_pdf, data, runtime.settings, [index]))
                pages = [page["text"] for page in ocr_pages]
                if not any(page.strip() for page in pages):
                    raise OCRFailed("문자를 인식하지 못했습니다. 해상도와 문서 방향을 확인해 주세요.")
                image_pages = [False] * len(pages)
            return {
                "pages": pages,
                "page_count": len(pages),
                "image_pages": image_pages,
                "needs_vision": any(image_pages) or any(not p.strip() for p in pages),
                "ocr_pages": ocr_pages,
            }

        return {"inspected": await runtime.step("inspect_document", inspect_pdf)}

    async def extract_candidates(state):
        async def extract():
            inspected = state["inspected"]
            if payload.document_type == "payslip_ocr":
                from agent.services.payslip_ocr import extract_payslip

                return extract_payslip(inspected["ocr_pages"])
            data = {
                "document_type": payload.document_type,
                "pages": inspected["pages"],
                "page_text": "\n\n".join(f"PAGE {i}\n{p}" for i, p in enumerate(inspected["pages"], 1)),
            }
            if inspected["needs_vision"]:
                data["pdf_bytes"] = await read()
            return await runtime.call_model(
                "extract_candidates",
                "extract",
                data,
                validator=lambda response: verify_response(
                    response, inspected["pages"], payload.document_type, inspected.get("image_pages")
                ),
            )

        return {"candidates": await runtime.step("extract_candidates", extract)}

    async def verify_candidates(state):
        def verify():
            verify_response(
                state["candidates"],
                state["inspected"]["pages"],
                payload.document_type,
                state["inspected"].get("image_pages"),
            )
            return True

        return {"verified": await runtime.step("verify_candidates", verify)}

    async def package_candidates(state):
        def package():
            if not state["verified"]:
                raise IntegrityError("Unverified extraction")
            response = ExtractionResponse.model_validate(
                expand_tables(
                    state["candidates"], state["inspected"]["pages"], state["inspected"].get("image_pages")
                )
            )
            fields, evidence = [], []
            for item in response.fields:
                refs = []
                if item.raw_value is not None:
                    eid = digest([payload.source.source_id, item.field_id, item.page, item.quote])
                    method = (
                        "ocr_unverified"
                        if payload.document_type == "payslip_ocr"
                        else "text_match"
                        if normalized_text(item.quote)
                        in normalized_text(state["inspected"]["pages"][item.page - 1])
                        else "vision_unverified"
                    )
                    evidence.append(
                        EvidenceRef(
                            evidence_id=eid,
                            source_id=payload.source.source_id,
                            page=item.page,
                            quote=item.quote,
                            verification_method=method,
                        )
                    )
                    refs = [eid]
                fields.append(
                    ExtractedField(
                        field_id=item.field_id,
                        raw_value=item.raw_value,
                        normalized_value=item.normalized_value,
                        unit=item.unit
                        or (
                            (
                                expected_unit(payload.document_type, item.field_id)
                                or CANONICAL_UNITS.get(item.field_id)
                            )
                            if item.raw_value
                            else None
                        ),
                        evidence_refs=refs,
                        issues=item.issues,
                    )
                )
            issues = list(response.issues)
            if state["inspected"]["needs_vision"] and payload.document_type != "payslip_ocr":
                issues.append("VISION_REQUIRES_USER_CONFIRMATION")
            return ExtractionCandidate(
                document_id=payload.source.document_id,
                document_version=payload.source.document_version,
                document_type=payload.document_type,
                extraction_version=payload.extraction_version,
                model_config_version=payload.model_config_version,
                fields=fields,
                evidence_catalog=evidence,
                issues=issues,
                page_count=state["inspected"]["page_count"],
                verification_method="ocr_unverified"
                if payload.document_type == "payslip_ocr"
                else "vision_unverified"
                if state["inspected"]["needs_vision"]
                else "text_match",
                recognized_pages=state["inspected"]["pages"]
                if payload.document_type == "payslip_ocr"
                else [],
            ).model_dump(mode="json")

        return {"result": await runtime.step("package_candidates", package)}

    graph = StateGraph(ExtractionState)
    names = ["inspect_document", "extract_candidates", "verify_candidates", "package_candidates"]
    for name, fn in zip(names, [inspect_document, extract_candidates, verify_candidates, package_candidates]):
        graph.add_node(name, fn)
    graph.add_edge(START, names[0])
    for a, b in zip(names, names[1:]):
        graph.add_edge(a, b)
    graph.add_edge(names[-1], END)
    return graph.compile()
