"""Constant-time separation checks across active credential sources.

No secret values, credential payloads or exception prose leave this module.
"""
from __future__ import annotations
import json
import os
import secrets
from collections.abc import Mapping


APPLICATION_SECRET_NAMES = ("PIPE_BEARER_TOKEN", "UI_AUTH_PASSWORD",
                            "SCANOPS_AGENT_API_TOKEN", "INTEGRATION_CREDENTIALS_KEY")


def matches_any(candidate: str, values) -> bool:
    return bool(candidate) and any(
        isinstance(value, str) and bool(value)
        and secrets.compare_digest(candidate.encode(), value.encode())
        for value in values
    )


def secret_values(value):
    """Extract structured instance credentials without counting disabled entries."""
    if isinstance(value, Mapping):
        if value.get("enabled") is False:
            return
        for item in value.values():
            yield from secret_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from secret_values(item)
    elif isinstance(value, str) and value:
        yield value
        if value.lstrip().startswith(("[", "{")):
            try:
                yield from secret_values(json.loads(value))
            except (ValueError, RecursionError):
                # Malformed instance configuration is rejected by its connector
                # validator; runtime store inspection separately fails closed.
                return


def application_secrets(*, include_agent=True):
    return [os.getenv(name, "") for name in APPLICATION_SECRET_NAMES
            if include_agent or name != "SCANOPS_AGENT_API_TOKEN"]


def environment_integration_secrets():
    from .integration_settings import CONNECTOR_SPECS
    for spec in CONNECTOR_SPECS.values():
        for field in spec["fields"]:
            if field.get("secret"):
                for name in field.get("env", ()):
                    yield from secret_values(os.getenv(name, ""))
    yield os.getenv("ALERT_SMTP_PASSWORD", "")


def environment_agent_conflict() -> bool:
    candidate = os.getenv("SCANOPS_AGENT_API_TOKEN", "")
    return matches_any(candidate, [*application_secrets(include_agent=False),
                                  *environment_integration_secrets()])


def ensure_no_agent_reuse(values) -> None:
    if matches_any(os.getenv("SCANOPS_AGENT_API_TOKEN", ""), secret_values(values)):
        raise ValueError("Integration credentials must differ from the agent credential")


def stored_agent_conflict(db) -> bool:
    """Inspect authoritative enabled credentials; caller fails closed on errors."""
    from .integration_settings import (CONNECTOR_SPECS, CredentialStoreUnavailable,
                                       _runtime_gitlab_instances, load_runtime_connector)
    from .models import AsocDestination
    from .asoc_adapters import decrypt_destination_token
    candidate = os.getenv("SCANOPS_AGENT_API_TOKEN", "")
    if not candidate:
        return False
    for key in CONNECTOR_SPECS:
        runtime = load_runtime_connector(key, db=db)
        if not runtime.enabled:
            continue
        if runtime.error:
            raise CredentialStoreUnavailable("Credential separation cannot be verified")
        if key == "gitlab":
            values = [entry["token"] for entry in _runtime_gitlab_instances(runtime) if entry["enabled"]]
        else:
            values = list(secret_values(runtime.secrets))
        if matches_any(candidate, values):
            return True
    for row in db.query(AsocDestination).filter(AsocDestination.enabled.is_(True)):
        if matches_any(candidate, [decrypt_destination_token(row.secrets_encrypted)]):
            return True
    return False
