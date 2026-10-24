from pathlib import Path

from scripts.preflight_internal import parse_env_file, validate


ROOT = Path(__file__).resolve().parents[1]


def _valid_values():
    return {
        "SECPIPE_IMAGE": "registry.internal/secops:2026.07.20-1",
        "POSTGRES_PASSWORD": "postgres-password-unique-123",
        "PIPE_BEARER_TOKEN": "pipeline-bearer-token-unique-123",
        "INTEGRATION_CREDENTIALS_KEY": "credential-key-unique-456789",
        "UI_AUTH_USERNAME": "scanops",
        "UI_AUTH_PASSWORD": "ui-password-unique-789",
        "GITLAB_API_URL": "https://gitlab.internal/api/v4",
        "GITLAB_PAT": "gitlab-token",
        "MATTERMOST_WEBHOOK_URL": "https://mattermost.internal/hooks/scanops",
        "ASOC_VERIFY_SSL": "true",
        "PTAI_VERIFY_SSL": "true",
    }


def test_internal_preflight_accepts_release_configuration():
    errors, warnings = validate(_valid_values())

    assert errors == []
    assert warnings == []


def test_internal_preflight_rejects_placeholders_reused_secrets_and_insecure_tls():
    values = _valid_values()
    values.update(
        {
            "SECPIPE_IMAGE": "registry.example.com/secops:latest",
            "PIPE_BEARER_TOKEN": "change-me",
            "INTEGRATION_CREDENTIALS_KEY": "change-me",
            "ASOC_VERIFY_SSL": "false",
        }
    )

    errors, _warnings = validate(values)

    assert any("SECPIPE_IMAGE" in error for error in errors)
    assert any("PIPE_BEARER_TOKEN" in error for error in errors)
    assert any("different" in error for error in errors)
    assert any("TLS verification" in error for error in errors)


def test_internal_preflight_parses_exported_and_quoted_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text('export UI_AUTH_USERNAME="scanops"\nPIPE_BEARER_TOKEN=token\n', encoding="utf-8")

    assert parse_env_file(path) == {
        "UI_AUTH_USERNAME": "scanops",
        "PIPE_BEARER_TOKEN": "token",
    }


def test_internal_preflight_warns_when_alert_channel_requires_ui_verification():
    values = _valid_values()
    values.pop("MATTERMOST_WEBHOOK_URL")

    errors, warnings = validate(values)

    assert errors == []
    assert any("alert channel" in warning for warning in warnings)


def test_internal_preflight_accepts_complete_smtp_and_rejects_partial_channel():
    values = _valid_values()
    values.pop("MATTERMOST_WEBHOOK_URL")
    values.update(
        {
            "ALERT_SMTP_HOST": "smtp.internal",
            "ALERT_EMAIL_TO": "scanops@example.internal",
        }
    )

    errors, warnings = validate(values)

    assert errors == []
    assert warnings == []

    values.pop("ALERT_EMAIL_TO")
    errors, _warnings = validate(values)
    assert any("configured together" in error for error in errors)


def test_internal_preflight_rejects_invalid_smtp_runtime_values():
    values = _valid_values()
    values.pop("MATTERMOST_WEBHOOK_URL")
    values.update(
        {
            "ALERT_SMTP_HOST": "smtp.internal",
            "ALERT_EMAIL_TO": "scanops@example.internal",
            "ALERT_SMTP_PORT": "not-a-number",
            "ALERT_SMTP_USE_TLS": "sometimes",
            "ALERT_SMTP_USER": "scanops",
            "ALERT_SMTP_PASSWORD": "",
        }
    )

    errors, _warnings = validate(values)

    assert any("ALERT_SMTP_PORT" in error for error in errors)
    assert any("ALERT_SMTP_USE_TLS" in error for error in errors)
    assert any("ALERT_SMTP_USER" in error for error in errors)

    values["ALERT_SMTP_PORT"] = "70000"
    values["ALERT_SMTP_USE_TLS"] = "true"
    values["ALERT_SMTP_PASSWORD"] = "smtp-password"
    errors, _warnings = validate(values)
    assert any("ALERT_SMTP_PORT" in error for error in errors)


def test_internal_preflight_rejects_insecure_mattermost_webhook():
    values = _valid_values()
    values["MATTERMOST_WEBHOOK_URL"] = "http://mattermost.internal/hooks/scanops"

    errors, _warnings = validate(values)

    assert any("MATTERMOST_WEBHOOK_URL" in error for error in errors)


def test_env_example_documents_complete_alert_runtime_contract():
    values = parse_env_file(ROOT / ".env.example")

    expected = {
        "MATTERMOST_WEBHOOK_URL",
        "MATTERMOST_ALERT_ICON_URL",
        "ALERT_SMTP_HOST",
        "ALERT_SMTP_PORT",
        "ALERT_SMTP_USER",
        "ALERT_SMTP_PASSWORD",
        "ALERT_SMTP_USE_TLS",
        "ALERT_EMAIL_FROM",
        "ALERT_EMAIL_TO",
        "SECPIPE_JOB_LONG_ALERT_ENABLED",
        "SECPIPE_JOB_LONG_ALERT_MINUTES",
        "SECPIPE_JOB_ALERT_INTERVAL_SECONDS",
        "SECPIPE_JOB_ALERT_RESEND_MINUTES",
        "SECPIPE_JOB_LONG_ALERT_NAMES",
        "SECPIPE_JOB_LONG_ALERT_EXCLUDE_NAMES",
        "SECPIPE_JOB_LONG_ALERT_REGEX",
        "PTAI_QUEUE_ALERT_ENABLED",
        "PTAI_QUEUE_ALERT_THRESHOLD",
        "PTAI_QUEUE_ALERT_INTERVAL_SECONDS",
        "PTAI_QUEUE_ALERT_RESEND_MINUTES",
        "PTAI_QUEUE_ALERT_STAGES",
        "PTAI_QUEUE_PROJECT_LIMIT",
        "PTAI_QUEUE_BRANCH_LIMIT",
        "PTAI_SCAN_STATUS_ALERT_ENABLED",
        "PTAI_SCAN_STATUS_POLL_INTERVAL_SECONDS",
        "PTAI_SCAN_HUNG_ALERT_MINUTES",
        "PTAI_SCAN_STATUS_BATCH_SIZE",
        "AUDIT_DIGEST_ENABLED",
        "AUDIT_DIGEST_INTERVAL_DAYS",
        "AUDIT_SECURITY_JOB_NAMES",
        "HEALTH_EXPECTED_WORKERS",
        "LOG_LEVEL",
    }

    assert expected <= values.keys()
