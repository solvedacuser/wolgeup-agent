from pathlib import Path

import pytest

from agent.config import AgentSettings
from agent.dev.fake_ports import FakePorts
from agent.dev.fixtures import analysis_fixture


def pytest_collection_modifyitems(items):
    root = Path(__file__).resolve().parents[2]
    corpus_available = (root / "output/contract-validation/pdfs/filled-hourly.pdf").is_file() and (
        root / "계약_법률_문서_서식_데이터"
    ).is_dir()
    if not corpus_available:
        skip = pytest.mark.skip(reason="The original validation corpus is not distributed with the source.")
        for item in items:
            if item.get_closest_marker("private_corpus"):
                item.add_marker(skip)


@pytest.fixture
def setup_agent():
    task, ref, data = analysis_fixture()
    ports = FakePorts()
    ports.bind(task.context)
    ports.add_source(ref, data)
    return task, ports, AgentSettings(retry_delay_seconds=0)
