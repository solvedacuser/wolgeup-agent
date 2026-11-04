import asyncio

from pydantic import ValidationError

from agent.config import SCHEMA_VERSION
from agent.providers.fixture_provider import FixtureProvider
from agent.providers.model_provider import OpenAIProvider
from agent.rules.registry import RuleRegistry
from agent.runtime import Runtime
from agent.schemas.public.errors import (
    AccessRevoked,
    AgentError,
    ExecutionRevoked,
    IntegrityError,
    JobCancelled,
    OCRFailed,
    OCRUnavailable,
)
from agent.schemas.public.tasks import AgentFailure, AgentOutcome, AgentTask
from agent.services.hashing import result_hash


async def run_task(task, *, ports, settings, provider=None, registry=None):
    if not isinstance(task, AgentTask):
        # Malformed envelopes have no trustworthy job identifier; reject before constructing an outcome.
        task = AgentTask.model_validate(task)
    context = task.context
    runtime = None
    try:
        if context.schema_version != SCHEMA_VERSION:
            raise IntegrityError("Unsupported public schema version")
        if settings.provider_mode not in ("fixture", "openai"):
            raise IntegrityError("Unknown provider mode")
        if provider is None:
            provider = FixtureProvider() if settings.provider_mode == "fixture" else OpenAIProvider(settings)
        payload = task.payload
        if context.task_kind == "extract_document":
            sources = [payload.source]
        elif context.task_kind == "analyze_case":
            sources = payload.snapshot.authorized_sources
        else:
            if payload.result.owner_id != context.owner_id or payload.result.case_id != context.case_id:
                raise IntegrityError("Confirmed result ownership mismatch")
            if result_hash(payload.result) != payload.result.result_hash:
                raise IntegrityError("Confirmed result hash mismatch")
            sources = payload.selected_sources if context.task_kind == "build_report" else []
        if any(s.owner_id != context.owner_id for s in sources):
            raise IntegrityError("Source owner mismatch")
        if context.task_kind in ("extract_document", "explain_result"):
            if payload.model_config_version != settings.model_config_version:
                raise IntegrityError("Model configuration version mismatch")
            expected_prompt = "extract-v1" if context.task_kind == "extract_document" else "explain-v1"
            if payload.prompt_version != expected_prompt:
                raise IntegrityError("Unsupported prompt version")
            if context.task_kind == "extract_document" and payload.extraction_version != "1.0":
                raise IntegrityError("Unsupported extraction schema version")
        runtime = Runtime(task, ports, settings, provider)
        await runtime.start(sources)
        if context.task_kind == "analyze_case":
            from agent.graphs.analysis_graph import build_analysis_graph

            result = await build_analysis_graph(runtime, registry or RuleRegistry()).ainvoke({})
            output = result["result"]
        elif context.task_kind == "extract_document":
            from agent.graphs.extraction_graph import build_extraction_graph

            output = (await build_extraction_graph(runtime).ainvoke({}))["result"]
        elif context.task_kind == "explain_result":
            from agent.graphs.explanation_graph import build_explanation_graph

            output = (await build_explanation_graph(runtime).ainvoke({}))["result"]
        else:
            from agent.services.reports import build_report

            output = await runtime.step("build_report", lambda: build_report(runtime))
        await runtime.control()
        return AgentOutcome(
            provider_mode=settings.provider_mode,
            job_id=context.job_id,
            run_id=context.run_id,
            status="completed",
            payload=output,
            usage=runtime.usage,
        )
    except (JobCancelled, AccessRevoked, ExecutionRevoked) as exc:
        return AgentOutcome(
            provider_mode=settings.provider_mode,
            job_id=context.job_id,
            run_id=context.run_id,
            status="cancelled",
            error=AgentFailure(code=exc.code, message="작업이 취소되었거나 접근·실행 권한이 철회되었습니다."),
            usage=runtime.usage if runtime else {},
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if isinstance(exc, AgentError):
            code, retryable = exc.code, exc.retryable
        elif isinstance(exc, ValidationError):
            code, retryable = "RESULT_SCHEMA_INVALID", False
        elif isinstance(exc, TimeoutError):
            code, retryable = "MODEL_TIMEOUT", True
        else:
            code, retryable = "INTERNAL_ERROR", False
        return AgentOutcome(
            provider_mode=settings.provider_mode,
            job_id=context.job_id,
            run_id=context.run_id,
            status="failed",
            error=AgentFailure(
                code=code,
                message=(
                    str(exc)
                    if isinstance(exc, (OCRUnavailable, OCRFailed))
                    else "선택한 문서 유형과 원문이 다릅니다. 문서 유형을 변경한 뒤 다시 추출해 주세요."
                    if getattr(exc, "reason", None) == "DOCUMENT_TYPE_MISMATCH"
                    else "처리에 실패했습니다. 코드와 작업 ID로 확인해 주세요."
                ),
                retryable=retryable,
            ),
            usage=runtime.usage if runtime else {},
        )
