"""Versioned, replay-safe ingestion of external execution telemetry."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .artifact_delivery_service import ArtifactDeliveryService, DeliveryStatus
from .execution_contract import ExecutionStatus, FailureCategory, FailureReason, StepStatus
from .execution_service import ExecutionService
from .models import Artifact, Execution, ExecutionEvent, ExecutionStep
from .schemas import ExecutionEventIngestRequest


class EventConflictError(ValueError):
    pass


class EventProjectionError(ValueError):
    pass


class ExecutionEventService:
    def __init__(self, db: Session):
        self.db = db

    def ingest(self, payload: ExecutionEventIngestRequest) -> tuple[ExecutionEvent, bool]:
        canonical = payload.model_dump(mode="json")
        payload_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        existing = self._find(payload.source, payload.source_scope, payload.event_id)
        if existing is not None:
            return self._replay(existing, payload_sha256)

        try:
            with self.db.begin_nested():
                event = ExecutionEvent(
                    schema_version=payload.schema_version,
                    event_id=payload.event_id,
                    event_type=payload.event_type,
                    source=payload.source,
                    source_scope=payload.source_scope,
                    correlation_key=payload.correlation_key,
                    reason=payload.reason,
                    observed_at=payload.observed_at,
                    payload_sha256=payload_sha256,
                    payload_json=payload_json,
                )
                self.db.add(event)
                self.db.flush()
        except IntegrityError:
            existing = self._find(payload.source, payload.source_scope, payload.event_id)
            if existing is None:
                raise
            return self._replay(existing, payload_sha256)

        execution = self._execution_for(payload)
        event.execution = execution
        self._project(event, payload, execution)
        self.db.flush()
        return event, False

    def _find(self, source: str, source_scope: str, event_id: str) -> ExecutionEvent | None:
        return self.db.query(ExecutionEvent).filter(
            ExecutionEvent.source == source,
            ExecutionEvent.source_scope == source_scope,
            ExecutionEvent.event_id == event_id,
        ).first()

    @staticmethod
    def _replay(existing: ExecutionEvent, payload_sha256: str) -> tuple[ExecutionEvent, bool]:
        if existing.payload_sha256 != payload_sha256:
            raise EventConflictError("event identity was already used with a different payload")
        return existing, True

    def _execution_for(self, payload: ExecutionEventIngestRequest) -> Execution:
        execution = self.db.query(Execution).filter(
            Execution.source == payload.source,
            Execution.source_scope == payload.source_scope,
            Execution.correlation_key == payload.correlation_key,
        ).first()
        if execution is not None:
            if payload.repository_id is not None and execution.repository_id not in {None, payload.repository_id}:
                raise EventConflictError("correlation_key belongs to a different repository")
            if payload.product_id is not None and execution.product_id not in {None, payload.product_id}:
                raise EventConflictError("correlation_key belongs to a different product")
            if execution.repository_id is None:
                execution.repository_id = payload.repository_id
            if execution.product_id is None:
                execution.product_id = payload.product_id
            return execution
        if payload.event_type != "execution.transition":
            raise EventProjectionError("execution.transition must be ingested before child events")
        return ExecutionService(self.db).observe_execution(
            source=payload.source,
            correlation_key=payload.correlation_key,
            repository_id=payload.repository_id,
            product_id=payload.product_id,
            status=self._enum(ExecutionStatus, payload.data, "status"),
            observed_at=payload.observed_at,
            source_scope=payload.source_scope,
        )

    def _project(
        self,
        event: ExecutionEvent,
        payload: ExecutionEventIngestRequest,
        execution: Execution,
    ) -> None:
        if payload.event_type == "execution.transition":
            projected = ExecutionService(self.db).observe_execution(
                source=payload.source,
                correlation_key=payload.correlation_key,
                repository_id=payload.repository_id,
                product_id=payload.product_id,
                status=self._enum(ExecutionStatus, payload.data, "status"),
                observed_at=payload.observed_at,
                source_scope=payload.source_scope,
            )
            self._set_failure(projected, payload.data)
            event.execution = projected
            return
        if payload.event_type == "step.transition":
            step = ExecutionService(self.db).observe_step(
                execution,
                source=payload.source,
                external_id=self._required_str(payload.data, "external_id"),
                name=self._required_str(payload.data, "name"),
                status=self._enum(StepStatus, payload.data, "status"),
                observed_at=payload.observed_at,
            )
            self._set_failure(step, payload.data)
            return
        if payload.event_type == "artifact.created":
            step = None
            step_external_id = payload.data.get("execution_step_external_id")
            if step_external_id:
                step = self.db.query(ExecutionStep).filter(
                    ExecutionStep.execution_id == execution.id,
                    ExecutionStep.source == payload.source,
                    ExecutionStep.external_id == step_external_id,
                ).first()
                if step is None:
                    raise EventProjectionError("execution step for artifact provenance was not found")
            contract = ArtifactDeliveryService(self.db)
            artifact = contract.record_artifact(
                execution=execution,
                source=payload.source,
                external_id=self._required_str(payload.data, "external_id"),
                kind=self._required_str(payload.data, "kind"),
                uri=self._required_str(payload.data, "uri"),
                digest_sha256=self._required_str(payload.data, "digest_sha256"),
                size_bytes=self._required_int(payload.data, "size_bytes"),
                schema_version=payload.data.get("schema_version"),
                observed_at=payload.observed_at,
            )
            contract.record_provenance(
                artifact=artifact,
                execution=execution,
                execution_step=step,
                source_revision=payload.data.get("source_revision"),
                source_ref=payload.data.get("source_ref"),
                observed_at=payload.observed_at,
            )
            return
        if payload.event_type == "delivery.transition":
            artifact = self.db.query(Artifact).filter(
                Artifact.execution_id == execution.id,
                Artifact.source == payload.source,
                Artifact.external_id == self._required_str(payload.data, "artifact_external_id"),
            ).first()
            if artifact is None:
                raise EventProjectionError("delivery artifact was not found")
            ArtifactDeliveryService(self.db).observe_delivery(
                artifact=artifact,
                source=payload.source,
                destination=self._required_str(payload.data, "destination"),
                attempt_key=self._required_str(payload.data, "attempt_key"),
                status=self._enum(DeliveryStatus, payload.data, "status"),
                external_id=payload.data.get("external_id"),
                error_text=payload.data.get("error_text"),
                observed_at=payload.observed_at,
            )

    def history(
        self,
        *,
        source: str | None = None,
        source_scope: str | None = None,
        correlation_key: str | None = None,
        execution_id: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        query = self.db.query(ExecutionEvent)
        if source:
            query = query.filter(ExecutionEvent.source == source)
        if source_scope:
            query = query.filter(ExecutionEvent.source_scope == source_scope)
        if correlation_key:
            query = query.filter(ExecutionEvent.correlation_key == correlation_key)
        if execution_id:
            query = query.filter(ExecutionEvent.execution_id == execution_id)
        rows = query.order_by(ExecutionEvent.observed_at.desc(), ExecutionEvent.id.desc()).limit(limit).all()
        return [self.serialize(row) for row in rows]

    @staticmethod
    def serialize(row: ExecutionEvent) -> dict:
        payload = json.loads(row.payload_json)
        return {
            "id": row.id,
            "execution_id": row.execution_id,
            "schema_version": row.schema_version,
            "event_id": row.event_id,
            "event_type": row.event_type,
            "source": row.source,
            "source_scope": row.source_scope,
            "correlation_key": row.correlation_key,
            "reason": row.reason,
            "observed_at": row.observed_at.isoformat(),
            "received_at": row.received_at.isoformat() if row.received_at else None,
            "data": payload["data"],
        }

    @staticmethod
    def _required_str(data: dict, field: str) -> str:
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise EventProjectionError(f"data.{field} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _required_int(data: dict, field: str) -> int:
        value = data.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise EventProjectionError(f"data.{field} must be a positive integer")
        return value

    @classmethod
    def _enum(cls, enum_type, data: dict, field: str):
        value = cls._required_str(data, field)
        try:
            return enum_type(value)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in enum_type)
            raise EventProjectionError(f"data.{field} must be one of: {allowed}") from exc

    @staticmethod
    def _set_failure(row: Execution | ExecutionStep, data: dict) -> None:
        category = data.get("failure_category")
        reason = data.get("failure_reason")
        if category is not None:
            try:
                row.failure_category = FailureCategory(category).value
            except ValueError as exc:
                raise EventProjectionError("data.failure_category is invalid") from exc
        if reason is not None:
            try:
                row.failure_reason = FailureReason(reason).value
            except ValueError as exc:
                raise EventProjectionError("data.failure_reason is invalid") from exc
