"""UI-managed integration settings with encrypted credential storage.

Database configuration is authoritative once a connector has a row. Until an
operator saves or disables it in the UI, the existing environment variables
remain a backwards-compatible fallback.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import (
    IntegrationConfig,
    IntegrationConfigEvent,
    IntegrationInstanceTestState,
    SessionLocal,
)

logger = logging.getLogger(__name__)


class IntegrationConfigError(ValueError):
    pass


class CredentialStoreUnavailable(IntegrationConfigError):
    pass


class _GitlabInstanceTestMessage(str):
    """User-facing test result carrying the tested credential generation."""

    def __new__(
        cls,
        value: str,
        *,
        instance_id: str,
        config_fingerprint: str,
    ):
        result = super().__new__(cls, value)
        result.instance_id = instance_id
        result.config_fingerprint = config_fingerprint
        return result


class _GitlabInstanceTestError(IntegrationConfigError):
    def __init__(
        self,
        message: str,
        *,
        instance_id: str,
        config_fingerprint: str,
    ):
        super().__init__(message)
        self.message = message
        self.instance_id = instance_id
        self.config_fingerprint = config_fingerprint

    def __str__(self) -> str:
        return _GitlabInstanceTestMessage(
            self.message,
            instance_id=self.instance_id,
            config_fingerprint=self.config_fingerprint,
        )


CONNECTOR_SPECS: dict[str, dict[str, Any]] = {
    "gitlab": {
        "name": "GitLab",
        "role": "Trigger + execution source",
        "detail": "Pipeline registration, job polling, audit and repository discovery.",
        "direction": "inbound",
        "fields": [
            {"name": "api_url", "label": "API URL", "env": ("GITLAB_API_URL",), "default": "https://gitlab.com/api/v4", "kind": "url"},
            {"name": "token", "label": "Access token", "env": ("GITLAB_PAT", "GITLAB"), "secret": True},
            {"name": "instances_json", "label": "Multiple instances (JSON)", "env": ("GITLAB_INSTANCES",), "secret": True, "kind": "instances"},
        ],
    },
    "asoc": {
        "name": "ASOC / Vampy",
        "role": "Artifact delivery",
        "detail": "PT AI orchestration, SARIF delivery and connected-product reporting.",
        "direction": "outbound",
        "fields": [
            {"name": "url", "label": "API URL", "env": ("HEXWAY_URL",), "default": "https://asoc.example.com/api", "kind": "url"},
            {"name": "token", "label": "Bearer token", "env": ("HEXWAY_TOKEN",), "secret": True},
            {"name": "space_id", "label": "Space ID", "env": ("HEXWAY_SPACE_ID",), "default": ""},
        ],
    },
    "ptai": {
        "name": "PT AI Engine",
        "role": "Scanner telemetry",
        "detail": "Optional direct stage, progress and engine error enrichment.",
        "direction": "bidirectional",
        "fields": [
            {"name": "url", "label": "Engine URL", "env": ("PTAI_URL",), "default": "", "kind": "url"},
            {"name": "token", "label": "Access token", "env": ("PTAI_TOKEN",), "secret": True},
            {"name": "verify_ssl", "label": "Verify TLS certificate", "env": ("PTAI_VERIFY_SSL",), "default": "true", "kind": "boolean"},
        ],
    },
    "mattermost": {
        "name": "Mattermost",
        "role": "Operational alerts",
        "detail": "Failed scans, long-running jobs, queue pressure and audit digests.",
        "direction": "outbound",
        "fields": [
            {"name": "webhook_url", "label": "Incoming webhook URL", "env": ("MATTERMOST_WEBHOOK_URL",), "secret": True, "kind": "url"},
            {"name": "icon_url", "label": "Alert icon URL", "env": ("MATTERMOST_ALERT_ICON_URL",), "default": "", "kind": "url_optional"},
        ],
    },
}


ENV_TO_FIELD: dict[str, tuple[str, str]] = {}
for _connector_key, _spec in CONNECTOR_SPECS.items():
    for _field in _spec["fields"]:
        for _env_name in _field.get("env", ()):
            ENV_TO_FIELD[_env_name] = (_connector_key, _field["name"])


@dataclass
class RuntimeConnector:
    key: str
    values: dict[str, str]
    secrets: dict[str, str]
    enabled: bool
    source: str
    error: str | None = None
    row: IntegrationConfig | None = None

    def get(self, field: str, default: str = "") -> str:
        if not self.enabled:
            return default
        return self.secrets.get(field, self.values.get(field, default))


@dataclass(frozen=True)
class CredentialMigrationResult:
    total: int
    empty: int
    current: int
    recoverable: int
    migrated: int
    irrecoverable: int
    reset: int
    irrecoverable_connectors: tuple[str, ...]
    irrecoverable_fingerprints: tuple[tuple[str, str], ...]

    @property
    def remaining_irrecoverable(self) -> int:
        return self.irrecoverable - self.reset

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "empty": self.empty,
            "current": self.current,
            "recoverable": self.recoverable,
            "migrated": self.migrated,
            "irrecoverable": self.irrecoverable,
            "reset": self.reset,
            "remaining_irrecoverable": self.remaining_irrecoverable,
            "irrecoverable_connectors": list(self.irrecoverable_connectors),
        }

    def reset_manifest_entries(self) -> dict[str, str]:
        return dict(self.irrecoverable_fingerprints)


def credential_store_ready() -> bool:
    return bool((os.getenv("INTEGRATION_CREDENTIALS_KEY") or os.getenv("PIPE_BEARER_TOKEN") or "").strip())


def _fernet_for_material(material: str):
    if not material:
        raise CredentialStoreUnavailable(
            "INTEGRATION_CREDENTIALS_KEY is required to encrypt UI-managed integration credentials"
        )
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise CredentialStoreUnavailable("cryptography package is not installed") from exc
    digest = hashlib.sha256(b"scanops:integration-credentials:v1:" + material.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _current_key_material() -> str:
    return (os.getenv("INTEGRATION_CREDENTIALS_KEY") or os.getenv("PIPE_BEARER_TOKEN") or "").strip()


def _fernet():
    return _fernet_for_material(_current_key_material())


def _encrypt_secrets_with_material(values: dict[str, str], material: str) -> str:
    payload = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _fernet_for_material(material).encrypt(payload).decode("ascii")


def _encrypt_secrets(values: dict[str, str]) -> str:
    from .credential_separation import ensure_no_agent_reuse
    try:
        ensure_no_agent_reuse(values)
    except ValueError:
        raise IntegrationConfigError("Integration credentials must differ from the agent credential") from None
    return _encrypt_secrets_with_material(values, _current_key_material())


def _try_decrypt_secrets_with_material(payload: str, material: str) -> dict[str, str] | None:
    try:
        decoded = _fernet_for_material(material).decrypt(payload.encode("ascii"))
        values = json.loads(decoded.decode("utf-8"))
        if not isinstance(values, dict):
            return None
    except CredentialStoreUnavailable:
        raise
    except Exception:
        return None
    return {str(key): str(value) for key, value in values.items() if value is not None}


def _decrypt_secrets(payload: str | None) -> dict[str, str]:
    if not payload:
        return {}
    material = _current_key_material()
    if not material:
        raise CredentialStoreUnavailable(
            "INTEGRATION_CREDENTIALS_KEY is required to decrypt UI-managed integration credentials"
        )
    values = _try_decrypt_secrets_with_material(payload, material)
    if values is None:
        raise CredentialStoreUnavailable(
            "Stored credentials cannot be decrypted with the current application key"
        )
    return values


def _ciphertext_fingerprint(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def migrate_integration_credentials(
    db: Session,
    *,
    dry_run: bool = True,
    reset_irrecoverable: bool = False,
    reset_manifest: dict[str, str] | None = None,
    actor: str = "credential-migration",
    dedicated_material: str | None = None,
    legacy_material: str | None = None,
) -> CredentialMigrationResult:
    """Move legacy bearer-key ciphertext to the dedicated credential key.

    The legacy key is consulted only by this explicit migration path. All
    planned writes are committed together; a failed database operation rolls
    the whole batch back.
    """
    dedicated = (
        dedicated_material
        if dedicated_material is not None
        else os.getenv("INTEGRATION_CREDENTIALS_KEY", "")
    ).strip()
    legacy = (
        legacy_material
        if legacy_material is not None
        else os.getenv("PIPE_BEARER_TOKEN", "")
    ).strip()
    if not dedicated:
        raise CredentialStoreUnavailable(
            "INTEGRATION_CREDENTIALS_KEY is required for credential migration"
        )
    if not legacy:
        raise CredentialStoreUnavailable(
            "PIPE_BEARER_TOKEN must contain the legacy encryption key during credential migration"
        )
    if dedicated == legacy:
        raise CredentialStoreUnavailable(
            "INTEGRATION_CREDENTIALS_KEY must differ from the legacy PIPE_BEARER_TOKEN"
        )
    if reset_irrecoverable and dry_run:
        raise IntegrationConfigError(
            "reset_irrecoverable requires an applied migration, not a dry run"
        )
    if reset_irrecoverable and reset_manifest is None:
        raise IntegrationConfigError(
            "reset_irrecoverable requires a manifest from a prior dry run"
        )
    if reset_manifest is not None and not reset_irrecoverable:
        raise IntegrationConfigError(
            "reset_manifest is only valid with reset_irrecoverable"
        )

    query = db.query(IntegrationConfig).order_by(IntegrationConfig.connector)
    if not dry_run:
        query = query.with_for_update()
    rows = query.all()
    empty = current = 0
    recoverable_rows: list[tuple[IntegrationConfig, dict[str, str]]] = []
    irrecoverable_rows: list[tuple[IntegrationConfig, str]] = []

    try:
        for row in rows:
            payload = row.secrets_encrypted
            if not payload:
                empty += 1
                continue

            current_values = _try_decrypt_secrets_with_material(payload, dedicated)
            if current_values is not None:
                current += 1
                continue

            legacy_values = _try_decrypt_secrets_with_material(payload, legacy)
            if legacy_values is not None:
                recoverable_rows.append((row, legacy_values))
                continue

            irrecoverable_rows.append((row, _ciphertext_fingerprint(payload)))

        actual_reset_manifest = {
            row.connector: fingerprint
            for row, fingerprint in irrecoverable_rows
        }
        if reset_irrecoverable and reset_manifest != actual_reset_manifest:
            raise IntegrationConfigError(
                "Reset manifest does not exactly match the current irrecoverable rows"
            )

        migrated = 0
        reset = 0
        if not dry_run:
            for row, legacy_values in recoverable_rows:
                row.secrets_encrypted = _encrypt_secrets_with_material(
                    legacy_values, dedicated
                )
                row.updated_by = actor
                db.add(
                    IntegrationConfigEvent(
                        connector=row.connector,
                        action="credential_migrate",
                        actor=actor,
                        changed_fields_json='["credentials"]',
                        success=True,
                        message="Credentials re-encrypted with the dedicated key",
                    )
                )
                migrated += 1

            if reset_irrecoverable:
                for row, _fingerprint in irrecoverable_rows:
                    row.secrets_encrypted = None
                    row.enabled = False
                    row.updated_by = actor
                    row.tested_at = None
                    row.test_status = None
                    row.test_message = None
                    db.add(
                        IntegrationConfigEvent(
                            connector=row.connector,
                            action="credential_reset",
                            actor=actor,
                            changed_fields_json=(
                                '["credentials", "enabled", "test_status"]'
                            ),
                            success=True,
                            message=(
                                "Irrecoverable credentials reset; connector disabled"
                            ),
                        )
                    )
                    reset += 1
            db.commit()
    except Exception:
        db.rollback()
        raise

    return CredentialMigrationResult(
        total=len(rows),
        empty=empty,
        current=current,
        recoverable=len(recoverable_rows),
        migrated=migrated,
        irrecoverable=len(irrecoverable_rows),
        reset=reset,
        irrecoverable_connectors=tuple(
            row.connector for row, _fingerprint in irrecoverable_rows
        ),
        irrecoverable_fingerprints=tuple(
            (row.connector, fingerprint)
            for row, fingerprint in irrecoverable_rows
        ),
    )
def _json_object(raw: str | None) -> dict[str, str]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if item is not None}


def _env_value(field: dict[str, Any]) -> str:
    for name in field.get("env", ()):
        value = os.getenv(name)
        if value is not None and value != "":
            return value
    return str(field.get("default", ""))


def _environment_connector(key: str) -> RuntimeConnector:
    values: dict[str, str] = {}
    secrets: dict[str, str] = {}
    for field in CONNECTOR_SPECS[key]["fields"]:
        value = _env_value(field)
        (secrets if field.get("secret") else values)[field["name"]] = value
    return RuntimeConnector(key=key, values=values, secrets=secrets, enabled=True, source="environment")


def load_runtime_connector(key: str, db: Session | None = None) -> RuntimeConnector:
    if key not in CONNECTOR_SPECS:
        raise KeyError(key)
    owns_session = db is None
    session = db or SessionLocal()
    try:
        row = session.query(IntegrationConfig).filter(IntegrationConfig.connector == key).first()
        if row is None:
            return _environment_connector(key)
        values = _json_object(row.values_json)
        try:
            secrets = _decrypt_secrets(row.secrets_encrypted)
            error = None
        except CredentialStoreUnavailable as exc:
            secrets = {}
            error = str(exc)
        return RuntimeConnector(
            key=key,
            values=values,
            secrets=secrets,
            enabled=bool(row.enabled),
            source="database",
            error=error,
            row=row,
        )
    except SQLAlchemyError as exc:
        logger.debug("integration_config_db_fallback connector=%s error=%s", key, exc.__class__.__name__)
        return _environment_connector(key)
    finally:
        if owns_session:
            session.close()


def runtime_setting(env_name: str, default: str = "", db: Session | None = None) -> str:
    mapping = ENV_TO_FIELD.get(env_name)
    if mapping is None:
        return os.getenv(env_name, default)
    connector, field = mapping
    runtime = load_runtime_connector(connector, db)
    if connector == "gitlab":
        if not runtime.enabled:
            value = ""
        else:
            try:
                active_instances = [
                    entry
                    for entry in _runtime_gitlab_instances(runtime)
                    if entry["enabled"]
                    and bool(entry.get("polling", {}).get("enabled", True))
                ]
            except IntegrationConfigError:
                active_instances = []
            if field == "instances_json":
                value = json.dumps(
                    active_instances, sort_keys=True, separators=(",", ":")
                )
            elif field == "api_url":
                value = active_instances[0]["api_url"] if active_instances else ""
            elif field == "token":
                value = active_instances[0]["token"] if active_instances else ""
            else:
                value = runtime.get(field, default)
    elif runtime.source == "environment":
        spec = next(item for item in CONNECTOR_SPECS[connector]["fields"] if item["name"] == field)
        value = _env_value(spec)
    else:
        if not runtime.enabled:
            return default
        value = runtime.get(field, default)
    return value if value is not None else default


def connector_is_configured(runtime: RuntimeConnector) -> bool:
    if not runtime.enabled or runtime.error:
        return False
    if runtime.key == "gitlab":
        try:
            return any(
                entry["enabled"] and bool(entry["token"])
                for entry in _runtime_gitlab_instances(runtime)
            )
        except IntegrationConfigError:
            return False
    if runtime.key == "asoc":
        return bool(runtime.get("url").strip() and runtime.get("token").strip())
    if runtime.key == "ptai":
        return bool(runtime.get("url").strip() and runtime.get("token").strip())
    if runtime.key == "mattermost":
        return bool(runtime.get("webhook_url").strip())
    return False


_GITLAB_INSTANCE_ID_RE = re.compile(r"gl-[a-f0-9]{16}")
_GITLAB_TEST_STATUS_INVALIDATED = "unknown"
_GITLAB_TEST_MESSAGE_INVALIDATED = "Configuration changed · test required"


def _gitlab_instance_id(host: str) -> str:
    """Return a stable, non-secret identifier for a GitLab host.

    Existing installations did not persist instance identifiers. Deriving the
    initial id from the normalized host lets those configurations participate
    in instance-scoped routes without exposing their token or relying on a
    mutable form index.
    """
    normalized = host.strip().lower()
    if not normalized:
        raise IntegrationConfigError("GitLab instance host is required")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"gl-{digest}"


def _normalize_gitlab_instance_id(value: str, host: str) -> str:
    candidate = value.strip().lower()
    if not candidate:
        return _gitlab_instance_id(host)
    if not _GITLAB_INSTANCE_ID_RE.fullmatch(candidate):
        raise IntegrationConfigError("GitLab instance identifier is invalid")
    return candidate


def _new_gitlab_instance_id() -> str:
    return f"gl-{secrets.token_hex(8)}"


def _gitlab_instance_config_fingerprint(entry: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "id": entry["id"],
            "api_url": entry["api_url"],
            "token": entry["token"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _runtime_gitlab_instances(runtime: RuntimeConnector) -> list[dict[str, Any]]:
    """Resolve configured GitLab instances, including their credentials.

    This private representation must never be sent to a template or serialized
    into an event because it contains access tokens.
    """
    if runtime.key != "gitlab":
        raise IntegrationConfigError("GitLab instance operation requires the GitLab connector")
    raw = runtime.secrets.get(
        "instances_json", runtime.values.get("instances_json", "")
    ).strip()
    entries: list[dict[str, Any]] = []
    use_direct_environment_fallback = False
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise IntegrationConfigError("Stored GitLab instances are not valid JSON") from exc
        if isinstance(parsed, dict):
            parsed = [
                {
                    **item,
                    "host": str(item.get("host") or host),
                }
                for host, item in parsed.items()
                if isinstance(item, dict)
            ]
        if not isinstance(parsed, list):
            raise IntegrationConfigError(
                "Stored GitLab instances must be an array or host mapping"
            )
        if not parsed:
            direct_api_url = runtime.values.get("api_url", "").strip()
            direct_token = runtime.secrets.get("token", "").strip()
            if runtime.source == "environment" and direct_api_url and direct_token:
                use_direct_environment_fallback = True
            elif runtime.source == "environment":
                return []
            else:
                raise IntegrationConfigError(
                    "Stored GitLab instances must be a non-empty array"
                )
        for item in parsed:
            if not isinstance(item, dict):
                raise IntegrationConfigError("Stored GitLab instance is invalid")
            api_url = _validate_url(str(item.get("api_url") or ""))
            host = str(item.get("host") or urlparse(api_url).netloc or "").strip().lower()
            token = str(item.get("token") or "").strip()
            if not host:
                raise IntegrationConfigError("Stored GitLab instance is incomplete")
            entries.append(
                {
                    "id": _normalize_gitlab_instance_id(str(item.get("id") or ""), host),
                    "name": str(item.get("name") or host).strip(),
                    "host": host,
                    "api_url": api_url,
                    "token": token,
                    "enabled": bool(item.get("enabled", True)),
                    "polling": (
                        dict(item.get("polling") or {})
                        if isinstance(item.get("polling"), dict)
                        else {"enabled": True}
                    ),
                }
            )
    if not raw or use_direct_environment_fallback:
        api_url = _validate_url(
            runtime.values.get("api_url", "https://gitlab.com/api/v4")
        )
        host = urlparse(api_url).netloc.strip().lower()
        token = runtime.secrets.get("token", "").strip()
        entries.append(
            {
                "id": _gitlab_instance_id(host),
                "name": host or "GitLab",
                "host": host,
                "api_url": api_url,
                "token": token,
                "enabled": True,
                "polling": {"enabled": True},
            }
        )
    ids = [entry["id"] for entry in entries]
    if len(ids) != len(set(ids)):
        raise IntegrationConfigError("GitLab instance identifiers must be unique")
    return entries


def _gitlab_instances_for_snapshot(runtime: RuntimeConnector) -> list[dict[str, Any]]:
    private_entries = _runtime_gitlab_instances(runtime)
    return [
        {
            "id": entry["id"],
            "name": entry["name"],
            "host": entry["host"],
            "api_url": entry["api_url"],
            "token_configured": bool(entry["token"]),
            "enabled": bool(entry["enabled"]),
            "polling_enabled": bool(entry.get("polling", {}).get("enabled", True)),
        }
        for entry in private_entries
    ]


def _gitlab_instance_test_metadata(
    db: Session,
) -> dict[
    str,
    tuple[
        datetime | None,
        str | None,
        str | None,
        datetime | None,
        str | None,
    ],
]:
    """Return the latest independent test result for each GitLab instance."""
    return {
        state.instance_id: (
            (
                None
                if (
                    state.test_status == _GITLAB_TEST_STATUS_INVALIDATED
                    and state.test_message == _GITLAB_TEST_MESSAGE_INVALIDATED
                )
                else state.tested_at
            ),
            (
                None
                if (
                    state.test_status == _GITLAB_TEST_STATUS_INVALIDATED
                    and state.test_message == _GITLAB_TEST_MESSAGE_INVALIDATED
                )
                else state.test_status
            ),
            (
                None
                if (
                    state.test_status == _GITLAB_TEST_STATUS_INVALIDATED
                    and state.test_message == _GITLAB_TEST_MESSAGE_INVALIDATED
                )
                else state.test_message
            ),
            state.last_success_at,
            state.last_success_message,
        )
        for state in (
            db.query(IntegrationInstanceTestState)
            .filter(IntegrationInstanceTestState.connector == "gitlab")
            .all()
        )
    }


def _connector_test_metadata(
    db: Session,
    runtime: RuntimeConnector,
) -> tuple[datetime | None, str | None, str | None]:
    """Read test state without turning environment fallback into DB config."""
    if runtime.row is not None:
        return (
            runtime.row.tested_at,
            runtime.row.test_status,
            runtime.row.test_message,
        )
    latest_event = (
        db.query(IntegrationConfigEvent)
        .filter(
            IntegrationConfigEvent.connector == runtime.key,
            IntegrationConfigEvent.action == "test",
        )
        .order_by(IntegrationConfigEvent.id.desc())
        .first()
    )
    if latest_event is None:
        return None, None, None
    return (
        latest_event.created_at,
        "healthy" if latest_event.success else "failed",
        latest_event.message,
    )


def _connector_last_success_metadata(
    db: Session,
    runtime: RuntimeConnector,
) -> tuple[datetime | None, str | None]:
    latest_success = (
        db.query(IntegrationConfigEvent)
        .filter(
            IntegrationConfigEvent.connector == runtime.key,
            IntegrationConfigEvent.action == "test",
            IntegrationConfigEvent.success.is_(True),
        )
        .order_by(IntegrationConfigEvent.id.desc())
        .first()
    )
    if latest_success is None:
        return None, None
    return latest_success.created_at, latest_success.message


def connector_snapshot(db: Session, key: str) -> dict[str, Any]:
    spec = CONNECTOR_SPECS[key]
    runtime = load_runtime_connector(key, db)
    tested_at, test_status, test_message = _connector_test_metadata(db, runtime)
    last_success_at, last_success_message = _connector_last_success_metadata(
        db, runtime
    )
    fields = []
    for field in spec["fields"]:
        is_secret = bool(field.get("secret"))
        stored_value = (
            runtime.secrets.get(field["name"], str(field.get("default", "")))
            if is_secret
            else runtime.values.get(field["name"], str(field.get("default", "")))
        )
        fields.append(
            {
                **field,
                "value": "" if is_secret else stored_value,
                "configured": bool(stored_value),
            }
        )
    snapshot = {
        "key": key,
        "name": spec["name"],
        "role": spec["role"],
        "detail": spec["detail"],
        "direction": spec["direction"],
        "fields": fields,
        "configured": connector_is_configured(runtime),
        "enabled": runtime.enabled,
        "source": runtime.source,
        "credential_ref": f"database://integration-configs/{key}" if runtime.source == "database" else "environment fallback",
        "updated_at": runtime.row.updated_at if runtime.row else None,
        "updated_by": runtime.row.updated_by if runtime.row else None,
        "tested_at": tested_at,
        "test_status": test_status,
        "test_message": test_message,
        "last_successful_check": (
            {
                "at": last_success_at,
                "message": last_success_message,
            }
            if last_success_at
            else None
        ),
        "error": runtime.error,
    }
    if key == "gitlab":
        try:
            snapshot["instances"] = _gitlab_instances_for_snapshot(runtime)
        except IntegrationConfigError as exc:
            snapshot["instances"] = []
            snapshot["error"] = snapshot["error"] or str(exc)
        instance_test_metadata = _gitlab_instance_test_metadata(db)
        for instance in snapshot["instances"]:
            tested_at, status, message, last_success_at, last_success_message = (
                instance_test_metadata.get(
                    instance["id"], (None, None, None, None, None)
                )
            )
            instance["tested_at"] = tested_at
            instance["test_status"] = status
            instance["test_message"] = message
            instance["last_success_at"] = last_success_at
            instance["last_success_message"] = last_success_message
            instance["last_successful_check"] = (
                {
                    "at": last_success_at,
                    "message": last_success_message,
                }
                if last_success_at
                else None
            )
            instance["credential_ref"] = (
                f"database://integration-configs/gitlab/instances/{instance['id']}"
                if runtime.source == "database"
                else f"environment://gitlab/instances/{instance['id']}"
            )
            instance["health_label"] = (
                "Disabled"
                if not instance["enabled"]
                else (
                    message
                    if tested_at and message
                    else ("Connected · not tested" if instance["token_configured"] else "Not connected")
                )
            )
        snapshot["instance_count"] = sum(
            1 for instance in snapshot["instances"] if instance["token_configured"]
        )
        snapshot["endpoints"] = [
            {
                "name": instance["name"],
                "value": instance["api_url"],
                "configured": instance["token_configured"],
            }
            for instance in snapshot["instances"]
        ]
    elif key == "asoc":
        endpoint = str(runtime.values.get("url") or "").strip()
        snapshot["endpoints"] = [
            {
                "name": "ASOC API",
                "value": endpoint,
                "configured": bool(endpoint and str(runtime.secrets.get("token") or "").strip()),
            }
        ] if endpoint else []
    elif key == "ptai":
        endpoint = str(runtime.values.get("url") or "").strip()
        snapshot["endpoints"] = [
            {
                "name": "PT AI Engine",
                "value": endpoint,
                "configured": bool(endpoint and str(runtime.secrets.get("token") or "").strip()),
            }
        ] if endpoint else []
    elif key == "mattermost":
        webhook = str(runtime.secrets.get("webhook_url") or "").strip()
        webhook_host = urlparse(webhook).netloc if webhook else ""
        snapshot["endpoints"] = [
            {
                "name": "Mattermost webhook",
                "value": webhook_host,
                "configured": bool(webhook),
            }
        ] if webhook_host else []
    snapshot["health_label"] = (
        "Disabled"
        if not snapshot["enabled"]
        else (
            snapshot["test_message"]
            if snapshot["tested_at"] and snapshot["test_message"]
            else ("Connected · not tested" if snapshot["configured"] else "Not connected")
        )
    )
    return snapshot


def _validate_url(value: str, *, optional: bool = False) -> str:
    value = value.strip().rstrip("/")
    if not value and optional:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise IntegrationConfigError("URL must start with http:// or https:// and include a host")
    if parsed.username or parsed.password:
        raise IntegrationConfigError("Credentials must not be embedded in a URL")
    return value


def _validate_instances(raw: str) -> str:
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntegrationConfigError("Multiple GitLab instances must be valid JSON") from exc
    if not isinstance(entries, list) or not entries:
        raise IntegrationConfigError("Multiple GitLab instances must be a non-empty JSON array")
    normalized = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise IntegrationConfigError("Each GitLab instance must be a JSON object")
        host = str(entry.get("host") or "").strip().lower()
        api_url = _validate_url(str(entry.get("api_url") or ""))
        token = str(entry.get("token") or "").strip()
        if not host or not token:
            raise IntegrationConfigError("Each GitLab instance requires host, api_url and token")
        normalized.append(
            {
                "id": (
                    _normalize_gitlab_instance_id(str(entry.get("id") or ""), host)
                    if entry.get("id")
                    else _new_gitlab_instance_id()
                ),
                "name": str(entry.get("name") or host).strip(),
                "host": host,
                "api_url": api_url,
                "token": token,
                "enabled": bool(entry.get("enabled", True)),
                "polling": (
                    dict(entry.get("polling") or {})
                    if isinstance(entry.get("polling"), dict)
                    else {"enabled": True}
                ),
            }
        )
    _validate_gitlab_runtime_ambiguity(normalized)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _validate_gitlab_runtime_ambiguity(
    instances: list[dict[str, Any]],
) -> None:
    """Prevent workers from silently choosing one credential for a host."""
    active_by_host: dict[str, str] = {}
    for entry in instances:
        if not entry.get("enabled", True):
            continue
        if not bool(entry.get("polling", {}).get("enabled", True)):
            continue
        host = str(entry.get("host") or "").strip().lower()
        if host in active_by_host:
            raise IntegrationConfigError(
                f"GitLab host {host} is added more than once for polling. "
                "Keep polling enabled for only one instance until products can bind to an instance id."
            )
        active_by_host[host] = str(entry.get("id") or "")


def _structured_gitlab_instances(
    submitted: dict[str, str],
    previous_values: dict[str, str],
    previous_secrets: dict[str, str],
) -> list[dict[str, str]]:
    indices = sorted(
        {
            int(match.group(1))
            for name in submitted
            if (match := re.fullmatch(r"gitlab_instance_api_url_(\d+)", name))
        }
    )
    if not indices:
        raise IntegrationConfigError("Add at least one GitLab instance")

    previous_by_host: dict[str, dict[str, Any]] = {}
    previous_by_id: dict[str, dict[str, Any]] = {}
    previous_raw = previous_secrets.get("instances_json", "")
    if previous_raw:
        try:
            previous_entries = json.loads(previous_raw)
        except json.JSONDecodeError:
            previous_entries = []
        if isinstance(previous_entries, list):
            previous_by_host = {
                str(entry.get("host") or "").strip().lower(): entry
                for entry in previous_entries
                if isinstance(entry, dict) and entry.get("host")
            }
            previous_by_id = {
                _normalize_gitlab_instance_id(
                    str(entry.get("id") or ""),
                    str(entry.get("host") or "").strip().lower(),
                ): entry
                for entry in previous_entries
                if isinstance(entry, dict) and entry.get("host")
            }
    if not previous_by_host and previous_secrets.get("token"):
        previous_api_url = previous_values.get("api_url", "")
        previous_host = urlparse(previous_api_url).netloc.strip().lower()
        if previous_host:
            legacy = {
                "id": _gitlab_instance_id(previous_host),
                "host": previous_host,
                "api_url": previous_api_url,
                "token": previous_secrets["token"],
            }
            previous_by_host[previous_host] = legacy
            previous_by_id[legacy["id"]] = legacy

    instances: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index in indices:
        api_url = _validate_url(submitted.get(f"gitlab_instance_api_url_{index}", ""))
        host = urlparse(api_url).netloc.strip().lower()
        original_host = submitted.get(f"gitlab_instance_original_host_{index}", "").strip().lower()
        name = submitted.get(f"gitlab_instance_name_{index}", "").strip() or host
        token = submitted.get(f"gitlab_instance_token_{index}", "").strip()
        submitted_id = submitted.get(f"gitlab_instance_id_{index}", "").strip().lower()
        if submitted_id and not _GITLAB_INSTANCE_ID_RE.fullmatch(submitted_id):
            raise IntegrationConfigError("GitLab instance identifier is invalid")
        previous = (
            previous_by_id.get(submitted_id)
            if submitted_id
            else previous_by_host.get(original_host) or previous_by_host.get(host)
        ) or {}
        if submitted_id and not previous:
            raise IntegrationConfigError("GitLab instance identifier does not match stored configuration")
        instance_id = (
            _normalize_gitlab_instance_id(str(previous.get("id") or submitted_id), host)
            if previous or submitted_id
            else _new_gitlab_instance_id()
        )
        token = token or str(previous.get("token") or "").strip()
        enabled_raw = submitted.get(f"gitlab_instance_enabled_{index}")
        enabled = (
            str(enabled_raw).lower() in {"1", "true", "yes", "on"}
            if enabled_raw is not None
            else bool(previous.get("enabled", True))
        )
        polling = (
            dict(previous.get("polling") or {})
            if isinstance(previous.get("polling"), dict)
            else {"enabled": True}
        )
        polling_raw = submitted.get(f"gitlab_instance_polling_enabled_{index}")
        if polling_raw is not None:
            polling["enabled"] = str(polling_raw).lower() in {
                "1",
                "true",
                "yes",
                "on",
            }

        if not token:
            raise IntegrationConfigError(f"Access token is required for {name}")
        if instance_id in seen_ids:
            raise IntegrationConfigError("GitLab instance identifier is added more than once")
        seen_ids.add(instance_id)
        instances.append(
            {
                "id": instance_id,
                "name": name,
                "host": host,
                "api_url": api_url,
                "token": token,
                "enabled": enabled,
                "polling": polling,
            }
        )
    _validate_gitlab_runtime_ambiguity(instances)
    return instances


def _has_complete_replacement_credentials(key: str, submitted: dict[str, str]) -> bool:
    """Return whether an unreadable encrypted payload can be replaced safely.

    Blank secret inputs normally mean "keep the stored value". That is
    impossible after a credential-key rotation, so recovery is allowed only
    when the operator explicitly supplies every credential needed by the
    connector.
    """
    if key == "gitlab":
        if submitted.get("gitlab_instances_mode") == "structured":
            indices = sorted(
                {
                    int(match.group(1))
                    for name in submitted
                    if (match := re.fullmatch(r"gitlab_instance_api_url_(\d+)", name))
                }
            )
            return bool(indices) and all(
                submitted.get(f"gitlab_instance_token_{index}", "").strip()
                for index in indices
            )
        return bool(submitted.get("token", "").strip())

    required = [
        field["name"]
        for field in CONNECTOR_SPECS[key]["fields"]
        if field.get("secret")
    ]
    return bool(required) and all(submitted.get(name, "").strip() for name in required)


def save_connector(
    db: Session,
    key: str,
    submitted: dict[str, str],
    *,
    actor: str,
) -> IntegrationConfig:
    if key not in CONNECTOR_SPECS:
        raise IntegrationConfigError("Unknown connector")
    if not credential_store_ready():
        raise CredentialStoreUnavailable("Credential storage needs INTEGRATION_CREDENTIALS_KEY")

    row = (
        db.query(IntegrationConfig)
        .filter(IntegrationConfig.connector == key)
        .with_for_update()
        .first()
    )
    replacing_unreadable_credentials = False
    if row:
        previous_values = _json_object(row.values_json)
        try:
            previous_secrets = _decrypt_secrets(row.secrets_encrypted)
        except CredentialStoreUnavailable as exc:
            if not _has_complete_replacement_credentials(key, submitted):
                raise CredentialStoreUnavailable(
                    "Stored credentials use a different INTEGRATION_CREDENTIALS_KEY. "
                    "Enter every credential field to replace them, or restore the previous key."
                ) from exc
            previous_secrets = {}
            replacing_unreadable_credentials = True
    else:
        environment = _environment_connector(key)
        previous_values = dict(environment.values)
        previous_secrets = dict(environment.secrets)
    values = dict(previous_values)
    secrets = dict(previous_secrets)
    changed: list[str] = []
    if replacing_unreadable_credentials:
        changed.append("credentials_replaced")

    if key == "gitlab" and submitted.get("gitlab_instances_mode") == "structured":
        instances = _structured_gitlab_instances(submitted, previous_values, previous_secrets)
        serialized_instances = json.dumps(instances, sort_keys=True, separators=(",", ":"))
        first = instances[0]
        if values.get("api_url") != first["api_url"]:
            changed.append("api_url")
        if secrets.get("instances_json") != serialized_instances:
            changed.append("instances")
        values["api_url"] = first["api_url"]
        secrets["token"] = first["token"]
        secrets["instances_json"] = serialized_instances
        instance_ids = [instance["id"] for instance in instances]
        (
            db.query(IntegrationInstanceTestState)
            .filter(
                IntegrationInstanceTestState.connector == "gitlab",
                ~IntegrationInstanceTestState.instance_id.in_(instance_ids),
            )
            .delete(synchronize_session=False)
        )
    else:
        fields = CONNECTOR_SPECS[key]["fields"]
        for field in fields:
            name = field["name"]
            raw = str(submitted.get(name, "")).strip()
            if field.get("secret"):
                if not raw:
                    continue
                if field.get("kind") == "url":
                    raw = _validate_url(raw)
                elif field.get("kind") == "instances":
                    raw = _validate_instances(raw)
                if secrets.get(name) != raw:
                    secrets[name] = raw
                    changed.append(name)
                continue

            if field.get("kind") == "url":
                raw = _validate_url(raw)
            elif field.get("kind") == "url_optional":
                raw = _validate_url(raw, optional=True)
            elif field.get("kind") == "boolean":
                raw = "true" if raw.lower() in {"1", "true", "yes", "on"} else "false"
            if values.get(name) != raw:
                values[name] = raw
                changed.append(name)

    if (
        key == "gitlab"
        and row is None
        and submitted.get("token", "").strip()
        and not submitted.get("instances_json", "").strip()
    ):
        # A legacy single-instance save explicitly supplies its own endpoint
        # and token. Do not copy an environment placeholder such as ``[]``
        # into the managed row, where it would mask those submitted fields.
        secrets.pop("instances_json", None)

    if row is None:
        row = IntegrationConfig(connector=key)
        db.add(row)
    row.values_json = json.dumps(values, sort_keys=True, separators=(",", ":"))
    row.secrets_encrypted = _encrypt_secrets(secrets)
    row.enabled = True
    row.updated_by = actor
    row.test_status = None
    row.test_message = None
    db.add(
        IntegrationConfigEvent(
            connector=key,
            action="save",
            actor=actor,
            changed_fields_json=json.dumps(sorted(set(changed))),
            success=True,
            message="Configuration saved",
        )
    )
    db.commit()
    db.refresh(row)
    return row


def save_gitlab_instance(
    db: Session,
    instance_id: str,
    submitted: dict[str, str],
    *,
    actor: str,
) -> IntegrationConfig:
    """Update exactly one existing GitLab instance.

    The instance id, rather than a form index or submitted host, selects the
    record. The complete connector payload is then saved under the same row
    lock as the existing structured editor, so another instance is not
    accidentally replaced by the edited values.
    """
    normalized_id = instance_id.strip().lower()
    if not _GITLAB_INSTANCE_ID_RE.fullmatch(normalized_id):
        raise IntegrationConfigError("GitLab instance identifier is invalid")
    row = (
        db.query(IntegrationConfig)
        .filter(IntegrationConfig.connector == "gitlab")
        .with_for_update()
        .first()
    )
    if row is None:
        if submitted.get("adopt_environment", "").lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise IntegrationConfigError(
                "Environment-backed GitLab configuration is read-only. "
                "Confirm environment import before editing this instance."
            )
        environment = _environment_connector("gitlab")
        values = dict(environment.values)
        stored_secrets = dict(environment.secrets)
        runtime = environment
        row = IntegrationConfig(connector="gitlab", enabled=True)
        db.add(row)
    else:
        values = _json_object(row.values_json)
        stored_secrets = _decrypt_secrets(row.secrets_encrypted)
        runtime = RuntimeConnector(
            key="gitlab",
            values=values,
            secrets=stored_secrets,
            enabled=bool(row.enabled),
            source="database",
            row=row,
        )
    instances = _runtime_gitlab_instances(runtime)
    matches = [entry for entry in instances if entry["id"] == normalized_id]
    if len(matches) != 1:
        raise IntegrationConfigError("GitLab instance identifier does not match stored configuration")

    edited = matches[0]
    api_url = _validate_url(submitted.get("api_url", edited["api_url"]))
    host = urlparse(api_url).netloc.strip().lower()
    name = submitted.get("name", "").strip() or edited["name"] or host
    token = submitted.get("token", "").strip() or edited["token"]
    enabled_raw = submitted.get("enabled")
    enabled = (
        str(enabled_raw).lower() in {"1", "true", "yes", "on"}
        if enabled_raw is not None
        else bool(edited.get("enabled", True))
    )
    polling = (
        dict(edited.get("polling") or {})
        if isinstance(edited.get("polling"), dict)
        else {"enabled": True}
    )
    polling_raw = submitted.get("polling_enabled")
    if polling_raw is not None:
        polling["enabled"] = str(polling_raw).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    changed_fields = []
    if name != edited["name"]:
        changed_fields.append("name")
    if api_url != edited["api_url"]:
        changed_fields.append("api_url")
    if token != edited["token"]:
        changed_fields.append("token")
    if enabled != bool(edited.get("enabled", True)):
        changed_fields.append("enabled")
    if bool(polling.get("enabled", True)) != bool(
        edited.get("polling", {}).get("enabled", True)
    ):
        changed_fields.append("polling_enabled")
    for index, entry in enumerate(instances):
        if entry["id"] == normalized_id:
            instances[index] = {
                **entry,
                "name": name,
                "host": host,
                "api_url": api_url,
                "token": token,
                "enabled": enabled,
                "polling": polling,
            }
            break
    _validate_gitlab_runtime_ambiguity(instances)

    first = instances[0]
    values["api_url"] = first["api_url"]
    stored_secrets["token"] = first["token"]
    stored_secrets["instances_json"] = json.dumps(
        instances, sort_keys=True, separators=(",", ":")
    )
    row.values_json = json.dumps(values, sort_keys=True, separators=(",", ":"))
    row.secrets_encrypted = _encrypt_secrets(stored_secrets)
    row.updated_by = actor
    if {"api_url", "token"} & set(changed_fields):
        test_state = (
            db.query(IntegrationInstanceTestState)
            .filter(
                IntegrationInstanceTestState.connector == "gitlab",
                IntegrationInstanceTestState.instance_id == normalized_id,
            )
            .with_for_update()
            .first()
        )
        if test_state is not None:
            test_state.test_status = _GITLAB_TEST_STATUS_INVALIDATED
            test_state.test_message = _GITLAB_TEST_MESSAGE_INVALIDATED
    db.add(
        IntegrationConfigEvent(
            connector="gitlab",
            action="save",
            actor=actor,
            changed_fields_json=json.dumps(
                {"instance_id": normalized_id, "fields": changed_fields},
                sort_keys=True,
            ),
            success=True,
            message="GitLab instance configuration saved",
        )
    )
    db.commit()
    db.refresh(row)
    return row


def create_gitlab_instance(
    db: Session,
    submitted: dict[str, str],
    *,
    actor: str,
) -> tuple[IntegrationConfig, str]:
    """Create one independent GitLab instance with a new opaque id."""
    if not credential_store_ready():
        raise CredentialStoreUnavailable("Credential storage needs INTEGRATION_CREDENTIALS_KEY")
    api_url = _validate_url(submitted.get("api_url", ""))
    host = urlparse(api_url).netloc.strip().lower()
    name = submitted.get("name", "").strip() or host
    token = submitted.get("token", "").strip()
    if not token:
        raise IntegrationConfigError(f"Access token is required for {name}")
    enabled = str(submitted.get("enabled", "true")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    polling_enabled = str(submitted.get("polling_enabled", "true")).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    row = (
        db.query(IntegrationConfig)
        .filter(IntegrationConfig.connector == "gitlab")
        .with_for_update()
        .first()
    )
    if row is None:
        environment = _environment_connector("gitlab")
        environment_instances = _runtime_gitlab_instances(environment)
        has_environment_credentials = any(
            bool(entry.get("token")) for entry in environment_instances
        )
        if has_environment_credentials and submitted.get("adopt_environment", "").lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise IntegrationConfigError(
                "Environment-backed GitLab configuration is read-only. "
                "Confirm environment import before adding a managed instance."
            )
        instances = environment_instances if has_environment_credentials else []
        values = dict(environment.values)
        stored_secrets = dict(environment.secrets)
        row = IntegrationConfig(connector="gitlab")
        db.add(row)
    else:
        values = _json_object(row.values_json)
        stored_secrets = _decrypt_secrets(row.secrets_encrypted)
        instances = _runtime_gitlab_instances(
            RuntimeConnector(
                key="gitlab",
                values=values,
                secrets=stored_secrets,
                enabled=bool(row.enabled),
                source="database",
                row=row,
            )
        )
    instance_id = _new_gitlab_instance_id()
    instances.append(
        {
            "id": instance_id,
            "name": name,
            "host": host,
            "api_url": api_url,
            "token": token,
            "enabled": enabled,
            "polling": {"enabled": polling_enabled},
        }
    )
    _validate_gitlab_runtime_ambiguity(instances)
    first = instances[0]
    values["api_url"] = first["api_url"]
    stored_secrets["token"] = first["token"]
    stored_secrets["instances_json"] = json.dumps(
        instances, sort_keys=True, separators=(",", ":")
    )
    row.values_json = json.dumps(values, sort_keys=True, separators=(",", ":"))
    row.secrets_encrypted = _encrypt_secrets(stored_secrets)
    row.enabled = True
    row.updated_by = actor
    db.add(
        IntegrationConfigEvent(
            connector="gitlab",
            action="create_instance",
            actor=actor,
            changed_fields_json=json.dumps({"instance_id": instance_id}, sort_keys=True),
            success=True,
            message="GitLab instance created",
        )
    )
    db.commit()
    db.refresh(row)
    return row, instance_id


def delete_gitlab_instance(
    db: Session,
    instance_id: str,
    *,
    actor: str,
) -> IntegrationConfig:
    """Remove one database-backed GitLab instance and its health state."""
    normalized_id = instance_id.strip().lower()
    if not _GITLAB_INSTANCE_ID_RE.fullmatch(normalized_id):
        raise IntegrationConfigError("GitLab instance identifier is invalid")
    row = (
        db.query(IntegrationConfig)
        .filter(IntegrationConfig.connector == "gitlab")
        .with_for_update()
        .first()
    )
    if row is None:
        raise IntegrationConfigError(
            "Environment-backed GitLab configuration is read-only. "
            "Import it into Integrations before removing an instance."
        )
    values = _json_object(row.values_json)
    stored_secrets = _decrypt_secrets(row.secrets_encrypted)
    runtime = RuntimeConnector(
        key="gitlab",
        values=values,
        secrets=stored_secrets,
        enabled=bool(row.enabled),
        source="database",
        row=row,
    )
    instances = _runtime_gitlab_instances(runtime)
    remaining = [entry for entry in instances if entry["id"] != normalized_id]
    if len(remaining) == len(instances):
        raise IntegrationConfigError("GitLab instance identifier does not match stored configuration")
    if not remaining:
        raise IntegrationConfigError(
            "The last GitLab instance cannot be removed; disable the connector instead"
        )
    first = remaining[0]
    values["api_url"] = first["api_url"]
    stored_secrets["token"] = first["token"]
    stored_secrets["instances_json"] = json.dumps(
        remaining, sort_keys=True, separators=(",", ":")
    )
    row.values_json = json.dumps(values, sort_keys=True, separators=(",", ":"))
    row.secrets_encrypted = _encrypt_secrets(stored_secrets)
    row.updated_by = actor
    (
        db.query(IntegrationInstanceTestState)
        .filter(
            IntegrationInstanceTestState.connector == "gitlab",
            IntegrationInstanceTestState.instance_id == normalized_id,
        )
        .delete(synchronize_session=False)
    )
    db.add(
        IntegrationConfigEvent(
            connector="gitlab",
            action="delete_instance",
            actor=actor,
            changed_fields_json=json.dumps({"instance_id": normalized_id}, sort_keys=True),
            success=True,
            message="GitLab instance removed",
        )
    )
    db.commit()
    db.refresh(row)
    return row


def disable_connector(db: Session, key: str, *, actor: str) -> IntegrationConfig:
    if key not in CONNECTOR_SPECS:
        raise IntegrationConfigError("Unknown connector")
    row = db.query(IntegrationConfig).filter(IntegrationConfig.connector == key).first()
    if row is None:
        row = IntegrationConfig(connector=key, values_json="{}", secrets_encrypted=None)
        db.add(row)
    row.enabled = False
    row.updated_by = actor
    row.test_status = None
    row.test_message = None
    db.add(
        IntegrationConfigEvent(
            connector=key,
            action="disable",
            actor=actor,
            changed_fields_json="[]",
            success=True,
            message="Connector disabled; environment fallback is suppressed",
        )
    )
    db.commit()
    db.refresh(row)
    return row


def _http_ok(response: requests.Response, label: str) -> str:
    if 300 <= response.status_code < 400:
        raise IntegrationConfigError(
            f"{label} returned an unexpected redirect; configure its canonical API endpoint"
        )
    if response.status_code in {401, 403}:
        raise IntegrationConfigError(f"{label} rejected the configured credentials (HTTP {response.status_code})")
    if response.status_code >= 500:
        raise IntegrationConfigError(f"{label} is unavailable (HTTP {response.status_code})")
    if response.status_code >= 400:
        raise IntegrationConfigError(f"{label} request failed (HTTP {response.status_code})")
    return f"Connected · HTTP {response.status_code}"


def _connection_test_timeout() -> float:
    """Bound connection checks while allowing fast deterministic local tests."""
    raw = (os.getenv("INTEGRATION_TEST_TIMEOUT_SECONDS") or "10").strip()
    try:
        timeout = float(raw)
    except ValueError:
        timeout = 10.0
    if not math.isfinite(timeout):
        timeout = 10.0
    return min(max(timeout, 0.05), 60.0)


def _response_object(response: requests.Response, label: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise IntegrationConfigError(f"{label} returned a malformed response") from exc
    if not isinstance(payload, dict):
        raise IntegrationConfigError(f"{label} returned an unexpected response")
    return payload


def _test_gitlab_connection(
    api_url: str,
    token: str,
    label: str,
    *,
    timeout: float,
) -> str:
    """Verify the selected instance without probing an arbitrary project.

    A connection check answers whether the credential can authenticate and
    discover an accessible project. CI Lint and repository-content reads are
    distinct, project-scoped capabilities; using the first project in a list
    made a valid token look broken when its role or the project's policy
    rejected that POST request.
    """
    base_url = api_url.rstrip("/")
    headers = {"PRIVATE-TOKEN": token}
    version_response = requests.get(
        f"{base_url}/version",
        headers=headers,
        timeout=timeout,
        allow_redirects=False,
    )
    _http_ok(version_response, label)

    projects_response = requests.get(
        f"{base_url}/projects",
        headers=headers,
        params={"membership": "true", "simple": "true", "per_page": 1},
        timeout=timeout,
        allow_redirects=False,
    )
    _http_ok(projects_response, f"{label} project discovery")
    try:
        projects = projects_response.json()
    except ValueError as exc:
        raise IntegrationConfigError(f"{label} returned an invalid project response") from exc
    if not isinstance(projects, list) or not projects:
        raise IntegrationConfigError(
            f"{label} token has no accessible projects; check GitLab membership and project visibility"
        )

    project = projects[0] if isinstance(projects[0], dict) else None
    if not project or project.get("id") is None:
        raise IntegrationConfigError(f"{label} project response does not contain an id")
    return "Connected · authentication and project discovery verified"


def test_gitlab_instance_connection(
    runtime: RuntimeConnector,
    instance_id: str,
) -> str:
    """Test one explicitly selected GitLab instance and no others."""
    entry = resolve_gitlab_instance(runtime, instance_id)
    config_fingerprint = _gitlab_instance_config_fingerprint(entry)
    try:
        message = _test_gitlab_connection(
            entry["api_url"],
            entry["token"],
            f"GitLab {entry['host']}",
            timeout=_connection_test_timeout(),
        )
    except requests.Timeout as exc:
        raise _GitlabInstanceTestError(
            "Connection test timed out; check routing, proxy and endpoint availability",
            instance_id=entry["id"],
            config_fingerprint=config_fingerprint,
        ) from exc
    except requests.exceptions.SSLError as exc:
        raise _GitlabInstanceTestError(
            "TLS verification failed; install the issuing CA in the application trust store",
            instance_id=entry["id"],
            config_fingerprint=config_fingerprint,
        ) from exc
    except requests.RequestException as exc:
        raise _GitlabInstanceTestError(
            "Connection test failed: endpoint is unreachable; check DNS and routing",
            instance_id=entry["id"],
            config_fingerprint=config_fingerprint,
        ) from exc
    except (IntegrationConfigError, CredentialStoreUnavailable) as exc:
        raise _GitlabInstanceTestError(
            str(exc) or "Connection test failed",
            instance_id=entry["id"],
            config_fingerprint=config_fingerprint,
        ) from exc
    return _GitlabInstanceTestMessage(
        message,
        instance_id=entry["id"],
        config_fingerprint=config_fingerprint,
    )


def resolve_gitlab_instance(
    runtime: RuntimeConnector,
    instance_id: str,
) -> dict[str, Any]:
    """Resolve an explicit public id to exactly one private runtime instance."""
    if not runtime.enabled:
        raise IntegrationConfigError("GitLab connector is disabled; enable it before testing")
    normalized_id = instance_id.strip().lower()
    if not _GITLAB_INSTANCE_ID_RE.fullmatch(normalized_id):
        raise IntegrationConfigError("GitLab instance identifier is invalid")
    instances = _runtime_gitlab_instances(runtime)
    matches = [entry for entry in instances if entry["id"] == normalized_id]
    if len(matches) != 1:
        raise IntegrationConfigError("GitLab instance identifier does not match stored configuration")
    if not matches[0].get("enabled", True):
        raise IntegrationConfigError("GitLab instance is disabled; enable it before testing")
    if not str(matches[0].get("token") or "").strip():
        raise IntegrationConfigError("GitLab instance has no configured access token")
    return matches[0]


def test_connector_connection(runtime: RuntimeConnector) -> str:
    if not connector_is_configured(runtime):
        raise IntegrationConfigError("Connector is not fully configured")
    timeout = _connection_test_timeout()
    if runtime.key == "gitlab":
        instances = _runtime_gitlab_instances(runtime)
        if len(instances) != 1:
            raise IntegrationConfigError(
                "Select a GitLab instance; shared testing is disabled for multi-instance configuration"
            )
        return test_gitlab_instance_connection(runtime, instances[0]["id"])
    if runtime.key == "asoc":
        response = requests.get(
            f"{runtime.get('url').rstrip('/')}/ext/v1/products/",
            headers={"Authorization": f"Bearer {runtime.get('token')}"},
            params={"limit": 1, "offset": 0},
            timeout=timeout,
            allow_redirects=False,
        )
        message = _http_ok(response, "ASOC")
        payload = _response_object(response, "ASOC")
        if not isinstance(payload.get("items"), list):
            raise IntegrationConfigError("ASOC returned an unexpected products response")
        return message
    if runtime.key == "ptai":
        verify = runtime.get("verify_ssl", "true").lower() in {"1", "true", "yes", "on"}
        response = requests.get(
            f"{runtime.get('url').rstrip('/')}/api/auth/signin",
            headers={"Access-Token": runtime.get("token")},
            timeout=timeout,
            verify=verify,
            allow_redirects=False,
        )
        message = _http_ok(response, "PT AI")
        payload = _response_object(response, "PT AI")
        if not str(payload.get("accessToken") or "").strip():
            raise IntegrationConfigError("PT AI response does not contain an access token")
        return message
    if runtime.key == "mattermost":
        response = requests.post(
            runtime.get("webhook_url"),
            json={"text": "ScanOps integration check: Mattermost connection is healthy."},
            timeout=timeout,
            allow_redirects=False,
        )
        message = _http_ok(response, "Mattermost")
        if response.text.strip().lower() != "ok":
            raise IntegrationConfigError("Mattermost returned an unexpected webhook response")
        return message
    raise IntegrationConfigError("Unsupported connector")


def record_test_result(
    db: Session,
    key: str,
    *,
    actor: str,
    success: bool,
    message: str,
    instance_id: str | None = None,
) -> None:
    """Persist test metadata while leaving environment credentials external."""
    if instance_id is not None:
        instance_id = instance_id.strip().lower()
        if key != "gitlab" or not _GITLAB_INSTANCE_ID_RE.fullmatch(instance_id):
            raise IntegrationConfigError("Instance test result requires a valid GitLab instance identifier")
    now = datetime.now(timezone.utc)
    row_query = db.query(IntegrationConfig).filter(IntegrationConfig.connector == key)
    row = row_query.with_for_update().first() if instance_id is not None else row_query.first()
    if instance_id is not None:
        runtime = (
            RuntimeConnector(
                key="gitlab",
                values=_json_object(row.values_json),
                secrets=_decrypt_secrets(row.secrets_encrypted),
                enabled=bool(row.enabled),
                source="database",
                row=row,
            )
            if row is not None
            else _environment_connector("gitlab")
        )
        instance = resolve_gitlab_instance(runtime, instance_id)
        tested_instance_id = getattr(message, "instance_id", None)
        tested_fingerprint = getattr(message, "config_fingerprint", None)
        if tested_instance_id is not None and tested_instance_id != instance_id:
            raise IntegrationConfigError(
                "GitLab instance configuration changed during the connection test"
            )
        if (
            tested_fingerprint is not None
            and tested_fingerprint
            != _gitlab_instance_config_fingerprint(instance)
        ):
            raise IntegrationConfigError(
                "GitLab instance configuration changed during the connection test"
            )
    if row is not None and instance_id is None:
        row.tested_at = now
        row.test_status = "healthy" if success else "failed"
        row.test_message = message[:500]
    if instance_id is not None:
        table = IntegrationInstanceTestState.__table__
        dialect = db.get_bind().dialect.name
        values = {
            "connector": key,
            "instance_id": instance_id,
            "tested_at": now,
            "test_status": "healthy" if success else "failed",
            "test_message": message[:500],
            "last_success_at": now if success else None,
            "last_success_message": message[:500] if success else None,
        }
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert

            statement = insert(table).values(**values)
            update_values = {
                "tested_at": statement.excluded.tested_at,
                "test_status": statement.excluded.test_status,
                "test_message": statement.excluded.test_message,
                "last_success_at": (
                    statement.excluded.last_success_at
                    if success
                    else table.c.last_success_at
                ),
                "last_success_message": (
                    statement.excluded.last_success_message
                    if success
                    else table.c.last_success_message
                ),
            }
            db.execute(
                statement.on_conflict_do_update(
                    index_elements=[table.c.connector, table.c.instance_id],
                    set_=update_values,
                )
            )
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert

            statement = insert(table).values(**values)
            update_values = {
                "tested_at": statement.excluded.tested_at,
                "test_status": statement.excluded.test_status,
                "test_message": statement.excluded.test_message,
                "last_success_at": (
                    statement.excluded.last_success_at
                    if success
                    else table.c.last_success_at
                ),
                "last_success_message": (
                    statement.excluded.last_success_message
                    if success
                    else table.c.last_success_message
                ),
            }
            db.execute(
                statement.on_conflict_do_update(
                    index_elements=[table.c.connector, table.c.instance_id],
                    set_=update_values,
                )
            )
        else:
            state = (
                db.query(IntegrationInstanceTestState)
                .filter(
                    IntegrationInstanceTestState.connector == key,
                    IntegrationInstanceTestState.instance_id == instance_id,
                )
                .with_for_update()
                .first()
            )
            if state is None:
                state = IntegrationInstanceTestState(
                    connector=key,
                    instance_id=instance_id,
                    tested_at=now,
                    test_status="healthy" if success else "failed",
                )
                db.add(state)
            state.tested_at = now
            state.test_status = "healthy" if success else "failed"
            state.test_message = message[:500]
            if success:
                state.last_success_at = now
                state.last_success_message = message[:500]
    db.add(
        IntegrationConfigEvent(
            connector=key,
            action="test",
            actor=actor,
            changed_fields_json=(
                json.dumps({"instance_id": instance_id}, sort_keys=True)
                if instance_id is not None
                else "[]"
            ),
            success=success,
            message=message[:500],
        )
    )
    db.commit()
