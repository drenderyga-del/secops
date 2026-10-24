#!/usr/bin/env python3
"""Safely migrate UI-managed credentials off the legacy bearer key."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.integration_settings import (
    CredentialStoreUnavailable,
    IntegrationConfigError,
    migrate_integration_credentials,
)
from app.models import SessionLocal


def _database_target(raw_url: str) -> str:
    if not raw_url:
        raise IntegrationConfigError(
            "DATABASE_URL must be explicitly set; the migration refuses the application default"
        )
    parsed = urlparse(raw_url)
    if not parsed.scheme.startswith("postgresql") or not parsed.hostname:
        raise IntegrationConfigError(
            "DATABASE_URL must identify an explicit PostgreSQL database"
        )
    if parsed.query or parsed.fragment:
        raise IntegrationConfigError(
            "DATABASE_URL query parameters and fragments are forbidden during "
            "credential migration because they can override the confirmed target"
        )
    database = unquote(parsed.path.lstrip("/"))
    if not database:
        raise IntegrationConfigError("DATABASE_URL must include a database name")
    return f"{parsed.hostname}:{parsed.port or 5432}/{database}"


def _write_reset_manifest(
    path: Path,
    *,
    database_target: str,
    entries: dict[str, str],
) -> None:
    payload = {
        "version": 1,
        "database_target": database_target,
        "entries": entries,
    }
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
    except FileExistsError as exc:
        raise IntegrationConfigError(
            "Reset manifest already exists; refusing to overwrite it"
        ) from exc
    except OSError as exc:
        raise IntegrationConfigError(
            "Reset manifest cannot be created at the requested path"
        ) from exc


def _load_reset_manifest(
    path: Path,
    *,
    database_target: str,
) -> dict[str, str]:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        raw_payload = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IntegrationConfigError("Reset manifest cannot be read") from exc
    if mode != 0o600:
        raise IntegrationConfigError(
            f"Reset manifest must have mode 600, got {mode:o}"
        )
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise IntegrationConfigError("Reset manifest is not valid JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("database_target") != database_target
        or not isinstance(payload.get("entries"), dict)
    ):
        raise IntegrationConfigError(
            "Reset manifest version or database target does not match"
        )
    entries = payload["entries"]
    if not all(
        isinstance(connector, str)
        and isinstance(fingerprint, str)
        and len(fingerprint) == 64
        for connector, fingerprint in entries.items()
    ):
        raise IntegrationConfigError("Reset manifest entries are invalid")
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or migrate integration_configs from PIPE_BEARER_TOKEN "
            "encryption to INTEGRATION_CREDENTIALS_KEY"
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit recoverable credential re-encryption; default is dry-run",
    )
    parser.add_argument(
        "--reset-irrecoverable",
        action="store_true",
        help="with --apply and --reset-manifest, clear exactly the approved rows",
    )
    parser.add_argument(
        "--write-reset-manifest",
        type=Path,
        help="during dry-run, create a mode-600 manifest for the unreadable rows",
    )
    parser.add_argument(
        "--reset-manifest",
        type=Path,
        help="manifest produced by a prior dry-run for this exact database",
    )
    parser.add_argument(
        "--confirm-target",
        help="required with --apply; exact redacted host:port/database from dry-run",
    )
    parser.add_argument("--actor", default="credential-migration")
    args = parser.parse_args()

    if args.reset_irrecoverable and not args.apply:
        parser.error("--reset-irrecoverable requires --apply")
    if args.reset_irrecoverable != bool(args.reset_manifest):
        parser.error(
            "--reset-irrecoverable and --reset-manifest must be supplied together"
        )
    if args.write_reset_manifest and args.apply:
        parser.error("--write-reset-manifest is only valid during dry-run")
    if args.apply and not args.confirm_target:
        parser.error("--apply requires --confirm-target")

    try:
        database_target = _database_target(os.getenv("DATABASE_URL", "").strip())
        if args.apply and args.confirm_target != database_target:
            raise IntegrationConfigError(
                "Confirmed database target does not match DATABASE_URL"
            )
        reset_manifest = (
            _load_reset_manifest(
                args.reset_manifest,
                database_target=database_target,
            )
            if args.reset_manifest
            else None
        )
        with SessionLocal() as db:
            result = migrate_integration_credentials(
                db,
                dry_run=not args.apply,
                reset_irrecoverable=args.reset_irrecoverable,
                reset_manifest=reset_manifest,
                actor=args.actor,
            )
        if args.write_reset_manifest:
            _write_reset_manifest(
                args.write_reset_manifest,
                database_target=database_target,
                entries=result.reset_manifest_entries(),
            )
    except (CredentialStoreUnavailable, IntegrationConfigError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    payload = result.as_dict()
    payload["database_target"] = database_target
    payload["mode"] = "apply" if args.apply else "dry-run"
    if result.remaining_irrecoverable:
        payload["status"] = "blocked"
    elif args.apply:
        payload["status"] = "complete"
    else:
        payload["status"] = "ready_to_apply"
    print(json.dumps(payload, sort_keys=True))
    return 2 if result.remaining_irrecoverable else 0


if __name__ == "__main__":
    raise SystemExit(main())
