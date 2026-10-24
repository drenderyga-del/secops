"""Provider-neutral, opt-in API boundary for external diagnostic agents."""
from __future__ import annotations
import hashlib
import json
import os
import re
import secrets
from fastapi import Header, HTTPException, Request

# Narrow token recognizers complement the explicit operator sharing boundary.
# Pattern matching is not a substitute for reviewing a context before sharing it.
_SECRET = re.compile(
    r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----"
    r"|\b(?:glpat-|ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{12,}"
    r"|\b(?:authorization|private[-_]?token|access[-_]?token|api[-_]?key|password|secret)\s*[:=]\s*\S+"
    r"|https?://[^\s/]+@"
    r"|https?://[^\s]+[?#][^\s]+",
    re.IGNORECASE,
)


def agent_credential_conflict() -> bool:
    from .credential_separation import environment_agent_conflict
    return environment_agent_conflict()


def agent_token_ok(header: str) -> bool:
    expected = os.getenv("SCANOPS_AGENT_API_TOKEN", "")
    if len(expected) < 32 or not header.startswith("Bearer ") or agent_credential_conflict():
        return False
    supplied = header.removeprefix("Bearer ")
    return secrets.compare_digest(supplied.encode(), expected.encode())


def require_agent(authorization: str, *, db) -> str:
    if not agent_token_ok(authorization):
        raise HTTPException(status_code=401, detail="Dedicated agent authentication required")
    from .credential_separation import stored_agent_conflict
    try:
        conflict = stored_agent_conflict(db)
    except Exception:
        # Unavailable/decryption-failed stores cannot establish credential
        # separation. Do not leak the upstream error or authenticate anyway.
        raise HTTPException(status_code=503, detail="Credential separation could not be verified") from None
    if conflict:
        raise HTTPException(status_code=503, detail="Agent credential conflicts with an active integration credential")
    return "external-agent"


def require_operator(request: Request) -> str:
    from .main import _basic_auth_ok, _csrf_ok, _ui_auth_configured
    if agent_credential_conflict():
        raise HTTPException(status_code=503, detail="Authentication credentials must be distinct")
    if not _ui_auth_configured():
        raise HTTPException(status_code=503, detail="Operator authentication is not configured")
    header = request.headers.get("authorization", "")
    if not header.startswith("Basic ") or not _basic_auth_ok(header):
        raise HTTPException(status_code=401, detail="Operator authentication required")
    if not _csrf_ok(request):
        raise HTTPException(status_code=403, detail="Origin/Referer must match the operator UI")
    return os.environ["UI_AUTH_USERNAME"]


def checked_text(value: str) -> str:
    known = [os.getenv(name, "") for name in (
        "SCANOPS_AGENT_API_TOKEN", "PIPE_BEARER_TOKEN", "INTEGRATION_CREDENTIALS_KEY",
        "GITLAB_PAT", "HEXWAY_TOKEN", "PTAI_TOKEN", "UI_AUTH_PASSWORD",
    )]
    if _SECRET.search(value) or any(len(item) >= 8 and item in value for item in known):
        raise HTTPException(status_code=422, detail="Remove credential-shaped content before sharing")
    return value


def context_version(job, context: str) -> str:
    # Include source facts so a changed job invalidates a diagnosis/approval.
    facts = [job.id, job.repository_id, job.job_name, int(job.job_status), job.ref,
             str(job.finished_at), context]
    return hashlib.sha256(json.dumps(facts, ensure_ascii=False).encode()).hexdigest()
