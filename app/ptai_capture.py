"""Capture ASOC identifiers from a finished GitLab `ptai_scan` job.

The async `ptai_scan` job is fire-and-forget: it POSTs to ASOC `/ext/v1/scan/`
and prints a single line to its trace, e.g.

    [INFO] Vampy scan accepted: scan_id=<uuid>, task_id=<uuid>, pipeline_id=<uuid>, repository=<slug>

Only fields with a value are printed, so each key is parsed independently. The
parsed ids let the status poller follow the real scan inside ASOC, which the
GitLab job duration never reflects (the job ends right after the POST).
"""
from __future__ import annotations

import os
import re
from typing import Dict, Set

# label -> dict key. Labels match what controls/ptai.yml prints.
_FIELDS = {
    "scan_id": "scan_id",
    "repository_id": "repository_id",
    "task_id": "task_id",
    "pipeline_id": "pipeline_id",
    "repository": "repository",
}

_DEFAULT_PTAI_JOB_NAMES = "ptai_scan"


def ptai_job_names() -> Set[str]:
    """Job names that trigger an async ASOC scan (lowercased)."""
    raw = (os.getenv("PTAI_SCAN_JOB_NAMES") or _DEFAULT_PTAI_JOB_NAMES).strip()
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def is_ptai_job(job_name: str | None) -> bool:
    return bool(job_name) and job_name.strip().lower() in ptai_job_names()


def parse_scan_ids(trace_text: str | None) -> Dict[str, str]:
    """Extract ASOC scan/repository/task/pipeline identifiers from a job trace.

    Returns only the keys actually present. Values are unquoted tokens (UUIDs or
    a repository slug), terminated by whitespace or a comma.
    """
    out: Dict[str, str] = {}
    if not trace_text:
        return out
    for label, key in _FIELDS.items():
        match = re.search(rf"\b{label}=([^\s,]+)", trace_text)
        if match:
            out[key] = match.group(1).strip()
    return out
