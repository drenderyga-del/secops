#!/usr/bin/env python3
"""Fail-closed deployment preflight for the internal ScanOps contour.

The command validates configuration without printing secret values. It is
intentionally dependency-free so the internal repository can run it before
building or starting any container.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from urllib.parse import urlparse


REQUIRED = (
    "SECPIPE_IMAGE",
    "POSTGRES_PASSWORD",
    "PIPE_BEARER_TOKEN",
    "INTEGRATION_CREDENTIALS_KEY",
    "UI_AUTH_USERNAME",
    "UI_AUTH_PASSWORD",
)
SECRET_MIN_LENGTH = {
    "POSTGRES_PASSWORD": 16,
    "PIPE_BEARER_TOKEN": 24,
    "INTEGRATION_CREDENTIALS_KEY": 24,
    "UI_AUTH_PASSWORD": 16,
}
PLACEHOLDER_FRAGMENTS = (
    "change-me",
    "changeme",
    "example.com",
    "example/",
    "super-secret-token",
    "gitlab-pat-or-empty",
)


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"line {number} is not KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(fragment in lowered for fragment in PLACEHOLDER_FRAGMENTS)


def _valid_url(value: str, *, https_required: bool = False) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in ({"https"} if https_required else {"http", "https"}):
        return False
    return bool(parsed.netloc and not parsed.username and not parsed.password)


def validate(values: dict[str, str], *, allow_insecure_tls: bool = False) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    for name in REQUIRED:
        value = values.get(name, "").strip()
        if not value:
            errors.append(f"{name} is required")
        elif _is_placeholder(value):
            errors.append(f"{name} still contains an example/placeholder value")

    for name, minimum in SECRET_MIN_LENGTH.items():
        value = values.get(name, "")
        if value and len(value) < minimum:
            errors.append(f"{name} must be at least {minimum} characters")

    secret_values = {
        name: values.get(name, "")
        for name in ("POSTGRES_PASSWORD", "PIPE_BEARER_TOKEN", "INTEGRATION_CREDENTIALS_KEY", "UI_AUTH_PASSWORD")
        if values.get(name, "")
    }
    seen: dict[str, str] = {}
    for name, value in secret_values.items():
        if value in seen:
            errors.append(f"{name} must be different from {seen[value]}")
        else:
            seen[value] = name

    image = values.get("SECPIPE_IMAGE", "").strip()
    if image and (":" not in image.rsplit("/", 1)[-1] or image.endswith(":latest")):
        errors.append("SECPIPE_IMAGE must use an immutable, explicit tag (not latest)")

    for name in ("GITLAB_API_URL", "SECPIPE_PUBLIC_URL"):
        value = values.get(name, "").strip()
        if value and not _valid_url(value):
            errors.append(f"{name} must be an http(s) URL without embedded credentials")

    for name in ("HEXWAY_URL", "PTAI_URL"):
        value = values.get(name, "").strip()
        if value and not _valid_url(value, https_required=True):
            errors.append(f"{name} must use https and must not embed credentials")

    insecure = [
        name
        for name in ("ASOC_VERIFY_SSL", "PTAI_VERIFY_SSL")
        if values.get(name, "true").strip().lower() in {"0", "false", "no", "off"}
    ]
    if insecure and not allow_insecure_tls:
        errors.append(f"TLS verification is disabled for: {', '.join(insecure)}")
    elif insecure:
        warnings.append(f"explicit insecure-TLS exception accepted for: {', '.join(insecure)}")

    instances_raw = values.get("GITLAB_INSTANCES", "").strip()
    configured_gitlab = bool(values.get("GITLAB_PAT", "").strip())
    if instances_raw and instances_raw != "[]":
        try:
            instances = json.loads(instances_raw)
        except json.JSONDecodeError:
            errors.append("GITLAB_INSTANCES must be valid JSON")
        else:
            if not isinstance(instances, list) or not instances:
                errors.append("GITLAB_INSTANCES must be a non-empty JSON array when set")
            else:
                configured_gitlab = True
                for index, instance in enumerate(instances):
                    if not isinstance(instance, dict):
                        errors.append(f"GITLAB_INSTANCES[{index}] must be an object")
                        continue
                    if not _valid_url(str(instance.get("api_url") or "")):
                        errors.append(f"GITLAB_INSTANCES[{index}].api_url is invalid")
                    if not str(instance.get("token") or "").strip():
                        errors.append(f"GITLAB_INSTANCES[{index}].token is required")
    if not configured_gitlab:
        warnings.append("GitLab is not configured in env; verify a UI-managed connection before release")

    mattermost = values.get("MATTERMOST_WEBHOOK_URL", "").strip()
    smtp_host = values.get("ALERT_SMTP_HOST", "").strip()
    email_to = values.get("ALERT_EMAIL_TO", "").strip()
    smtp_user = values.get("ALERT_SMTP_USER", "").strip()
    smtp_password = values.get("ALERT_SMTP_PASSWORD", "").strip()
    if mattermost and not _valid_url(mattermost, https_required=True):
        errors.append(
            "MATTERMOST_WEBHOOK_URL must use https and must not embed credentials"
        )
    if bool(smtp_host) != bool(email_to):
        errors.append(
            "ALERT_SMTP_HOST and ALERT_EMAIL_TO must be configured together"
        )
    if smtp_host and email_to:
        raw_port = values.get("ALERT_SMTP_PORT", "587").strip()
        try:
            smtp_port = int(raw_port)
        except ValueError:
            errors.append("ALERT_SMTP_PORT must be an integer from 1 to 65535")
        else:
            if not 1 <= smtp_port <= 65535:
                errors.append("ALERT_SMTP_PORT must be an integer from 1 to 65535")

        raw_tls = values.get("ALERT_SMTP_USE_TLS", "true").strip().lower()
        if raw_tls not in {"1", "0", "true", "false", "yes", "no"}:
            errors.append(
                "ALERT_SMTP_USE_TLS must be one of: true, false, 1, 0, yes, no"
            )
        if bool(smtp_user) != bool(smtp_password):
            errors.append(
                "ALERT_SMTP_USER and ALERT_SMTP_PASSWORD must be configured together"
            )
    if not mattermost and not (smtp_host and email_to):
        warnings.append(
            "No env-backed alert channel is configured; verify a UI-managed "
            "Mattermost connection after release"
        )

    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate internal ScanOps deployment configuration")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--allow-insecure-tls", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args()

    if not args.env_file.is_file():
        result = {"status": "failed", "errors": [f"env file not found: {args.env_file}"], "warnings": []}
        print(json.dumps(result) if args.json_output else f"FAIL: {result['errors'][0]}")
        return 1

    try:
        values = parse_env_file(args.env_file)
    except (OSError, ValueError) as exc:
        result = {"status": "failed", "errors": [str(exc)], "warnings": []}
        print(json.dumps(result) if args.json_output else f"FAIL: {exc}")
        return 1

    # Explicit process environment wins, matching Docker Compose behavior.
    values.update(os.environ)
    errors, warnings = validate(values, allow_insecure_tls=args.allow_insecure_tls)

    mode = stat.S_IMODE(args.env_file.stat().st_mode)
    if mode & 0o077:
        warnings.append(f"{args.env_file} is readable by group/others; prefer chmod 600")

    status_value = "failed" if errors else "ready"
    if args.json_output:
        print(json.dumps({"status": status_value, "errors": errors, "warnings": warnings}, ensure_ascii=False))
    else:
        for message in errors:
            print(f"FAIL: {message}")
        for message in warnings:
            print(f"WARN: {message}")
        print("Internal deployment preflight: READY" if not errors else "Internal deployment preflight: FAILED")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
