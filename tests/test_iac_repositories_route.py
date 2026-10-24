"""Regression tests for the IaC repository-list route."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


def test_iac_repositories_maps_public_q_to_service_query_text(monkeypatch):
    from app.routers.web import iac as module

    service = MagicMock()
    service.latest_repo_rows.return_value = ([], 0)
    service.repository_filter_values.return_value = {
        "scan_statuses": [],
        "asoc_statuses": [],
    }
    monkeypatch.setattr(module, "IacService", lambda db: service)
    response = object()
    monkeypatch.setattr(module.templates, "TemplateResponse", lambda *args: response)

    result = asyncio.run(
        module.iac_repositories(
            MagicMock(),
            page=1,
            q="portal",
            iac="all",
            scan_status="",
            asoc_status="",
            has_important_findings="",
            bulk_message="",
            db=MagicMock(),
        )
    )

    assert result is response
    kwargs = service.latest_repo_rows.call_args.kwargs
    assert kwargs["query_text"] == "portal"
    assert "q" not in kwargs


def test_iac_repository_detail_selects_scan_from_same_repository_history(monkeypatch):
    from app.routers.web import iac as module

    run_1 = SimpleNamespace(id=11)
    run_2 = SimpleNamespace(id=12)
    anchor = SimpleNamespace(id=101, run_id=11, run=run_1)
    selected = SimpleNamespace(id=102, run_id=12, run=run_2)
    service = MagicMock()
    service.get_repo_row.return_value = anchor
    service.repository_scan_history.return_value = [selected, anchor]
    service.findings_for_repo_row.return_value = [SimpleNamespace(id=501)]
    monkeypatch.setattr(module, "IacService", lambda db: service)
    captured = {}
    response = object()

    def render(template, context):
        captured.update({"template": template, "context": context})
        return response

    monkeypatch.setattr(module.templates, "TemplateResponse", render)

    result = asyncio.run(
        module.iac_repository_detail(
            MagicMock(),
            repo_row_id=anchor.id,
            scan_id=selected.id,
            db=MagicMock(),
        )
    )

    assert result is response
    assert captured["template"] == "iac_repository_detail.html"
    assert captured["context"]["repository_target"] is anchor
    assert captured["context"]["repo"] is selected
    assert captured["context"]["run"] is run_2
    service.findings_for_repo_row.assert_called_once_with(selected.id)


def test_iac_repository_detail_rejects_scan_from_another_repository(monkeypatch):
    from app.routers.web import iac as module

    anchor = SimpleNamespace(id=101, run_id=11, run=SimpleNamespace(id=11))
    service = MagicMock()
    service.get_repo_row.return_value = anchor
    service.repository_scan_history.return_value = [anchor]
    monkeypatch.setattr(module, "IacService", lambda db: service)

    with pytest.raises(HTTPException, match="does not belong") as error:
        asyncio.run(
            module.iac_repository_detail(
                MagicMock(),
                repo_row_id=anchor.id,
                scan_id=999,
                db=MagicMock(),
            )
        )

    assert error.value.status_code == 404


def test_iac_repository_detail_template_has_history_and_client_side_finding_filters():
    from pathlib import Path

    source = Path("app/templates/iac_repository_detail.html").read_text()

    assert "Scan history" in source
    assert "data-severity-filter" in source
    assert "data-finding-search" in source
    assert "IaC execution stages" in source
    assert "Finding details unavailable" in source
    assert "details unavailable" in source
