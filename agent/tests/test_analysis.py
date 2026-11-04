from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent import run_task
from agent.graphs.analysis_graph import build_analysis_graph
from agent.providers.fixture_provider import FixtureProvider
from agent.rules.registry import RuleRegistry, VerifiedRule
from agent.rules.support_manifest import support_manifest
from agent.runtime import Runtime
from agent.schemas.public.inputs import WorkRecord
from agent.services.calculation_context import build_calculation_context
from agent.services.hashing import result_hash, snapshot_hash


def rehash(task):
    task.payload.snapshot.snapshot_hash = snapshot_hash(task.payload.snapshot)
    task.payload.snapshot_hash = task.payload.snapshot.snapshot_hash


async def test_analysis_graph_eight_nodes_and_single_join(setup_agent):
    task, ports, settings = setup_agent
    runtime = Runtime(task, ports, settings, FixtureProvider())
    graph = build_analysis_graph(runtime, RuleRegistry())
    assert len(set(graph.get_graph().nodes) - {"__start__", "__end__"}) == 8
    outcome = await run_task(task, ports=ports, settings=settings)
    assert outcome.status == "completed", outcome.error
    assert outcome.provider_mode == "fixture"
    assert [r.status for r in outcome.payload.comparisons[:6]] == ["match"] * 6
    assert all(r.status == "needs_review" for r in outcome.payload.wage_section.items)
    assert outcome.payload.wage_section.gross_total is None
    assert outcome.payload.comparisons[6].status == "needs_review"
    assert not outcome.payload.wage_section.scope_complete
    assert len(ports.checkpoints[task.context.job_id].completed_nodes) == 8
    assert len([event for event in ports.events if event["node_id"] == "aggregate"]) == 1
    assert result_hash(outcome.payload) == outcome.payload.result_hash


@pytest.mark.parametrize(
    "key,value,rid,delta",
    [
        ("slip_rate", "10500", "R-01", "500"),
        ("slip_minutes", "12100", "R-02", "100"),
        ("gross", "1999999", "R-03", "1"),
        ("deduction", "199999", "R-04", "1"),
        ("net", "1799999", "R-05", "1"),
        ("net", "1799999", "R-06", "-1"),
    ],
)
async def test_difference_signs(setup_agent, key, value, rid, delta):
    task, ports, settings = setup_agent
    next(f for f in task.payload.snapshot.confirmed_fields if f.field_id == key).value = Decimal(value)
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "completed", out.error
    result = next(r for r in out.payload.comparisons if r.rule_id == rid)
    assert result.delta == Decimal(delta)


async def test_missing_does_not_block_independent_checks(setup_agent):
    task, ports, settings = setup_agent
    task.payload.snapshot.confirmed_fields = [
        f for f in task.payload.snapshot.confirmed_fields if f.field_id != "contract_rate"
    ]
    task.payload.snapshot.earnings_complete = False
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "completed"
    assert out.payload.comparisons[0].status == "insufficient"
    assert out.payload.comparisons[2].status == "insufficient"
    assert out.payload.comparisons[4].status == "match"
    assert out.payload.comparisons[5].status == "match"


async def test_no_deposits_is_unknown_not_zero(setup_agent):
    task, ports, settings = setup_agent
    task.payload.snapshot.transaction_allocations = []
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.payload.comparisons[5].rhs is None
    assert out.payload.comparisons[5].delta is None


async def test_cash_and_duplicate_excluded(setup_agent):
    task, ports, settings = setup_agent
    allocation = task.payload.snapshot.transaction_allocations[0]
    cash = allocation.model_copy(update={"transaction_id": "cash", "kind": "cash_memo"})
    duplicate = allocation.model_copy(update={"transaction_id": "duplicate", "duplicate": True})
    task.payload.snapshot.transaction_allocations.extend([cash, duplicate])
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.payload.comparisons[5].rhs == Decimal("1800000")


@pytest.mark.parametrize("change", ["unit", "time_kind", "period"])
async def test_semantic_mismatch_holds_comparison(setup_agent, change):
    task, ports, settings = setup_agent
    field = next(f for f in task.payload.snapshot.confirmed_fields if f.field_id == "slip_minutes")
    if change == "unit":
        field.unit = "hour"
    elif change == "time_kind":
        field.time_kind = "paid"
    else:
        field.period = None
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.payload.comparisons[1].status == "needs_review"
    assert out.payload.comparisons[1].delta is None


async def test_hash_and_evidence_tampering_rejected(setup_agent):
    task, ports, settings = setup_agent
    task.payload.snapshot.input_version = 2
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "failed"
    rehash(task)
    task.payload.snapshot.confirmed_fields[0].evidence_refs = ["invented"]
    rehash(task)
    out = await run_task(task, ports=ports, settings=settings)
    assert out.status == "failed"


def test_snapshot_hash_is_set_order_independent(setup_agent):
    task, _, _ = setup_agent
    snapshot = task.payload.snapshot
    previous = snapshot_hash(snapshot)
    snapshot.confirmed_fields.reverse()
    assert snapshot_hash(snapshot) == previous


def test_cross_midnight_breaks_and_missing_context(setup_agent):
    task, _, _ = setup_agent
    s = task.payload.snapshot
    s.work_records = [
        WorkRecord(
            record_id="night",
            start_at="2026-10-01T22:00:00+09:00",
            end_at="2026-10-02T02:00:00+09:00",
            evidence_refs=["evidence-demo"],
            breaks=[("2026-10-01T23:30:00+09:00", "2026-10-02T00:30:00+09:00")],
        )
    ]
    rehash(task)
    ctx = build_calculation_context(s, s.owner_id, s.case_id)
    assert len(ctx.normalized_intervals) == 4
    assert sum(i["minutes"] for i in ctx.normalized_intervals if not i["on_break"]) == 180


def test_numeric_json_rejected_and_unknown_not_zero(setup_agent):
    task, _, _ = setup_agent
    f = task.payload.snapshot.confirmed_fields[0]
    with pytest.raises(ValidationError):
        type(f).model_validate(f.model_dump() | {"value": 10000})
    with pytest.raises(ValidationError):
        type(f).model_validate(f.model_dump() | {"state": "unknown", "value": "0"})


def test_unverified_rule_cannot_activate():
    from datetime import date

    with pytest.raises(ValueError):
        RuleRegistry(
            [
                VerifiedRule(
                    rule_id="W-01",
                    version="test",
                    profile="H-STD",
                    effective_from=date(2026, 1, 1),
                    effective_to=date(2026, 12, 31),
                    required_fields=(),
                    official_sources=(),
                    verification_cases=(),
                    verification_status="pending",
                    formula_id="unverified",
                    calculate=lambda _: Decimal(10),
                )
            ]
        )
    assert not support_manifest(RuleRegistry())["release_ready"]


async def test_verified_rule_injection_is_explicit_not_default(setup_agent):
    from datetime import date

    task, ports, settings = setup_agent
    registry = RuleRegistry(
        [
            VerifiedRule(
                rule_id="W-01",
                version="TEST-ONLY",
                profile="H-STD",
                effective_from=date(2026, 1, 1),
                effective_to=date(2026, 12, 31),
                required_fields=("contract_rate",),
                official_sources=("test-fixture-not-legal-authority",),
                verification_cases=("independent-fixture",),
                verification_status="passed",
                formula_id="TEST-ONLY",
                calculate=lambda _: Decimal("12.5"),
            )
        ]
    )
    out = await run_task(task, ports=ports, settings=settings, registry=registry)
    assert out.payload.wage_section.items[0].amount == Decimal("13")
    assert out.payload.wage_section.gross_total is None
