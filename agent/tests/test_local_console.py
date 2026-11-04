import asyncio
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.config import AgentSettings
from agent.dev.fixtures import pdf_bytes
from agent.dev.local_workflow import Confirmation, LocalPorts, LocalStore, synthetic_auxiliary
from agent.dev.test_server import LocalHTTPServer
from agent.providers.fixture_provider import FixtureProvider
from agent.schemas.public.errors import ExecutionRevoked
from agent.services.hashing import content_hash

pytestmark = pytest.mark.private_corpus


async def run_stage(store, cid, stage, **options):
    job = await store.create_job(cid, stage, **options)
    await store.tasks[job["id"]]
    saved = await store.get_job(job["id"])
    assert saved["status"] == "completed", saved
    return saved


@pytest.mark.parametrize(
    "doc_id,rule,status",
    [
        ("filled-hourly", "R-01", "difference"),
        ("filled-monthly", "R-06", "difference"),
        ("filled-missing", "R-01", "insufficient"),
    ],
)
async def test_full_case_flow_confirm_stale_download_reload(tmp_path, doc_id, rule, status):
    store = LocalStore(AgentSettings(), state=tmp_path)
    case = await store.create_case(doc_id)
    cid = case["id"]
    with pytest.raises(ValueError):
        await store.create_job(cid, "analyze")
    await run_stage(store, cid, "extract")
    case = store.cases[cid]
    assert case["dirty"] and case["draft"]["manual"] == {}
    assert not case["draft"]["synthetic_auxiliary"]
    await store.save_draft(cid, {**case["draft"], **synthetic_auxiliary(doc_id)})
    await store.confirm(cid)
    analysis_job = await run_stage(store, cid, "analyze")
    assert len([e for e in analysis_job["events"] if e["node_id"] == "aggregate"]) == 1
    analysis = case["analysis"]
    assert next(c for c in analysis["comparisons"] if c["rule_id"] == rule)["status"] == status
    assert analysis["wage_section"]["gross_total"] is None
    await run_stage(store, cid, "explain")
    await run_stage(store, cid, "report", include_original=True)
    artifacts = case["report"]["artifacts"]
    zip_ref = next(a for a in artifacts if a["mime_type"] == "application/zip")
    archive_bytes, _ = await store.artifact(zip_ref["artifact_id"])
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        originals = json.loads(archive.read("manifest.json"))
        assert len(originals) == 1
        for name, expected in originals.items():
            assert content_hash(archive.read(name)) == expected
        assert archive.read("summary.pdf").startswith(b"%PDF")
    restored = LocalStore(AgentSettings(), state=tmp_path)
    assert (await restored.get_case(cid))["analysis"] == analysis
    assert (await restored.artifact(zip_ref["artifact_id"]))[0] == archive_bytes
    draft = {**case["draft"], "manual": {**case["draft"]["manual"], "net": "0"}}
    await store.save_draft(cid, draft)
    assert case["stale"] and case["dirty"]
    with pytest.raises(ValueError):
        await store.create_job(cid, "explain")
    with pytest.raises(KeyError):
        await store.artifact(zip_ref["artifact_id"])


async def test_upload_validation_opaque_ids_and_source_hash(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path)
    for invalid in (b"not a pdf", b"%PDF-malformed"):
        with pytest.raises(ValueError):
            await store.upload(invalid)
    case = await store.upload(pdf_bytes(["hourly_rate: 15000 KRW/hour"]))
    assert case["document_id"].startswith("upload-") and "pdf" not in case["upload"]
    data = store.document_data(case["document_id"])
    assert content_hash(data) == case["upload"]["sha256"]
    store2 = LocalStore(AgentSettings(), state=tmp_path)
    assert store2.document_data(case["document_id"]) == data
    with pytest.raises(ValueError):
        await store.save_draft(case["id"], {**case["draft"], "synthetic_auxiliary": True})


async def test_units_period_zero_missing_are_not_silently_assumed(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path)
    case = await store.create_case("filled-hourly")
    cid = case["id"]
    draft = {
        **case["draft"],
        "values": {"contract_rate": "15000", "contract_start": "2020-01-01", "contract_end": "2020-12-31"},
        "period_start": "2026-10-01",
        "period_end": "2026-10-31",
        "manual": {"slip_rate": "15000", "deduction": "0"},
        "slip_unit": "KRW/month",
    }
    await store.save_draft(cid, draft)
    await store.confirm(cid)
    await run_stage(store, cid, "analyze")
    comparison = next(c for c in store.cases[cid]["analysis"]["comparisons"] if c["rule_id"] == "R-01")
    assert comparison["status"] == "needs_review"
    assert "UNIT_MISMATCH" in comparison["reason_codes"] and "PERIOD_MISMATCH" in comparison["reason_codes"]
    confirmation = Confirmation.model_validate({**draft, "slip_unit": ""})
    with pytest.raises(ValueError):
        confirmation.validate_for_analysis()


class BlockingProvider(FixtureProvider):
    def __init__(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def invoke(self, operation, data):
        self.entered.set()
        await self.release.wait()
        return await super().invoke(operation, data)


async def test_cancellation_restore_reserved_budget_and_reject_old_execution(tmp_path):
    provider = BlockingProvider()
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    case = await store.create_case("json-01")
    job = await store.create_job(case["id"], "extract", use_saved=False)
    await asyncio.wait_for(provider.entered.wait(), 3)
    jid = job["id"]
    old_ports = store.ports[jid]
    old_task = store.jobs[jid]["task"]
    old_token = store.current_tokens[jid]
    await store.cancel_job(jid)
    assert store.jobs[jid]["status"] == "cancelled"
    cp = old_ports.checkpoints[jid]
    assert cp.call_budget["extract_candidates"] == 1
    restored = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    provider.release.set()
    await restored.resume_job(jid)
    await restored.tasks[jid]
    assert restored.jobs[jid]["status"] == "completed"
    assert restored.ports[jid].checkpoints[jid].call_budget["extract_candidates"] == 2
    # All LocalPorts in one server share the execution-token fence.
    from agent.schemas.public.tasks import JobContext

    old_context = JobContext.model_validate(old_task["context"])
    old_context.execution_token = old_token
    newer = LocalPorts(store, store.jobs[jid])
    newer_context = old_context.model_copy(deep=True)
    newer_context.execution_token = "newer-execution"
    newer.bind(newer_context)
    old_ports.cancelled.discard(jid)
    with pytest.raises(ExecutionRevoked):
        await old_ports.check_control(old_context)


async def test_edit_during_execution_rejects_old_result(tmp_path):
    provider = BlockingProvider()
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    case = await store.create_case("json-01")
    job = await store.create_job(case["id"], "extract", use_saved=False)
    await asyncio.wait_for(provider.entered.wait(), 3)
    await store.save_draft(case["id"], case["draft"])
    provider.release.set()
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["status"] == "superseded"
    assert store.cases[case["id"]]["extraction"] is None


async def test_saved_failure_never_silently_makes_another_call(tmp_path):
    provider = BlockingProvider()
    store = LocalStore(AgentSettings(), state=tmp_path, provider=provider)
    case = await store.create_case("json-04")
    job = await store.create_job(case["id"], "extract", use_saved=True)
    await asyncio.wait_for(store.tasks[job["id"]], 2)
    assert store.jobs[job["id"]]["status"] == "failed"
    assert not provider.entered.is_set()
    assert store.jobs[job["id"]]["cache_source"]


async def test_corrupt_local_source_becomes_failed_job_not_forever_pending(tmp_path):
    store = LocalStore(AgentSettings(), state=tmp_path)
    case = await store.upload(pdf_bytes(["SYNTHETIC"]))
    cid = case["id"]
    (store.state / "uploads" / (case["document_id"] + ".pdf")).write_bytes(b"corrupt")
    job = await store.create_job(cid, "extract", use_saved=False)
    await store.tasks[job["id"]]
    assert store.jobs[job["id"]]["status"] == "failed"


def test_http_api_roundtrip_security_and_private_metadata(tmp_path):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def initialize():
        return LocalStore(AgentSettings(), state=tmp_path)

    store = asyncio.run_coroutine_threadsafe(initialize(), loop).result()
    server = LocalHTTPServer(("127.0.0.1", 0), store, loop)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base + path, data=data, method=method, headers={"X-Local-Agent": "1", **(headers or {})}
        )
        return urllib.request.urlopen(req, timeout=5)

    try:
        with request("/api/documents") as response:
            documents = json.load(response)["documents"]
            assert len(documents) == 37
            assert not any("source" in d or "pdf" in d for d in documents)
        with request("/api/evaluation") as response:
            assert json.load(response)["evaluated"] == 37
        with request("/api/cases", "POST", {"document_id": "filled-hourly"}) as response:
            case = json.load(response)
        with request(f"/api/cases/{case['id']}/jobs", "POST", {"stage": "extract"}) as response:
            assert response.status == 202
            job = json.load(response)
            assert "task" not in job and "execution_token" not in json.dumps(job)

        # Wait on the actual scheduled task, not a time-based sleep.
        async def completion():
            await store.tasks[job["id"]]

        asyncio.run_coroutine_threadsafe(completion(), loop).result(timeout=5)
        with request("/api/jobs/" + job["id"]) as response:
            assert json.load(response)["status"] == "completed"
        with request("/api/documents/filled-hourly/pdf") as response:
            assert response.headers["Content-Type"] == "application/pdf"
        with request("/preview.html?document=filled-hourly") as response:
            assert "PDF" in response.read().decode()
        with request("/api/documents/filled-hourly/pages/1.png") as response:
            assert response.read().startswith(b"\x89PNG")
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(
                "/api/cases", "POST", {"document_id": "filled-hourly"}, {"Origin": "https://other.example"}
            )
        assert exc.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as exc:
            request("/agent/.env")
        assert exc.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as exc:
            request("/api/health", headers={"Host": "attacker.example"})
        assert exc.value.code == 403
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: request("/api/health").status, range(2)))
        assert results == [200, 200]
    finally:
        server.shutdown()
        server.server_close()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
