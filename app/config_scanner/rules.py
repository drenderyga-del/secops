from __future__ import annotations

import fnmatch
import re
from typing import Any

SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
SENSITIVE_KEY_RE = re.compile(r"(token|secret|password|passwd|api[_-]?key|private[_-]?key)", re.IGNORECASE)
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SUSPICIOUS_SCRIPT_RE = re.compile(r"(curl|wget).*(\||>)\s*(bash|sh)", re.IGNORECASE)

GITLAB_TOP_LEVEL_KEYWORDS = {
    "stages",
    "variables",
    "include",
    "workflow",
    "default",
    "image",
    "services",
    "before_script",
    "after_script",
    "cache",
    "pages",
}


def finding(
    severity: str,
    code: str,
    title: str,
    description: str,
    recommendation: str,
    evidence: str,
) -> dict[str, str]:
    return {
        "severity": severity,
        "code": code,
        "title": title,
        "description": description,
        "recommendation": recommendation,
        "evidence": evidence,
    }


def branch_is_protected(default_branch: str | None, protected_branches: list[dict[str, Any]]) -> bool:
    if not default_branch:
        return False
    for branch_rule in protected_branches:
        pattern = str(branch_rule.get("name", ""))
        if pattern == default_branch or fnmatch.fnmatch(default_branch, pattern):
            return True
    return False


def is_sensitive_key(key: str) -> bool:
    return bool(SENSITIVE_KEY_RE.search(key or ""))


def analyze_project_settings(
    project: dict[str, Any],
    protected_branches: list[dict[str, Any]] | None,
    approvals: dict[str, Any] | None,
    push_rule: dict[str, Any] | None,
    variables: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    visibility = project.get("visibility")

    if visibility != "private":
        findings.append(
            finding(
                "high",
                "PROJECT_VISIBILITY",
                "Project visibility is not private",
                "Public or internal visibility increases accidental data exposure risk.",
                "Set project visibility to private unless a stronger business requirement exists.",
                f"visibility={visibility}",
            )
        )

    default_branch = project.get("default_branch")
    if default_branch and isinstance(protected_branches, list) and not branch_is_protected(default_branch, protected_branches):
        findings.append(
            finding(
                "high",
                "DEFAULT_BRANCH_UNPROTECTED",
                "Default branch is not protected",
                "Unprotected default branch can allow direct pushes and bypass review controls.",
                "Protect the default branch and restrict push permissions.",
                f"default_branch={default_branch}",
            )
        )

    if approvals is not None:
        approvals_before_merge = approvals.get("approvals_before_merge", 0)
        if isinstance(approvals_before_merge, int) and approvals_before_merge < 1:
            findings.append(
                finding(
                    "medium",
                    "MR_APPROVALS_DISABLED",
                    "Merge requests do not require approvals",
                    "No approval requirement lowers review quality for changes.",
                    "Set at least one required approval for merge requests.",
                    f"approvals_before_merge={approvals_before_merge}",
                )
            )

    push_rule_unavailable = isinstance(push_rule, dict) and bool(push_rule.get("_scanner_unavailable"))
    if push_rule is None and not push_rule_unavailable:
        findings.append(
            finding(
                "low",
                "PUSH_RULE_MISSING",
                "Push rule is not configured",
                "Push rules can enforce basic commit hygiene and block weak pushes.",
                "Enable project push rules for commit messages and branch restrictions.",
                "push_rule=None",
            )
        )

    if isinstance(variables, list):
        for variable in variables:
            key = str(variable.get("key", ""))
            if not is_sensitive_key(key):
                continue
            if not bool(variable.get("masked", False)):
                findings.append(
                    finding(
                        "high",
                        "UNMASKED_SENSITIVE_VARIABLE",
                        "Sensitive CI/CD variable is not masked",
                        "Unmasked sensitive variables can leak in job logs.",
                        "Mark sensitive variables as masked.",
                        f"variable={key}",
                    )
                )
            if not bool(variable.get("protected", False)):
                findings.append(
                    finding(
                        "high",
                        "UNPROTECTED_SENSITIVE_VARIABLE",
                        "Sensitive CI/CD variable is not protected",
                        "Unprotected variables may be exposed in untrusted branches.",
                        "Mark sensitive variables as protected and use protected branches/tags.",
                        f"variable={key}",
                    )
                )

    if project.get("only_allow_merge_if_pipeline_succeeds") is False:
        findings.append(
            finding(
                "medium",
                "PIPELINE_GATING_DISABLED",
                "Merging without successful pipeline is allowed",
                "Changes can be merged without pipeline validation.",
                "Enable 'Pipelines must succeed'.",
                "only_allow_merge_if_pipeline_succeeds=false",
            )
        )

    return findings


def _extract_script_lines(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def analyze_ci_configuration(
    ci_documents: list[dict[str, Any]],
    include_records: list[dict[str, Any]],
    lint_result: dict[str, Any] | None,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []

    if lint_result is not None:
        if lint_result.get("valid") is False:
            errors = lint_result.get("errors") or []
            evidence = "; ".join(str(item) for item in errors[:3])
            findings.append(
                finding(
                    "high",
                    "CI_LINT_INVALID",
                    "GitLab CI lint validation failed",
                    "Pipeline configuration contains syntax or semantic errors.",
                    "Fix CI errors shown by GitLab lint API.",
                    evidence or "lint.valid=false",
                )
            )

    for include in include_records:
        include_type = include.get("type")
        status = include.get("status")

        if include_type == "remote":
            findings.append(
                finding(
                    "high",
                    "REMOTE_INCLUDE_USED",
                    "Remote include is used",
                    "Remote include introduces external supply chain dependency and was excluded by policy.",
                    "Replace remote include with local/project/template include.",
                    str(include),
                )
            )
            continue

        if include_type == "artifact":
            findings.append(
                finding(
                    "medium",
                    "ARTIFACT_INCLUDE_USED",
                    "Artifact include requires runtime context",
                    "Artifact include cannot be fully validated statically and may hide configuration drift.",
                    "Validate artifact includes with strict pipeline controls and signed artifacts.",
                    str(include),
                )
            )

        if include_type == "project":
            ref = include.get("ref")
            if not ref:
                findings.append(
                    finding(
                        "high",
                        "PROJECT_INCLUDE_WITHOUT_REF",
                        "Cross-project include is not pinned",
                        "Unpinned include can unexpectedly change pipeline behavior.",
                        "Pin include ref to a commit SHA or immutable release tag.",
                        str(include),
                    )
                )
            elif not SHA1_RE.match(str(ref)):
                findings.append(
                    finding(
                        "medium",
                        "PROJECT_INCLUDE_FLOATING_REF",
                        "Cross-project include uses floating ref",
                        "Branch-based refs can drift over time and silently alter CI behavior.",
                        "Use immutable refs for include dependencies.",
                        str(include),
                    )
                )

        if status and status.startswith("error"):
            findings.append(
                finding(
                    "high",
                    "INCLUDE_RESOLUTION_FAILED",
                    "Failed to resolve included CI file",
                    "Part of CI configuration was not loaded, reducing scan completeness.",
                    "Fix include path/project/ref and ensure token has read permissions.",
                    str(include),
                )
            )

    for doc in ci_documents:
        parsed = doc.get("parsed")
        doc_name = doc.get("name", "ci.yml")
        if not isinstance(parsed, dict):
            continue

        variables = parsed.get("variables")
        if isinstance(variables, dict):
            for key, value in variables.items():
                if is_sensitive_key(str(key)) and isinstance(value, str) and "$" not in value:
                    findings.append(
                        finding(
                            "high",
                            "PLAINTEXT_SECRET_IN_CI",
                            "Potential plaintext secret in CI variables",
                            "Sensitive-looking key has a literal value in YAML.",
                            "Move secret values to protected masked CI/CD variables in GitLab settings.",
                            f"file={doc_name}; variable={key}",
                        )
                    )

        for job_name, job_def in parsed.items():
            if job_name in GITLAB_TOP_LEVEL_KEYWORDS:
                continue
            if not isinstance(job_def, dict):
                continue

            image = job_def.get("image")
            if isinstance(image, str):
                if image.endswith(":latest") or ":" not in image:
                    findings.append(
                        finding(
                            "low",
                            "UNPINNED_JOB_IMAGE",
                            "CI job uses unpinned image tag",
                            "Floating image tags can introduce non-deterministic pipeline behavior.",
                            "Pin image tags to immutable digest or fixed version.",
                            f"job={job_name}; image={image}",
                        )
                    )

            for key in ("before_script", "script", "after_script"):
                for line in _extract_script_lines(job_def.get(key)):
                    if SUSPICIOUS_SCRIPT_RE.search(line):
                        findings.append(
                            finding(
                                "medium",
                                "SUSPICIOUS_SCRIPT_PATTERN",
                                "Potentially unsafe pipe-to-shell pattern",
                                "Direct execution of downloaded scripts increases supply chain risk.",
                                "Replace with verified artifacts/signatures or pinned trusted scripts.",
                                f"job={job_name}; line={line}",
                            )
                        )

    return findings


def summarize_findings(findings: list[dict[str, str]]) -> dict[str, int]:
    summary = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for item in findings:
        severity = str(item.get("severity", "info")).lower()
        summary[severity] = summary.get(severity, 0) + 1
    return summary


def sort_findings(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(
        findings,
        key=lambda item: (
            -SEVERITY_ORDER.get(str(item.get("severity", "")).lower(), 0),
            str(item.get("code", "")),
        ),
    )
