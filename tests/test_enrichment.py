"""Unit tests for KEV/EPSS feed parsing and annotation building."""
import gzip
import json

import pytest

from app.asoc_enrichment import (
    CveAnnotation,
    build_annotations,
    chunked,
    load_epss,
    load_kev,
)


def test_load_kev_from_file(tmp_path):
    feed = {
        "vulnerabilities": [
            {"cveID": "CVE-2024-1234", "dateAdded": "2024-05-01", "knownRansomwareCampaignUse": "Known"},
            {"cveID": "cve-2023-9999", "dateAdded": "2023-01-01", "knownRansomwareCampaignUse": "Unknown"},
            {"cveID": "", "dateAdded": "2023-01-01"},
        ]
    }
    path = tmp_path / "kev.json"
    path.write_text(json.dumps(feed))

    kev = load_kev(str(path))

    assert kev["CVE-2024-1234"]["ransomware"] is True
    assert kev["CVE-2023-9999"]["ransomware"] is False  # uppercased key
    assert len(kev) == 2


def test_load_epss_gzip_with_threshold(tmp_path):
    csv_body = "#model_version:v2023.03.01\ncve,epss,percentile\nCVE-2024-1111,0.92,0.999\nCVE-2024-2222,0.10,0.50\n"
    path = tmp_path / "epss.csv.gz"
    path.write_bytes(gzip.compress(csv_body.encode()))

    epss = load_epss(str(path), min_score=0.5)

    assert "CVE-2024-1111" in epss
    assert "CVE-2024-2222" not in epss
    score, percentile = epss["CVE-2024-1111"]
    assert score == pytest.approx(0.92)
    assert percentile == pytest.approx(0.999)


def test_build_annotations_merges_kev_and_epss():
    kev = {"CVE-1": {"dateAdded": "2024-01-01", "ransomware": False}}
    epss = {"CVE-1": (0.8, 0.99), "CVE-2": (0.6, 0.9)}

    annotations = build_annotations(kev, epss)

    both = annotations["CVE-1"]
    assert both.kev and both.epss_score == 0.8
    assert sorted(both.kinds) == ["epss", "kev"]
    only_epss = annotations["CVE-2"]
    assert not only_epss.kev and only_epss.kinds == ["epss"]


def test_comment_body_mentions_feeds():
    ann = CveAnnotation(
        cve="CVE-2024-1234", kev=True, kev_date_added="2024-05-01",
        kev_ransomware=True, epss_score=0.93, epss_percentile=0.998,
    )
    body = ann.comment_body()
    assert "CISA KEV" in body
    assert "ransomware" in body
    assert "0.93" in body
    assert "CVE-2024-1234" in body


def test_chunked():
    assert chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert chunked([], 10) == []
