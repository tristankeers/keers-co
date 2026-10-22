"""The frozen KCI files must be exact slices of the published page."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LEGACY = ROOT / "legacy" / "kci"


def test_files_match_manifest_hashes() -> None:
    manifest = json.loads((LEGACY / "SOURCE.json").read_text())
    for name, spec in manifest["slices"].items():
        digest = hashlib.sha256((LEGACY / name).read_bytes()).hexdigest()
        assert digest == spec["sha256"], f"{name} has been edited; legacy files must stay byte-identical"


@pytest.mark.network
def test_files_are_byte_identical_to_published_page() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "extract_kci.py"), "--check"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
