from urllib.parse import quote

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.asoc_description_safety import (
    safe_asoc_public_base,
    safe_description_links,
    sanitize_description,
    sanitize_existing_asoc_descriptions,
)
from app.models import AsocProduct, Base


@pytest.fixture(autouse=True)
def configured_gitlab_hosts(monkeypatch):
    monkeypatch.setenv("ASOC_PRODUCT_REFERENCE_HOSTS", "product.example.com")
    monkeypatch.setattr("app.gitlab_url.known_gitlab_hosts", lambda: {"gitlab.example"})


def test_description_projection_drops_credentials_prose_query_and_fragment():
    description = """Internal-only note with password=do-not-store
https://user:password@gitlab.example/group/repo
https://gitlab.example/group/repo?access_token=do-not-store#private-fragment
https://product.example/products/BI_77
ScanOps-Scope: EXTERNAL"""
    description = description.replace("product.example", "product.example.com")

    links = safe_description_links(description)
    sanitized = sanitize_description(description) or ""

    expected_urls = [
        "https://gitlab.example/group/repo",
        "https://product.example/products/BI_77",
    ]
    expected_urls = [
        url.replace("product.example", "product.example.com") for url in expected_urls
    ]
    assert [link["url"] for link in links] == expected_urls
    assert links[0]["kind"] == "gitlab"
    assert "ScanOps-Scope: external" in sanitized
    assert "password=do-not-store" not in sanitized
    assert "do-not-store" not in sanitized
    assert "private-fragment" not in sanitized


def test_description_projection_drops_pat_webhook_and_opaque_secret_paths():
    description = "\n".join((
        "https://gitlab.example.com/group/project/-/settings/access_tokens/glpat-abcdefghijklmnopqrst",
        "https://gitlab.example.com/group/project/hooks/0123456789abcdef0123456789abcdef",
        "https://gitlab.example.com/group/project/0123456789abcdef0123456789abcdef",
        "https://webhook.site/0123456789abcdef0123456789abcdef",
    ))

    assert safe_description_links(description) == []
    assert sanitize_description(description) is None


@pytest.mark.parametrize(
    "path",
    ["platform/secrets", "auth/oauth2", "sandbox/jwt", "team/webhooks", "ops/tokens", "infra/hooks"],
)
def test_gitlab_group_names_that_look_like_credential_words_are_kept(path):
    url = f"https://gitlab.example/{path}"

    assert safe_description_links(url) == [{"url": url, "kind": "gitlab"}]


def test_gitlab_sub_routes_are_trimmed_to_the_identity_and_secrets_still_drop():
    assert safe_description_links("https://gitlab.example/group/project/-/tree/main") == [
        {"url": "https://gitlab.example/group/project", "kind": "gitlab"}
    ]
    assert safe_description_links(
        "https://gitlab.example/group/project/-/settings/access_tokens/glpat-abcdefghijklmnopqrst"
    ) == [{"url": "https://gitlab.example/group/project", "kind": "gitlab"}]
    for rejected in (
        "https://gitlab.example/api/v4/projects/1",
        "https://gitlab.example/oauth/authorize",
        "https://gitlab.example/-/profile/personal_access_tokens",
        "https://gitlab.example/group/private_token=abcdef",
        "https://gitlab.example/group/glpat-abcdefghijklmnopqrst",
        "https://gitlab.example/group/project/hooks/0123456789abcdef0123456789abcdef",
    ):
        assert safe_description_links(rejected) == [], rejected


def test_webhook_only_gitlab_hosts_are_recognized_from_monitored_products(monkeypatch):
    monkeypatch.setattr(
        "app.asoc_description_safety._monitored_product_hosts", lambda: frozenset({"gitlab.webhook-only.example"})
    )

    assert safe_description_links("https://gitlab.webhook-only.example/team/service") == [
        {"url": "https://gitlab.webhook-only.example/team/service", "kind": "gitlab"}
    ]
    assert safe_description_links("https://gitlab.attacker.invalid/team/service") == []


def test_external_urls_keep_the_strict_credential_word_check():
    assert safe_asoc_public_base("https://asoc.example/hooks/abc") is None


def test_asoc_public_base_rejects_credential_bearing_configuration():
    assert safe_asoc_public_base("https://user:password@asoc.example") is None
    assert safe_asoc_public_base("https://asoc.example/?token=secret") is None
    assert safe_asoc_public_base("https://asoc.example/#secret") is None
    assert safe_asoc_public_base("https://asoc.example/api/") == "https://asoc.example"
    assert safe_asoc_public_base("https://asoc.example/hexway/api") == "https://asoc.example/hexway"


def test_description_urls_reject_deeply_encoded_credentials_and_unknown_gitlab_hosts():
    token = "glpat-" + "a" * 24
    encoded_path = f"/group/{token}"
    for _ in range(5):
        encoded_path = quote(encoded_path, safe="")
    deep_path = f"/group/{token}"
    for _ in range(10):
        deep_path = quote(deep_path, safe="")

    assert safe_description_links(f"https://gitlab.example{encoded_path}") == []
    assert safe_description_links(f"https://gitlab.example{deep_path}") == []
    assert safe_description_links("https://gitlab.attacker.invalid/group/project") == []


def test_public_base_rejects_credential_shaped_hostname_labels():
    token_label = "glpat-" + "b" * 24
    assert safe_asoc_public_base("https://token.example.invalid") is None
    assert safe_asoc_public_base(f"https://{token_label}.example.invalid") is None
    assert safe_description_links(f"https://{token_label}.example.invalid/group") == []


def test_legacy_description_data_migration_replaces_raw_text_with_safe_projection():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(
        AsocProduct(
            asoc_id="legacy",
            name="Legacy product",
            description=(
                "operator note secret-value\n"
                "https://gitlab.example/group/repo?token=secret-value\n"
                "ScanOps-Scope: internal"
            ),
        )
    )
    db.commit()

    sanitize_existing_asoc_descriptions(engine)

    sanitized = db.query(AsocProduct).filter_by(asoc_id="legacy").one().description
    assert sanitized == "ScanOps-Scope: internal\nhttps://gitlab.example/group/repo"
    assert "secret-value" not in sanitized
    db.close()


def test_legacy_data_migration_does_not_persist_deeply_encoded_credentials():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    token = "glpat-" + "c" * 24
    encoded_path = f"/group/{token}"
    for _ in range(6):
        encoded_path = quote(encoded_path, safe="")
    db.add(
        AsocProduct(
            asoc_id="legacy-nested-token",
            name="Legacy product",
            description=f"https://gitlab.example{encoded_path}",
        )
    )
    db.commit()

    sanitize_existing_asoc_descriptions(engine)

    stored = db.query(AsocProduct).filter_by(asoc_id="legacy-nested-token").one().description
    assert stored is None
    db.close()


def test_product_directory_requires_explicit_exact_host(monkeypatch):
    monkeypatch.delenv("ASOC_PRODUCT_REFERENCE_HOSTS", raising=False)
    assert safe_description_links("https://product.example.com/products/77") == []
    monkeypatch.setenv("ASOC_PRODUCT_REFERENCE_HOSTS", "product.example.com")
    assert safe_description_links("https://product.example.com/products/77")
    assert safe_description_links("https://other.product.example.com/products/77") == []
