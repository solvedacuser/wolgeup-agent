from typing import Literal

from pydantic import Field, SecretStr, model_validator

from agent.config import ENGINE_VERSION
from agent.schemas.public.base import AwareDateTime, ContractModel
from agent.schemas.public.inputs import InputSnapshot, SourceRef
from agent.schemas.public.results import (
    AnalysisResult,
    AnalysisResultCandidate,
    ExplanationCandidate,
    ExtractionCandidate,
    ReportCandidate,
)

TaskKind = Literal["extract_document", "analyze_case", "explain_result", "build_report"]


class JobContext(ContractModel):
    job_id: str
    run_id: str
    execution_token: SecretStr
    owner_id: str
    case_id: str
    task_kind: TaskKind
    schema_version: str = "1.0"
    idempotency_key: str
    requested_at: AwareDateTime


class ExtractionInput(ContractModel):
    source: SourceRef
    document_type: Literal["contract", "attendance", "payslip", "deposit", "payslip_ocr"]
    extraction_version: str = "1.0"
    prompt_version: str = "extract-v1"
    model_config_version: str = "luna-v1"


class AnalysisJobInput(ContractModel):
    snapshot: InputSnapshot
    snapshot_id: str
    snapshot_hash: str
    rule_bundle_version: str = "pending-v1"
    support_manifest_version: str = "pending-v1"
    engine_version: str = ENGINE_VERSION


class ExplanationInput(ContractModel):
    result: AnalysisResult
    allowed_facts: dict[str, str]
    prompt_version: str = "explain-v1"
    model_config_version: str = "luna-v1"


class ReportInput(ContractModel):
    result: AnalysisResult
    explanation: ExplanationCandidate
    explanation_version: str
    selected_sources: list[SourceRef] = Field(default_factory=list)
    generated_at: AwareDateTime
    template_version: str = "report-v1"


Payload = ExtractionInput | AnalysisJobInput | ExplanationInput | ReportInput


class AgentTask(ContractModel):
    context: JobContext
    payload: Payload

    @model_validator(mode="after")
    def matching_payload(self):
        types = {
            "extract_document": ExtractionInput,
            "analyze_case": AnalysisJobInput,
            "explain_result": ExplanationInput,
            "build_report": ReportInput,
        }
        if not isinstance(self.payload, types[self.context.task_kind]):
            raise ValueError("task_kind does not match payload")
        return self


class AgentFailure(ContractModel):
    code: str
    message: str
    retryable: bool = False


class AgentOutcome(ContractModel):
    job_id: str
    run_id: str
    schema_version: str = "1.0"
    provider_mode: Literal["fixture", "openai"]
    status: Literal["completed", "failed", "cancelled"]
    payload: AnalysisResultCandidate | ExtractionCandidate | ExplanationCandidate | ReportCandidate | None = (
        None
    )
    error: AgentFailure | None = None
    usage: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def consistent_status(self):
        if self.status == "completed":
            if self.payload is None or self.error is not None:
                raise ValueError("Completed outcome requires only a success payload")
        elif self.payload is not None or self.error is None:
            raise ValueError("Failed/cancelled outcomes require only an error")
        return self
