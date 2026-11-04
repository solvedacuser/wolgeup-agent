import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent.services.extraction_profiles import PROFILES


def test_both_document_selectors_use_all_api_profiles():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the UI initialization regression test")
    result = subprocess.run(
        [
            node,
            str(Path(__file__).parent / "web/document_types.cjs"),
            json.dumps(PROFILES, ensure_ascii=False),
        ],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_material_linking_review_and_evidence_ui():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the UI initialization regression test")
    result = subprocess.run(
        [node, str(Path(__file__).parent / "web/comparison_ui.cjs")],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
