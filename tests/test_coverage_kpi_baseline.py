from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from jinja2 import Environment, FileSystemLoader

from app.models.audit import AuditCoverageStatus
from app.routers.web import audit


ROOT = Path(__file__).resolve().parents[1]


def _row(status):
    return {"snapshot": SimpleNamespace(status=status)}


def test_coverage_counts_are_computed_from_the_unfiltered_baseline():
    rows = [
        _row(AuditCoverageStatus.COVERED),
        _row(AuditCoverageStatus.COVERED),
        _row(AuditCoverageStatus.PIPELINE_NO_SEC),
        _row(AuditCoverageStatus.NO_PIPELINES),
        _row(AuditCoverageStatus.INACCESSIBLE),
        _row(AuditCoverageStatus.EXCLUDED),
    ]

    counts = audit._coverage_counts(rows)
    matching = audit._filter_coverage_rows(rows, "inactive", False)

    assert counts == {
        "total": 5,
        "active": 2,
        "not_configured": 1,
        "inactive": 2,
    }
    assert len(matching) == 2
    assert counts["total"] == 5


def test_problems_only_and_status_filters_are_mutually_exclusive():
    assert audit._normalize_coverage_filters("active", True) == ("active", False)
    assert audit._normalize_coverage_filters("all", True) == ("all", True)
    assert audit._normalize_coverage_filters("unknown", False) == ("all", False)

    rows = [
        _row(AuditCoverageStatus.COVERED),
        _row(AuditCoverageStatus.PIPELINE_NO_SEC),
        _row(AuditCoverageStatus.NO_PIPELINES),
    ]
    problems = audit._filter_coverage_rows(rows, "all", True)
    assert [row["snapshot"].status for row in problems] == [
        AuditCoverageStatus.PIPELINE_NO_SEC,
        AuditCoverageStatus.NO_PIPELINES,
    ]


def test_metric_cards_share_canonical_url_and_selected_state_contract():
    cards = audit._coverage_metric_cards(
        {
            "total": 9,
            "active": 4,
            "not_configured": 3,
            "inactive": 2,
        },
        base_url="/coverage",
        status_filter="not_configured",
        only_problems=False,
        persistent_query={"selected_product_ids": "4,9"},
    )

    assert [card["label"] for card in cards] == [
        "Total",
        "Active",
        "Not Configured",
        "Inactive",
    ]
    assert [card["count"] for card in cards] == [9, 4, 3, 2]
    assert [card["selected"] for card in cards] == [False, False, True, False]
    for card in cards:
        query = parse_qs(urlsplit(card["href"]).query)
        assert query == {
            "selected_product_ids": ["4,9"],
            "status": [card["value"]],
            "only_problems": ["false"],
        }


def test_list_and_detail_use_one_interactive_metric_macro_and_unambiguous_form():
    aggregate = (ROOT / "app/templates/audit.html").read_text(encoding="utf-8")
    detail = (ROOT / "app/templates/audit_product_detail.html").read_text(
        encoding="utf-8"
    )
    macro = (
        ROOT / "app/templates/macros/coverage_metrics.html"
    ).read_text(encoding="utf-8")

    for template in (aggregate, detail):
        assert "{% import 'macros/coverage_metrics.html' as coverage %}" in template
        assert "coverage.coverage_metrics(" in template
        assert '<input type="hidden" name="status" value="all">' in template

    assert 'class="stat-card audit-stat-card"' not in detail
    assert 'aria-label="Coverage status filter"' in macro
    assert 'aria-current="page"' in macro
    assert "Showing <strong>{{ matching_count }}</strong> of" in macro
    assert "status_filter={{ status_filter }}" not in aggregate

    environment = Environment(
        loader=FileSystemLoader(ROOT / "app/templates"),
        autoescape=True,
    )
    environment.get_template("audit.html")
    environment.get_template("audit_product_detail.html")
    rendered = environment.get_template(
        "macros/coverage_metrics.html"
    ).module.coverage_metrics(
        audit._coverage_metric_cards(
            {
                "total": 5,
                "active": 2,
                "not_configured": 2,
                "inactive": 1,
            },
            base_url="/coverage",
            status_filter="inactive",
            only_problems=False,
        ),
        filter_active=True,
        matching_count=1,
        total_count=5,
    )
    assert 'aria-current="page"' in rendered
    assert "Showing <strong>1</strong> of <strong>5</strong>" in rendered
