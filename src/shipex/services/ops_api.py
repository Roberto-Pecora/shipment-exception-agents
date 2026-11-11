"""ops-api: lets a person see and release cases that automation handed to them.

The ops console (services/ops-console) is a thin UI over these endpoints.

An escalated case is held, not closed, so automation stays out of that shipment while a
person works it. Releasing the case hands the shipment back to automation.

Run: uvicorn shipex.services.ops_api:app_from_env --factory --port 8080
"""

from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI, HTTPException, Response
from prometheus_client import make_asgi_app
from pydantic import BaseModel

from shipex import telemetry
from shipex.contracts.events import Action
from shipex.store.cases import CaseRegistry, OpenCase, Status


class CaseView(BaseModel):
    shipment_id: str
    case_id: str
    status: Status
    reason: str | None
    opened_at: datetime | None
    actions: list[Action]  # what automation already did, so ops does not repeat it

    @classmethod
    def of(cls, case: OpenCase) -> CaseView:
        return cls(
            shipment_id=case.shipment_id,
            case_id=case.case_id,
            status=case.status,
            reason=case.reason,
            opened_at=case.opened_at,
            actions=list(case.actions),
        )


def create_app(cases: CaseRegistry) -> FastAPI:
    app = FastAPI(title="shipex ops-api", version="0.1.0")

    @app.get("/v1/shipments/{shipment_id}/case", response_model=CaseView)
    def shipment_case(shipment_id: str) -> CaseView:
        """The open or held case for a shipment, if any."""
        case = cases.get(shipment_id)
        if case is None:
            raise HTTPException(status_code=404, detail="no open case for this shipment")
        return CaseView.of(case)

    @app.get("/v1/cases/held", response_model=list[CaseView])
    def held_cases() -> list[CaseView]:
        """The ops queue: cases automation handed to a person, oldest first."""
        return [CaseView.of(case) for case in cases.list_held()]

    @app.post("/v1/cases/{case_id}/release", status_code=204)
    def release(case_id: str) -> Response:
        """Ops has finished with a held case; automation may handle the shipment again."""
        with telemetry.tracer.start_as_current_span(
            "release case", attributes={"shipex.case_id": case_id}
        ):
            if not cases.release(case_id):
                raise HTTPException(status_code=404, detail="no held case with this id")
        return Response(status_code=204)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/metrics", make_asgi_app())
    return app


def app_from_env() -> FastAPI:
    import psycopg

    from shipex.config import Settings
    from shipex.store.cases import PostgresCaseRegistry

    settings = Settings()
    telemetry.configure("shipex-ops-api")
    conn = psycopg.connect(settings.database_url)
    return create_app(PostgresCaseRegistry(conn))
