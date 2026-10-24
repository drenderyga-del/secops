"""ASOC repository resolution for IaC uploads: slug first, then GitLab URL.

The URL fallback mirrors asoc/asoc_repository_audit.py identity rules: an
ASOC card is "the same repository" when its url or externalLink.url (VCS
binding) points at the GitLab project, regardless of the slug.
"""
from __future__ import annotations

import pytest

import app.iac_asoc as iac_asoc
from app.iac_asoc import (
    AsocClient,
    normalize_repo_url,
    repository_identity_urls,
    _repository_preference,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("HEXWAY_TOKEN", "test-token")
    monkeypatch.setenv("HEXWAY_URL", "https://asoc.example/api")
    return AsocClient()


def _fake_get(monkeypatch, handler):
    def fake_get(url, **kwargs):
        return handler(url, kwargs.get("params") or {})

    monkeypatch.setattr(iac_asoc.requests, "get", fake_get)


def test_normalize_repo_url_strips_git_suffix_slash_and_case():
    assert (
        normalize_repo_url("https://GitLab.enterprise.example.com/Sandbox/App.git")
        == "https://gitlab.enterprise.example.com/sandbox/app"
    )
    assert normalize_repo_url("https://gitlab.enterprise.example.com/sandbox/app/") == (
        "https://gitlab.enterprise.example.com/sandbox/app"
    )
    assert normalize_repo_url(None) == ""
    assert normalize_repo_url(
        "https://gitlab.enterprise.example.com/sandbox/app.git?source=asoc#readme"
    ) == "https://gitlab.enterprise.example.com/sandbox/app"
    assert normalize_repo_url("https://user:secret@gitlab.example/team/app") == ""
    assert normalize_repo_url("javascript:alert(1)") == ""


def test_repository_identity_urls_reads_url_and_external_link():
    repo = {
        "url": "https://gitlab.enterprise.example.com/sandbox/app.git",
        "externalLink": {"url": "https://gitlab.enterprise.example.com/sandbox/app"},
    }
    assert repository_identity_urls(repo) == ["https://gitlab.enterprise.example.com/sandbox/app"]
    assert repository_identity_urls({"externalLink": None, "url": ""}) == []


def test_find_repository_prefers_exact_slug_match(client, monkeypatch):
    def handler(url, params):
        assert params.get("search") == "enterprise/sandbox/app"
        return FakeResponse({"items": [{"id": "r1", "slug": "enterprise/sandbox/app"}]})

    _fake_get(monkeypatch, handler)
    found = client.find_repository("enterprise/sandbox/app", project_url="https://gitlab.enterprise.example.com/sandbox/app")
    assert found["id"] == "r1"
    assert found["_matched_by"] == "slug"


def test_find_repository_falls_back_to_url_match(client, monkeypatch):
    project_url = "https://gitlab.enterprise.example.com/sandbox/app"

    def handler(url, params):
        if params.get("search") == "enterprise/sandbox/app":
            return FakeResponse({"items": []})
        if params.get("search") == "app":
            return FakeResponse(
                {
                    "items": [
                        {"id": "legacy", "slug": "ci/app", "externalLink": {"url": project_url + ".git"}},
                        {"id": "other", "slug": "ci/app-other", "url": "https://gitlab.enterprise.example.com/other/app"},
                    ]
                }
            )
        return FakeResponse({"items": []})

    _fake_get(monkeypatch, handler)
    found = client.find_repository("enterprise/sandbox/app", project_url=project_url)
    assert found["id"] == "legacy"
    assert found["_matched_by"] == "url"


def test_find_repository_url_match_fetches_details_when_list_payload_has_no_urls(client, monkeypatch):
    project_url = "https://gitlab.enterprise.example.com/sandbox/app"

    def handler(url, params):
        if params.get("search") == "enterprise/sandbox/app":
            return FakeResponse({"items": []})
        if params.get("search") == "app":
            return FakeResponse({"items": [{"id": "bare", "slug": "ci/app"}]})
        if url.endswith("/repositories/bare/"):
            return FakeResponse({"id": "bare", "externalLink": {"url": project_url}})
        return FakeResponse({"items": []})

    _fake_get(monkeypatch, handler)
    found = client.find_repository("enterprise/sandbox/app", project_url=project_url)
    assert found["id"] == "bare"
    assert found["_matched_by"] == "url"


def test_find_repository_returns_none_when_nothing_matches(client, monkeypatch):
    _fake_get(monkeypatch, lambda url, params: FakeResponse({"items": []}))
    assert client.find_repository("enterprise/sandbox/app", project_url="https://gitlab.enterprise.example.com/sandbox/app") is None


def test_list_product_repositories_uses_product_filter_and_detail_url_fallback(client, monkeypatch):
    calls = []

    def handler(url, params):
        calls.append((url, params))
        if url.endswith("/ext/v1/repositories/"):
            return FakeResponse(
                {
                    "items": [
                        {"id": "with-url", "url": "https://gitlab.example/team/api"},
                        {"id": "needs-detail"},
                    ],
                    "hasNext": False,
                }
            )
        if url.endswith("/repositories/needs-detail/"):
            return FakeResponse(
                {
                    "id": "needs-detail",
                    "externalLink": {"url": "https://gitlab.example/team/web"},
                }
            )
        raise AssertionError(url)

    _fake_get(monkeypatch, handler)

    repositories = client.list_product_repositories("product-uuid")

    assert calls[0][1]["product"] == "product-uuid"
    assert repository_identity_urls(repositories[0]) == ["https://gitlab.example/team/api"]
    assert repository_identity_urls(repositories[1]) == ["https://gitlab.example/team/web"]


def test_list_products_rejects_has_next_at_the_safety_limit(client, monkeypatch):
    _fake_get(
        monkeypatch,
        lambda url, params: FakeResponse(
            {"items": [{"id": "one"}, {"id": "two"}], "hasNext": True}
        ),
    )

    with pytest.raises(RuntimeError, match="incomplete snapshot"):
        client.list_products(page_size=2, max_items=2)


def test_list_products_rejects_a_page_that_overshoots_the_safety_limit(client, monkeypatch):
    def handler(url, params):
        if params["offset"] == 0:
            return FakeResponse(
                {"items": [{"id": "one"}, {"id": "two"}], "hasNext": True}
            )
        return FakeResponse(
            {"items": [{"id": "three"}, {"id": "four"}], "hasNext": False}
        )

    _fake_get(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="exceeded the safety limit"):
        client.list_products(page_size=2, max_items=3)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"items": None, "hasNext": False},
        {"items": []},
        {"items": [{"id": "one"}], "hasNext": "false"},
        {"items": [None], "hasNext": False},
        {"items": [{"id": ""}], "hasNext": False},
    ],
)
def test_list_products_rejects_malformed_snapshots(client, monkeypatch, payload):
    _fake_get(monkeypatch, lambda url, params: FakeResponse(payload))

    with pytest.raises(RuntimeError, match="invalid"):
        client.list_products()


def test_list_products_accepts_an_explicit_empty_page_when_no_cards_exist(client, monkeypatch):
    _fake_get(
        monkeypatch,
        lambda url, params: FakeResponse({"items": [], "hasNext": False}),
    )

    assert client.list_products() == []


def test_list_products_does_not_include_upstream_error_body(client, monkeypatch):
    def handler(url, params):
        response = FakeResponse({}, status_code=502)
        response.text = "secret upstream payload"
        return response

    _fake_get(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="HTTP 502") as error:
        client.list_products()
    assert "secret upstream payload" not in str(error.value)


def test_repository_preference_ranks_vcs_linked_then_canonical_then_recent():
    legacy = {"slug": "ci/app", "created": "2024-01-01T00:00:00Z"}
    canonical = {"slug": "enterprise/sandbox/app", "created": "2023-01-01T00:00:00Z"}
    vcs_linked = {
        "slug": "ci/app-old",
        "externalLink": {"url": "https://gitlab.enterprise.example.com/sandbox/app"},
        "created": "2022-01-01T00:00:00Z",
    }
    ranked = sorted([legacy, canonical, vcs_linked], key=_repository_preference, reverse=True)
    assert [r["slug"] for r in ranked] == ["ci/app-old", "enterprise/sandbox/app", "ci/app"]

    fresh = {"slug": "ci/app-new", "lastScan": "2026-01-01T00:00:00Z"}
    stale = {"slug": "ci/app-stale", "lastScan": "2024-01-01T00:00:00Z"}
    assert max([stale, fresh], key=_repository_preference) is fresh
