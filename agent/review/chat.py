"""Project-scoped, read-only conversation graph over saved review evidence."""

import asyncio
import copy
import json
import re
import time
from typing import TypedDict
from urllib.parse import urlencode

from langgraph.graph import END, START, StateGraph

from agent.review.flow import visit
from agent.review.model import ChatAnswer
from agent.review.pipeline import identifier
from agent.review.recommendations import assess
from agent.review.registry import ISSUES
from agent.review.telemetry import begin_span, timestamp, usage_numbers
from agent.services.facts import COMPARISON_SIDES, STATUS_LABELS
from agent.services.local_json import read_json, write_json
from agent.services.messages import RULE_LABELS, reason_text

GUIDE = (
    "기본 정보 → 자료 등록 → 내용 확인 → 확정한 값으로 문서 대조 순서로 진행합니다. "
    "자료 등록에서 PDF·JPG·PNG를 추가하거나 기존 자료를 연결합니다. "
    "근로계약서는 약정 급여·조건, 급여명세서는 지급·공제·실수령액, 근태기록은 출퇴근·휴게·근무시간, "
    "입금내역은 실제 급여 입금 대조에 사용합니다. 목적에 필요한 자료부터 추가할 수 있습니다. "
    "처리 실패 시 파일의 방향·선명도·암호 여부를 확인하고 자료 카드에서 다시 처리하거나 다른 파일로 교체합니다. "
    "유형이 모호하면 유형·페이지를 지정하거나 직접 입력합니다. 내용 확인에서 원문 위치와 후보를 보고 "
    "확인 필요·값 없음 필터로 좁혀 값을 수정하거나 보류합니다. 필요한 경우 선택 항목을 재검증합니다. "
    "원문에 없는 값은 추측하지 않습니다. 값을 입력해도 자동 확정되지 않으므로 사용자가 확정해야 합니다. "
    "기본 정보도 확인·확정하고 동일 근로자·사업장 여부와 기준 자료를 확인합니다. "
    "문서 대조 요청은 필요한 확정값이 부족하면 대기하며, 요건이 충족되면 재개됩니다. "
    "표준 임금 계산 조건 입력은 준비된 비교 입력 화면에서 지원 조건을 직접 확인하는 기능입니다. "
    "결과 화면에서 대조 결과·확인 질문·회사 문의 초안·보고서 PDF·선택 원본 ZIP·쉬운 용어를 볼 수 있습니다. "
    "계산은 지원 범위와 확정된 입력에 한하며 자료상 차이만으로 체불이나 위법을 확정하지 않습니다. "
    "챗봇은 현재 검토를 조회해 안내하며 자료 수정·확정·업로드·작업 실행을 대신하지 않습니다."
)


STEP_TITLES = {
    "prepare": "자료 준비",
    "review": "원문·충돌 확인",
    "confirm": "값 확정",
    "compare": "계산·대조",
    "results": "결과 검토",
}


def workflow_progress(review):
    docs = [d for d in review["documents"] if not d.get("excluded")]
    fields = [f for d in docs for p in d["parts"] for f in p["fields"]]
    conflicts = any(not c.get("resolved") for c in assess(review)["conflicts"])
    complete = next(
        (
            r
            for r in reversed(review["requests"])
            if r["state"] == "completed" and r.get("capability") == "compare" and not r.get("stale")
        ),
        None,
    )
    relevant = [f for f in fields if f.get("value")]
    confirmed = (
        bool(relevant)
        and all(f["review_state"] == "confirmed" for f in relevant)
        and all(review["basic"][k]["confirmed"] for k in ("period_start", "period_end"))
    )
    statuses = dict(
        prepare="completed" if docs else "current",
        review="blocked" if conflicts else "completed" if confirmed else "current" if fields else "waiting",
        confirm="completed"
        if confirmed and not conflicts
        else "current"
        if fields and not conflicts
        else "waiting",
        compare="completed" if complete else "current" if confirmed and not conflicts else "waiting",
        results="current" if complete else "waiting",
    )
    return {
        key: dict(title=title, status=statuses[key], number=index + 1)
        for index, (key, title) in enumerate(STEP_TITLES.items())
    }


def validated_steps(raw_steps, context):
    sources = {s["id"]: s for s in context["sources"]}
    output, progress = [], context["workflow"]
    for item in sorted(raw_steps, key=lambda s: progress[s["stage"]]["number"]):
        step = {k: v for k, v in item.items() if k != "source_ids"}
        step.update(
            stage_title=progress[item["stage"]]["title"],
            status=progress[item["stage"]]["status"],
            number=progress[item["stage"]]["number"],
            sources=[
                {k: v for k, v in sources[sid].items() if k != "data"}
                for sid in dict.fromkeys(item["source_ids"])
                if sid in sources
            ],
        )
        output.append(step)
    return output


def evidence_context(store, review, question, focus=None):
    """Retrieve only this review's active documents and its pinned business results."""
    rid = review["id"]
    base = "/review/" + rid
    sources = []

    def add(sid, label, data, href, **location):
        sources.append(dict(id=sid, label=label, data=data, href=href, **location))

    add("workflow", "검토 진행 방법", GUIDE, base + "/materials")
    assessment = store.recommendations.view(review)
    add(
        "recommendations",
        "AI 추천·필수 확인",
        {
            "conflicts": assessment["conflicts"],
            "proposals": assessment["proposals"],
            "risk_text": assessment["risk_text"],
        },
        base + "/verify",
    )
    add(
        "requirements",
        "현재 실행 조건",
        {"capabilities": store.capabilities(review), "options": review["options"]},
        base + "/verify",
    )
    documents = [d for d in review["documents"] if not d.get("excluded")]
    field_sources, page_sources = [], []
    totals = dict(documents=len(documents), fields=0, confirmed=0, needs_review=0, missing=0)
    for doc in documents:
        did = doc["id"]
        job = store.jobs.get(doc.get("job_id"), {})
        if len(sources) < 80:
            add(
                did,
                doc["title"],
                {
                    "filename": doc["title"],
                    "pages": doc["pages"],
                    "status": job.get("status"),
                    "error": job.get("error"),
                    "parts": [
                        {
                            k: p.get(k)
                            for k in ("label", "document_type", "start_page", "end_page", "needs_review")
                        }
                        for p in doc["parts"]
                    ],
                },
                base + "/verify?" + urlencode({"document": did}),
                document_id=did,
            )
        for part in doc["parts"]:
            for field in part["fields"]:
                totals["fields"] += 1
                totals["confirmed"] += field["review_state"] == "confirmed"
                totals["needs_review"] += field["validation_status"] == "needs_review"
                totals["missing"] += not bool(field.get("value"))
                evidence = field.get("evidence") or {}
                data = {
                    k: field.get(k)
                    for k in (
                        "field_id",
                        "label",
                        "value",
                        "unit",
                        "review_state",
                        "validation_status",
                        "source_kind",
                    )
                }
                data.update(
                    filename=doc["title"],
                    document_type=part["document_type"],
                    page=evidence.get("page"),
                    quote=evidence.get("quote", "")[:400],
                    issues=[ISSUES.get(code, code) for code in field.get("issues", [])],
                    candidates=[
                        {k: c.get(k) for k in ("value", "raw_value", "unit", "validation_status", "issues")}
                        for c in field.get("candidates", [])[-3:]
                    ],
                )
                field_sources.append(
                    dict(
                        id=field["id"],
                        label=doc["title"] + " · " + field["label"],
                        data=data,
                        href=base + "/verify?" + urlencode({"document": did, "field": field["id"]}),
                        document_id=did,
                        field_id=field["id"],
                        page=evidence.get("page"),
                    )
                )
        for page in (job.get("reading") or {}).get("pages", []):
            page_sources.append(
                dict(
                    id=did + "-page-" + str(page["page"]),
                    label=doc["title"] + f" · {page['page']}쪽 판독문",
                    data={"filename": doc["title"], "page": page["page"], "text": page.get("text", "")},
                    href=base + "/verify?" + urlencode({"document": did, "page": page["page"]}),
                    document_id=did,
                    page=page["page"],
                )
            )

    requests = []
    for request in review["requests"]:
        requests.append(
            {k: request.get(k) for k in ("id", "capability", "state", "missing", "error", "stale")}
        )
    latest = next(
        (
            r
            for r in reversed(review["requests"])
            if r.get("comparison_id")
            and r["state"] == "completed"
            and store.legacy.cases.get(r["comparison_id"], {}).get("analysis")
        ),
        None,
    )
    if latest:
        case = store.legacy.cases.get(latest["comparison_id"], {})
        result = case.get("analysis") or {}
        stale = bool(
            latest.get("stale")
            or case.get("stale")
            or case.get("dirty")
            or result.get("freshness") == "stale"
        )
        href = "/results?comparison=" + latest["comparison_id"]
        add(
            "result",
            "최근 실행 결과",
            {"stale": stale, "state": latest["state"], "has_analysis": bool(result)},
            href,
        )
        for index, comparison in enumerate(result.get("comparisons", [])):
            data = copy.deepcopy(comparison)
            data.update(
                label=RULE_LABELS.get(comparison["rule_id"], comparison["rule_id"]),
                sides=COMPARISON_SIDES.get(comparison["rule_id"]),
                stale=stale,
                status_label=STATUS_LABELS.get(comparison["status"], comparison["status"]),
                reasons=[reason_text(code) for code in comparison.get("reason_codes", [])],
            )
            add("comparison-" + str(index), data["label"] + " 대조 결과", data, href)
        for index, item in enumerate(result.get("questions", [])[:30]):
            add("question-" + str(index), "결과의 확인 질문", item, href)
        if result.get("wage_section"):
            add("wages", "지원 범위의 임금 계산 결과", {"stale": stale, **result["wage_section"]}, href)

    words = set(re.findall(r"[가-힣A-Za-z0-9_]{2,}", question.lower()))
    words.update(re.sub(r"(에서|으로|은|는|이|가|을|를|과|와|도)$", "", word) for word in list(words))

    def relevance(source):
        text = json.dumps(source["data"], ensure_ascii=False).lower()
        label = source["label"].lower()
        score = sum(10 if word in label else 2 for word in words if word and (word in text or word in label))
        if focus and source.get("field_id") == focus.get("field_id"):
            score += 100
        if focus and source.get("document_id") == focus.get("document_id"):
            score += 5
        return score

    # Prefer requested fields, then suspect/usable facts. Keep explicit omitted counts.
    field_sources.sort(
        key=lambda s: (
            relevance(s),
            s["data"]["validation_status"] == "needs_review",
            bool(s["data"]["value"]),
        ),
        reverse=True,
    )
    selected_fields = field_sources[:100]
    sources.extend(selected_fields)
    page_sources.sort(key=relevance, reverse=True)
    for source in page_sources[:6]:
        text = source["data"]["text"]
        positions = [text.lower().find(word) for word in words if word in text.lower()]
        start = max(0, min(positions, default=0) - 300)
        source["data"]["text"] = text[start : start + 1400]
        source["data"]["excerpt"] = True
        sources.append(source)
    # Keep model input bounded even for many documents or large manually entered clauses.
    priority = {"workflow": 0, "requirements": 0, "recommendations": 0, "result": 1, "wages": 1}
    ordered = sorted(
        sources,
        key=lambda s: priority.get(s["id"], 1 if s["id"].startswith(("comparison-", "question-")) else 2),
    )
    bounded, used = [], 0
    for source in ordered:
        cost = len(json.dumps(source, ensure_ascii=False))
        if used + cost <= 70000:
            bounded.append(source)
            used += cost
    kept_ids = {s["id"] for s in bounded}
    return dict(
        review_id=rid,
        revision=review["revision"],
        title=review["title"],
        basic=review["basic"],
        workflow=workflow_progress(review),
        totals=totals,
        requests=requests[-20:],
        sources=bounded,
        retrieval={
            "omitted_fields": sum(s["id"] not in kept_ids for s in field_sources),
            "omitted_page_excerpts": sum(s["id"] not in kept_ids for s in page_sources),
            "omitted_sources_due_to_budget": len(sources) - len(bounded),
            "note": "판독문은 검색된 일부 구간이며 전체 원문을 시각적으로 확인한 결과가 아닙니다.",
        },
        upload_limits={
            "max_bytes": store.settings.max_file_bytes,
            "max_pages": store.settings.max_pages,
            "types": ["PDF", "JPG", "PNG"],
        },
    )


def local_guidance(context, question):
    """Useful grounded assistance if the model is unavailable; never masquerades as AI."""
    sources = context["sources"]
    counts = context["totals"]
    lines = [f"현재 검토에는 자료 {counts['documents']}개, 확정한 항목 {counts['confirmed']}개가 있습니다."]
    ids = ["requirements", "workflow"]
    comparisons = [s for s in sources if s["id"].startswith("comparison-")]
    if any(word in question for word in ("비교", "대조", "차이", "결과", "문제")):
        if comparisons:
            for source in comparisons:
                d = source["data"]
                lines.append(
                    f"• {d['label']}: {d['status_label']} · 왼쪽 {d.get('lhs')} / 오른쪽 {d.get('rhs')} / 기록된 차이 {d.get('delta')} {d.get('unit', '')}"
                )
                if d["stale"]:
                    lines.append("입력이 바뀐 이전 결과입니다. 현재 값으로 다시 대조해 주세요.")
                lines.extend(d["reasons"])
                ids.append(source["id"])
        else:
            lines.append(
                "아직 완료된 대조 결과가 없습니다. 내용 확인에서 필요한 값을 확정한 뒤 문서 대조를 실행해 주세요."
            )
    if any(word in question for word in ("인식", "확인", "입력", "누락", "수정")):
        fields = [
            s
            for s in sources
            if s.get("field_id")
            and (not s["data"]["value"] or s["data"]["validation_status"] == "needs_review")
        ][:4]
        for source in fields:
            d = source["data"]
            lines.append(
                f"• {source['label']}: {' / '.join(d['issues']) or '원문 확인 필요'}. 원문과 후보를 확인해 수정하고 필요한 항목을 확정해 주세요."
            )
            ids.append(source["id"])
    requirements = next(s for s in sources if s["id"] == "requirements")["data"]
    missing = next(c["missing"] for c in requirements["capabilities"] if c["id"] == "compare")
    if missing:
        lines.append("다음에 확인할 내용:\n" + "\n".join("• " + item for item in missing))
    else:
        lines.append("내용 확인에서 ‘확정한 값으로 문서 대조’를 실행할 수 있습니다.")
    if any(word in question for word in ("파일", "업로드", "자료")) or not counts["documents"]:
        lines.append(
            "계약 조건은 근로계약서, 지급·공제는 급여명세서, 근무시간은 근태기록, 입금 대조는 입금내역을 자료 등록에 추가해 주세요. PDF·JPG·PNG를 지원합니다."
        )
    lines.append(
        "AI 답변을 연결하지 못해 저장된 상태를 기준으로 안내했습니다. 잠시 후 다시 질문할 수 있습니다."
    )
    steps = []
    if not counts["documents"]:
        steps.append(
            dict(
                stage="prepare",
                title="필요한 자료 추가",
                description="자료 등록에서 목적에 맞는 계약서·명세서·근태·입금 기록을 추가하세요.",
                reason="문서가 있어야 현재 상태를 확인할 수 있습니다.",
                source_ids=["workflow"],
            )
        )
    else:
        steps.extend(
            [
                dict(
                    stage="review",
                    title="원문과 필수 확인 항목 검토",
                    description="필수 확인 항목을 먼저 확인하고 AI 추천은 근거를 본 뒤 ‘AI가 채우기’로 적용하세요.",
                    reason="원문과 추천값을 구분해 검토해야 합니다.",
                    source_ids=["recommendations"],
                ),
                dict(
                    stage="confirm",
                    title="필요한 값 확정",
                    description="기본 정보와 대조에 사용할 값을 확인·확정하세요. 실제 충돌은 개별 확인이 필요합니다.",
                    reason="미확정 값은 계산·대조에 사용하지 않습니다.",
                    source_ids=["requirements"],
                ),
            ]
        )
    if any(word in question for word in ("비교", "대조", "차이", "결과")):
        steps.append(
            dict(
                stage="results" if comparisons else "compare",
                title="대조 결과 확인" if comparisons else "확정값으로 대조 실행",
                description="기존 대조 결과와 확인 질문을 확인하세요."
                if comparisons
                else "내용 확인 아래에서 ‘확정한 값으로 문서 대조’를 실행하세요.",
                reason="실제 실행 결과로 차이를 확인합니다.",
                source_ids=["result"] if comparisons else ["requirements"],
            )
        )
    return dict(answer="\n\n".join(lines), source_ids=list(dict.fromkeys(ids))[:8], steps=steps)


class ConversationState(TypedDict, total=False):
    context: dict
    answer: dict
    fallback: bool


class ReviewChat:
    def __init__(self, store):
        self.store = store
        self.directory = store.directory / "chats"
        self.directory.mkdir(exist_ok=True)
        self.records = {p.stem: read_json(p) for p in self.directory.glob("review-*.json")}
        self.locks, self.tasks = {}, {}
        for job in store.jobs.values():
            if job.get("kind") == "chat" and job["status"] in ("queued", "running"):
                job.update(status="interrupted", error="대화 중 서버가 중단되었습니다. 다시 질문해 주세요.")
                store.save_job(job)

    def record(self, rid):
        self.store.records[rid]
        return self.records.setdefault(rid, dict(review_id=rid, messages=[]))

    def save(self, record):
        write_json(self.directory / (record["review_id"] + ".json"), record)

    def view(self, rid):
        result = copy.deepcopy(self.record(rid))
        jobs = sorted(
            [j for j in self.store.jobs.values() if j.get("kind") == "chat" and j["review_id"] == rid],
            key=lambda j: j["created_at"],
        )
        active = next((j for j in jobs if j["status"] in ("queued", "running")), None)
        result["active_job"] = active["id"] if active else None
        result["last_job"] = {k: jobs[-1].get(k) for k in ("id", "status", "error")} if jobs else None
        result["messages"] = result["messages"][-80:]
        return result

    async def send(self, rid, payload):
        if not isinstance(payload, dict) or set(payload) - {
            "message",
            "request_id",
            "page",
            "document_id",
            "field_id",
        }:
            raise ValueError("질문 형식을 확인해 주세요.")
        message, request_id = payload.get("message"), payload.get("request_id")
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000:
            raise ValueError("질문은 1~4,000자로 입력해 주세요.")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{8,80}", request_id):
            raise ValueError("대화 요청 ID를 확인해 주세요.")
        if payload.get("page") not in (
            None,
            "home",
            "new",
            "materials",
            "verify",
            "library",
            "result",
            "compare",
            "results",
            "questions",
            "inquiry",
            "reports",
            "glossary",
        ):
            raise ValueError("현재 화면 정보를 확인해 주세요.")
        lock = self.locks.setdefault(rid, asyncio.Lock())
        async with lock:
            record = self.record(rid)
            if any(m.get("request_id") == request_id for m in record["messages"]):
                return self.view(rid)
            if self.view(rid)["active_job"]:
                raise ValueError("이 검토의 답변을 작성하고 있습니다. 잠시 후 질문해 주세요.")
            async with self.store.lock(rid):
                snapshot = copy.deepcopy(self.store.records[rid])
            focus = {k: payload.get(k) for k in ("document_id", "field_id")}
            allowed_documents = [d for d in snapshot["documents"] if not d.get("excluded")]
            if focus["document_id"] and focus["document_id"] not in {d["id"] for d in allowed_documents}:
                raise ValueError("현재 검토의 자료를 선택해 주세요.")
            if focus["field_id"] and focus["field_id"] not in {
                f["id"] for d in allowed_documents for p in d["parts"] for f in p["fields"]
            }:
                raise ValueError("현재 검토의 항목을 선택해 주세요.")
            job = dict(
                id=identifier("chat"),
                kind="chat",
                review_id=rid,
                status="queued",
                stage="chat",
                created_at=timestamp(),
                model=self.store.settings.explanation_model,
                trigger="chat",
                execution_trace=[],
                route=[],
                usage={},
                revision=snapshot["revision"],
                screen=payload.get("page"),
                focus=focus,
            )
            record["messages"].append(
                dict(
                    id=identifier("message"),
                    role="user",
                    content=message.strip(),
                    created_at=timestamp(),
                    request_id=request_id,
                    job_id=job["id"],
                )
            )
            self.store.jobs[job["id"]] = job
            self.save(record)
            self.store.save_job(job)
            self.tasks[job["id"]] = asyncio.create_task(self.run(job, snapshot, message.strip()))
            return self.view(rid)

    async def run(self, job, snapshot, question):
        record = self.record(job["review_id"])

        def persist():
            self.store.save_job(job)

        def trace(name, node, kind="tool"):
            span = begin_span(kind, "chat", name)
            span.update(flow_node=node)
            job["execution_trace"].append(span)
            return span, time.monotonic()

        def finish(span, started, status="completed"):
            span.update(
                status=status, finished_at=timestamp(), duration_ms=round((time.monotonic() - started) * 1000)
            )
            persist()

        async def retrieve(state):
            visit(job, "chat_context", "chat_start")
            span, started = trace("review.retrieve", "chat_context")
            search = " ".join(m["content"] for m in record["messages"][-6:] if m["role"] == "user")
            context = evidence_context(self.store, snapshot, search, job["focus"])
            context.update(screen=job["screen"], focused_item=job["focus"])
            finish(span, started)
            return {"context": context}

        async def answer(state):
            visit(job, "chat_answer", "chat_context", status="running")
            span, started = trace("model.chat", "chat_answer", "agent")
            persist()
            try:
                response, usage = await self.store.model.invoke(
                    "chat",
                    {
                        "context": state["context"],
                        "question": question,
                        "history": [
                            {"role": m["role"], "content": m["content"][:4000]}
                            for m in record["messages"][-13:-1]
                        ],
                    },
                )
                span.update(
                    usage=usage_numbers(usage),
                    usage_status="reported" if usage_numbers(usage) else "unavailable",
                )
                job["usage"] = usage_numbers(usage)
                parsed = ChatAnswer.model_validate(response).model_dump()
                finish(span, started)
                visit(job, "chat_answer")
                return {"answer": parsed, "fallback": False}
            except asyncio.CancelledError:
                span.update(usage_status="unavailable")
                finish(span, started, "interrupted")
                raise
            except Exception as exc:
                usage = usage_numbers(getattr(exc, "usage", None)) or span.get("usage", {})
                span.update(
                    usage=usage,
                    usage_status="reported"
                    if usage
                    else "not_sent"
                    if getattr(exc, "api_call_started", True) is False
                    else "unavailable",
                )
                job["usage"] = usage
                finish(span, started, "failed")
                visit(job, "chat_answer", status="failed")
                visit(job, "chat_fallback", "chat_answer")
                return {"answer": local_guidance(state["context"], question), "fallback": True}

        async def guard(state):
            before = "chat_fallback" if state["fallback"] else "chat_answer"
            visit(job, "chat_guard", before)
            span, started = trace("chat.validate_sources", "chat_guard")
            allowed = {s["id"]: s for s in state["context"]["sources"]}
            answer = state["answer"]
            # Links and source metadata always come from the server's scoped evidence.
            references = [
                {k: v for k, v in allowed[sid].items() if k != "data"}
                for sid in dict.fromkeys(answer["source_ids"])
                if sid in allowed
            ][:8]
            current_revision = self.store.records[job["review_id"]]["revision"]
            record["messages"].append(
                dict(
                    id=identifier("message"),
                    role="assistant",
                    content=answer["answer"],
                    sources=references,
                    steps=validated_steps(answer.get("steps", []), state["context"]),
                    mode="guidance" if state["fallback"] else "model",
                    created_at=timestamp(),
                    job_id=job["id"],
                    revision=snapshot["revision"],
                    stale=current_revision != snapshot["revision"],
                )
            )
            self.save(record)
            finish(span, started)
            visit(job, "chat_reply", "chat_guard")
            return {}

        graph = StateGraph(ConversationState)
        for name, function in (("retrieve", retrieve), ("answer", answer), ("guard", guard)):
            graph.add_node(name, function)
        graph.add_edge(START, "retrieve")
        graph.add_edge("retrieve", "answer")
        graph.add_edge("answer", "guard")
        graph.add_edge("guard", END)
        job["status"] = "running"
        visit(job, "chat_start")
        persist()
        try:
            await graph.compile().ainvoke({})
            job["status"] = "completed"
        except asyncio.CancelledError:
            job.update(status="interrupted", error="답변이 중단되었습니다. 다시 질문해 주세요.")
        except Exception:
            job.update(status="failed", error="답변을 불러오지 못했습니다. 다시 질문해 주세요.")
        finally:
            job["finished_at"] = timestamp()
            persist()
