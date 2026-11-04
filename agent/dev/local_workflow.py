"""File-backed development cases; intentionally independent of a production backend."""

import asyncio
import copy
import io
import re
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pypdf import PdfReader

from agent import run_task
from agent.dev.contract_corpus import DEFAULT_OUTPUT, FIELDS, read_json, write_json
from agent.dev.fake_ports import FakePorts
from agent.schemas.public.base import Period
from agent.schemas.public.inputs import (
    ConfirmedField,
    EvidenceRef,
    InputSnapshot,
    SourceRef,
    StatementItem,
    TransactionAllocation,
)
from agent.schemas.public.results import AnalysisResult, ExplanationCandidate
from agent.schemas.public.tasks import (
    AgentTask,
    AnalysisJobInput,
    ExplanationInput,
    ExtractionInput,
    JobContext,
    ReportInput,
)
from agent.services.extraction_profiles import validate_document_type
from agent.services.facts import allowed_facts
from agent.services.hashing import content_hash, snapshot_hash

MANUAL_FIELDS = (
    "slip_rate",
    "worked_minutes",
    "slip_minutes",
    "gross",
    "deduction",
    "net",
    "deposit",
    "earning_item",
    "deduction_item",
)


def identifier(prefix):
    return prefix + "-" + uuid.uuid4().hex


class ComparisonInputError(ValueError):
    """Safe, actionable input errors for the local materials UI."""


class ReviewedTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected: bool = False
    amount: str | None = None
    transaction_amount: str | None = None

    @field_validator("amount", "transaction_amount")
    @classmethod
    def allocation_amount(cls, value):
        if value not in (None, "") and not re.fullmatch(r"\d{1,13}(?:\.\d{1,2})?", value):
            raise ValueError("Allocation must be a nonnegative decimal string")
        return value


class MaterialReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    earnings: dict[str, str | None] = Field(default_factory=dict)
    deductions: dict[str, str | None] = Field(default_factory=dict)
    transactions: dict[str, ReviewedTransaction] = Field(default_factory=dict)
    distinct_transactions_confirmed: bool = False

    @field_validator("earnings", "deductions")
    @classmethod
    def row_amounts(cls, values):
        if len(values) > 9999:
            raise ValueError("Too many material rows")
        for value in values.values():
            if value not in (None, "") and not re.fullmatch(r"-?\d{1,13}(?:\.\d{1,2})?", value):
                raise ValueError("Row amount must be a decimal string")
        return values


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str | None] = Field(default_factory=dict)
    contract_unit: Literal["KRW/hour", "KRW/month", "KRW/year", "KRW/day"] = "KRW/hour"
    slip_unit: Literal["", "KRW/hour", "KRW/month", "KRW/year", "KRW/day"] = ""
    contract_period_confirmed: bool = False
    period_start: str = ""
    period_end: str = ""
    manual: dict[str, str | None] = Field(default_factory=dict)
    time_kind: Literal["", "actual", "paid"] = ""
    earnings_complete: bool = False
    deductions_complete: bool = False
    payment_scope_confirmed: bool = False
    synthetic_auxiliary: bool = False
    material_review: MaterialReview = Field(default_factory=MaterialReview)

    @field_validator("manual")
    @classmethod
    def manual_numbers(cls, values):
        if set(values) - set(MANUAL_FIELDS):
            raise ValueError("Unknown manual field")
        for key, value in values.items():
            pattern = (
                r"-?\d{1,13}(?:\.\d{1,2})?"
                if key in ("gross", "deduction", "net", "earning_item", "deduction_item")
                else r"\d{1,13}(?:\.\d{1,2})?"
            )
            if value not in (None, "") and (
                not re.fullmatch(pattern, value) or key in ("worked_minutes", "slip_minutes") and "." in value
            ):
                raise ValueError("Manual values must be nonnegative decimal strings; minutes integral")
        return values

    @field_validator("values")
    @classmethod
    def contract_values(cls, values):
        if set(values) - set(FIELDS):
            raise ValueError("Unknown extracted field")
        for key, value in values.items():
            if value is not None and len(value) > 100:
                raise ValueError("Field too long")
            if (
                key == "contract_rate"
                and value not in (None, "")
                and not re.fullmatch(r"\d{1,13}(?:\.\d{1,2})?", value)
            ):
                raise ValueError("Rate must be a nonnegative decimal string")
            if key in ("contract_start", "contract_end") and value:
                from datetime import date

                date.fromisoformat(value)
        return values

    def validate_for_analysis(self):
        if bool(self.period_start) != bool(self.period_end):
            raise ValueError("Both analysis period dates are required")
        if self.period_start:
            Period(start=self.period_start, end=self.period_end)
        if (
            any(self.manual.get(k) not in (None, "") for k in ("worked_minutes", "slip_minutes"))
            and not self.time_kind
        ):
            raise ValueError("Time basis actual/paid must be confirmed")
        if self.manual.get("slip_rate") not in (None, "") and not self.slip_unit:
            raise ValueError("Statement rate unit must be confirmed")
        return self


def synthetic_auxiliary(doc_id):
    if doc_id not in ("filled-hourly", "filled-monthly", "filled-missing"):
        raise ValueError("Synthetic auxiliary data is available for filled test samples only")
    base = {
        "period_start": "2026-10-01",
        "period_end": "2026-10-31",
        "time_kind": "actual",
        "synthetic_auxiliary": True,
        "contract_period_confirmed": True,
        "earnings_complete": True,
        "deductions_complete": True,
        "payment_scope_confirmed": True,
    }
    if doc_id == "filled-hourly":
        base["slip_unit"] = "KRW/hour"
        base["manual"] = {
            "slip_rate": "14000",
            "worked_minutes": "480",
            "slip_minutes": "480",
            "gross": "112000",
            "deduction": "0",
            "net": "112000",
            "deposit": "112000",
            "earning_item": "112000",
            "deduction_item": "0",
        }
    elif doc_id == "filled-monthly":
        base["slip_unit"] = "KRW/month"
        base["manual"] = {
            "slip_rate": "3000000",
            "gross": "3000000",
            "deduction": "300000",
            "net": "2700000",
            "deposit": "2600000",
            "earning_item": "3000000",
            "deduction_item": "300000",
        }
    else:
        base.update(
            manual={}, earnings_complete=False, deductions_complete=False, payment_scope_confirmed=False
        )
    return base


def make_snapshot(case, document_ref, confirmation):
    period = Period(start=confirmation.period_start, end=confirmation.period_end)
    version = case["confirmed_revision"]
    manual_data = str(confirmation.model_dump()).encode()
    manual_ref = SourceRef(
        source_id=identifier("manual"),
        owner_id="owner-local",
        workplace_id="workplace-local",
        link_id=identifier("link"),
        link_version=1,
        document_id=identifier("manualdoc"),
        document_version=version,
        content_hash=content_hash(manual_data),
        kind="manual",
    )
    evidence = EvidenceRef(
        evidence_id=identifier("confirmed"),
        source_id=manual_ref.source_id,
        quote="사용자가 확인한 가상 보조자료"
        if confirmation.synthetic_auxiliary
        else "사용자가 확인한 수기 입력",
        verification_method="manual_confirmed",
    )
    fields = []
    values = {
        k: (v, "minute" if k in ("worked_minutes", "slip_minutes") else "KRW")
        for k, v in confirmation.manual.items()
        if k in ("worked_minutes", "slip_minutes", "gross", "deduction", "net") and v not in (None, "")
    }
    unit = confirmation.contract_unit
    if unit in ("KRW/hour", "KRW/month"):
        values["contract_rate"] = (confirmation.values.get("contract_rate"), unit)
        values["slip_rate"] = (confirmation.manual.get("slip_rate"), confirmation.slip_unit or unit)
    for key, (value, value_unit) in values.items():
        fields.append(
            ConfirmedField(
                field_id=key,
                value=value if value not in (None, "") else None,
                state="known" if value not in (None, "") else "unknown",
                unit=value_unit,
                confirmation_id=evidence.evidence_id + "-" + key,
                confirmation_version=version,
                evidence_refs=[evidence.evidence_id],
                period=(
                    Period(
                        start=confirmation.values["contract_start"], end=confirmation.values["contract_end"]
                    )
                    if key == "contract_rate"
                    and confirmation.values.get("contract_start")
                    and confirmation.values.get("contract_end")
                    else period
                    if key != "contract_rate" or confirmation.contract_period_confirmed
                    else None
                ),
                time_kind=confirmation.time_kind or None if value_unit == "minute" else None,
            )
        )
    manual = confirmation.manual
    snapshot = InputSnapshot(
        snapshot_id=identifier("snapshot"),
        input_version=version,
        owner_id="owner-local",
        workplace_id="workplace-local",
        case_id=case["id"],
        period=period,
        profile="M-STD" if unit == "KRW/month" else "H-STD",
        confirmed_fields=fields,
        authorized_sources=[document_ref, manual_ref],
        evidence_catalog=[evidence],
        earnings=[
            StatementItem(
                item_id="earning", amount=manual["earning_item"], evidence_refs=[evidence.evidence_id]
            )
        ]
        if manual.get("earning_item") not in (None, "")
        else [],
        deductions=[
            StatementItem(
                item_id="deduction", amount=manual["deduction_item"], evidence_refs=[evidence.evidence_id]
            )
        ]
        if manual.get("deduction_item") not in (None, "")
        else [],
        earnings_complete=confirmation.earnings_complete,
        deductions_complete=confirmation.deductions_complete,
        payment_scope_confirmed=confirmation.payment_scope_confirmed,
        transaction_allocations=[
            TransactionAllocation(
                transaction_id=identifier("deposit"),
                amount=manual["deposit"],
                transaction_amount=manual["deposit"],
                evidence_refs=[evidence.evidence_id],
            )
        ]
        if manual.get("deposit") not in (None, "")
        else [],
    )
    snapshot.snapshot_hash = snapshot_hash(snapshot)
    return snapshot, manual_ref, manual_data


class LocalPorts(FakePorts):
    def __init__(self, store, job):
        super().__init__(directory=store.state / "artifacts-and-checkpoints")
        self.store, self.job = store, job
        self.current_tokens, self.jobs = store.current_tokens, store.job_owners

    async def emit_progress(self, context, event):
        await super().emit_progress(context, event)
        self.job["events"].append(event.model_dump(mode="json"))
        self.store.save_job(self.job)


class LocalStore:
    def __init__(self, settings, *, corpus=DEFAULT_OUTPUT, state=None, provider=None, origin="local_console"):
        self.settings, self.corpus, self.provider = settings, Path(corpus), provider
        self.origin = origin
        self.state = Path(state) if state else self.corpus / "local-state"
        for folder in ("cases", "jobs", "uploads", "artifacts-and-checkpoints"):
            (self.state / folder).mkdir(parents=True, exist_ok=True)
        manifest_path = self.corpus / "manifest.json"
        if not manifest_path.exists():
            write_json(manifest_path, {"version": "local-empty-v1", "count": 0, "documents": []})
        self.documents = {d["id"]: d for d in read_json(manifest_path)["documents"]}
        self.cases = {p.stem: read_json(p) for p in (self.state / "cases").glob("*.json")}
        self.jobs = {p.stem: read_json(p) for p in (self.state / "jobs").glob("*.json")}
        self.tasks, self.ports = {}, {}
        self.current_tokens, self.job_owners = {}, {}
        self.semaphore = asyncio.Semaphore(2)
        for case in self.cases.values():
            case.setdefault("document_type_revision", 1)
            case.setdefault("document_type", (case.get("extraction") or {}).get("document_type", "contract"))
            case.setdefault("extraction_revision", 1 if case.get("extraction") else 0)
            case.setdefault("materials", None)
            case.setdefault("materials_revision", 0)
            case.setdefault("confirmed_materials", None)
            case.setdefault("materials_invalid", False)
            case.setdefault("analysis_history", [])
            if case.get("upload"):
                self.documents[case["document_id"]] = case["upload"]
        for job in self.jobs.values():
            if job["status"] in ("queued", "running"):
                job.update(status="interrupted", error="서버가 중단되었습니다. 이어서 실행할 수 있습니다.")
                self.save_job(job)

    def save_case(self, case):
        write_json(self.state / "cases" / (case["id"] + ".json"), case)

    def save_job(self, job):
        write_json(self.state / "jobs" / (job["id"] + ".json"), job)

    def _refresh_materials(self, case):
        from agent.dev.comparison_materials import materials_current

        if case.get("materials") and not materials_current(self, case["materials"]):
            if not case.get("materials_invalid"):
                case.update(
                    materials_invalid=True,
                    dirty=True,
                    revision=case["revision"] + 1,
                    stale=bool(case["analysis"]),
                    explanation=None,
                    report=None,
                )
                self.save_case(case)
        return case

    def _invalidate_material_dependents(self, source_case_id):
        for dependent in self.cases.values():
            if source_case_id in (dependent.get("materials") or {}).get("source_versions", {}):
                self._refresh_materials(dependent)

    async def list_comparison_sources(self):
        result = []
        for case in self.cases.values():
            fields = {
                f["field_id"]: f.get("normalized_value")
                for f in (case.get("extraction") or {}).get("fields", [])
            }
            result.append(
                {
                    "case_id": case["id"],
                    "document_id": case["document_id"],
                    "title": self.documents[case["document_id"]]["title"],
                    "document_type": case["document_type"],
                    "revision": case["revision"],
                    "has_extraction": bool(case.get("extraction")),
                    "period_start": fields.get("period_start"),
                    "period_end": fields.get("period_end"),
                }
            )
        return result

    async def load_materials(self, case_id, selection):
        from agent.dev.comparison_materials import build_materials

        case = self.cases[case_id]
        if any(self.jobs[j]["status"] in ("queued", "running") for j in case["jobs"]):
            raise ComparisonInputError("실행 중인 작업을 마친 뒤 대조 자료를 변경해 주세요.")
        expected = {"contract_case_id", "attendance_case_ids", "payslip_case_id", "deposit_case_ids"}
        if not isinstance(selection, dict) or set(selection) - expected:
            raise ComparisonInputError("대조할 문서를 유형에 맞게 선택해 주세요.")
        for key in ("attendance_case_ids", "deposit_case_ids"):
            if not isinstance(selection.get(key, []), list) or any(
                not isinstance(v, str) for v in selection.get(key, [])
            ):
                raise ComparisonInputError("대조 자료 목록을 확인해 주세요.")
        for key in ("contract_case_id", "payslip_case_id"):
            if selection.get(key) is not None and not isinstance(selection[key], str):
                raise ComparisonInputError("대조할 문서를 선택해 주세요.")
        draft = Confirmation(
            period_start=case["draft"].get("period_start", ""),
            period_end=case["draft"].get("period_end", ""),
        ).model_dump()
        materials = None
        if any(selection.get(key) for key in expected):
            try:
                materials = build_materials(self, selection)
            except (ValueError, KeyError) as exc:
                raise ComparisonInputError(
                    str(exc) if isinstance(exc, ValueError) else "선택한 자료를 찾을 수 없습니다."
                ) from exc
            for key, value in materials["suggested"].items():
                if key in draft and value is not None:
                    draft[key] = value
            draft["material_review"] = {
                "earnings": {row["item_id"]: row["amount"] for row in materials["earnings"]},
                "deductions": {row["item_id"]: row["amount"] for row in materials["deductions"]},
                "transactions": {
                    row["transaction_id"]: {
                        "selected": False,
                        "amount": row["amount"],
                        "transaction_amount": row["transaction_amount"],
                    }
                    for row in materials["transactions"]
                },
            }
        case.update(
            materials=materials,
            materials_revision=case["revision"] + 1,
            materials_invalid=False,
            confirmed_materials=None,
            draft=Confirmation.model_validate(draft).model_dump(),
            confirmed=None,
            confirmed_revision=None,
            dirty=True,
            revision=case["revision"] + 1,
            stale=bool(case["analysis"]),
            explanation=None,
            report=None,
        )
        self.save_case(case)
        return copy.deepcopy(case)

    def _validate_material_review(self, case, confirmation, *, allocations=False):
        from decimal import Decimal, InvalidOperation

        from agent.dev.comparison_materials import materials_current, reviewed_duplicate_groups

        materials = case.get("materials")
        review = confirmation.material_review.model_dump()
        if not materials:
            if any(review.values()):
                raise ComparisonInputError("자료를 먼저 연결한 뒤 상세 항목을 확인해 주세요.")
            return
        if confirmation.synthetic_auxiliary:
            raise ComparisonInputError(
                "실제 연결 자료와 가상 보조자료를 함께 사용할 수 없습니다. 가상 보조자료를 해제해 주세요."
            )
        if not materials_current(self, materials):
            raise ComparisonInputError("연결한 문서가 변경되었습니다. 자료를 다시 불러온 뒤 확인해 주세요.")
        for group, key in (
            ("earnings", "item_id"),
            ("deductions", "item_id"),
            ("transactions", "transaction_id"),
        ):
            ids = {row[key] for row in materials[group]}
            if set(review[group]) - ids:
                raise ComparisonInputError("연결 자료에 없는 상세 항목입니다. 자료를 다시 불러와 주세요.")
        selected = {}
        for row in materials["transactions"]:
            item = review["transactions"].get(row["transaction_id"], {})
            if not item.get("selected"):
                continue
            try:
                amount = Decimal(item.get("amount") or "")
                original = Decimal(
                    item.get("transaction_amount")
                    if item.get("transaction_amount") is not None
                    else row["transaction_amount"] or ""
                )
                if not amount.is_finite() or amount < 0 or amount > original:
                    raise ValueError
            except InvalidOperation, ValueError, TypeError:
                raise ComparisonInputError(
                    "선택한 거래의 배분액은 확인된 입금액 이내의 숫자로 입력해 주세요."
                ) from None
            tid = row["transaction_id"]
            if tid in selected:
                raise ComparisonInputError("같은 입금 거래가 중복 선택됐습니다. 연결 자료를 확인해 주세요.")
            selected[tid] = (amount, original)
        if not allocations:
            return
        effective_groups = reviewed_duplicate_groups(materials, review)
        groups = {}
        for row in materials["transactions"]:
            group = effective_groups.get(row["transaction_id"])
            if group and row["transaction_id"] in selected:
                groups[group] = groups.get(group, 0) + 1
        if any(count > 1 for count in groups.values()) and not review["distinct_transactions_confirmed"]:
            raise ComparisonInputError(
                "날짜·금액이 같은 입금 중복 후보가 있습니다. 하나만 선택하거나 각각 별개 거래인지 확인해 주세요."
            )
        used = {}
        for other in self.cases.values():
            saved = other.get("confirmed_materials")
            if (
                other["id"] == case["id"]
                or other.get("dirty")
                or not saved
                or not materials_current(self, saved)
            ):
                continue
            other_review = (other.get("confirmed") or {}).get("material_review", {}).get("transactions", {})
            for tid, item in other_review.items():
                if item.get("selected") and item.get("amount") not in (None, ""):
                    used[tid] = used.get(tid, Decimal(0)) + Decimal(item["amount"])
        for tid, (amount, original) in selected.items():
            if used.get(tid, Decimal(0)) + amount > original:
                raise ComparisonInputError(
                    "다른 대조 사례에 배분한 금액과 합치면 입금액을 초과합니다. 거래 배분액을 조정해 주세요."
                )

    def document_data(self, doc_id):
        doc = self.documents[doc_id]
        data = (
            (self.state / "uploads" / (doc_id + ".pdf"))
            if doc["kind"] == "upload"
            else self.corpus / doc["pdf"]
        ).read_bytes()
        if content_hash(data) != doc["sha256"]:
            raise ValueError("자료 해시가 달라졌습니다.")
        return data

    def document_ref(self, doc_id):
        doc = self.documents[doc_id]
        return SourceRef(
            source_id="source-" + doc_id,
            owner_id="owner-local",
            workplace_id="workplace-local",
            link_id="link-" + doc_id,
            link_version=1,
            document_id=doc_id,
            document_version=1,
            content_hash=doc["sha256"],
        )

    async def list_documents(self):
        evaluation = (
            read_json(self.corpus / "evaluation.json") if (self.corpus / "evaluation.json").exists() else {}
        )
        scores = {r["document_id"]: r for r in evaluation.get("records", [])}
        return [
            {
                **{k: d[k] for k in ("id", "kind", "category", "title", "pages")},
                "evaluation_status": scores.get(d["id"], {}).get("status", "pending"),
            }
            for d in self.documents.values()
        ]

    async def create_case(self, doc_id, document_type="contract"):
        validate_document_type(document_type)
        if doc_id not in self.documents:
            raise KeyError("자료를 찾을 수 없습니다.")
        case = {
            "id": identifier("case"),
            "document_id": doc_id,
            "document_type": document_type,
            "document_type_revision": 1,
            "revision": 1,
            "confirmed_revision": None,
            "dirty": True,
            "draft": Confirmation().model_dump(),
            "confirmed": None,
            "extraction": None,
            "extraction_revision": 0,
            "materials": None,
            "materials_revision": 0,
            "confirmed_materials": None,
            "materials_invalid": False,
            "analysis_history": [],
            "analysis": None,
            "explanation": None,
            "report": None,
            "jobs": [],
            "stale": False,
        }
        if self.documents[doc_id]["kind"] == "upload":
            case["upload"] = self.documents[doc_id]
        self.cases[case["id"]] = case
        self.save_case(case)
        return copy.deepcopy(case)

    async def upload(self, data, document_type="contract", filename=None):
        validate_document_type(document_type)
        if not data.startswith(b"%PDF-") or len(data) > self.settings.max_file_bytes:
            raise ValueError("20MB 이하 PDF만 업로드할 수 있습니다.")
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted or not 1 <= len(reader.pages) <= self.settings.max_pages:
                raise ValueError("페이지 수 또는 암호화 상태를 확인해 주세요.")
        except Exception as exc:
            raise ValueError("읽을 수 있는 암호화되지 않은 PDF가 필요합니다.") from exc
        doc_id = identifier("upload")
        (self.state / "uploads" / (doc_id + ".pdf")).write_bytes(data)
        self.documents[doc_id] = {
            "id": doc_id,
            "kind": "upload",
            "category": "사용자 업로드",
            "title": re.sub(r"[\x00-\x1f]", "", Path(filename).name)[:160] if filename else "업로드 PDF",
            "pages": len(reader.pages),
            "sha256": content_hash(data),
        }
        return await self.create_case(doc_id, document_type)

    async def change_document_type(self, case_id, document_type):
        validate_document_type(document_type)
        case = self.cases[case_id]
        if any(self.jobs[j]["status"] in ("queued", "running") for j in case["jobs"]):
            raise ValueError("실행 중인 작업을 마친 뒤 문서 유형을 변경해 주세요.")
        if case["document_type"] == document_type:
            return copy.deepcopy(case)
        case.update(
            document_type=document_type,
            document_type_revision=case["revision"] + 1,
            extraction=None,
            draft=Confirmation().model_dump(),
            confirmed=None,
            confirmed_revision=None,
            dirty=True,
            revision=case["revision"] + 1,
            stale=bool(case["analysis"]),
            explanation=None,
            report=None,
            materials=None,
            confirmed_materials=None,
            materials_invalid=False,
        )
        self.save_case(case)
        self._invalidate_material_dependents(case_id)
        return copy.deepcopy(case)

    async def get_case(self, case_id):
        return copy.deepcopy(self._refresh_materials(self.cases[case_id]))

    async def save_draft(self, case_id, draft):
        confirmation = Confirmation.model_validate(draft)
        case = self.cases[case_id]
        self._validate_material_review(case, confirmation)
        if confirmation.synthetic_auxiliary and self.documents[case["document_id"]]["kind"] != "synthetic":
            raise ValueError("가상 보조자료는 가상 작성본에만 연결할 수 있습니다.")
        case.update(
            draft=confirmation.model_dump(),
            revision=case["revision"] + 1,
            dirty=True,
            stale=bool(case["analysis"]),
        )
        self.save_case(case)
        return copy.deepcopy(case)

    async def confirm(self, case_id):
        case = self.cases[case_id]
        self._refresh_materials(case)
        if case.get("materials_invalid"):
            raise ComparisonInputError("연결한 문서가 변경되었습니다. 대조 자료를 다시 불러와 주세요.")
        confirmation = Confirmation.model_validate(case["draft"])
        confirmation.validate_for_analysis()
        if not confirmation.period_start or not confirmation.period_end:
            raise ValueError("대조할 급여 기간의 시작일과 종료일을 입력해 주세요.")
        self._validate_material_review(case, confirmation, allocations=True)
        case.update(
            confirmed=confirmation.model_dump(),
            confirmed_revision=case["revision"],
            confirmed_materials=copy.deepcopy(case.get("materials")),
            dirty=False,
        )
        self.save_case(case)
        return copy.deepcopy(case)

    def public_job(self, job):
        return {k: copy.deepcopy(v) for k, v in job.items() if k not in ("task", "manual_sources")}

    async def get_job(self, job_id):
        return self.public_job(self.jobs[job_id])

    async def create_job(self, case_id, stage, *, use_saved=True, include_original=False):
        if stage not in ("extract", "analyze", "explain", "report"):
            raise ValueError("알 수 없는 작업입니다.")
        case = self.cases[case_id]
        self._refresh_materials(case)
        if any(self.jobs[j]["status"] in ("queued", "running") for j in case["jobs"]):
            raise ValueError("현재 작업이 끝난 뒤 실행해 주세요.")
        if stage != "extract" and (case["dirty"] or case["confirmed_revision"] is None):
            raise ValueError("수정한 값을 먼저 확인해 주세요.")
        if stage in ("explain", "report") and (not case["analysis"] or case["stale"]):
            raise ValueError("최신 입력으로 대조를 실행해 주세요.")
        if stage == "report" and not case["explanation"]:
            raise ValueError("설명을 먼저 생성해 주세요.")
        jid = identifier("job")
        kind = {
            "extract": "extract_document",
            "analyze": "analyze_case",
            "explain": "explain_result",
            "report": "build_report",
        }[stage]
        context = JobContext(
            job_id=jid,
            run_id=identifier("run"),
            execution_token=secrets.token_urlsafe(32),
            owner_id="owner-local",
            case_id=case_id,
            task_kind=kind,
            idempotency_key=jid,
            requested_at=datetime.now(UTC),
        )
        ref, manual_sources = self.document_ref(case["document_id"]), []
        if stage == "extract":
            payload = ExtractionInput(
                source=ref,
                document_type=case["document_type"],
                model_config_version=self.settings.model_config_version,
            )
        elif stage == "analyze":
            snapshot, manual_ref, manual_data = make_snapshot(
                case, ref, Confirmation.model_validate(case["confirmed"])
            )
            if case.get("confirmed_materials"):
                from agent.dev.comparison_materials import enrich_snapshot

                self._validate_material_review(
                    case, Confirmation.model_validate(case["confirmed"]), allocations=True
                )
                snapshot = enrich_snapshot(
                    snapshot, case["confirmed_materials"], case["confirmed"]["material_review"]
                )
            manual_sources = [{"ref": manual_ref.model_dump(mode="json"), "data": manual_data.decode()}]
            payload = AnalysisJobInput(
                snapshot=snapshot, snapshot_id=snapshot.snapshot_id, snapshot_hash=snapshot.snapshot_hash
            )
        elif stage == "explain":
            result = AnalysisResult.model_validate(case["analysis"])
            payload = ExplanationInput(
                result=result,
                allowed_facts=allowed_facts(result),
                model_config_version=self.settings.model_config_version,
            )
        else:
            result = AnalysisResult.model_validate(case["analysis"])
            payload = ReportInput(
                result=result,
                explanation=ExplanationCandidate.model_validate(case["explanation"]),
                explanation_version=str(case["confirmed_revision"]),
                selected_sources=list(
                    {
                        s["ref"]["source_id"]: SourceRef.model_validate(s["ref"])
                        for s in (case.get("confirmed_materials") or {}).get("sources", [])
                    }.values()
                )
                if include_original and case.get("confirmed_materials")
                else [ref]
                if include_original
                else [],
                generated_at=datetime.now(UTC),
            )
        task = AgentTask(context=context, payload=payload)
        # SecretStr dumps mask the execution token. It is deliberately regenerated on resume.
        job = {
            "id": jid,
            "case_id": case_id,
            "stage": stage,
            "document_type": case["document_type"],
            "status": "queued",
            "revision": case["revision"],
            "events": [],
            "error": None,
            "usage": {},
            "use_saved": bool(use_saved),
            "task": task.model_dump(mode="json"),
            "manual_sources": manual_sources,
            "provider_mode": self.settings.provider_mode,
            "origin": self.origin,
            "model_config_version": self.settings.model_config_version,
            "model": self.settings.extraction_model
            if stage == "extract"
            else "Python 규칙"
            if stage == "analyze"
            else self.settings.explanation_model,
            "material_versions": copy.deepcopy((case.get("materials") or {}).get("source_versions", {})),
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.jobs[jid] = job
        case["jobs"].append(jid)
        self.save_case(case)
        self.save_job(job)
        self.tasks[jid] = asyncio.create_task(self._execute(job, task))
        return self.public_job(job)

    async def resume_job(self, job_id):
        job = self.jobs[job_id]
        self._refresh_materials(self.cases[job["case_id"]])
        if any(self.jobs[j]["status"] in ("queued", "running") for j in self.cases[job["case_id"]]["jobs"]):
            raise ValueError("현재 작업이 끝난 뒤 이어서 실행해 주세요.")
        if job["status"] not in ("interrupted", "cancelled", "failed"):
            raise ValueError("중단 또는 실패한 작업만 이어서 실행할 수 있습니다.")
        if self.cases[job["case_id"]]["revision"] != job["revision"]:
            raise ValueError("입력이 바뀌었습니다. 새 작업을 실행해 주세요.")
        if (
            job["provider_mode"] != self.settings.provider_mode
            or job["model_config_version"] != self.settings.model_config_version
        ):
            raise ValueError("이전 작업과 모델 설정이 달라 이어서 실행할 수 없습니다.")
        task = AgentTask.model_validate(job["task"])
        task.context.execution_token = secrets.token_urlsafe(32)
        task.context.run_id = identifier("run")
        job.update(status="queued", error=None)
        job.pop("error_code", None)
        self.save_job(job)
        self.tasks[job_id] = asyncio.create_task(self._execute(job, task))
        return self.public_job(job)

    async def cancel_job(self, job_id):
        job = self.jobs[job_id]
        if job["status"] not in ("queued", "running"):
            return self.public_job(job)
        if job_id in self.ports:
            self.ports[job_id].cancelled.add(job_id)
        self.tasks[job_id].cancel()
        job.update(
            status="cancelled",
            usage_may_be_incomplete=True,
            error="사용자가 작업을 취소했습니다. 이미 시작한 호출의 사용량은 발생할 수 있습니다.",
        )
        self.save_job(job)
        await asyncio.gather(self.tasks[job_id], return_exceptions=True)
        return self.public_job(job)

    async def _execute(self, job, task):
        ports = LocalPorts(self, job)
        self.ports[job["id"]] = ports
        ports.bind(task.context)
        case = self.cases[job["case_id"]]
        try:
            ref = self.document_ref(case["document_id"])
            ports.add_source(ref, self.document_data(case["document_id"]))
            source_refs = (
                task.payload.snapshot.authorized_sources
                if job["stage"] == "analyze"
                else task.payload.selected_sources
                if job["stage"] == "report"
                else []
            )
            for linked_ref in source_refs:
                if linked_ref.kind == "document" and linked_ref.source_id != ref.source_id:
                    ports.add_source(linked_ref, self.document_data(linked_ref.document_id))
            for manual in job["manual_sources"]:
                ports.add_source(SourceRef.model_validate(manual["ref"]), manual["data"].encode())
            async with self.semaphore:
                job["status"] = "running"
                self.save_job(job)
                cached = self.corpus / "results" / (case["document_id"] + ".json")
                outcome = None
                if (
                    job["stage"] == "extract"
                    and case["document_type"] == "contract"
                    and job["use_saved"]
                    and cached.exists()
                ):
                    from agent.schemas.public.tasks import AgentOutcome

                    saved = AgentOutcome.model_validate(read_json(cached))
                    proof_path = self.corpus / "usage" / (case["document_id"] + ".inputs.json")
                    proof = read_json(proof_path) if proof_path.exists() else {}
                    if proof.get("pdf_hash") == ref.content_hash and (
                        saved.status != "completed" or saved.payload.document_type == case["document_type"]
                    ):
                        outcome = saved
                        job["cache_source"] = (
                            "실제 평가의 저장 결과 (추가 호출 없음)"
                            if saved.status == "completed"
                            else "실제 평가의 저장된 실패 결과 (추가 호출 없음)"
                        )
                if outcome is None:
                    outcome = await run_task(
                        task, ports=ports, settings=self.settings, provider=self.provider
                    )
                if self.current_tokens.get(job["id"]) != task.context.execution_token.get_secret_value():
                    return
                if job["stage"] != "extract":
                    self._refresh_materials(case)
                if case["revision"] != job["revision"]:
                    job.update(
                        status="superseded", error="입력이 바뀌어 이전 작업 결과를 적용하지 않았습니다."
                    )
                elif outcome.status != "completed":
                    job.update(
                        status=outcome.status, error=outcome.error.message, error_code=outcome.error.code
                    )
                else:
                    self._apply(case, job, outcome.payload)
                    job["status"] = "completed"
                job["usage"] = {} if job.get("cache_source") else outcome.usage
                self.save_case(case)
                self.save_job(job)
        except asyncio.CancelledError:
            job.update(
                status="cancelled",
                error="작업이 취소되었습니다. 이어서 실행하면 누적 호출 예산을 복구합니다.",
            )
            self.save_job(job)
        except Exception:
            job.update(
                status="failed",
                error="로컬 연결 작업에 실패했습니다. 원본·입력과 작업 상태를 확인해 주세요.",
                error_code="LOCAL_ADAPTER_ERROR",
            )
            self.save_job(job)
        finally:
            checkpoint = ports.checkpoints.get(job["id"])
            if (
                checkpoint
                and self.current_tokens.get(job["id"]) == task.context.execution_token.get_secret_value()
            ):
                job["call_budget"] = dict(checkpoint.call_budget)
                job["usage"] = dict(checkpoint.payload.get("__usage__", {}))
                self.save_job(job)
                if self.settings.provider_mode == "openai" and job.get("origin") == "local_console":
                    write_json(
                        self.corpus / "usage" / ("interactive-" + job["id"] + ".json"),
                        {
                            "usage": job["usage"],
                            "job_id": job["id"],
                            "status": job["status"],
                            "call_budget": job["call_budget"],
                            "model_calls": 0
                            if job.get("cache_source")
                            else sum(
                                v for k, v in job["call_budget"].items() if not k.endswith(":format_failures")
                            ),
                            "usage_may_be_incomplete": bool(job.get("usage_may_be_incomplete"))
                            or job["status"] in ("cancelled", "interrupted"),
                        },
                    )

    def _apply(self, case, job, payload):
        stage = job["stage"]
        if stage == "extract":
            if payload.document_type != case["document_type"]:
                raise ValueError("추출 결과와 선택한 문서 유형이 다릅니다.")
            case["extraction"] = payload.model_dump(mode="json")
            case["extraction_revision"] = case.get("extraction_revision", 0) + 1
            case.update(materials=None, confirmed_materials=None, materials_invalid=False)
            draft = Confirmation().model_dump()
            draft["values"] = {f.field_id: f.normalized_value for f in payload.fields if f.field_id in FIELDS}
            if case["document_type"] != "contract":
                draft["manual"] = {
                    f.field_id: f.normalized_value
                    for f in payload.fields
                    if f.field_id in MANUAL_FIELDS and f.normalized_value is not None
                }
                # Only use an explicitly printed total; never substitute a transaction balance or sum.
                deposit_total = next((f for f in payload.fields if f.field_id == "deposit_total"), None)
                if (
                    case["document_type"] == "deposit"
                    and deposit_total
                    and deposit_total.normalized_value is not None
                ):
                    draft["manual"]["deposit"] = deposit_total.normalized_value
                for key in ("period_start", "period_end"):
                    value = next((f.normalized_value for f in payload.fields if f.field_id == key), None)
                    if value:
                        draft[key] = value
                slip = next((f for f in payload.fields if f.field_id == "slip_rate"), None)
                if slip and slip.unit in ("KRW/hour", "KRW/month", "KRW/year", "KRW/day"):
                    draft["slip_unit"] = slip.unit
            rate = next((f for f in payload.fields if f.field_id == "contract_rate"), None)
            if rate and rate.unit in ("KRW/hour", "KRW/month", "KRW/year", "KRW/day"):
                draft["contract_unit"] = rate.unit
            case.update(draft=draft, revision=case["revision"] + 1, dirty=True, stale=bool(case["analysis"]))
            self._invalidate_material_dependents(case["id"])
        elif stage == "analyze":
            history = case.setdefault("analysis_history", [])
            if case["analysis"] and not any(
                entry["analysis"]["result_id"] == case["analysis"]["result_id"] for entry in history
            ):
                history.append(
                    {
                        "analysis": copy.deepcopy(case["analysis"]),
                        "period": copy.deepcopy(case.get("analysis_period")),
                        "materials": copy.deepcopy(case.get("analysis_materials")),
                        "snapshot": copy.deepcopy(case.get("analysis_snapshot")),
                    }
                )
            snapshot = copy.deepcopy(job["task"]["payload"]["snapshot"])
            case["analysis_period"] = snapshot["period"]
            result = AnalysisResult(
                **payload.model_dump(),
                result_id=identifier("result"),
                result_version=case["confirmed_revision"],
                freshness="current",
                created_at=datetime.now(UTC),
            )
            case.update(analysis=result.model_dump(mode="json"), explanation=None, report=None, stale=False)
            case["analysis_materials"] = copy.deepcopy(case.get("confirmed_materials"))
            case["analysis_snapshot"] = snapshot
            history.append(
                {
                    "analysis": copy.deepcopy(case["analysis"]),
                    "period": copy.deepcopy(snapshot["period"]),
                    "materials": copy.deepcopy(case["analysis_materials"]),
                    "snapshot": snapshot,
                }
            )
        elif stage == "explain":
            case.update(explanation=payload.model_dump(mode="json"), report=None)
        else:
            case["report"] = payload.model_dump(mode="json")
            for artifact in payload.artifacts:
                self.jobs[job["id"]].setdefault("downloads", []).append(
                    {
                        "id": artifact.artifact_id,
                        "mime_type": artifact.mime_type,
                        "size_bytes": artifact.size_bytes,
                    }
                )

    async def artifact(self, artifact_id):
        for case in self.cases.values():
            if case["report"] and not case["stale"] and not case["dirty"]:
                for value in case["report"]["artifacts"]:
                    if value["artifact_id"] == artifact_id:
                        from agent.schemas.public.results import ArtifactRef

                        ref = ArtifactRef.model_validate(value)
                        ports = FakePorts(directory=self.state / "artifacts-and-checkpoints")
                        return ports.artifact_bytes(ref), ref.mime_type
        raise KeyError("최신 산출물을 찾을 수 없습니다.")
