"""Recorded token accounting and execution spans; never estimate historical usage."""

import copy
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")
STAGES = {
    "reader": "원문 판독",
    "classify": "문서 분류",
    "extract": "필드 추출",
    "validator": "필드 검증",
    "verify": "원본 재검증",
    "business": "계산·보고서",
    "chat": "프로젝트 챗봇",
    "recommendation": "표기 비교·추천",
    "unattributed": "이전 실행 · 단계별 기록 없음",
}


def timestamp():
    return datetime.now(UTC).isoformat()


def usage_numbers(value):
    if not isinstance(value, dict):
        return {}
    result = {key: value[key] for key in TOKEN_KEYS if type(value.get(key)) is int and value[key] >= 0}
    if "total_tokens" not in result and all(key in result for key in ("input_tokens", "output_tokens")):
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def add_usage(total, value):
    for key, number in usage_numbers(value).items():
        total[key] = total.get(key, 0) + number


def begin_span(kind, stage, name, *, parent_id=None, context=None):
    return {
        "id": "span-" + uuid.uuid4().hex[:16],
        "kind": kind,
        "stage": stage,
        "name": name,
        "status": "running",
        "started_at": timestamp(),
        "finished_at": None,
        "parent_id": parent_id,
        "context": context or {},
        "usage": {},
        "usage_status": "pending" if kind == "agent" else "local",
    }


@contextmanager
def local_span(events, name, *, page=None):
    event = begin_span("tool", "reader", name, context={"page": page} if page else {})
    events.append(event)
    started = time.monotonic()
    try:
        yield event
        event["status"] = "completed"
    except BaseException:
        event["status"] = "failed"
        raise
    finally:
        event.update(finished_at=timestamp(), duration_ms=round((time.monotonic() - started) * 1000))


def _legacy_trace(job):
    """Old jobs kept separate arrays without interleaving/timestamps or per-call usage."""
    entries = []
    for index, item in enumerate(job.get("tool_calls", [])):
        entries.append(
            {
                **copy.deepcopy(item),
                "id": f"{job['id']}-oldtool-{index}",
                "kind": "tool",
                "stage": "verify" if item.get("role") == "verifier" else item.get("role", "reader"),
                "name": item.get("tool", "도구"),
                "usage": {},
                "usage_status": "local",
                "started_at": None,
                "parent_id": None,
                "context": {},
            }
        )
    for index, item in enumerate(job.get("agent_calls", [])):
        entries.append(
            {
                **copy.deepcopy(item),
                "id": f"{job['id']}-oldagent-{index}",
                "kind": "agent",
                "stage": item.get("agent", "unattributed"),
                "name": "model." + item.get("agent", "unknown"),
                "usage": usage_numbers(item.get("usage")),
                "usage_status": "reported" if usage_numbers(item.get("usage")) else "not_recorded",
                "started_at": None,
                "parent_id": None,
                "context": {},
            }
        )
    return entries


def execution_summary(review, jobs, legacy_jobs):
    """Include every attempt in this review, even superseded/excluded documents."""
    from agent.review.flow import TOPOLOGY, project

    documents = {d["id"]: d for d in review["documents"]}
    relevant = [job for job in jobs.values() if job.get("review_id") == review["id"]]
    comparisons = {r.get("comparison_id") for r in review["requests"] if r.get("comparison_id")}
    business = [job for job in legacy_jobs.values() if job.get("case_id") in comparisons]
    requests_by_job = {r.get("job_id"): r for r in review["requests"] if r.get("job_id")}
    for request in review["requests"]:
        if request.get("job_id") not in legacy_jobs:
            business.append(
                {
                    "id": request["id"],
                    "status": request["state"],
                    "stage": request["capability"],
                    "created_at": request["created_at"],
                    "usage": {},
                    "events": [],
                    "route": request.get("route", []),
                }
            )
    stages = {
        key: {
            "id": key,
            "label": label,
            "usage": {},
            "model_calls": 0,
            "tool_calls": 0,
            "pending_calls": 0,
            "unavailable_calls": 0,
            "not_recorded_calls": 0,
            "tools": [],
        }
        for key, label in STAGES.items()
    }
    output_jobs, total = [], {}
    pending, unavailable, historical = 0, 0, 0
    for job in sorted(relevant + business, key=lambda j: (j.get("created_at", ""), j["id"])):
        is_business = job in business
        doc = documents.get(job.get("document_id"), {})
        trace = copy.deepcopy(job.get("execution_trace")) if "execution_trace" in job else _legacy_trace(job)
        if is_business:
            nodes = {}
            completed = {item["node_id"]: item for item in job.get("execution_nodes", [])}
            for item in job.get("events", []):
                nodes[item.get("node_id", job.get("stage", "business"))] = item
            trace = [
                {
                    "id": f"{job['id']}-node-{index}",
                    "kind": "code",
                    "stage": "business",
                    "name": item.get("node_id", job.get("stage", "business")),
                    "status": "completed"
                    if item.get("node_id") in completed or job["status"] == "completed"
                    else item.get("status", "running"),
                    "usage": {},
                    "usage_status": "local",
                    "started_at": completed.get(item.get("node_id"), {}).get("started_at"),
                    "finished_at": completed.get(item.get("node_id"), {}).get("ended_at"),
                    "parent_id": None,
                    "context": {},
                }
                for index, item in enumerate(nodes.values())
            ]
        for event in trace:
            if event.get("started_at") and event.get("finished_at") and "duration_ms" not in event:
                event["duration_ms"] = max(
                    0,
                    round(
                        (
                            datetime.fromisoformat(event["finished_at"])
                            - datetime.fromisoformat(event["started_at"])
                        ).total_seconds()
                        * 1000
                    ),
                )
            if event.get("status") == "running" and job["status"] not in (
                "queued",
                "reading",
                "classifying",
                "extracting",
                "verifying",
                "running",
            ):
                event["status"] = "interrupted"
                if event.get("usage_status") == "pending":
                    event["usage_status"] = "unavailable"
        if trace and all(item.get("started_at") for item in trace):
            trace.sort(key=lambda item: item["started_at"])
        attributed = {}
        for event in trace:
            stage = stages.get(
                event.get("stage"), stages["business"] if is_business else stages["unattributed"]
            )
            usage = usage_numbers(event.get("usage"))
            if event.get("kind") == "agent":
                stage["model_calls"] += 1
                add_usage(stage["usage"], usage)
                add_usage(attributed, usage)
                if event.get("usage_status") == "pending":
                    pending += 1
                    stage["pending_calls"] += 1
                elif event.get("usage_status") == "unavailable":
                    unavailable += 1
                    stage["unavailable_calls"] += 1
                elif event.get("usage_status") == "not_recorded":
                    stage["not_recorded_calls"] += 1
            else:
                stage["tool_calls"] += 1
                if event["name"] not in stage["tools"]:
                    stage["tools"].append(event["name"])
        recorded = usage_numbers(job.get("usage"))
        # The job total is authoritative. Call details subdivide it, never add to it a second time.
        job_total = recorded or attributed
        if not job_total and not any(event.get("kind") == "agent" for event in trace):
            job_total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        add_usage(total, job_total)
        unassigned = {key: max(0, value - attributed.get(key, 0)) for key, value in job_total.items()}
        if not is_business and (
            any(unassigned.values()) or any(e.get("usage_status") == "not_recorded" for e in trace)
        ):
            historical += 1
            add_usage(stages["unattributed"]["usage"], unassigned)
        if is_business and recorded:
            add_usage(stages["business"]["usage"], recorded)
        output_jobs.append(
            {
                "id": job["id"],
                "title": doc.get(
                    "title",
                    {
                        "analyze": "확정값으로 임금 계산·대조",
                        "compare": "문서 대조 요청",
                        "prepare": "비교 입력 준비",
                        "explain": "결과 설명",
                        "report": "보고서 생성",
                        "chat": "월급 도우미 대화",
                        "recommendation": "표기 비교·추천 검토",
                    }.get(job.get("stage"), "계산·보고서"),
                ),
                "document_id": job.get("document_id"),
                "is_current": doc.get("job_id") == job["id"],
                "excluded": bool(doc.get("excluded")),
                "kind": job["kind"]
                if job.get("kind") in ("chat", "recommendation")
                else "business"
                if is_business
                else "document",
                "status": job["status"],
                "stage": job.get("stage"),
                "created_at": job.get("created_at"),
                "model": job.get("model"),
                "trigger": job.get(
                    "trigger",
                    "verify"
                    if job.get("verify_fields")
                    else "locate"
                    if job.get("locate_only")
                    else "document",
                ),
                "usage": job_total,
                "usage_reported": bool(job_total),
                "trace_order": "chronological"
                if "execution_trace" in job or trace and all(e.get("started_at") for e in trace)
                else "not_recorded",
                "trace": trace,
                "flow": project(
                    job, trace, business=is_business, route=requests_by_job.get(job["id"], {}).get("route")
                ),
            }
        )
    return {
        "schema_version": 2,
        "topology": TOPOLOGY,
        "scope": "이 검토의 모든 실행 누적 · 재시도와 제외한 자료 포함",
        "usage": total,
        "model_calls": sum(stage["model_calls"] for stage in stages.values()),
        "pending_calls": pending,
        "unavailable_calls": unavailable,
        "historical_jobs": historical,
        "stages": list(stages.values()),
        "jobs": output_jobs,
    }
