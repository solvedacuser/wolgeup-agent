import io
import subprocess
import sys

from pypdf import PdfReader

from agent import run_task
from agent.dev.fixtures import confirmed_result, explanation_task
from agent.services.hashing import result_hash
from agent.services.reports import render_pdf


def test_import_has_no_network_or_environment_loading():
    script = """
import socket
import dotenv
def forbidden(*args, **kwargs):
    raise AssertionError('unexpected external initialization')
socket.socket = forbidden
dotenv.load_dotenv = forbidden
dotenv.dotenv_values = forbidden
import agent
import agent.config
assert callable(agent.run_task)
print('clean import')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert "clean import" in result.stdout


async def test_multipage_report_has_text_and_page_footers(setup_agent):
    task, ports, settings = setup_agent
    outcome = await run_task(task, ports=ports, settings=settings)
    result = confirmed_result(outcome.payload)
    evidence = result.evidence_catalog[0]
    result.evidence_catalog.extend(
        evidence.model_copy(update={"evidence_id": f"evidence-{i}"}) for i in range(90)
    )
    result.result_hash = result_hash(result)
    explain = explanation_task(result)
    ports.bind(explain.context)
    out = await run_task(explain, ports=ports, settings=settings)
    data = render_pdf(result, out.payload, settings, task.context.requested_at)
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) >= 3
    for index, page in enumerate(reader.pages, 1):
        text = page.extract_text()
        assert f"월급잇다 | {index}" in text
        assert "evidence" in text or "계약 단가" in text
