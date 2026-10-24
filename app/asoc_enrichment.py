"""KEV/EPSS enrichment of Vampy (ASOC) issues.

Hundreds of CRITICAL CVEs sit in ASOC; only a handful are exploited in the
wild. This module pulls two public threat feeds and annotates matching open
issues directly in Vampy, so analysts can triage by real-world exploitability
without leaving ASOC:

- CISA KEV (Known Exploited Vulnerabilities) — catalogue of CVEs with
  confirmed in-the-wild exploitation;
- FIRST EPSS — probability of exploitation in the next 30 days; we annotate
  CVEs with a score above EPSS_MIN_SCORE.

Flow (daily, in pipeline_worker):

1. Load both feeds. URLs are configurable; a path or file:// URL works for
   air-gapped setups with a mirrored copy on disk.
2. For interesting CVEs, find DETECTED issues via POST /scan_issues/filter/
   (cveIDs accepts a batch; we chunk to keep payloads sane).
3. Fetch each candidate issue's detail to learn its exact cveID, then post a
   comment. Optionally (ASOC_ENRICH_RAISE_SEVERITY=true) bulk-raise KEV
   issues to CRITICAL.
4. Track annotated (issue_id, kind) pairs in asoc_enriched_issues so reruns
   never duplicate comments.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import requests

from .integration_settings import runtime_setting

logger = logging.getLogger(__name__)

DEFAULT_KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
DEFAULT_EPSS_URL = "https://epss.cyentia.com/epss_scores-current.csv.gz"

FILTER_CHUNK_SIZE = 100
FILTER_PAGE_LIMIT = 200


def _enabled() -> bool:
    return os.getenv("ASOC_ENRICH_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _epss_min_score() -> float:
    try:
        return float(os.getenv("EPSS_MIN_SCORE", "0.5"))
    except ValueError:
        return 0.5


@dataclass
class CveAnnotation:
    cve: str
    kev: bool = False
    kev_date_added: str | None = None
    kev_ransomware: bool = False
    epss_score: float | None = None
    epss_percentile: float | None = None

    def comment_body(self) -> str:
        parts: list[str] = []
        if self.kev:
            line = f"⚠️ **{self.cve} is listed in CISA KEV** (confirmed exploitation in the wild"
            if self.kev_date_added:
                line += f", added {self.kev_date_added}"
            line += ")."
            if self.kev_ransomware:
                line += " Used in ransomware campaigns."
            parts.append(line)
        if self.epss_score is not None:
            line = f"📈 EPSS for {self.cve}: **{self.epss_score:.2f}**"
            if self.epss_percentile is not None:
                line += f" (percentile {self.epss_percentile:.0%})"
            line += " — high probability of exploitation within the next 30 days."
            parts.append(line)
        parts.append("_Automated ScanOps annotation (KEV/EPSS feeds)._")
        return "\n\n".join(parts)

    @property
    def kinds(self) -> list[str]:
        kinds = []
        if self.kev:
            kinds.append("kev")
        if self.epss_score is not None:
            kinds.append("epss")
        return kinds


# ── Feed loading ─────────────────────────────────────────────────────


def _read_source(url: str, timeout: int = 120) -> bytes:
    """Fetch a feed from http(s), file:// or a plain filesystem path."""
    if url.startswith("file://"):
        return Path(url[len("file://"):]).read_bytes()
    if url.startswith("/") or not url.split(":", 1)[0] in ("http", "https"):
        return Path(url).read_bytes()
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.content


def load_kev(url: str | None = None) -> dict[str, dict]:
    """Return {cve: {dateAdded, ransomware}} from the CISA KEV catalogue."""
    raw = _read_source(url or os.getenv("KEV_FEED_URL") or DEFAULT_KEV_URL)
    data = json.loads(raw)
    result: dict[str, dict] = {}
    for vuln in data.get("vulnerabilities", []):
        cve = (vuln.get("cveID") or "").strip().upper()
        if not cve:
            continue
        result[cve] = {
            "dateAdded": vuln.get("dateAdded"),
            "ransomware": (vuln.get("knownRansomwareCampaignUse") or "").strip().lower() == "known",
        }
    return result


def load_epss(url: str | None = None, min_score: float | None = None) -> dict[str, tuple[float, float]]:
    """Return {cve: (score, percentile)} for CVEs at or above the threshold."""
    threshold = _epss_min_score() if min_score is None else min_score
    raw = _read_source(url or os.getenv("EPSS_FEED_URL") or DEFAULT_EPSS_URL)
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    result: dict[str, tuple[float, float]] = {}
    reader = csv.reader(io.StringIO(raw.decode("utf-8", errors="replace")))
    for row in reader:
        if not row or row[0].startswith("#") or row[0] == "cve":
            continue
        try:
            cve, score, percentile = row[0].strip().upper(), float(row[1]), float(row[2])
        except (ValueError, IndexError):
            continue
        if score >= threshold:
            result[cve] = (score, percentile)
    return result


def build_annotations(
    kev: dict[str, dict], epss: dict[str, tuple[float, float]]
) -> dict[str, CveAnnotation]:
    annotations: dict[str, CveAnnotation] = {}
    for cve, meta in kev.items():
        annotations[cve] = CveAnnotation(
            cve=cve, kev=True, kev_date_added=meta.get("dateAdded"),
            kev_ransomware=bool(meta.get("ransomware")),
        )
    for cve, (score, percentile) in epss.items():
        ann = annotations.get(cve)
        if ann is None:
            ann = CveAnnotation(cve=cve)
            annotations[cve] = ann
        ann.epss_score = score
        ann.epss_percentile = percentile
    return annotations


def chunked(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# ── Vampy client (issues API) ────────────────────────────────────────


class VampyIssuesClient:
    """Minimal client for the Vampy scan-issues API used by enrichment."""

    def __init__(self) -> None:
        self.url = runtime_setting("HEXWAY_URL", "https://asoc.example.com/api").rstrip("/")
        self.token = runtime_setting("HEXWAY_TOKEN", "")
        self.space_id = runtime_setting("HEXWAY_SPACE_ID", "")
        self.timeout = int(os.getenv("HEXWAY_TIMEOUT", "30"))
        self.verify_ssl = os.getenv("ASOC_VERIFY_SSL", "true").lower() in {"1", "true", "yes"}

    def enabled(self) -> bool:
        return bool(self.url and self.token)

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def filter_issue_ids(self, cve_ids: list[str]) -> list[str]:
        """Return ids of DETECTED issues matching any of the given CVEs."""
        ids: list[str] = []
        offset = 0
        while True:
            payload: dict = {
                "cveIDs": cve_ids,
                "findingState": ["DETECTED"],
            }
            if self.space_id:
                payload["spaceID"] = self.space_id
            response = requests.post(
                f"{self.url}/scan_issues/filter/",
                json=payload,
                params={"limit": FILTER_PAGE_LIMIT, "offset": offset},
                headers=self._headers,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            body = response.json()
            items = body.get("items") or []
            ids.extend(str(item.get("id")) for item in items if item.get("id"))
            if not body.get("hasNext"):
                break
            offset += len(items) or FILTER_PAGE_LIMIT
        return ids

    def get_issue_cve(self, issue_id: str) -> str | None:
        response = requests.get(
            f"{self.url}/scan_issues/{issue_id}/",
            headers=self._headers,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        response.raise_for_status()
        cve = (response.json().get("cveID") or "").strip().upper()
        return cve or None

    def post_comment(self, issue_id: str, body: str) -> None:
        response = requests.post(
            f"{self.url}/scan_issues/{issue_id}/comments/",
            json={"body": body},
            headers=self._headers,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        response.raise_for_status()

    def raise_severity(self, issue_ids: list[str], comment: str) -> None:
        response = requests.post(
            f"{self.url}/scan_issues/update/",
            json={
                "issues": issue_ids,
                "severity": "CRITICAL",
                "statusChangeComment": comment,
            },
            headers=self._headers,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        response.raise_for_status()


# ── Main entry ───────────────────────────────────────────────────────


@dataclass
class EnrichmentStats:
    issues_seen: int = 0
    commented: int = 0
    severity_raised: int = 0
    errors: int = 0
    skipped_already: int = 0


def run_enrichment_once(client: VampyIssuesClient | None = None) -> EnrichmentStats | None:
    """One enrichment pass. Returns stats, or None when disabled/unconfigured."""
    if not _enabled():
        return None
    client = client or VampyIssuesClient()
    if not client.enabled():
        logger.warning("ASOC enrichment enabled but HEXWAY_URL/HEXWAY_TOKEN are not configured")
        return None

    from .models import AsocEnrichedIssue, SessionLocal

    try:
        kev = load_kev()
    except Exception:
        logger.exception("Failed to load KEV feed")
        kev = {}
    try:
        epss = load_epss()
    except Exception:
        logger.exception("Failed to load EPSS feed")
        epss = {}
    annotations = build_annotations(kev, epss)
    if not annotations:
        logger.warning("ASOC enrichment: both feeds empty, nothing to do")
        return None
    logger.info(
        "ASOC enrichment: %s KEV CVEs, %s high-EPSS CVEs (threshold %.2f)",
        len(kev), len(epss), _epss_min_score(),
    )

    stats = EnrichmentStats()
    raise_severity = os.getenv("ASOC_ENRICH_RAISE_SEVERITY", "false").lower() in {"1", "true", "yes"}
    kev_to_raise: list[str] = []

    all_cves = sorted(annotations)
    for chunk in chunked(all_cves, FILTER_CHUNK_SIZE):
        try:
            issue_ids = client.filter_issue_ids(chunk)
        except Exception:
            logger.exception("Issue filter failed for chunk of %s CVEs", len(chunk))
            stats.errors += 1
            continue
        for issue_id in issue_ids:
            stats.issues_seen += 1
            try:
                cve = client.get_issue_cve(issue_id)
            except Exception:
                logger.exception("Failed to fetch issue %s", issue_id)
                stats.errors += 1
                continue
            ann = annotations.get(cve or "")
            if ann is None:
                continue
            with SessionLocal() as db:
                done_kinds = {
                    row.kind
                    for row in db.query(AsocEnrichedIssue)
                    .filter(AsocEnrichedIssue.issue_id == issue_id)
                    .all()
                }
                new_kinds = [k for k in ann.kinds if k not in done_kinds]
                if not new_kinds:
                    stats.skipped_already += 1
                    continue
                try:
                    client.post_comment(issue_id, ann.comment_body())
                except Exception:
                    logger.exception("Failed to comment issue %s", issue_id)
                    stats.errors += 1
                    continue
                for kind in new_kinds:
                    db.add(AsocEnrichedIssue(issue_id=issue_id, kind=kind, cve=ann.cve))
                db.commit()
                stats.commented += 1
                if raise_severity and ann.kev and "kev" in new_kinds:
                    kev_to_raise.append(issue_id)

    if kev_to_raise:
        try:
            client.raise_severity(
                kev_to_raise,
                "Severity raised automatically: the CVE is listed in CISA KEV and exploited in the wild.",
            )
            stats.severity_raised = len(kev_to_raise)
        except Exception:
            logger.exception("Bulk severity raise failed for %s issues", len(kev_to_raise))
            stats.errors += 1

    logger.info(
        "ASOC enrichment done: seen=%s commented=%s raised=%s already=%s errors=%s",
        stats.issues_seen, stats.commented, stats.severity_raised,
        stats.skipped_already, stats.errors,
    )
    return stats
