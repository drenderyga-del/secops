"""Replay-safe artifact, delivery and provenance contract."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .execution_contract import ExecutionStatus, FailureCategory, FailureReason, TERMINAL_EXECUTION_STATUSES
from .execution_service import ExecutionService
from .models.artifact import Artifact, ArtifactProvenance, Delivery
from .models.execution import Execution, ExecutionStep


class DeliveryStatus(str, Enum):
    REQUESTED = "requested"
    RUNNING = "running"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    SKIPPED = "skipped"


_DELIVERY_TRANSITIONS = {
    DeliveryStatus.REQUESTED: {DeliveryStatus.RUNNING, DeliveryStatus.CONFIRMED, DeliveryStatus.FAILED, DeliveryStatus.SKIPPED},
    DeliveryStatus.RUNNING: {DeliveryStatus.CONFIRMED, DeliveryStatus.FAILED, DeliveryStatus.SKIPPED},
    DeliveryStatus.CONFIRMED: set(),
    DeliveryStatus.FAILED: set(),
    DeliveryStatus.SKIPPED: set(),
}


class ArtifactDeliveryService:
    def __init__(self, db: Session):
        self.db = db

    def record_artifact(
        self,
        *,
        execution: Execution,
        source: str,
        external_id: str,
        kind: str,
        uri: str,
        digest_sha256: str,
        size_bytes: int,
        schema_version: str | None,
        observed_at: datetime,
    ) -> Artifact:
        if size_bytes <= 0:
            raise ValueError("Artifact must be non-empty")
        if len(digest_sha256) != 64 or any(char not in "0123456789abcdef" for char in digest_sha256.lower()):
            raise ValueError("Artifact SHA-256 must be 64 hexadecimal characters")
        row = self._find_artifact(execution.id, source, external_id)
        if row is None:
            try:
                with self.db.begin_nested():
                    row = Artifact(
                        execution_id=execution.id,
                        source=source,
                        external_id=external_id,
                        kind=kind,
                        status="created",
                        uri=uri,
                        digest_sha256=digest_sha256.lower(),
                        size_bytes=size_bytes,
                        schema_version=schema_version,
                        created_at=observed_at,
                    )
                    self.db.add(row)
                    self.db.flush()
            except IntegrityError:
                row = self._find_artifact(execution.id, source, external_id)
                if row is None:
                    raise
        if row.digest_sha256 != digest_sha256.lower() or row.size_bytes != size_bytes:
            raise ValueError("Artifact identity replayed with different content")
        self.db.add(row)
        self.db.flush()
        return row

    def _find_artifact(self, execution_id: int, source: str, external_id: str) -> Artifact | None:
        return self.db.query(Artifact).filter(
            Artifact.execution_id == execution_id,
            Artifact.source == source,
            Artifact.external_id == external_id,
        ).first()

    def record_provenance(
        self,
        *,
        artifact: Artifact,
        execution: Execution,
        source_revision: str | None,
        source_ref: str | None,
        observed_at: datetime,
        execution_step: ExecutionStep | None = None,
    ) -> ArtifactProvenance:
        row = self.db.query(ArtifactProvenance).filter(ArtifactProvenance.artifact_id == artifact.id).first()
        if row is None:
            try:
                with self.db.begin_nested():
                    row = ArtifactProvenance(
                        artifact_id=artifact.id,
                        execution_id=execution.id,
                        execution_step_id=execution_step.id if execution_step else None,
                        source_revision=source_revision,
                        source_ref=source_ref,
                        observed_at=observed_at,
                    )
                    self.db.add(row)
                    self.db.flush()
            except IntegrityError:
                row = self.db.query(ArtifactProvenance).filter(
                    ArtifactProvenance.artifact_id == artifact.id
                ).first()
                if row is None:
                    raise
        if row.source_revision != source_revision or row.source_ref != source_ref:
            raise ValueError("Artifact provenance replayed with different source identity")
        self.db.add(row)
        self.db.flush()
        return row

    def observe_delivery(
        self,
        *,
        artifact: Artifact,
        source: str,
        destination: str,
        status: DeliveryStatus,
        external_id: str | None,
        observed_at: datetime,
        error_text: str | None = None,
        attempt_key: str = "initial",
    ) -> Delivery:
        row = self.db.query(Delivery).filter(
            Delivery.artifact_id == artifact.id,
            Delivery.destination == destination,
            Delivery.attempt_key == attempt_key,
        ).first()
        if row is None:
            try:
                with self.db.begin_nested():
                    row = Delivery(
                        execution_id=artifact.execution_id,
                        artifact_id=artifact.id,
                        source=source,
                        destination=destination,
                        attempt_key=attempt_key,
                        external_id=external_id,
                        status=status.value,
                        attempted_at=observed_at,
                    )
                    self.db.add(row)
                    self.db.flush()
            except IntegrityError:
                row = self.db.query(Delivery).filter(
                    Delivery.artifact_id == artifact.id,
                    Delivery.destination == destination,
                    Delivery.attempt_key == attempt_key,
                ).first()
                if row is None:
                    raise
        current = DeliveryStatus(row.status)
        if current is not status:
            if status not in _DELIVERY_TRANSITIONS[current]:
                raise ValueError(f"Invalid delivery transition: {current.value} -> {status.value}")
            row.status = status.value
        row.external_id = external_id or row.external_id
        row.error_text = error_text
        if status is DeliveryStatus.CONFIRMED and row.confirmed_at is None:
            row.confirmed_at = observed_at
        if status is DeliveryStatus.FAILED:
            row.failure_category = FailureCategory.DELIVERY.value
            row.failure_reason = FailureReason.UPLOAD_FAILED.value
        self.db.add(row)
        self.db.flush()
        if status in {DeliveryStatus.REQUESTED, DeliveryStatus.RUNNING}:
            execution = self.db.query(Execution).filter(Execution.id == artifact.execution_id).one()
            if ExecutionStatus(execution.status) not in TERMINAL_EXECUTION_STATUSES:
                self._observe_execution(
                    lifecycle=ExecutionService(self.db),
                    execution=execution,
                    status=ExecutionStatus.DELIVERING,
                    observed_at=observed_at,
                )
        return row

    def finalize_execution(
        self,
        execution: Execution,
        *,
        delivery_required: bool,
        observed_at: datetime,
        not_applicable: bool = False,
        required_delivery_source: str | None = None,
    ) -> Execution:
        lifecycle = ExecutionService(self.db)
        if not_applicable:
            return self._finish(lifecycle, execution, ExecutionStatus.SUCCEEDED, observed_at)
        artifacts = self.db.query(Artifact).filter(
            Artifact.execution_id == execution.id,
            Artifact.status == "created",
        ).all()
        if not artifacts:
            return self._fail(
                lifecycle,
                execution,
                FailureCategory.ARTIFACT,
                FailureReason.ARTIFACT_MISSING,
                observed_at,
            )
        if delivery_required:
            if not required_delivery_source:
                raise ValueError("Required delivery source must be specified")
            artifact_ids = [artifact.id for artifact in artifacts]
            confirmed = self.db.query(Delivery).filter(
                Delivery.artifact_id.in_(artifact_ids),
                Delivery.source == required_delivery_source,
                Delivery.status == DeliveryStatus.CONFIRMED.value,
            ).first()
            if confirmed is None:
                failed = self.db.query(Delivery).filter(
                    Delivery.artifact_id.in_(artifact_ids),
                    Delivery.source == required_delivery_source,
                    Delivery.status == DeliveryStatus.FAILED.value,
                ).first()
                reason = FailureReason.UPLOAD_FAILED if failed else FailureReason.CONFIRMATION_MISSING
                return self._fail(lifecycle, execution, FailureCategory.DELIVERY, reason, observed_at)
        return self._finish(lifecycle, execution, ExecutionStatus.SUCCEEDED, observed_at)

    def _finish(
        self,
        lifecycle: ExecutionService,
        execution: Execution,
        status: ExecutionStatus,
        observed_at: datetime,
    ) -> Execution:
        row = self._observe_execution(
            lifecycle=lifecycle,
            execution=execution,
            status=status,
            observed_at=observed_at,
        )
        row.failure_category = None
        row.failure_reason = None
        self.db.flush()
        return row

    @staticmethod
    def _observe_execution(
        *,
        lifecycle: ExecutionService,
        execution: Execution,
        status: ExecutionStatus,
        observed_at: datetime,
    ) -> Execution:
        kwargs = {
            "source": execution.source,
            "correlation_key": execution.correlation_key,
            "repository_id": execution.repository_id,
            "product_id": execution.product_id,
            "status": status,
            "observed_at": observed_at,
        }
        # INT-1/9 adds source-scoped execution identity. Keep this delivery
        # contract forward-compatible while INT-1/8 remains independently reviewable.
        if hasattr(execution, "source_scope"):
            kwargs["source_scope"] = execution.source_scope
        return lifecycle.observe_execution(**kwargs)

    def _fail(
        self,
        lifecycle: ExecutionService,
        execution: Execution,
        category: FailureCategory,
        reason: FailureReason,
        observed_at: datetime,
    ) -> Execution:
        row = self._finish(lifecycle, execution, ExecutionStatus.FAILED, observed_at)
        row.failure_category = category.value
        row.failure_reason = reason.value
        self.db.flush()
        return row
