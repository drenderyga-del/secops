"""Idempotent writer for the shared execution lifecycle."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .execution_contract import (
    ExecutionStatus,
    LegacySource,
    StepStatus,
    can_step_transition,
    can_transition,
    map_legacy_status,
)
from .models.artifact import Artifact, ArtifactProvenance, Delivery
from .models.execution import Execution, ExecutionEvent, ExecutionStep


class ExecutionService:
    def __init__(self, db: Session):
        self.db = db

    def observe_execution(
        self,
        *,
        source: str,
        correlation_key: str,
        repository_id: int | None,
        product_id: int | None,
        status: ExecutionStatus,
        observed_at: datetime,
        source_scope: str = "default",
    ) -> Execution:
        row = self.db.query(Execution).filter(
            Execution.source == source,
            Execution.source_scope == source_scope,
            Execution.correlation_key == correlation_key,
        ).first()
        if row is None:
            try:
                with self.db.begin_nested():
                    row = Execution(
                        source=source,
                        source_scope=source_scope,
                        correlation_key=correlation_key,
                        repository_id=repository_id,
                        product_id=product_id,
                        status=status.value,
                        requested_at=observed_at,
                    )
                    self.db.add(row)
                    self.db.flush()
            except IntegrityError:
                row = self.db.query(Execution).filter(
                    Execution.source == source,
                    Execution.source_scope == source_scope,
                    Execution.correlation_key == correlation_key,
                ).first()
                if row is None:
                    raise
        if row.status != status.value:
            current = ExecutionStatus(row.status)
            if not can_transition(current, status):
                raise ValueError(f"Invalid execution transition: {current.value} -> {status.value}")
            row.status = status.value
        if status is ExecutionStatus.RUNNING and row.started_at is None:
            row.started_at = observed_at
        if (
            status in {ExecutionStatus.SUCCEEDED, ExecutionStatus.PARTIAL, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
            and row.finished_at is None
        ):
            row.finished_at = observed_at
        self.db.add(row)
        self.db.flush()
        return row

    def observe_legacy_execution(
        self,
        *,
        source: LegacySource,
        raw_status: str,
        correlation_key: str,
        repository_id: int | None,
        product_id: int | None,
        observed_at: datetime,
        source_scope: str = "default",
    ) -> Execution:
        """Persist a source observation only when it has canonical execution meaning."""
        observation = map_legacy_status(source, raw_status)
        if observation.execution_status is None:
            raise ValueError(f"{source.value}:{raw_status} does not define an execution status")
        return self.observe_execution(
            source=source.value,
            correlation_key=correlation_key,
            repository_id=repository_id,
            product_id=product_id,
            status=observation.execution_status,
            observed_at=observed_at,
            source_scope=source_scope,
        )

    def observe_step(
        self,
        execution: Execution,
        *,
        source: str,
        external_id: str,
        name: str,
        status: StepStatus,
        observed_at: datetime,
    ) -> ExecutionStep:
        row = self.db.query(ExecutionStep).filter(
            ExecutionStep.execution_id == execution.id,
            ExecutionStep.source == source,
            ExecutionStep.external_id == external_id,
        ).first()
        if row is None:
            try:
                with self.db.begin_nested():
                    row = ExecutionStep(
                        execution=execution,
                        source=source,
                        external_id=external_id,
                        name=name,
                        status=status.value,
                    )
                    self.db.add(row)
                    self.db.flush()
            except IntegrityError:
                row = self.db.query(ExecutionStep).filter(
                    ExecutionStep.execution_id == execution.id,
                    ExecutionStep.source == source,
                    ExecutionStep.external_id == external_id,
                ).first()
                if row is None:
                    raise
        if row.status != status.value:
            current = StepStatus(row.status)
            if not can_step_transition(current, status):
                raise ValueError(f"Invalid step transition: {current.value} -> {status.value}")
            row.name = name
            row.status = status.value
        if status is StepStatus.RUNNING and row.started_at is None:
            row.started_at = observed_at
        if (
            status in {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.CANCELLED, StepStatus.SKIPPED}
            and row.finished_at is None
        ):
            row.finished_at = observed_at
        self.db.add(row)
        self.db.flush()
        return row

    def observe_legacy_step(
        self,
        execution: Execution,
        *,
        source: LegacySource,
        raw_status: str,
        external_id: str,
        name: str,
        observed_at: datetime,
    ) -> ExecutionStep:
        observation = map_legacy_status(source, raw_status)
        if observation.step_status is None:
            raise ValueError(f"{source.value}:{raw_status} does not define a step status")
        return self.observe_step(
            execution,
            source=source.value,
            external_id=external_id,
            name=name,
            status=observation.step_status,
            observed_at=observed_at,
        )

    def repository_timeline(
        self,
        repository_id: int,
        *,
        limit: int = 100,
        source: str | None = None,
    ) -> list[dict]:
        latest_evidence_at = func.coalesce(
            Execution.finished_at,
            Execution.started_at,
            Execution.requested_at,
        )
        candidates = (
            select(func.max(ExecutionEvent.observed_at))
            .where(ExecutionEvent.execution_id == Execution.id)
            .correlate(Execution)
            .scalar_subquery(),
            select(func.max(func.coalesce(ExecutionStep.finished_at, ExecutionStep.started_at)))
            .where(ExecutionStep.execution_id == Execution.id)
            .correlate(Execution)
            .scalar_subquery(),
            select(func.max(Artifact.created_at))
            .where(Artifact.execution_id == Execution.id)
            .correlate(Execution)
            .scalar_subquery(),
            select(func.max(ArtifactProvenance.observed_at))
            .where(ArtifactProvenance.execution_id == Execution.id)
            .correlate(Execution)
            .scalar_subquery(),
            select(func.max(func.coalesce(Delivery.confirmed_at, Delivery.attempted_at)))
            .where(Delivery.execution_id == Execution.id)
            .correlate(Execution)
            .scalar_subquery(),
        )
        for candidate in candidates:
            latest_evidence_at = case(
                (
                    candidate.is_not(None)
                    & (latest_evidence_at.is_(None) | (candidate > latest_evidence_at)),
                    candidate,
                ),
                else_=latest_evidence_at,
            )
        latest_evidence_at = func.coalesce(latest_evidence_at, Execution.created_at)

        query = (
            self.db.query(Execution)
            .options(
                selectinload(Execution.steps),
                selectinload(Execution.events),
                selectinload(Execution.artifacts).selectinload(Artifact.deliveries),
                selectinload(Execution.artifacts).selectinload(Artifact.provenance),
            )
            .filter(Execution.repository_id == repository_id)
        )
        if source:
            query = query.filter(Execution.source == source)
        rows = query.order_by(latest_evidence_at.desc(), Execution.id.desc()).limit(limit).all()
        return [
            {
                "id": row.id,
                "source": row.source,
                "source_scope": row.source_scope,
                "correlation_key": row.correlation_key,
                "status": row.status,
                "failure_category": row.failure_category,
                "failure_reason": row.failure_reason,
                "requested_at": row.requested_at.isoformat() if row.requested_at else None,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                "latest_evidence_at": self._latest_evidence_at(row).isoformat(),
                "context": json.loads(row.context_json or "{}"),
                "events": [
                    {
                        "id": event.id,
                        "schema_version": event.schema_version,
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "source": event.source,
                        "source_scope": event.source_scope,
                        "reason": event.reason,
                        "observed_at": event.observed_at.isoformat(),
                        "received_at": event.received_at.isoformat() if event.received_at else None,
                        "data": json.loads(event.payload_json).get("data", {}),
                    }
                    for event in sorted(row.events, key=lambda item: (item.observed_at, item.id or 0), reverse=True)
                ],
                "steps": [
                    {
                        "id": step.id,
                        "source": step.source,
                        "external_id": step.external_id,
                        "name": step.name,
                        "status": step.status,
                        "failure_category": step.failure_category,
                        "failure_reason": step.failure_reason,
                        "started_at": step.started_at.isoformat() if step.started_at else None,
                        "finished_at": step.finished_at.isoformat() if step.finished_at else None,
                        "context": json.loads(step.context_json or "{}"),
                    }
                    for step in sorted(row.steps, key=lambda item: item.id or 0)
                ],
                "artifacts": [
                    {
                        "id": artifact.id,
                        "source": artifact.source,
                        "external_id": artifact.external_id,
                        "kind": artifact.kind,
                        "status": artifact.status,
                        "uri": artifact.uri,
                        "digest_sha256": artifact.digest_sha256,
                        "size_bytes": artifact.size_bytes,
                        "schema_version": artifact.schema_version,
                        "provenance": (
                            {
                                "execution_step_id": artifact.provenance.execution_step_id,
                                "source_revision": artifact.provenance.source_revision,
                                "source_ref": artifact.provenance.source_ref,
                                "observed_at": artifact.provenance.observed_at.isoformat(),
                                "context": json.loads(artifact.provenance.context_json or "{}"),
                            }
                            if artifact.provenance
                            else None
                        ),
                        "deliveries": [
                            {
                                "id": delivery.id,
                                "source": delivery.source,
                                "destination": delivery.destination,
                                "attempt_key": delivery.attempt_key,
                                "external_id": delivery.external_id,
                                "status": delivery.status,
                                "failure_category": delivery.failure_category,
                                "failure_reason": delivery.failure_reason,
                                "attempted_at": delivery.attempted_at.isoformat() if delivery.attempted_at else None,
                                "confirmed_at": delivery.confirmed_at.isoformat() if delivery.confirmed_at else None,
                            }
                            for delivery in sorted(artifact.deliveries, key=lambda item: item.id or 0)
                        ],
                    }
                    for artifact in sorted(row.artifacts, key=lambda item: item.id or 0)
                ],
            }
            for row in rows
        ]

    @staticmethod
    def _latest_evidence_at(execution: Execution) -> datetime:
        timestamps = [
            execution.finished_at,
            execution.started_at,
            execution.requested_at,
        ]
        timestamps.extend(event.observed_at for event in execution.events)
        timestamps.extend(
            step.finished_at or step.started_at
            for step in execution.steps
        )
        for artifact in execution.artifacts:
            timestamps.append(artifact.created_at)
            if artifact.provenance:
                timestamps.append(artifact.provenance.observed_at)
            timestamps.extend(
                delivery.confirmed_at or delivery.attempted_at
                for delivery in artifact.deliveries
            )
        normalized = []
        for value in timestamps:
            if value is None:
                continue
            normalized.append(
                value.replace(tzinfo=timezone.utc)
                if value.tzinfo is None
                else value.astimezone(timezone.utc)
            )
        if normalized:
            return max(normalized)
        created_at = execution.created_at
        return (
            created_at.replace(tzinfo=timezone.utc)
            if created_at.tzinfo is None
            else created_at.astimezone(timezone.utc)
        )
