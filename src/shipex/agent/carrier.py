"""Client for the carrier-gateway microservice (TypeScript, services/carrier-gateway).

`HttpCarrierClient` adds timeouts, retries on transient statuses and a circuit breaker.
Side-effecting calls carry an `Idempotency-Key`, so a redelivered agent run cannot rebook
a shipment twice. `InMemoryCarrier` implements the same contract (including idempotency
and injectable faults) for tests and offline evals.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from shipex import telemetry
from shipex.resilience import (
    RETRYABLE_STATUS,
    CircuitBreaker,
    CircuitOpenError,
    call_with_retry,
)


class CarrierError(Exception):
    def __init__(self, message: str, *, transient: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.transient = transient
        self.status = status


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class TrackingInfo(_Model):
    shipment_id: str
    status: str
    location: str | None = None
    eta: datetime | None = None
    history: list[str] = Field(default_factory=list)
    missing_customs_documents: list[str] = Field(default_factory=list)


class RebookingOption(_Model):
    option_id: str
    service: str
    new_eta: datetime
    cost_delta_gbp: float


class RebookingConfirmation(_Model):
    rebooking_id: str
    shipment_id: str
    option_id: str
    new_eta: datetime


class DocumentRequestConfirmation(_Model):
    request_id: str
    shipment_id: str
    documents: list[str]


class CarrierFixture(_Model):
    """Per-shipment state served by the gateway (same JSON shape in Python and TS)."""

    tracking: TrackingInfo
    rebooking_options: list[RebookingOption] = Field(default_factory=list)


class CarrierClient(Protocol):
    def get_tracking(self, shipment_id: str) -> TrackingInfo: ...

    def get_rebooking_options(self, shipment_id: str) -> list[RebookingOption]: ...

    def rebook(
        self, shipment_id: str, option_id: str, idempotency_key: str
    ) -> RebookingConfirmation: ...

    def request_customs_documents(
        self, shipment_id: str, documents: list[str], idempotency_key: str
    ) -> DocumentRequestConfirmation: ...


class InMemoryCarrier:
    def __init__(self, fixtures: dict[str, CarrierFixture] | None = None) -> None:
        self.fixtures = dict(fixtures or {})
        self.faults: dict[str, list[CarrierError]] = defaultdict(list)
        self.side_effects: list[tuple[str, str]] = []
        self._idempotent: dict[str, BaseModel] = {}

    def fail_next(self, operation: str, *errors: CarrierError) -> None:
        self.faults[operation].extend(errors)

    def _maybe_fail(self, operation: str) -> None:
        if self.faults[operation]:
            raise self.faults[operation].pop(0)

    def _fixture(self, shipment_id: str) -> CarrierFixture:
        if shipment_id not in self.fixtures:
            raise CarrierError(f"unknown shipment {shipment_id}", transient=False, status=404)
        return self.fixtures[shipment_id]

    def get_tracking(self, shipment_id: str) -> TrackingInfo:
        self._maybe_fail("get_tracking")
        return self._fixture(shipment_id).tracking

    def get_rebooking_options(self, shipment_id: str) -> list[RebookingOption]:
        self._maybe_fail("get_rebooking_options")
        return list(self._fixture(shipment_id).rebooking_options)

    def rebook(
        self, shipment_id: str, option_id: str, idempotency_key: str
    ) -> RebookingConfirmation:
        self._maybe_fail("rebook")
        if idempotency_key in self._idempotent:
            prior = self._idempotent[idempotency_key]
            assert isinstance(prior, RebookingConfirmation)
            return prior
        option = next(
            (o for o in self._fixture(shipment_id).rebooking_options if o.option_id == option_id),
            None,
        )
        if option is None:
            raise CarrierError(f"unknown option {option_id}", transient=False, status=422)
        confirmation = RebookingConfirmation(
            rebooking_id=f"rb-{len(self.side_effects) + 1:04d}",
            shipment_id=shipment_id,
            option_id=option_id,
            new_eta=option.new_eta,
        )
        self._idempotent[idempotency_key] = confirmation
        self.side_effects.append(("rebook", idempotency_key))
        return confirmation

    def request_customs_documents(
        self, shipment_id: str, documents: list[str], idempotency_key: str
    ) -> DocumentRequestConfirmation:
        self._maybe_fail("request_customs_documents")
        if idempotency_key in self._idempotent:
            prior = self._idempotent[idempotency_key]
            assert isinstance(prior, DocumentRequestConfirmation)
            return prior
        self._fixture(shipment_id)
        confirmation = DocumentRequestConfirmation(
            request_id=f"cdr-{len(self.side_effects) + 1:04d}",
            shipment_id=shipment_id,
            documents=documents,
        )
        self._idempotent[idempotency_key] = confirmation
        self.side_effects.append(("customs_docs", idempotency_key))
        return confirmation


class HttpCarrierClient:
    def __init__(
        self,
        base_url: str,
        timeout_s: float = 5.0,
        max_attempts: int = 3,
        breaker: CircuitBreaker | None = None,
        client: httpx.Client | None = None,
        retry_base_delay: float = 0.2,
    ) -> None:
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout_s)
        self._max_attempts = max_attempts
        self._retry_base_delay = retry_base_delay
        self._breaker = breaker or CircuitBreaker("carrier-gateway")

    def _request(
        self, method: str, path: str, *, json: Any = None, idempotency_key: str | None = None
    ) -> Any:
        headers = telemetry.inject_headers()
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        def send() -> Any:
            resp = self._client.request(method, path, json=json, headers=headers)
            resp.raise_for_status()
            return resp.json()

        def retryable(exc: Exception) -> bool:
            if isinstance(exc, httpx.HTTPStatusError):
                return exc.response.status_code in RETRYABLE_STATUS
            return isinstance(exc, httpx.TransportError)

        try:
            return self._breaker.call(
                lambda: call_with_retry(
                    send,
                    is_retryable=retryable,
                    max_attempts=self._max_attempts,
                    base_delay=self._retry_base_delay,
                ),
                is_failure=retryable,
            )
        except CircuitOpenError as exc:
            raise CarrierError(str(exc), transient=True) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise CarrierError(
                f"{method} {path} -> {status}: {exc.response.text[:200]}",
                transient=status in RETRYABLE_STATUS,
                status=status,
            ) from exc
        except httpx.TransportError as exc:
            raise CarrierError(f"{method} {path}: {exc}", transient=True) from exc

    def get_tracking(self, shipment_id: str) -> TrackingInfo:
        return TrackingInfo.model_validate(
            self._request("GET", f"/v1/shipments/{shipment_id}/tracking")
        )

    def get_rebooking_options(self, shipment_id: str) -> list[RebookingOption]:
        data = self._request("GET", f"/v1/shipments/{shipment_id}/rebooking-options")
        return [RebookingOption.model_validate(o) for o in data["options"]]

    def rebook(
        self, shipment_id: str, option_id: str, idempotency_key: str
    ) -> RebookingConfirmation:
        data = self._request(
            "POST",
            "/v1/rebookings",
            json={"shipment_id": shipment_id, "option_id": option_id},
            idempotency_key=idempotency_key,
        )
        return RebookingConfirmation.model_validate(data)

    def request_customs_documents(
        self, shipment_id: str, documents: list[str], idempotency_key: str
    ) -> DocumentRequestConfirmation:
        data = self._request(
            "POST",
            "/v1/customs-document-requests",
            json={"shipment_id": shipment_id, "documents": documents},
            idempotency_key=idempotency_key,
        )
        return DocumentRequestConfirmation.model_validate(data)
