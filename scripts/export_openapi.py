"""Write ops-api's OpenAPI spec for the ops console, which generates its types from it.

The pydantic models in services/ops_api.py are the single definition of the API shape.
Run `make api-types` after changing them; CI fails if the committed spec or the console's
generated types are stale.

  python scripts/export_openapi.py [--out services/ops-console/openapi.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from shipex.services.ops_api import create_app
from shipex.store.cases import InMemoryCaseRegistry

DEFAULT_OUT = Path(__file__).parents[1] / "services" / "ops-console" / "openapi.json"


def spec() -> dict[str, Any]:
    """The spec as served by ops-api; the registry is only needed to build the app."""
    return create_app(InMemoryCaseRegistry()).openapi()


def render() -> str:
    return json.dumps(spec(), indent=2, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    args.out.write_text(render())
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
