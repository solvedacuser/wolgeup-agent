"""Single-user durable review workspaces, independent of document and comparison cases."""

import asyncio
import copy
import re
from datetime import UTC, date, datetime

from agent.review.model import ReviewModel
from agent.review.pipeline import field_seed, identifier, manual_value, run_pipeline
from agent.review.reading import probe
from agent.review.registry import ISSUES, PIPELINE_VERSION, definition, profiles
from agent.review.rows import link_rows
from agent.review.telemetry import execution_summary
from agent.services.hashing import content_hash, digest
from agent.services.identity import identity_key
from agent.services.local_json import read_json, write_json

BUSY = {"queued", "reading", "classifying", "extracting", "verifying"}
BASIC = {
    "employer_name": "text",
    "pay_type": "category",
    "period_start": "date",
    "period_end": "date",
    "payment_date": "date",
}


def now():
    return datetime.now(UTC).isoformat()


class RevisionConflict(ValueError):
    pass


class ReviewStore:
    def __init__(self, legacy, model=None):
        self.legacy, self.settings = legacy, legacy.settings
        self.directory = legacy.state / "reviews"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.job_directory = self.directory / "jobs"
        self.job_directory.mkdir(exist_ok=True)
        self.records = {p.stem: read_json(p) for p in self.directory.glob("review-*.json")}
        self.jobs = {p.stem: read_json(p) for p in self.job_directory.glob("*.json")}
        self.tasks, self.locks, self.request_tasks = {}, {}, {}
        self.model = model or ReviewModel(self.settings)
        for job in self.jobs.values():
            if job["status"] in BUSY:
                job.update(status="interrupted", error="서버가 중단되었습니다. 다시 시작할 수 있습니다.")
                self.save_job(job)
        for review in self.records.values():
            for request in review["requests"]:
                if request["state"] == "running":
                    request["state"] = "interrupted"
            self.save(review)
        from agent.review.chat import ReviewChat

        self.chat = ReviewChat(self)
        from agent.review.recommendations import ReviewRecommendations

        self.recommendations = ReviewRecommendations(self)

    def lock(self, rid):
        return self.locks.setdefault(rid, asyncio.Lock())

    def save(self, review):
        review["updated_at"] = now()
        write_json(self.directory / (review["id"] + ".json"), review)

    def save_job(self, job):
        write_json(self.job_directory / (job["id"] + ".json"), job)

    def check_revision(self, review, revision):
        if type(revision) is not int or revision != review["revision"]:
            raise RevisionConflict(
                "다른 처리 결과가 반영되었습니다. 최신 내용을 불러온 뒤 다시 저장해 주세요."
            )

    def change(self, review):
        review["revision"] += 1
        for request in review["requests"]:
            if request["state"] in ("running", "completed"):
                request["stale"] = True
        self.save(review)

    async def create(self):
        rid = identifier("review")
        record = {
            "id": rid,
            "schema_version": 1,
            "pipeline_version": PIPELINE_VERSION,
            "revision": 1,
            "state": "draft",
            "created_at": now(),
            "title": "새 월급 검토",
            "basic": {
                key: {"value": "", "touched": False, "confirmed": False, "candidates": []} for key in BASIC
            },
            "documents": [],
            "requests": [],
            "history": [],
            "workplace_id": None,
            "options": {
                "subjects_confirmed": False,
                "earnings_complete": False,
                "deductions_complete": False,
                "payment_scope_confirmed": False,
                "distinct_transactions_confirmed": False,
                "selected_transactions": [],
                "contract_part_id": None,
                "payslip_part_id": None,
                "worked_time_kind": "",
                "slip_time_kind": "",
            },
        }
        self.records[rid] = record
        self.save(record)
        return self.view(rid)

    def parts(self, review):
        for document in review["documents"]:
            if not document.get("excluded"):
                for part in document.get("parts", []):
                    yield document, part

    def view(self, rid):
        record = self.records[rid]
        # Request snapshots are internal pinned inputs; never ship full copies to the browser.
        result = copy.deepcopy({k: v for k, v in record.items() if k != "requests"})
        result["requests"] = []
        for request in record["requests"]:
            item = copy.deepcopy({k: v for k, v in request.items() if k != "snapshot"})
            if request.get("snapshot"):
                item["confirmed_field_count"] = sum(
                    f["review_state"] == "confirmed" and not f.get("extra")
                    for _, p in self.parts(request["snapshot"])
                    for f in p["fields"]
                )
            result["requests"].append(item)
        for document in result["documents"]:
            job = self.jobs.get(document.get("job_id"))
            document["job"] = self.job_view(job) if job else None
            document["confirmed_count"] = sum(
                f["review_state"] == "confirmed" for p in document["parts"] for f in p["fields"]
            )
            document["field_count"] = sum(len(p["fields"]) for p in document["parts"])
            for part in document["parts"]:
                part["rows"] = link_rows(part)
        result["capabilities"] = self.capabilities(self.records[rid])
        result["recommendation_review"] = self.recommendations.view(record)
        assessments = result["recommendation_review"]["fields"]
        for document in result["documents"]:
            for part in document["parts"]:
                for field in part["fields"]:
                    field["assessment"] = assessments.get(field["id"])
                    field["recommendation_stale"] = bool(
                        field.get("ai_recommendation")
                        and field["ai_recommendation"]["input_hash"]
                        != result["recommendation_review"]["input_hash"]
                    )
                    if (
                        field["assessment"]
                        and field["assessment"]["kind"] == "conflict"
                        and not field["assessment"].get("resolved")
                    ):
                        field["extracted_value"] = field["value"]
                        if not field["touched"] and field["review_state"] != "confirmed":
                            field["value"] = ""
        for key, field in result["basic"].items():
            field["assessment"] = assessments.get("basic:" + key)
            field["recommendation_stale"] = bool(
                field.get("ai_recommendation")
                and field["ai_recommendation"]["input_hash"] != result["recommendation_review"]["input_hash"]
            )
        result["issue_labels"] = ISSUES
        result["execution"] = execution_summary(self.records[rid], self.jobs, self.legacy.jobs)
        return result

    def summaries(self):
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "state": r["state"],
                "workplace_id": r.get("workplace_id"),
                "employer_name": r["basic"]["employer_name"]["value"],
                "period_start": r["basic"]["period_start"]["value"],
                "period_end": r["basic"]["period_end"]["value"],
                "document_count": sum(not d.get("excluded") for d in r["documents"]),
                "updated_at": r["updated_at"],
            }
            for r in sorted(self.records.values(), key=lambda r: r["updated_at"], reverse=True)
        ]

    def workplaces(self):
        return list(
            {
                r["workplace_id"]: {"id": r["workplace_id"], "name": r["basic"]["employer_name"]["value"]}
                for r in self.records.values()
                if r.get("workplace_id") and r["basic"]["employer_name"]["value"]
            }.values()
        )

    def job_view(self, job):
        return copy.deepcopy(
            {
                k: v
                for k, v in job.items()
                if k not in ("reading", "parts", "preset_parts", "verification_parts")
            }
        )

    async def update(self, rid, payload):
        async with self.lock(rid):
            review = self.records[rid]
            self.check_revision(review, payload.get("revision"))
            if set(payload) - {
                "revision",
                "basic",
                "options",
                "workplace_id",
                "activate",
                "confirm_basic",
                "confirm_basic_field",
                "conflict_acknowledgement",
            }:
                raise ValueError("지원하지 않는 수정 항목입니다.")
            basic = copy.deepcopy(review["basic"])
            for key, value in payload.get("basic", {}).items():
                if key not in BASIC:
                    raise ValueError("지원하지 않는 기본 항목입니다.")
                value = manual_value(value, BASIC[key], key=key)
                basic[key].update(value=value, touched=True, confirmed=False, source_kind="user_statement")
                basic[key].pop("ai_recommendation", None)
            start, end = basic["period_start"]["value"], basic["period_end"]["value"]
            if start and end and date.fromisoformat(start) > date.fromisoformat(end):
                raise ValueError("급여 산정 시작일이 종료일보다 늦습니다.")
            options = copy.deepcopy(review["options"])
            for key, value in payload.get("options", {}).items():
                if key not in options:
                    raise ValueError("지원하지 않는 확인 항목입니다.")
                if key.endswith("confirmed") or key.endswith("complete"):
                    if type(value) is not bool:
                        raise ValueError("확인 상태 형식을 확인해 주세요.")
                elif key == "selected_transactions":
                    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                        raise ValueError("거래 목록을 확인해 주세요.")
                elif key.endswith("time_kind") and value not in ("", "actual", "paid"):
                    raise ValueError("시간 기준을 확인해 주세요.")
                elif (
                    key.endswith("part_id")
                    and value is not None
                    and value not in {p["id"] for _, p in self.parts(review)}
                ):
                    raise ValueError("현재 검토의 자료를 선택해 주세요.")
                options[key] = value
            assessment = self.recommendations.view(review)
            if payload.get("confirm_basic"):
                if any(
                    f.get("ai_recommendation")
                    and f["ai_recommendation"]["input_hash"] != assessment["input_hash"]
                    for f in basic.values()
                    if not f.get("confirmed")
                ):
                    raise ValueError("근거가 바뀐 AI 임시값이 있습니다. 추천을 다시 검토하고 적용해 주세요.")
                if any(
                    c["target_id"].startswith("basic:") and not c.get("resolved")
                    for c in assessment["conflicts"]
                ):
                    raise ValueError("기본 정보의 실제 충돌은 원문을 확인하고 항목별로 확정해 주세요.")
                for field in basic.values():
                    if field["value"]:
                        field["confirmed"] = True
            if payload.get("confirm_basic_field"):
                key = payload["confirm_basic_field"]
                if key not in basic or not basic[key]["value"]:
                    raise ValueError("확정할 기본 정보의 값을 선택해 주세요.")
                item = assessment["fields"].get("basic:" + key, {})
                if item.get("kind") == "conflict":
                    if payload.get("conflict_acknowledgement") != item["conflict_id"]:
                        raise ValueError("원문 확인 표시 후 개별 확정해 주세요.")
                    basic[key]["conflict_acknowledgement"] = item["conflict_id"]
                basic[key]["confirmed"] = True
            workplace_id = payload.get("workplace_id", review.get("workplace_id"))
            if workplace_id and workplace_id not in {w["id"] for w in self.workplaces()}:
                raise ValueError("등록된 근무지를 선택해 주세요.")
            review["history"].append(
                {"event": "basic_updated", "at": now(), "before": copy.deepcopy(review["basic"])}
            )
            review.update(basic=basic, options=options, workplace_id=workplace_id)
            if payload.get("activate"):
                review["state"] = "active"
                if not review.get("workplace_id") and basic["employer_name"]["value"]:
                    review["workplace_id"] = identifier("workplace")
            review["title"] = (basic["employer_name"]["value"] or "근무지 미확인") + (
                " · " + start + " ~ " + end if start and end else " · 기간 확인 필요"
            )
            self.change(review)
        await self.wake(rid)
        return self.view(rid)

    async def upload(self, rid, data, filename):
        metadata = await asyncio.to_thread(probe, data, self.settings)
        fingerprint = content_hash(data)
        async with self.lock(rid):
            review = self.records[rid]
            duplicate = next(
                (d for d in review["documents"] if d["sha256"] == fingerprint and not d.get("excluded")), None
            )
            if duplicate:
                return {"review": self.view(rid), "document_id": duplicate["id"], "duplicate": True}
            doc_id = identifier("upload")
            title = re.sub(r"[\x00-\x1f/\\]", "_", filename or "업로드 자료")[:160]
            (self.legacy.state / "uploads" / (doc_id + metadata["extension"])).write_bytes(data)
            self.legacy.documents[doc_id] = {
                "id": doc_id,
                "kind": "upload" if metadata["media_type"] == "application/pdf" else "review_image",
                "category": "사용자 업로드",
                "title": title,
                "sha256": fingerprint,
                **metadata,
            }
            write_json(self.legacy.state / "documents.json", self.legacy.documents)
            document = {
                "id": doc_id,
                "title": title,
                "sha256": fingerprint,
                **metadata,
                "parts": [],
                "excluded": False,
                "history": [],
                "generation": 0,
            }
            review["documents"].append(document)
            self.start(review, document)
            self.change(review)
        return {"review": self.view(rid), "document_id": doc_id, "duplicate": False}

    async def link(self, rid, payload):
        async with self.lock(rid):
            review = self.records[rid]
            self.check_revision(review, payload.get("revision"))
            did = payload["document_id"]
            metadata = self.legacy.documents[did]
            if any(d["id"] == did and not d.get("excluded") for d in review["documents"]):
                return self.view(rid)
            source = next(
                (
                    c
                    for c in self.legacy.cases.values()
                    if c.get("document_id") == did and c.get("extraction")
                ),
                None,
            )
            document = {
                "id": did,
                "title": metadata["title"],
                "sha256": metadata["sha256"],
                "media_type": metadata.get("media_type", "application/pdf"),
                "pages": metadata["pages"],
                "parts": [],
                "excluded": False,
                "history": [],
                "generation": 0,
                "imported": True,
            }
            if source:
                part = {
                    "id": identifier("part"),
                    "document_type": source["document_type"],
                    "start_page": 1,
                    "end_page": metadata["pages"],
                    "label": metadata["title"],
                    "needs_review": False,
                    "status": "review_ready",
                    "fields": [],
                }
                catalog = {e["evidence_id"]: e for e in source["extraction"].get("evidence_catalog", [])}
                for item in source["extraction"]["fields"]:
                    spec = definition(part["document_type"], item["field_id"])
                    if not spec:
                        continue
                    field = field_seed(item["field_id"], spec)
                    evidence = next((catalog[e] for e in item["evidence_refs"] if e in catalog), {})
                    field.update(
                        value=item.get("normalized_value") or "",
                        raw_value=item.get("raw_value"),
                        unit=item.get("unit") or spec["unit"],
                        validation_status=item.get("validation_status", "valid"),
                        issues=["LOCATION_UNAVAILABLE"],
                        evidence={
                            "id": identifier("evidence"),
                            "page": evidence.get("page"),
                            "quote": evidence.get("quote", ""),
                            "boxes": [],
                            "location_status": "unavailable",
                            "method": evidence.get("verification_method", "ocr_unverified"),
                        },
                    )
                    part["fields"].append(field)
                document["parts"] = [part]
            review["documents"].append(document)
            self.autofill(review)
            self.change(review)
            return self.view(rid)

    def document(self, review, did):
        return next(d for d in review["documents"] if d["id"] == did)

    def start(
        self, review, document, preset_parts=None, *, locate_only=False, verify_fields=None, trigger="upload"
    ):
        previous = self.jobs.get(document.get("job_id"))
        if previous and previous["status"] in BUSY:
            raise ValueError("이 자료의 처리가 끝난 뒤 다시 시도해 주세요.")
        jid = identifier("reviewjob")
        document["generation"] += 1
        job = {
            "id": jid,
            "review_id": review["id"],
            "document_id": document["id"],
            "status": "queued",
            "stage": "document_review",
            "generation": document["generation"],
            "created_at": now(),
            "events": [],
            "usage": {},
            "error": None,
            "preset_parts": preset_parts,
            "locate_only": locate_only,
            "model": self.settings.extraction_model,
            "execution_trace": [],
            "route": [],
            "trigger": trigger,
            "verify_fields": verify_fields,
        }
        if verify_fields:
            job["verification_parts"] = copy.deepcopy(document["parts"])
            if previous and previous.get("reading"):
                job["reading"] = copy.deepcopy(previous["reading"])
        self.jobs[jid] = job
        document["job_id"] = jid
        self.save_job(job)
        self.tasks[jid] = asyncio.create_task(self.execute(job))

    async def document_action(self, rid, did, payload):
        async with self.lock(rid):
            review = self.records[rid]
            self.check_revision(review, payload.get("revision"))
            document = self.document(review, did)
            action = payload.get("action")
            if action == "exclude":
                document["excluded"] = True
                document["generation"] += 1
                task = self.tasks.get(document.get("job_id"))
                if task and not task.done():
                    task.cancel()
            elif action in ("retry", "classify", "locate", "manual", "verify"):
                preset = payload.get("parts")
                if preset:
                    covered = []
                    for part in preset:
                        if part.get("document_type") not in profiles():
                            raise ValueError("지원하는 문서 유형을 선택해 주세요.")
                        start, end = part.get("start_page"), part.get("end_page")
                        if (
                            type(start) is not int
                            or type(end) is not int
                            or not 1 <= start <= end <= document["pages"]
                        ):
                            raise ValueError("페이지 범위를 확인해 주세요.")
                        covered.extend(range(start, end + 1))
                        part.update(
                            label=profiles()[part["document_type"]]["label"],
                            needs_review=False,
                            reason="사용자 지정",
                        )
                    if sorted(covered) != list(range(1, document["pages"] + 1)):
                        raise ValueError("페이지를 중복 없이 모두 나눠 주세요.")
                document["excluded"] = False
                if action == "manual":
                    if not preset:
                        raise ValueError("직접 입력할 문서 유형과 페이지를 지정해 주세요.")
                    generated = [
                        {
                            **p,
                            "id": identifier("part"),
                            "status": "review_ready",
                            "fields": [
                                field_seed(k, v) for k, v in profiles()[p["document_type"]]["fields"].items()
                            ],
                        }
                        for p in preset
                    ]
                    self.merge_parts(document, generated)
                else:
                    ids = payload.get("field_ids") if action == "verify" else None
                    if action == "verify" and (
                        not isinstance(ids, list)
                        or not ids
                        or len(ids) > 50
                        or set(ids) - {f["id"] for p in document["parts"] for f in p["fields"]}
                    ):
                        raise ValueError("현재 자료의 항목을 1~50개 선택해 주세요.")
                    self.start(
                        review,
                        document,
                        preset,
                        locate_only=action == "locate",
                        verify_fields=ids,
                        trigger=action,
                    )
            else:
                raise ValueError("지원하지 않는 자료 동작입니다.")
            self.autofill(review)
            self.change(review)
        await self.wake(rid)
        return self.view(rid)

    async def execute(self, job):
        rid, did = job["review_id"], job["document_id"]

        async def progress(stage):
            job["status"] = stage
            job["events"].append({"stage": stage, "at": now()})
            self.save_job(job)

        try:
            async with self.legacy.semaphore:
                document = self.document(self.records[rid], did)
                data = self.legacy.document_data(did)
                if job.get("locate_only"):
                    from agent.review.flow import reading_route, visit
                    from agent.review.reading import locate, read_document

                    visit(job, "start")
                    visit(job, "format", "start")
                    await progress("reading")
                    reading = await asyncio.to_thread(
                        read_document, data, document["media_type"], self.settings
                    )
                    job["reading"] = reading
                    ends = reading_route(job, reading)
                    visit(job, "locate", *ends)
                    job["execution_trace"].extend(reading.get("reader_calls", []))
                    job.setdefault("tool_calls", []).extend(reading.get("reader_calls", []))
                    async with self.lock(rid):
                        for part in document["parts"]:
                            for field in part["fields"]:
                                evidence = field.get("evidence")
                                if evidence:
                                    evidence["boxes"] = locate(
                                        reading,
                                        evidence.get("page"),
                                        evidence["quote"],
                                        field.get("raw_value"),
                                    )
                                    evidence["location_status"] = (
                                        "located" if evidence["boxes"] else "unavailable"
                                    )
                        self.change(self.records[rid])
                    visit(job, "review", "locate", status="waiting")
                else:
                    result = await run_pipeline(
                        data,
                        document["media_type"],
                        self.settings,
                        self.model,
                        job,
                        progress,
                        preset_parts=job.get("preset_parts"),
                        prior_reading=job.get("reading") if job.get("verify_fields") else None,
                        verification_parts=job.get("verification_parts"),
                        verify_field_ids=job.get("verify_fields"),
                        persist=lambda: self.save_job(job),
                    )
                    async with self.lock(rid):
                        review = self.records[rid]
                        document = self.document(review, did)
                        if document["generation"] != job["generation"] or document["excluded"]:
                            job["status"] = "superseded"
                            self.save_job(job)
                            return
                        self.merge_parts(document, result["parts"])
                        self.autofill(review)
                        self.change(review)
                    job["reading"] = result["reading"]
                job["status"] = (
                    "completed"
                    if document["parts"] and any(p.get("status") == "review_ready" for p in document["parts"])
                    else "needs_review"
                )
                if (
                    not job.get("locate_only")
                    and document["parts"]
                    and all(p.get("status") == "failed" for p in document["parts"])
                ):
                    job["status"] = "failed"
                    job["error"] = "항목 추출에 실패했습니다. 다시 시도하거나 직접 입력할 수 있습니다."
        except asyncio.CancelledError:
            job.update(
                status="cancelled", error="처리를 중단했습니다. 이미 진행된 모델 사용량은 발생할 수 있습니다."
            )
        except Exception:
            job.update(
                status="failed",
                error="파일 처리에 실패했습니다. 원문과 모델 설정을 확인하고 다시 시도해 주세요.",
            )
        finally:
            job["finished_at"] = now()
            self.save_job(job)
        if job["status"] in ("completed", "needs_review") and not job.get("locate_only"):
            await self.recommendations.start(rid, automatic=True)
        await self.wake(rid)

    def merge_parts(self, document, parts):
        previous = copy.deepcopy(document["parts"])
        if previous:
            document["history"].append({"at": now(), "parts": previous})
        for part in parts:
            old = next(
                (
                    p
                    for p in previous
                    if (p["document_type"], p["start_page"], p["end_page"])
                    == (part["document_type"], part["start_page"], part["end_page"])
                ),
                None,
            )
            if not old:
                continue
            part["id"] = old["id"]
            if old.get("bridge_case_id"):
                part["bridge_case_id"] = old["bridge_case_id"]
            for field in part["fields"]:
                # Summary keys are stable. Repeated rows require matching source quote, never row index alone.
                repeated = re.match(r"(?:work|earning|deduction|transaction)_\d+_", field["field_id"])
                match = next(
                    (
                        f
                        for f in old["fields"]
                        if f["field_id"] == field["field_id"]
                        and (
                            not repeated
                            or (f.get("evidence") or {}).get("quote")
                            == (field.get("evidence") or {}).get("quote")
                        )
                    ),
                    None,
                )
                if match:
                    field["id"] = match["id"]
                    if (
                        match.get("ai_recommendation")
                        and not match["touched"]
                        and match["review_state"] != "confirmed"
                    ):
                        field["history"] = copy.deepcopy(match["history"]) + [
                            {
                                "event": "ai_recommendation_invalidated",
                                "at": now(),
                                "before": copy.deepcopy({k: v for k, v in match.items() if k != "history"}),
                            }
                        ]
                        field["revision"] = match["revision"] + 1
                    if match["touched"] or match["review_state"] == "confirmed":
                        old_ids = {candidate["id"] for candidate in match["candidates"]}
                        new_candidates = [
                            candidate for candidate in field["candidates"] if candidate["id"] not in old_ids
                        ]
                        field.update(copy.deepcopy(match))
                        field["candidates"].extend(new_candidates)
                        field["revision"] += 1
                        if any(
                            c["value"] is not None and c["value"] != field["value"] for c in new_candidates
                        ):
                            field["issues"] = list(dict.fromkeys(field["issues"] + ["CONFLICT"]))
                            field["review_state"] = "unreviewed"
            present = {f["id"] for f in part["fields"]}
            for field in old["fields"]:
                if field["id"] not in present and (field["touched"] or field["review_state"] == "confirmed"):
                    field["issues"] = list(dict.fromkeys(field["issues"] + ["CONFLICT"]))
                    field["review_state"] = "unreviewed"
                    part["fields"].append(field)
        # Preserve edits from a changed classification separately for explicit user reconciliation.
        ids = {p["id"] for p in parts}
        for old in previous:
            if old["id"] not in ids and any(
                f["touched"] or f["review_state"] == "confirmed" for f in old["fields"]
            ):
                old.update(
                    status="needs_classification",
                    needs_review=True,
                    reason="문서 구간이 바뀌었습니다. 이전 수정값을 보존했습니다.",
                )
                for field in old["fields"]:
                    field["review_state"] = "unreviewed"
                parts.append(old)
        document["parts"] = parts

    def autofill(self, review):
        for key, basic in review["basic"].items():
            choices = []
            for document, part in self.parts(review):
                if key in ("period_start", "period_end") and part["document_type"] not in (
                    "payslip",
                    "attendance",
                ):
                    continue
                if key == "payment_date" and part["document_type"] != "payslip":
                    continue
                for field in part["fields"]:
                    if field["field_id"] == key and field["value"]:
                        choices.append(
                            {
                                "value": field["value"],
                                "document_id": document["id"],
                                "title": document["title"],
                                "field_id": field["id"],
                            }
                        )
            basic["candidates"] = choices
            unique = {c["value"] for c in choices}
            canonical = {identity_key(v, company=True) for v in unique} if key == "employer_name" else unique
            basic["conflict"] = len(canonical) > 1
            if (
                not basic["touched"]
                and not basic["confirmed"]
                and basic.get("source_kind") != "ai_provisional"
            ):
                basic["value"] = next(iter(unique)) if len(unique) == 1 else ""
        name = review["basic"]["employer_name"]["value"]
        start, end = (review["basic"][key]["value"] for key in ("period_start", "period_end"))
        review["title"] = (name or "근무지 미확인") + (
            " · " + start + " ~ " + end if start and end else " · 기간 확인 필요"
        )
        if review["state"] == "active" and name and not review.get("workplace_id"):
            review["workplace_id"] = identifier("workplace")

    def find_field(self, review, fid):
        for document, part in self.parts(review):
            for field in part["fields"]:
                if field["id"] == fid:
                    return document, part, field
        raise KeyError(fid)

    async def edit_field(self, rid, payload):
        async with self.lock(rid):
            review = self.records[rid]
            self.check_revision(review, payload.get("revision"))
            _, part, field = self.find_field(review, payload["field_id"])
            old = copy.deepcopy({k: v for k, v in field.items() if k != "history"})
            unit = payload.get("unit", field["unit"])
            if unit not in (
                "rate",
                "text",
                "date",
                "time",
                "minute",
                "count",
                "category",
                "day_of_month",
                "KRW",
                "KRW/hour",
                "KRW/month",
                "KRW/year",
                "KRW/day",
            ):
                raise ValueError("단위를 확인해 주세요.")
            candidate = None
            if payload.get("candidate_id"):
                candidate = next(c for c in field["candidates"] if c["id"] == payload["candidate_id"])
                unit = candidate["unit"]
                value = candidate["value"] or candidate["raw_value"] or ""
            else:
                value = payload.get("value", field["value"])
            value = manual_value(value, unit, part["document_type"], field["field_id"])
            field["history"].append({"at": now(), "before": old})
            field.update(
                value=value,
                unit=unit,
                touched=True,
                review_state="deferred" if payload.get("defer") else "unreviewed",
                revision=field["revision"] + 1,
            )
            if payload.get("defer"):
                field["value"] = ""
            if candidate:
                field.update(
                    evidence=copy.deepcopy(candidate["evidence"]),
                    raw_value=candidate["raw_value"],
                    source_kind="document",
                )
            else:
                field["source_kind"] = "user_statement"
            field.pop("ai_recommendation", None)
            field.pop("conflict_acknowledgement", None)
            self.autofill(review)
            self.change(review)
        await self.wake(rid)
        return self.view(rid)

    async def add_fields(self, rid, payload):
        async with self.lock(rid):
            review = self.records[rid]
            self.check_revision(review, payload.get("revision"))
            part = next(p for _, p in self.parts(review) if p["id"] == payload["part_id"])
            prefix = payload.get("row_type")
            if prefix:
                row = profiles().get(part["document_type"], {}).get("rows", {}).get(prefix)
                if not row:
                    raise ValueError("이 문서에서 추가할 수 없는 행입니다.")
                indexes = [
                    int(m[1])
                    for f in part["fields"]
                    if (m := re.match(rf"{re.escape(prefix)}_(\d+)_", f["field_id"]))
                ]
                index = max(indexes, default=0) + 1
                fields = [field_seed(f"{prefix}_{index}_{key}", spec) for key, spec in row["fields"].items()]
            else:
                label = payload.get("label", "추가 정보")
                if not isinstance(label, str) or not label.strip() or len(label) > 120:
                    raise ValueError("항목 이름을 입력해 주세요.")
                fields = [
                    field_seed("extra_" + uuid_key(), {"label": label.strip(), "unit": "text"}, extra=True)
                ]
            for field in fields:
                field.update(touched=True, source_kind="user_statement", issues=["MANUAL"])
            part["fields"].extend(fields)
            self.change(review)
            return self.view(rid)

    async def confirm(self, rid, payload):
        async with self.lock(rid):
            review = self.records[rid]
            self.check_revision(review, payload.get("revision"))
            ids = payload.get("field_ids", [])
            if not isinstance(ids, list) or not ids or len(ids) > 2000:
                raise ValueError("원문과 확인한 항목을 선택해 주세요.")
            fields = [self.find_field(review, fid)[2] for fid in dict.fromkeys(ids)]
            assessments = self.recommendations.view(review)["fields"]
            from agent.review.recommendations import input_hash

            current_hash = input_hash(review)
            if any(
                f.get("ai_recommendation") and f["ai_recommendation"]["input_hash"] != current_hash
                for f in fields
                if f["review_state"] != "confirmed"
            ):
                raise ValueError("근거가 바뀐 AI 임시값이 있습니다. 추천을 다시 검토하고 적용해 주세요.")
            critical = [
                f
                for f in fields
                if assessments.get(f["id"], {}).get("kind") == "conflict"
                and not assessments[f["id"]].get("resolved")
            ]
            if critical:
                item = assessments[critical[0]["id"]]
                if (
                    len(fields) != 1
                    or not critical[0].get("touched")
                    or payload.get("conflict_acknowledgement") != item["conflict_id"]
                ):
                    raise ValueError("실제 충돌은 기준값을 선택하고 원문 확인 표시 후 개별 확정해 주세요.")
            if any(not field["value"] for field in fields):
                raise ValueError("빈값은 확정할 수 없습니다. 직접 입력하거나 보류해 주세요.")
            for field in fields:
                manual_value(field["value"], field["unit"], key=field["field_id"])
            for field in fields:
                if field in critical:
                    field["conflict_acknowledgement"] = assessments[field["id"]]["conflict_id"]
                field["history"].append(
                    {
                        "event": "confirmed",
                        "at": now(),
                        "value": field["value"],
                        "unit": field["unit"],
                        "actor": "local-user",
                    }
                )
                field.update(
                    review_state="confirmed",
                    confirmation_id=identifier("confirmation"),
                    confirmed_at=now(),
                    revision=field["revision"] + 1,
                )
            self.change(review)
            if review.get("recommendations"):
                from agent.review.flow import visit

                job = self.jobs.get(review["recommendations"]["job_id"])
                if job:
                    visit(job, "recommend_confirm", "recommend_conflict" if critical else "recommend_apply")
                    self.save_job(job)
        await self.wake(rid)
        return self.view(rid)

    def selection(self, review):
        groups = {
            kind: [
                p for _, p in self.parts(review) if p["document_type"] == kind and not p.get("needs_review")
            ]
            for kind in profiles()
        }
        selected, missing = [], []
        for kind in ("contract", "payslip"):
            chosen = review["options"].get(kind + "_part_id")
            parts = groups[kind]
            if chosen:
                part = next((p for p in parts if p["id"] == chosen), None)
                if part:
                    selected.append(part)
                else:
                    missing.append("기준 자료를 다시 선택해 주세요.")
            elif len(parts) == 1:
                selected.extend(parts)
            elif len(parts) > 1:
                missing.append(profiles()[kind]["label"] + " 기준 자료를 선택해 주세요.")
        selected.extend(groups["attendance"] + groups["deposit"])
        return selected, missing

    def capabilities(self, review):
        from agent.review.recommendations import assess

        selected, missing = self.selection(review)
        assessment = assess(review)
        if any(not c.get("resolved") for c in assessment["conflicts"]):
            missing.append("필수 확인 항목의 기준값을 선택하고 개별 확정해 주세요.")
        if not selected:
            missing.append("분류된 자료가 필요합니다.")
        for key in ("period_start", "period_end"):
            if not review["basic"][key]["value"] or not review["basic"][key]["confirmed"]:
                missing.append("급여 산정기간을 입력하고 기본 정보를 확정해 주세요.")
                break
        if not review["options"]["subjects_confirmed"]:
            missing.append("같은 근로자·사업장의 자료인지 확인해 주세요.")
        usable = [
            f
            for p in selected
            for f in p["fields"]
            if f["review_state"] == "confirmed"
            and not f["extra"]
            and f["unit"] in ("KRW", "KRW/hour", "KRW/month", "KRW/year", "KRW/day", "minute")
        ]
        if not usable:
            missing.append("대조에 사용할 금액 또는 시간 항목을 확정해 주세요.")
        for key, label in (("employee_name", "근로자"), ("employer_name", "사업장")):
            names = {
                identity_key(f["value"], company=key == "employer_name")
                for p in selected
                for f in p["fields"]
                if f["field_id"] == key and f["review_state"] == "confirmed" and f["value"]
            }
            if len(names) > 1:
                missing.append(label + "이 다른 자료가 있습니다. 수정하거나 제외해 주세요.")
        return [
            {
                "id": "compare",
                "label": "확정한 값으로 문서 대조",
                "status": "ready" if not missing else "needs_review",
                "missing": list(dict.fromkeys(missing)),
            },
            {
                "id": "prepare",
                "label": "표준 임금 계산 조건 입력",
                "status": "ready" if selected else "needs_data",
                "missing": [] if selected else ["자료를 먼저 분류해 주세요."],
            },
        ]

    async def request(self, rid, payload):
        async with self.lock(rid):
            review = self.records[rid]
            self.check_revision(review, payload.get("revision"))
            capability = payload.get("capability", "compare")
            if capability not in ("compare", "prepare"):
                raise ValueError("구현된 기능을 선택해 주세요.")
            old = next(
                (
                    r
                    for r in review["requests"]
                    if r["capability"] == capability and r["state"] in ("waiting", "running")
                ),
                None,
            )
            if not old:
                request = {
                    "id": identifier("request"),
                    "capability": capability,
                    "state": "waiting",
                    "created_at": now(),
                    "stale": False,
                    "scope": {
                        key: review["basic"][key]["value"]
                        for key in ("employer_name", "period_start", "period_end")
                    },
                    "auto_resume": True,
                    "route": [],
                }
                from agent.review.flow import visit

                visit(request, "request")
                review["requests"].append(request)
                self.save(review)
        await self.wake(rid)
        return self.view(rid)

    async def cancel_request(self, rid, request_id):
        async with self.lock(rid):
            review = self.records[rid]
            request = next(r for r in review["requests"] if r["id"] == request_id)
            request.update(state="cancelled", auto_resume=False)
            task = self.request_tasks.get(request_id)
            if task and not task.done():
                task.cancel()
            self.save(review)
        return self.view(rid)

    async def wake(self, rid):
        from agent.review.flow import visit
        from agent.review.work import run_work

        async with self.lock(rid):
            review = self.records[rid]
            caps = {c["id"]: c for c in self.capabilities(review)}
            for request in review["requests"]:
                if request["state"] != "waiting" or not request["auto_resume"]:
                    continue
                changed_scope = any(
                    value and value != review["basic"][key]["value"]
                    for key, value in request["scope"].items()
                )
                if changed_scope:
                    request.update(
                        state="cancelled",
                        error="근무지 또는 기간이 바뀌어 기존 자동 실행 요청을 취소했습니다.",
                    )
                    continue
                request["missing"] = caps[request["capability"]]["missing"]
                was_waiting = any(item["node"] == "wait" for item in request.get("route", []))
                visit(request, "ready", "wait" if was_waiting else "request")
                if caps[request["capability"]]["status"] == "ready":
                    if was_waiting:
                        visit(request, "wait", status="completed")
                    request["state"] = "running"
                    request["input_hash"] = digest(
                        {
                            "basic": review["basic"],
                            "parts": list(self.parts(review)),
                            "options": review["options"],
                        }
                    )
                    request["snapshot"] = copy.deepcopy({k: v for k, v in review.items() if k != "requests"})
                    self.request_tasks[request["id"]] = asyncio.create_task(
                        run_work(self, rid, request["id"])
                    )
                else:
                    visit(request, "wait", "ready", status="waiting")
            self.save(review)

    async def control_job(self, jid, action):
        job = self.jobs[jid]
        if action == "cancel":
            task = self.tasks.get(jid)
            if task and not task.done():
                task.cancel()
            job.update(status="cancelled", error="처리를 중단했습니다. 다시 시작할 수 있습니다.")
            self.save_job(job)
        elif action == "resume":
            if job["status"] in BUSY:
                raise ValueError("이미 실행 중입니다.")
            review = self.records[job["review_id"]]
            document = self.document(review, job["document_id"])
            if document["excluded"]:
                raise ValueError("제외한 자료는 다시 실행할 수 없습니다.")
            self.start(review, document, job.get("preset_parts"), trigger="resume")
            self.change(review)
            job = self.jobs[document["job_id"]]
        return self.job_view(job)


def uuid_key():
    return identifier("custom").replace("-", "_")
