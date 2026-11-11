"""Business graph: pin reviewed facts, adapt to the existing engine, execute once."""

import asyncio
import copy
import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agent.config import ENGINE_VERSION, EXTRACTION_COMPATIBILITY_VERSION
from agent.review.flow import visit
from agent.schemas.public.results import ExtractionCandidate
from agent.services.extraction_profiles import field_definition
from agent.services.hashing import content_hash, digest
from agent.services.local_json import write_json


async def bridge(store, snapshot):
    legacy = store.legacy
    selected, errors = store.selection(snapshot)
    if errors:
        raise ValueError(" / ".join(errors))
    selected_ids = {p["id"] for p in selected}
    selection = {
        "contract_case_id": None,
        "payslip_case_id": None,
        "attendance_case_ids": [],
        "deposit_case_ids": [],
    }
    transaction_ids = set()
    for document, part in store.parts(snapshot):
        if part["id"] not in selected_ids:
            continue
        alias = "reviewdoc-" + digest([document["id"], part["start_page"], part["end_page"]])[:20]
        original = legacy.documents[document["id"]]
        legacy.documents[alias] = {
            **original,
            "id": alias,
            "kind": "review_part",
            "storage_document_id": original.get("storage_document_id", document["id"]),
            "part_range": [part["start_page"], part["end_page"]],
            "title": document["title"] + " · " + part["label"],
        }
        case = await legacy.create_case(alias, part["document_type"])
        ref = legacy.document_ref(alias)
        manual_values = {
            f["field_id"]: f["value"]
            for f in part["fields"]
            if f["review_state"] == "confirmed"
            and f.get("source_kind") in ("user_statement", "ai_provisional")
        }
        manual_data = json.dumps(manual_values, ensure_ascii=False, sort_keys=True)
        manual_ref = ref.model_copy(
            update={
                "source_id": "review-manual-" + case["id"],
                "document_id": "manual-" + case["id"],
                "link_id": "manual-link-" + case["id"],
                "kind": "manual",
                "content_hash": content_hash(manual_data.encode()),
            }
        )
        case["review_manual_sources"] = (
            [{"ref": manual_ref.model_dump(mode="json"), "data": manual_data}] if manual_values else []
        )
        fields, evidence = [], []
        for field in part["fields"]:
            if field["extra"] or not field_definition(part["document_type"], field["field_id"]):
                continue
            confirmed = field["review_state"] == "confirmed" and bool(field["value"])
            ev = field.get("evidence") or {}
            eid = digest([case["id"], field["id"], field.get("confirmation_id")])
            if confirmed:
                evidence.append(
                    {
                        "evidence_id": eid,
                        "source_id": manual_ref.source_id
                        if field.get("source_kind") in ("user_statement", "ai_provisional")
                        else ref.source_id,
                        "page": None
                        if field.get("source_kind") in ("user_statement", "ai_provisional")
                        else ev.get("page"),
                        "quote": ev.get("quote", "")
                        if field.get("source_kind") not in ("user_statement", "ai_provisional")
                        else (
                            "AI 추천 후 사용자 확인: "
                            if field.get("source_kind") == "ai_provisional"
                            else "사용자 직접 입력: "
                        )
                        + field["value"],
                        "locator": "review:" + snapshot["id"] + ":" + field["id"],
                        "verification_method": "manual_confirmed",
                    }
                )
            fields.append(
                {
                    "field_id": field["field_id"],
                    "raw_value": field.get("raw_value"),
                    "normalized_value": field["value"] if confirmed else None,
                    "suggested_value": None,
                    "unit": field["unit"],
                    "validation_status": "valid" if confirmed else "missing",
                    "evidence_refs": [eid] if confirmed else [],
                    "issues": [] if confirmed else ["USER_CONFIRMATION_REQUIRED"],
                }
            )
        payload = ExtractionCandidate(
            document_id=alias,
            document_version=1,
            document_type=part["document_type"],
            extraction_version="1.2",
            model_config_version=store.settings.model_config_version,
            fields=fields,
            evidence_catalog=evidence,
            issues=[],
            page_count=document["pages"],
            verification_method="ocr_unverified",
            source_method="python_ocr",
        )
        case.update(
            extraction=payload.model_dump(mode="json"),
            extraction_revision=1,
            extraction_engine_version=ENGINE_VERSION,
            extraction_compatibility_version=EXTRACTION_COMPATIBILITY_VERSION,
            extraction_needs_review=False,
        )
        legacy.cases[case["id"]] = case
        legacy.save_case(case)
        kind = part["document_type"]
        if kind in ("contract", "payslip"):
            selection[kind + "_case_id"] = case["id"]
        else:
            selection[kind + "_case_ids"].append(case["id"])
        if kind == "deposit":
            values = {f["field_id"]: f["value"] for f in part["fields"] if f["review_state"] == "confirmed"}
            account = values.get("account_number")
            namespace = digest([document["sha256"], [part["start_page"], part["end_page"]]])
            for selected_row in snapshot["options"]["selected_transactions"]:
                if not selected_row.startswith(part["id"] + ":transaction_"):
                    continue
                index = int(selected_row.rsplit("_", 1)[1])
                reference = values.get(f"transaction_{index}_reference")
                key = (
                    ["bank-reference", account, reference]
                    if account and reference
                    else [namespace, "transaction", index]
                )
                transaction_ids.add("transaction-" + digest(key))
    write_json(legacy.state / "documents.json", legacy.documents)
    start, end = (snapshot["basic"][key]["value"] for key in ("period_start", "period_end"))
    if not (start and end):
        start = end = ""
    comparison = await legacy.create_comparison(
        snapshot["title"], period_start=start, period_end=end, selection=selection
    )
    draft = comparison["draft"]
    for key in (
        "subjects_confirmed",
        "earnings_complete",
        "deductions_complete",
        "payment_scope_confirmed",
        "worked_time_kind",
        "slip_time_kind",
    ):
        draft[key] = snapshot["options"][key]
    draft["material_review"]["distinct_transactions_confirmed"] = snapshot["options"][
        "distinct_transactions_confirmed"
    ]
    for tid, transaction in draft["material_review"]["transactions"].items():
        transaction["selected"] = tid in transaction_ids
    await legacy.save_draft(comparison["id"], draft)
    return comparison["id"]


class WorkState(TypedDict, total=False):
    comparison_id: str
    job_id: str


async def run_work(store, rid, request_id):
    review = store.records[rid]
    request = next(r for r in review["requests"] if r["id"] == request_id)
    snapshot = copy.deepcopy(request["snapshot"])

    async def prepare(state):
        visit(request, "snapshot", "ready", status="running")
        store.save(review)
        cid = await bridge(store, snapshot)
        visit(request, "snapshot")
        request["comparison_id"] = cid
        store.save(review)
        return {"comparison_id": cid}

    async def execute(state):
        if request["capability"] == "prepare":
            return {}
        await store.legacy.confirm(state["comparison_id"])
        job = await store.legacy.create_job(state["comparison_id"], "analyze")
        request["job_id"] = job["id"]
        store.save(review)
        await store.legacy.tasks[job["id"]]
        actual = store.legacy.jobs[job["id"]]
        if actual["status"] != "completed":
            raise ValueError(actual.get("error") or "계산에 필요한 조건을 확인해 주세요.")
        return {"job_id": job["id"]}

    graph = StateGraph(WorkState)
    graph.add_node("prepare_confirmed_snapshot", prepare)
    graph.add_node("run_existing_engine", execute)
    graph.add_edge(START, "prepare_confirmed_snapshot")
    graph.add_edge("prepare_confirmed_snapshot", "run_existing_engine")
    graph.add_edge("run_existing_engine", END)
    try:
        await graph.compile().ainvoke({})
        request["state"] = "completed"
    except asyncio.CancelledError:
        request["state"] = "cancelled"
        if request.get("job_id"):
            await store.legacy.cancel_job(request["job_id"])
    except Exception as exc:
        request.update(
            state="failed",
            error=str(exc)
            if isinstance(exc, ValueError)
            else "업무 실행에 실패했습니다. 입력 조건을 확인해 주세요.",
        )
    finally:
        store.save(review)
