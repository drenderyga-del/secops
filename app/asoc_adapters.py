"""Small, explicit adapters for report import; no remote response prose escapes."""
from __future__ import annotations
import json
import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID
import requests
from .integration_settings import (CredentialStoreUnavailable, _encrypt_secrets_with_material,
                                   _try_decrypt_secrets_with_material)


class AsocAdapterError(ValueError):
    def __init__(self, message, *, uncertain=False, status_code=422):
        super().__init__(message)
        self.uncertain = uncertain
        self.status_code = status_code


def destination_key_material() -> str:
    material = os.getenv("INTEGRATION_CREDENTIALS_KEY", "").strip()
    if not material or any(
        value and secrets.compare_digest(material.encode(), value.encode())
        for name in ("PIPE_BEARER_TOKEN", "UI_AUTH_PASSWORD", "SCANOPS_AGENT_API_TOKEN")
        if (value := os.getenv(name, ""))
    ):
        raise CredentialStoreUnavailable("A dedicated integration credential key is required")
    return material


def encrypt_destination_token(token: str) -> str:
    from .credential_separation import application_secrets, matches_any
    if matches_any(token, application_secrets()):
        raise AsocAdapterError("Provider credential must differ from application credentials")
    return _encrypt_secrets_with_material({"token": token}, destination_key_material())


def decrypt_destination_token(payload: str) -> str:
    values = _try_decrypt_secrets_with_material(payload, destination_key_material())
    if values is None:
        raise CredentialStoreUnavailable("Destination credential cannot be decrypted")
    return values.get("token", "")


PROVIDERS = {
    "vampy": {
        "name": "Hexway ASOC / Vampy", "formats": ["sarif"],
        "base_url_example": "https://asoc.example.com/api",
        "settings": ["repository", "branch", "vcs_url"],
        "scope": "SARIF import; existing ASOC product sync and PT AI orchestration use the legacy connector",
    },
    "defectdojo": {
        "name": "DefectDojo", "formats": ["sarif"],
        "base_url_example": "https://defectdojo.example.com/api/v2",
        "settings": ["engagement_id", "test_title"],
        "scope": "SARIF reimport into an existing engagement; no automatic closure of findings",
    },
    "dependencytrack": {
        "name": "OWASP Dependency-Track", "formats": ["cyclonedx"],
        "base_url_example": "https://dependencytrack.example.com/api/v1",
        "settings": ["project_id"],
        "scope": "CycloneDX JSON SBOM ingestion into an existing project; not a SARIF findings importer",
    },
}


def validate_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise AsocAdapterError("Invalid API base URL") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
        or parsed.password is not None or parsed.query or parsed.fragment or port == 0
        or parsed.netloc.endswith(":") or "\\" in value
        or any(c.isspace() or ord(c) < 32 for c in value)
        or any(part in {".", ".."} for part in parsed.path.split("/"))
        or "%" in parsed.netloc or "%" in parsed.path):
        raise AsocAdapterError("API base URL must be HTTPS without credentials, query or fragment")
    from .asoc_description_safety import _contains_credential_host, _contains_credential_path
    if _contains_credential_host(parsed.hostname) or _contains_credential_path(parsed.path):
        raise AsocAdapterError("API base URL must not contain credential-shaped host or path segments")
    return value.rstrip("/")


def validate_settings(provider: str, settings: dict) -> dict[str, str]:
    if provider not in PROVIDERS:
        raise AsocAdapterError("Unsupported ASOC provider")
    allowed = PROVIDERS[provider]["settings"]
    if any(key not in allowed for key in settings):
        raise AsocAdapterError("Unknown provider setting")
    result = {key: str(value).strip() for key, value in settings.items()}
    if any(len(value) > 2048 or any(ord(c) < 32 for c in value) for value in result.values()):
        raise AsocAdapterError("Provider setting is too long or contains control characters")
    if provider == "defectdojo":
        if not result.get("engagement_id", "").isdigit() or int(result["engagement_id"]) < 1:
            raise AsocAdapterError("DefectDojo requires an existing positive engagement_id")
        if not result.get("test_title") or len(result["test_title"]) > 200:
            raise AsocAdapterError("DefectDojo requires a stable scanner-specific test_title (max 200 characters)")
    elif provider == "dependencytrack":
        try:
            result["project_id"] = str(UUID(result.get("project_id", "")))
        except ValueError:
            raise AsocAdapterError("Dependency-Track requires an existing project UUID") from None
    else:
        if not result.get("repository") or not result.get("branch"):
            raise AsocAdapterError("Vampy requires repository slug and branch")
        validate_base_url(result.get("vcs_url", ""))
    return result


@dataclass(frozen=True)
class Receipt:
    state: str
    reference: str | None = None


class AsocAdapter:
    def __init__(self, destination):
        self.provider = destination.provider
        self.url = validate_base_url(destination.api_base_url)
        self.settings = validate_settings(self.provider, json.loads(destination.settings_json))
        self.token = decrypt_destination_token(destination.secrets_encrypted)
        if not destination.enabled or not self.token:
            raise AsocAdapterError("Destination is disabled or has no credential")

    def _request(self, method, path, **kwargs):
        header = {"vampy": ("Authorization", f"Bearer {self.token}"),
                  "defectdojo": ("Authorization", f"Token {self.token}"),
                  "dependencytrack": ("X-Api-Key", self.token)}[self.provider]
        try:
            response = requests.request(method, self.url + path, headers={header[0]: header[1]},
                                        timeout=(5, 30), verify=True, allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise AsocAdapterError("ASOC request failed; check TLS, routing and credentials", uncertain=True, status_code=502) from None
        if not 200 <= response.status_code < 300:
            # Upstream errors may echo credentials or private configuration.
            raise AsocAdapterError(f"ASOC returned HTTP {response.status_code}",
                                   uncertain=method != "GET" and response.status_code >= 500, status_code=502)
        return response

    def check(self):
        path, params = {
            "vampy": ("/ext/v1/products/", {"limit": 1}),
            "defectdojo": ("/engagements/", {"id": self.settings.get("engagement_id"), "limit": 1}),
            "dependencytrack": ("/project/" + self.settings.get("project_id", ""), None),
        }[self.provider]
        response = self._request("GET", path, params=params)
        try:
            payload = response.json()
        except ValueError:
            raise AsocAdapterError("ASOC returned an unexpected connection-check response") from None
        if not isinstance(payload, dict):
            raise AsocAdapterError("ASOC returned an unexpected connection-check response")
        if self.provider == "vampy" and not isinstance(payload.get("items"), list):
            raise AsocAdapterError("Vampy products response is missing items")
        if self.provider == "defectdojo":
            items = payload.get("results")
            if not isinstance(items, list) or not any(str(x.get("id")) == self.settings["engagement_id"] for x in items if isinstance(x, dict)):
                raise AsocAdapterError("Configured DefectDojo engagement is not accessible")
        if self.provider == "dependencytrack" and str(payload.get("uuid")) != self.settings["project_id"]:
            raise AsocAdapterError("Configured Dependency-Track project is not accessible")
        return Receipt("connected")

    def send(self, report_format: str, report: dict):
        if report_format not in PROVIDERS[self.provider]["formats"]:
            raise AsocAdapterError("Destination does not support this report format")
        if report_format == "sarif":
            if report.get("version") != "2.1.0" or not isinstance(report.get("runs"), list) or not report["runs"]:
                raise AsocAdapterError("Expected a SARIF 2.1.0 report with at least one run")
            # One scanner per report keeps DefectDojo test matching deterministic.
            tools = set()
            for run in report["runs"]:
                tool = run.get("tool") if isinstance(run, dict) else None
                driver = tool.get("driver") if isinstance(tool, dict) else None
                name = driver.get("name") if isinstance(driver, dict) else None
                if not isinstance(name, str) or not name.strip():
                    raise AsocAdapterError("SARIF report must identify exactly one scanner")
                tools.add(name)
            if len(tools) != 1:
                raise AsocAdapterError("SARIF report must identify exactly one scanner")
        elif report.get("bomFormat") != "CycloneDX" or not isinstance(report.get("components"), list):
            raise AsocAdapterError("Expected a CycloneDX JSON BOM with components")
        data = json.dumps(report).encode()
        if len(data) > 5 * 1024 * 1024:
            raise AsocAdapterError("Report exceeds the 5 MiB limit")
        if self.provider == "defectdojo":
            response = self._request("POST", "/reimport-scan/",
                data={"engagement": self.settings["engagement_id"], "test_title": self.settings["test_title"],
                      "scan_type": "SARIF", "close_old_findings": "false", "do_not_reactivate": "true",
                      "auto_create_context": "false", "active": "true", "verified": "false"},
                files={"file": ("report.sarif", data, "application/json")})
        elif self.provider == "dependencytrack":
            response = self._request("POST", "/bom", data={"project": self.settings["project_id"]},
                                     files={"bom": ("bom.json", data, "application/json")})
        else:
            response = self._request("POST", "/scan_uploads/",
                data={"scannerType": "SARIF", "repository": self.settings["repository"],
                      "repositoryBranch": self.settings["branch"], "vcsURL": self.settings["vcs_url"]},
                files={"scannerResultFile": ("report.sarif", data, "application/json")})
        reference = None
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if self.provider == "defectdojo":
            value = payload.get("test") if isinstance(payload, dict) else None
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                reference = str(value)
            else:
                raise AsocAdapterError("DefectDojo did not confirm a test identifier", uncertain=True, status_code=502)
        elif self.provider == "dependencytrack":
            try:
                reference = str(UUID(str(payload.get("token"))))
            except (ValueError, AttributeError):
                raise AsocAdapterError("Dependency-Track did not return a BOM processing token", uncertain=True, status_code=502) from None
        # Accepted is transport acceptance, never a claim that findings were processed.
        return Receipt("accepted", reference)
