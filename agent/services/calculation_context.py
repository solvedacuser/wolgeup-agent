from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from agent.schemas.internal.calculation_context import CalculationContext
from agent.schemas.public.errors import IntegrityError
from agent.schemas.public.inputs import InputSnapshot
from agent.services.hashing import digest, snapshot_hash


def build_calculation_context(snapshot: InputSnapshot, owner_id: str, case_id: str):
    if snapshot.owner_id != owner_id or snapshot.case_id != case_id:
        raise IntegrityError("Snapshot ownership does not match job")
    if snapshot.snapshot_hash != snapshot_hash(snapshot):
        raise IntegrityError("Snapshot hash mismatch")
    fields = [f.field_id for f in snapshot.confirmed_fields]
    if len({r.record_id for r in snapshot.work_records}) != len(snapshot.work_records):
        raise IntegrityError("Duplicate work record IDs")
    sources = {s.source_id: s for s in snapshot.authorized_sources}
    evidence = {e.evidence_id: e for e in snapshot.evidence_catalog}
    if (
        len(fields) != len(set(fields))
        or len(sources) != len(snapshot.authorized_sources)
        or len(evidence) != len(snapshot.evidence_catalog)
    ):
        raise IntegrityError("Duplicate input IDs")
    field_map = {f.field_id: f for f in snapshot.confirmed_fields}
    if len({c.field_id for c in snapshot.condition_states}) != len(snapshot.condition_states):
        raise IntegrityError("Duplicate condition state")
    for condition in snapshot.condition_states:
        if condition.state == "known":
            confirmed = field_map.get(condition.confirmed_field_ref)
            if confirmed is None or confirmed.state != "known" or confirmed.field_id != condition.field_id:
                raise IntegrityError("Known condition must reference its confirmed field")
        elif condition.confirmed_field_ref is not None:
            raise IntegrityError("Unconfirmed condition cannot select a confirmed value")
        if any(ref not in sources for ref in condition.conflicting_source_refs):
            raise IntegrityError("Condition conflict refers to an unauthorized source")
    for source in sources.values():
        if (
            source.owner_id != owner_id
            or source.workplace_id != snapshot.workplace_id
            or source.status != "active"
        ):
            raise IntegrityError("Source authorization mismatch")
    for ev in evidence.values():
        if ev.source_id not in sources:
            raise IntegrityError("Evidence refers to unauthorized source")
        if sources[ev.source_id].kind == "document" and ev.page is None:
            raise IntegrityError("Document evidence requires a page")
    for record in [
        *snapshot.confirmed_fields,
        *snapshot.earnings,
        *snapshot.deductions,
        *snapshot.transaction_allocations,
        *snapshot.work_records,
    ]:
        if any(ref not in evidence for ref in record.evidence_refs):
            raise IntegrityError("Unknown evidence ID")
        if (
            not record.evidence_refs
            and getattr(record, "state", "known") == "known"
            and getattr(record, "confirmed", True)
        ):
            raise IntegrityError("Confirmed input requires evidence")
    for field in snapshot.confirmed_fields:
        if field.state == "known":
            for ref in field.evidence_refs:
                src = sources[evidence[ref].source_id]
                if src.usage == "context":
                    raise IntegrityError("Context-only source cannot supply payroll scalar")
    dependencies = [f"source:{s.source_id}:{s.document_version}:{s.link_version}" for s in sources.values()]
    dependencies += [f"required-period:{p.start}:{p.end}" for p in snapshot.context_requirements]
    intervals, issues = [], []
    tz = ZoneInfo(snapshot.timezone)
    for record in sorted(snapshot.work_records, key=lambda r: (r.start_at, r.record_id)):
        start, end = record.start_at.astimezone(UTC), record.end_at.astimezone(UTC)
        if end <= start:
            raise IntegrityError("Invalid work interval")
        breaks = sorted((a.astimezone(UTC), b.astimezone(UTC)) for a, b in record.breaks)
        if any(a < start or b > end or b <= a for a, b in breaks):
            raise IntegrityError("Invalid break interval")
        if any(b > c for (_, b), (c, _) in zip(breaks, breaks[1:])):
            issues.append(f"break_overlap:{record.record_id}")
        if not record.break_location_known:
            issues.append(f"break_location_unknown:{record.record_id}")
        boundaries = {start, end, *(x for pair in breaks for x in pair)}
        day = start.astimezone(tz).date() + timedelta(days=1)
        while datetime.combine(day, time.min, tz).astimezone(UTC) < end:
            boundaries.add(datetime.combine(day, time.min, tz).astimezone(UTC))
            day += timedelta(days=1)
        for a, b in zip(sorted(boundaries), sorted(boundaries)[1:]):
            if (b - a).total_seconds() % 60:
                raise IntegrityError("Work intervals must use whole minutes")
            on_break = any(c <= a and b <= d for c, d in breaks)
            intervals.append(
                {
                    "interval_id": digest([record.record_id, a.isoformat(), b.isoformat()]),
                    "origin_record_id": record.record_id,
                    "start": a.isoformat(),
                    "end": b.isoformat(),
                    "minutes": int((b - a).total_seconds() // 60),
                    "on_break": on_break,
                    "evidence_refs": sorted(record.evidence_refs),
                }
            )
    records = sorted(snapshot.work_records, key=lambda r: r.start_at)
    for left, right in zip(records, records[1:]):
        if left.end_at > right.start_at:
            issues.append("overlapping_work_sources")
    for required in snapshot.context_requirements:
        if not any(c.start <= required.start and c.end >= required.end for c in snapshot.coverage):
            issues.append(f"missing_context:{required.start}:{required.end}")
    issues.sort()
    dependencies.sort()
    context_hash = digest(
        {
            "snapshot": snapshot.snapshot_hash,
            "intervals": intervals,
            "issues": issues,
            "normalizer": "normalizer-v1",
        }
    )
    return CalculationContext(
        snapshot_hash=snapshot.snapshot_hash,
        context_hash=context_hash,
        snapshot=snapshot,
        normalized_intervals=intervals,
        issues=issues,
        dependencies=dependencies,
    )
