"""The selected existing data package; never fall back to the internal database or files."""

import json
from functools import lru_cache
from pathlib import Path

SAMPLE_PATH = Path(__file__).resolve().parents[2] / "data" / "public_demo_sample.json"


@lru_cache
def sample_manifest():
    return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
