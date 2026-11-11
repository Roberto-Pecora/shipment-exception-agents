"""The ops console's types are generated from the committed spec, so it must match the API."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_committed_openapi_spec_matches_ops_api() -> None:
    loader = importlib.util.spec_from_file_location("export", ROOT / "scripts/export_openapi.py")
    assert loader is not None and loader.loader is not None
    export = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(export)
    committed = (ROOT / "services/ops-console/openapi.json").read_text()
    assert committed == export.render(), "ops-api changed: run `make api-types` and commit"
