import json

import pytest

from app.iac_asoc import product_slug_for_path, repository_slug_for_path
from app.iac_scanner import detect_iac_markers, parse_checkov_sarif


def test_iac_finder_detects_common_markers():
    tree = [
        {"type": "blob", "path": "main.tf"},
        {"type": "blob", "path": "charts/api/Chart.yaml"},
        {"type": "blob", "path": "deploy/kustomization.yaml"},
        {"type": "blob", "path": "Dockerfile"},
        {"type": "blob", "path": "roles/web/tasks/main.yml"},
        {"type": "blob", "path": "cloudformation/template.yaml"},
    ]

    markers = detect_iac_markers(tree)

    assert "terraform" in markers
    assert "helm" in markers
    assert "kubernetes" in markers
    assert "dockerfile" in markers
    assert "ansible" in markers
    assert "cloudformation" in markers


def test_iac_finder_does_not_treat_gitlab_ci_as_iac_by_itself():
    assert detect_iac_markers([{"type": "blob", "path": ".gitlab-ci.yml"}]) == []


def test_parse_checkov_sarif_maps_result(tmp_path):
    sarif = {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "rules": [
                            {
                                "id": "CKV_TEST_1",
                                "shortDescription": {"text": "Bad thing"},
                                "fullDescription": {"text": "Bad thing description"},
                                "properties": {"security-severity": "8.0"},
                            }
                        ]
                    }
                },
                "results": [
                    {
                        "ruleId": "CKV_TEST_1",
                        "message": {"text": "Evidence"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": "main.tf"},
                                    "region": {"startLine": 12},
                                }
                            }
                        ],
                    }
                ],
            }
        ]
    }
    path = tmp_path / "report.sarif"
    path.write_text(json.dumps(sarif), encoding="utf-8")

    findings = parse_checkov_sarif(path)

    assert findings[0]["severity"] == "high"
    assert findings[0]["rule_id"] == "CKV_TEST_1"
    assert findings[0]["file_path"] == "main.tf"
    assert findings[0]["line"] == 12


def test_parse_checkov_sarif_rejects_missing_schema(tmp_path):
    path = tmp_path / "invalid.sarif"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid SARIF schema"):
        parse_checkov_sarif(path)


def test_asoc_slug_helpers_use_host_prefix():
    assert (
        repository_slug_for_path("https://gitlab.primary.example.com", "group/project")
        == "primary/group/project"
    )
    assert (
        product_slug_for_path("https://gitlab.enterprise.example.com", "group/project", "group")
        == "enterprise/group"
    )


def test_asoc_slug_prefixes_have_no_organization_defaults(monkeypatch):
    from app.iac_asoc import host_slug_prefix
    monkeypatch.delenv('ASOC_GITLAB_HOST_PREFIXES', raising=False)
    assert host_slug_prefix('https://gitlab.primary.example.com') == 'gitlab-primary-example-com'
    assert host_slug_prefix('https://gitlab.secondary.example.com') == 'gitlab-secondary-example-com'
    monkeypatch.setenv('ASOC_GITLAB_HOST_PREFIXES', '{"gitlab.primary.example.com":"custom"}')
    assert host_slug_prefix('https://gitlab.primary.example.com') == 'custom'
