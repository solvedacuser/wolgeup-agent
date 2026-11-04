from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent import run_task
from agent.dev.fixtures import confirmed_result, explanation_task
from agent.providers.fixture_provider import FixtureProvider
from agent.schemas.public.inputs import ConditionState, Period
from agent.services.hashing import digest
from agent.tests.test_analysis import rehash


async def test_contract_can_cover_multiple_payroll_periods(setup_agent):
    task, ports, settings = setup_agent
    next(f for f in task.payload.snapshot.confirmed_fields if f.field_id == "contract_rate").period = Period(
        start="2026-01-01", end="2026-12-31"
    )
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.payload.comparisons[0].status == "match"


async def test_condition_conflict_cannot_be_ignored(setup_agent):
    task, ports, settings = setup_agent
    task.payload.snapshot.condition_states = [
        ConditionState(field_id="contract_rate", state="conflict", conflicting_source_refs=["source-demo"])
    ]
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.payload.comparisons[0].status == "needs_review"
    assert out.payload.comparisons[4].status == "match"


async def test_unknown_without_a_numeric_value_is_partial_input(setup_agent):
    task, ports, settings = setup_agent
    field = next(f for f in task.payload.snapshot.confirmed_fields if f.field_id == "contract_rate")
    replacement = field.model_dump() | {"state": "unknown", "value": None, "evidence_refs": []}
    task.payload.snapshot.confirmed_fields = [
        type(field).model_validate(replacement) if f.field_id == field.field_id else f
        for f in task.payload.snapshot.confirmed_fields
    ]
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "completed"
    assert out.payload.comparisons[0].status == "insufficient"
    assert out.payload.comparisons[5].status == "match"


async def test_confirmed_zero_is_compared(setup_agent):
    task, ports, settings = setup_agent
    for field in task.payload.snapshot.confirmed_fields:
        if field.field_id in ("worked_minutes", "slip_minutes"):
            field.value = Decimal(0)
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.payload.comparisons[1].delta == Decimal(0)


async def test_duplicate_and_overallocated_transactions_are_review(setup_agent):
    task, ports, settings = setup_agent
    allocation = task.payload.snapshot.transaction_allocations[0]
    task.payload.snapshot.transaction_allocations.append(allocation.model_copy(deep=True))
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.payload.comparisons[5].status == "needs_review"
    assert out.payload.comparisons[5].delta is None


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "not-a-number"])
def test_invalid_decimal_strings_rejected(setup_agent, invalid):
    task, _, _ = setup_agent
    field = task.payload.snapshot.confirmed_fields[0]
    with pytest.raises(ValidationError):
        type(field).model_validate(field.model_dump() | {"value": invalid})


def test_ordered_prose_hash_preserves_order():
    assert digest({"paragraphs": ["a", "b"]}) != digest({"paragraphs": ["b", "a"]})


async def test_usage_survives_resume(setup_agent):
    task, ports, settings = setup_agent
    analysis = await run_task(task, ports=ports, settings=settings)
    task = explanation_task(confirmed_result(analysis.payload))
    ports.bind(task.context)

    class UsageProvider(FixtureProvider):
        async def invoke(self, operation, data):
            out, _ = await super().invoke(operation, data)
            return out, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    first = await run_task(task, ports=ports, settings=settings, provider=UsageProvider())
    second = await run_task(task, ports=ports, settings=settings, provider=UsageProvider())
    assert first.usage == second.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


async def test_unsupported_schema_and_prompt_do_not_call_provider(setup_agent):
    task, ports, settings = setup_agent
    task.context.schema_version = "unsupported"
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "failed"
    assert not ports.checkpoints


async def test_missing_font_fails_report_instead_of_broken_pdf(setup_agent):
    from pathlib import Path

    from agent.schemas.public.errors import IntegrityError
    from agent.services.reports import render_pdf

    task, ports, settings = setup_agent
    analysis = await run_task(task, ports=ports, settings=settings)
    result = confirmed_result(analysis.payload)
    explain = explanation_task(result)
    ports.bind(explain.context)
    out = await run_task(explain, ports=ports, settings=settings)
    settings.font_path = Path("missing-font.ttf")
    with pytest.raises(IntegrityError):
        render_pdf(result, out.payload, settings, task.context.requested_at)


async def test_checkpoint_version_mismatch_never_reuses_state(setup_agent):
    task, ports, settings = setup_agent
    await run_task(task, ports=ports, settings=settings)
    ports.checkpoints[task.context.job_id].checkpoint_schema_version = "different"
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "failed"
