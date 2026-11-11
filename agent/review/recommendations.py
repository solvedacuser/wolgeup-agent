"""Read-only assessment, bounded evidence synthesis, and explicit provisional application."""

import asyncio
import copy
import json
import re
import time
from collections import defaultdict
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agent.review.flow import visit
from agent.review.model import RecommendationResponse
from agent.review.pipeline import identifier, manual_value
from agent.review.telemetry import begin_span, timestamp, usage_numbers
from agent.services.hashing import digest
from agent.services.identity import ambiguous_money, identity_key
from agent.services.normalization import normalize_scalar

RISK_TEXT = (
    "AI가 원문과 관련 자료를 종합한 임시 추천입니다. 오류가 있을 수 있으므로 원문 확인 후 확정해 주세요."
)
BUSINESS_IDS = {"business_number", "employer_id", "business_registration_number", "employer_business_number"}
SUMMARY_MONEY = {"gross", "net", "deduction", "contract_rate", "slip_rate"}


def entries(review):
    for doc in review["documents"]:
        if not doc.get("excluded"):
            for part in doc.get("parts", []):
                if not part.get("needs_review"):
                    for field in part["fields"]:
                        yield doc, part, field


def input_hash(review):
    # Confirmation and applying an existing proposal do not change its underlying evidence.
    return digest(
        {
            "basic": {
                k: f["value"]
                for k, f in review["basic"].items()
                if f.get("touched") and f.get("source_kind") != "ai_provisional"
            },
            "options": {k: v for k, v in review["options"].items() if k.endswith("part_id")},
            "documents": [
                {
                    "id": d["id"],
                    "generation": d.get("generation"),
                    "excluded": d.get("excluded"),
                    "parts": [
                        {
                            "id": p["id"],
                            "kind": p["document_type"],
                            "range": [p["start_page"], p["end_page"]],
                            "fields": [
                                {
                                    "id": f["id"],
                                    "raw": f.get("raw_value"),
                                    "unit": f["unit"],
                                    "candidates": f.get("candidates", []),
                                    "manual": f.get("value")
                                    if f.get("source_kind") == "user_statement"
                                    else None,
                                }
                                for f in p["fields"]
                            ],
                        }
                        for p in d.get("parts", [])
                    ],
                }
                for d in review["documents"]
            ],
        }
    )


def interpretations(raw, unit):
    values = set()
    try:
        value = normalize_scalar(raw, unit)
        if value is not None:
            values.add(value)
    except ValueError, TypeError:
        pass
    if unit.startswith("KRW") and ambiguous_money(raw):
        text = re.sub(r"[\s₩￦원]", "", raw)
        if re.fullmatch(r"-?\d[\d,.]*", text):
            values.add(text.replace(",", "").replace(".", ""))
    return values


def source_values(field):
    values = []
    for candidate in field.get("candidates", []):
        raw = candidate.get("raw_value")
        if not raw:
            continue
        try:
            value = normalize_scalar(raw, candidate.get("unit") or field["unit"])
        except ValueError, TypeError:
            value = None
        ambiguous = field["unit"].startswith("KRW") and ambiguous_money(raw)
        if value is None and not ambiguous:
            continue
        evidence = candidate.get("evidence") or {}
        values.append(
            dict(
                id=candidate["id"],
                field_id=field["id"],
                value=value,
                raw_value=raw,
                unit=candidate.get("unit") or field["unit"],
                page=evidence.get("page"),
                quote=evidence.get("quote", ""),
                method=evidence.get("method"),
                ambiguous=ambiguous,
                interpretations=sorted(interpretations(raw, candidate.get("unit") or field["unit"])),
                trusted=not any(
                    code in candidate.get("issues", [])
                    for code in (
                        "SOURCE_PAGE_INVALID",
                        "SOURCE_QUOTE_MISMATCH",
                        "SOURCE_VALUE_MISMATCH",
                        "NORMALIZATION_INVALID",
                        "TABLE_CELL_SOURCE_MISMATCH",
                        "TABLE_SOURCE_MISMATCH",
                    )
                ),
            )
        )
    return values


def assess(review):
    """All grouping is same-role; comparisons between contract and payslip remain distinct."""
    fields, sources, groups = {}, {}, defaultdict(list)
    all_entries = list(entries(review))
    for doc, part, field in all_entries:
        values = source_values(field)
        for value in values:
            value.update(document_id=doc["id"], filename=doc["title"], part_id=part["id"])
            sources[value["id"]] = value
        fields[field["id"]] = dict(
            target_id=field["id"],
            label=field["label"],
            document_id=doc["id"],
            part_id=part["id"],
            filename=doc["title"],
            kind="clear",
            reason="",
            source_ids=[v["id"] for v in values],
            suggested_value=None,
            unit=field["unit"],
        )
        item = fields[field["id"]]
        company = field["field_id"] == "employer_name"
        keys = {
            identity_key(v["value"], company=company) if field["unit"] == "text" else v["value"]
            for v in values
            if v["trusted"] and not v["ambiguous"]
        }
        if len(keys) > 1:
            item.update(kind="conflict", reason="동일 항목의 원문 후보가 실제로 다릅니다.")
        elif len({v["value"] for v in values}) > 1 and company and len(keys) == 1:
            item.update(
                kind="equivalent",
                reason="법인 표기와 공백 차이로 같은 회사명으로 보입니다.",
                suggested_value=next(v["value"] for v in values if v["trusted"]),
            )
        elif any(v["ambiguous"] for v in values) or field["validation_status"] == "needs_review":
            item.update(kind="ambiguous", reason="원문 표기나 인식 결과를 추가로 확인해야 합니다.")
        elif not field.get("value") and not values:
            item.update(kind="missing", reason="원문에서 확인할 근거가 부족합니다.")
        key = field["field_id"]
        if key in {"employer_name", "employee_name"} | BUSINESS_IDS:
            groups[("identity", key)].append(field["id"])
        elif key in SUMMARY_MONEY:
            values_by_key = {f["field_id"]: f.get("value") or f.get("raw_value") for f in part["fields"]}
            employee = identity_key(values_by_key.get("employee_name"))
            employer = identity_key(values_by_key.get("employer_name"), company=True)
            start, end = values_by_key.get("period_start"), values_by_key.get("period_end")
            if employee and employer and start and end:
                groups[(part["document_type"], key, field["unit"], employee, employer, start, end)].append(
                    field["id"]
                )

    conflicts = []
    for group_key, ids in groups.items():
        company = group_key == ("identity", "employer_name")
        known = [
            sources[sid]
            for fid in ids
            for sid in fields[fid]["source_ids"]
            if sources[sid]["trusted"] and not sources[sid]["ambiguous"]
        ]
        keys = {
            identity_key(s["value"], company=company) if group_key[0] == "identity" else s["value"]
            for s in known
        }
        if len(keys) > 1:
            for fid in ids:
                fields[fid].update(
                    kind="conflict",
                    suggested_value=None,
                    reason="대응하는 자료의 동일 항목 값이 다릅니다. 기준값을 직접 선택해 주세요.",
                )
            for fid in ids:
                fields[fid]["source_ids"] = list(
                    dict.fromkeys(fields[fid]["source_ids"] + [s["id"] for s in known])
                )
            if group_key[0] == "identity" and group_key[1] in BUSINESS_IDS:
                for _, _, field in all_entries:
                    if field["field_id"] == "employer_name":
                        fields[field["id"]].update(
                            kind="conflict",
                            suggested_value=None,
                            reason="사업자 식별정보가 다릅니다. 이름만으로 같은 사업장으로 판단할 수 없습니다.",
                        )
        elif company and len({s["value"] for s in known}) > 1:
            representative = sorted(known, key=lambda s: ("주식회사" not in s["value"], len(s["value"])))[0][
                "value"
            ]
            for fid in ids:
                if fields[fid]["kind"] != "conflict":
                    fields[fid].update(
                        kind="equivalent",
                        reason="법인 표기·공백·가상 자료 표식의 차이입니다.",
                        suggested_value=representative,
                        source_ids=list(dict.fromkeys(fields[fid]["source_ids"] + [s["id"] for s in known])),
                    )
        # A suspicious amount may be suggested only from unambiguous corresponding evidence.
        if group_key[0] != "identity" and len(keys) == 1:
            for fid in ids:
                if fields[fid]["kind"] == "ambiguous":
                    ambiguous_sources = [
                        sources[sid] for sid in fields[fid]["source_ids"] if sources[sid]["ambiguous"]
                    ]
                    if ambiguous_sources and not any(
                        known[0]["value"] in s["interpretations"] for s in ambiguous_sources
                    ):
                        fields[fid].update(
                            kind="conflict",
                            reason="금액의 구분 기호를 달리 해석해도 대응하는 원문 값과 다릅니다.",
                        )
                    else:
                        fields[fid]["suggested_value"] = known[0]["value"]
                    fields[fid]["source_ids"] = list(
                        dict.fromkeys(fields[fid]["source_ids"] + [s["id"] for s in known])
                    )
    for item in fields.values():
        if item["kind"] == "ambiguous" and not item["suggested_value"]:
            visual = [
                sources[sid]
                for sid in item["source_ids"]
                if sources[sid]["trusted"]
                and not sources[sid]["ambiguous"]
                and sources[sid]["method"] == "vision_unverified"
            ]
            if len({v["value"] for v in visual}) == 1:
                ambiguous_sources = [sources[sid] for sid in item["source_ids"] if sources[sid]["ambiguous"]]
                if ambiguous_sources and not any(
                    visual[0]["value"] in s["interpretations"] for s in ambiguous_sources
                ):
                    item.update(
                        kind="conflict",
                        reason="표기 차이로 설명되지 않는 다른 금액이 원본 재검증에서 확인됐습니다.",
                    )
                else:
                    item["suggested_value"] = visual[0]["value"]
                    item["reason"] = "원본 페이지 재검증에서 확인된 값을 추천합니다."
        if item["kind"] == "conflict":
            conflict_id = "conflict-" + digest([item["target_id"], sorted(item["source_ids"])])[:16]
            item["conflict_id"] = conflict_id
            field = next(f for _, _, f in all_entries if f["id"] == item["target_id"])
            item["resolved"] = (
                field.get("conflict_acknowledgement") == conflict_id and field["review_state"] == "confirmed"
            )
            conflicts.append(copy.deepcopy(item))
    # Basic information uses proposals too, while source-level spelling is retained.
    for key, basic in review["basic"].items():
        relevant = [
            item
            for _, part, f in all_entries
            if f["field_id"] == key
            and (
                key not in ("period_start", "period_end")
                or part["document_type"] in ("payslip", "attendance")
            )
            and (key != "payment_date" or part["document_type"] == "payslip")
            for item in [fields[f["id"]]]
        ]
        if not relevant:
            continue
        values = [
            sources[sid]
            for item in relevant
            for sid in item["source_ids"]
            if sources[sid]["trusted"] and not sources[sid]["ambiguous"]
        ]
        canonical = {
            identity_key(s["value"], company=True) if key == "employer_name" else s["value"] for s in values
        }
        kind = (
            "conflict"
            if len(canonical) > 1
            else "equivalent"
            if key == "employer_name" and len({s["value"] for s in values}) > 1
            else "clear"
        )
        if any(i["kind"] == "conflict" for i in relevant):
            kind = "conflict"
        item = dict(
            target_id="basic:" + key,
            label=key,
            kind=kind,
            reason="자료별 원문 값이 다릅니다. 기준값을 직접 선택해 주세요."
            if kind == "conflict"
            else "자료에서 확인한 기본 정보입니다.",
            source_ids=list(dict.fromkeys(s["id"] for s in values)),
            suggested_value=values[0]["value"] if len(canonical) == 1 and kind != "conflict" else None,
            unit="category" if key == "pay_type" else "date" if "date" in key or "period_" in key else "text",
        )
        fields[item["target_id"]] = item
        if kind == "conflict":
            item.update(conflict_id="conflict-" + digest([item["target_id"], item["source_ids"]])[:16])
            item["resolved"] = basic.get("conflict_acknowledgement") == item["conflict_id"] and basic.get(
                "confirmed", False
            )
            conflicts.append(copy.deepcopy(item))
    return dict(fields=fields, sources=sources, conflicts=conflicts)


class RecommendationState(TypedDict, total=False):
    proposals: list[dict]


class ReviewRecommendations:
    def __init__(self, store):
        self.store, self.tasks = store, {}
        for job in store.jobs.values():
            if job.get("kind") == "recommendation" and job["status"] in ("queued", "running"):
                job.update(status="interrupted", error="추천 검토가 중단되었습니다. 다시 요청할 수 있습니다.")
                store.save_job(job)

    def view(self, review):
        assessment = assess(review)
        current = input_hash(review)
        saved = review.get("recommendations") or {}
        proposals = copy.deepcopy(saved.get("proposals", [])) if saved.get("input_hash") == current else []
        jobs = sorted(
            [
                j
                for j in self.store.jobs.values()
                if j.get("kind") == "recommendation" and j["review_id"] == review["id"]
            ],
            key=lambda j: j["created_at"],
        )
        return dict(
            **assessment,
            proposals=proposals,
            input_hash=current,
            status=jobs[-1]["status"] if jobs else "not_requested",
            active_job=next((j["id"] for j in reversed(jobs) if j["status"] in ("running", "queued")), None),
            stale=bool(saved and saved.get("input_hash") != current),
            warning=(jobs[-1].get("warning") or jobs[-1].get("error")) if jobs else None,
            risk_text=RISK_TEXT,
        )

    async def start(self, rid, payload=None, *, automatic=False):
        payload = payload or {}
        async with self.store.lock(rid):
            review = self.store.records[rid]
            if not automatic:
                self.store.check_revision(review, payload.get("revision"))
            if automatic and not any(
                i["kind"] in ("equivalent", "ambiguous", "conflict")
                for i in assess(review)["fields"].values()
            ):
                return self.view(review)
            fingerprint = input_hash(review)
            for job in self.store.jobs.values():
                if (
                    job.get("kind") == "recommendation"
                    and job.get("review_id") == rid
                    and job.get("input_hash") == fingerprint
                    and job["status"] in ("queued", "running")
                ):
                    return self.view(review)
            job = dict(
                id=identifier("recommend"),
                review_id=rid,
                kind="recommendation",
                stage="recommendation",
                status="queued",
                created_at=timestamp(),
                trigger="automatic" if automatic else "recommendation",
                model=self.store.settings.extraction_model,
                usage={},
                execution_trace=[],
                route=[],
                input_hash=fingerprint,
            )
            self.store.jobs[job["id"]] = job
            self.store.save_job(job)
            self.tasks[job["id"]] = asyncio.create_task(self.run(job, copy.deepcopy(review)))
            return self.view(review)

    async def run(self, job, snapshot):
        store = self.store
        assessment = assess(snapshot)
        proposals = []
        targets = [
            item
            for item in assessment["fields"].values()
            if item["kind"] in ("equivalent", "ambiguous")
            or item["target_id"].startswith("basic:")
            and item["suggested_value"]
        ]

        def persist():
            store.save_job(job)

        def propose(item, value, reason, ids, origin):
            proposals.append(
                dict(
                    id=identifier("proposal"),
                    target_id=item["target_id"],
                    value=value,
                    unit=item["unit"],
                    reason=reason,
                    source_ids=ids,
                    origin=origin,
                    input_hash=job["input_hash"],
                )
            )

        async def compare(state):
            event = begin_span("tool", "recommendation", "recommend.compare_spelling")
            event["flow_node"] = "recommend_compare"
            job["execution_trace"].append(event)
            started = time.monotonic()
            visit(job, "recommend_compare", "recommend_start")
            for item in targets:
                if item["suggested_value"]:
                    propose(
                        item, item["suggested_value"], item["reason"], item["source_ids"], "evidence_rule"
                    )
            event.update(
                status="completed",
                finished_at=timestamp(),
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return {}

        async def synthesize(state):
            visit(job, "recommend_evidence", "recommend_compare")
            unresolved = [item for item in targets if not item["suggested_value"]][:30]
            if not unresolved:
                return {}
            # Existing visual verifier candidates are included. No implicit page/image uploads here.
            candidates, size = [], 0
            for item in unresolved:
                sources = []
                for sid in item["source_ids"][:8]:
                    source = copy.deepcopy(assessment["sources"][sid])
                    source["quote"] = source["quote"][:500]
                    source["raw_value"] = source["raw_value"][:1000]
                    sources.append(source)
                candidate = {**item, "sources": sources}
                cost = len(json.dumps(candidate, ensure_ascii=False))
                if size + cost > 60000:
                    break
                size += cost
                candidates.append(candidate)
            event = begin_span("agent", "recommendation", "model.recommend")
            event["flow_node"] = "recommend_model"
            job["execution_trace"].append(event)
            visit(job, "recommend_model", "recommend_evidence", status="running")
            persist()
            started = time.monotonic()
            try:
                async with store.legacy.semaphore:
                    raw, usage = await store.model.invoke("recommend", {"targets": candidates})
                event.update(
                    usage=usage_numbers(usage),
                    usage_status="reported" if usage_numbers(usage) else "unavailable",
                )
                job["usage"] = usage_numbers(usage)
                response = RecommendationResponse.model_validate(raw)
                for recommendation in response.recommendations:
                    item = next((t for t in unresolved if t["target_id"] == recommendation.target_id), None)
                    if not item or not recommendation.value or not recommendation.source_ids:
                        continue
                    allowed = [
                        assessment["sources"][sid]
                        for sid in recommendation.source_ids
                        if sid in item["source_ids"]
                    ]
                    support = [
                        s
                        for s in allowed
                        if s["trusted"] and not s["ambiguous"] and s["value"] == recommendation.value
                    ]
                    # A model cannot turn punctuation speculation into monetary evidence.
                    if not support:
                        continue
                    try:
                        value = manual_value(recommendation.value, item["unit"])
                    except ValueError:
                        continue
                    propose(item, value, recommendation.reason, [s["id"] for s in support], "model")
                event["status"] = "completed"
                visit(job, "recommend_model")
            except asyncio.CancelledError:
                event.update(status="interrupted", usage_status="unavailable")
                raise
            except Exception as exc:
                event.update(
                    status="failed",
                    usage=usage_numbers(getattr(exc, "usage", None)),
                    usage_status="not_sent"
                    if getattr(exc, "api_call_started", True) is False
                    else "unavailable",
                )
                job["usage"] = event["usage"]
                visit(job, "recommend_model", status="failed")
                job["warning"] = "추가 AI 검토를 완료하지 못했습니다. 근거로 검증된 추천만 제공합니다."
            finally:
                event.update(finished_at=timestamp(), duration_ms=round((time.monotonic() - started) * 1000))
                persist()
            return {}

        async def publish(state):
            previous = (
                "recommend_model"
                if any(e.get("flow_node") == "recommend_model" for e in job["execution_trace"])
                else "recommend_evidence"
            )
            visit(job, "recommend_gate", previous)
            if any(not c.get("resolved") for c in assessment["conflicts"]):
                visit(job, "recommend_conflict", "recommend_gate", status="waiting")
            if proposals:
                visit(job, "recommend_proposals", "recommend_gate")
            async with store.lock(snapshot["id"]):
                current = store.records[snapshot["id"]]
                if input_hash(current) != job["input_hash"]:
                    job["status"] = "superseded"
                    return {}
                current["recommendations"] = dict(
                    input_hash=job["input_hash"],
                    proposals=proposals,
                    job_id=job["id"],
                    created_at=timestamp(),
                )
                store.save(current)
            visit(
                job,
                "recommend_apply",
                "recommend_proposals" if proposals else "recommend_gate",
                status="waiting",
            )
            return {}

        graph = StateGraph(RecommendationState)
        for name, function in (("compare", compare), ("synthesize", synthesize), ("publish", publish)):
            graph.add_node(name, function)
        graph.add_edge(START, "compare")
        graph.add_edge("compare", "synthesize")
        graph.add_edge("synthesize", "publish")
        graph.add_edge("publish", END)
        job["status"] = "running"
        visit(job, "recommend_start")
        persist()
        try:
            await graph.compile().ainvoke({})
            if job["status"] != "superseded":
                job["status"] = "completed"
        except asyncio.CancelledError:
            job["status"] = "interrupted"
        except Exception:
            job.update(status="failed", error="추천 검토에 실패했습니다. 다시 요청해 주세요.")
        finally:
            job["finished_at"] = timestamp()
            persist()

    async def apply(self, rid, payload):
        async with self.store.lock(rid):
            review = self.store.records[rid]
            self.store.check_revision(review, payload.get("revision"))
            current = self.view(review)
            ids = payload.get("recommendation_ids")
            if (
                not isinstance(ids, list)
                or not ids
                or len(ids) > 2000
                or any(not isinstance(v, str) for v in ids)
            ):
                raise ValueError("적용할 추천을 선택해 주세요.")
            by_id = {p["id"]: p for p in current["proposals"]}
            if set(ids) - by_id.keys():
                raise ValueError("자료가 변경되어 추천이 만료되었습니다. 추천을 다시 검토해 주세요.")
            targets = []
            for pid in dict.fromkeys(ids):
                proposal = by_id[pid]
                tid = proposal["target_id"]
                field = (
                    review["basic"][tid[6:]]
                    if tid.startswith("basic:")
                    else self.store.find_field(review, tid)[2]
                )
                if field.get("touched") or field.get("confirmed") or field.get("review_state") == "confirmed":
                    raise ValueError("직접 수정하거나 확정한 값은 AI가 덮어쓰지 않습니다.")
                if current["fields"][tid]["kind"] == "conflict":
                    raise ValueError("실제 충돌은 추천으로 채울 수 없습니다. 개별 확인해 주세요.")
                value = manual_value(
                    proposal["value"],
                    proposal["unit"],
                    key=tid[6:] if tid.startswith("basic:") else field["field_id"],
                )
                targets.append((field, proposal, value))
            for field, proposal, value in targets:
                field.setdefault("history", []).append(
                    dict(
                        event="ai_recommendation_applied",
                        at=timestamp(),
                        before=copy.deepcopy({k: v for k, v in field.items() if k != "history"}),
                        proposal=copy.deepcopy(proposal),
                    )
                )
                field.update(
                    value=value, source_kind="ai_provisional", ai_recommendation=copy.deepcopy(proposal)
                )
                if "review_state" in field:
                    field.update(review_state="unreviewed", revision=field["revision"] + 1)
                else:
                    field["confirmed"] = False
            self.store.change(review)
            job = self.store.jobs.get(review["recommendations"]["job_id"])
            if job:
                visit(job, "recommend_apply", status="completed")
                visit(job, "recommend_confirm", "recommend_apply", status="waiting")
                self.store.save_job(job)
        return self.store.view(rid)
