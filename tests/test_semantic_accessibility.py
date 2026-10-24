import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"
STYLE = ROOT / "app" / "static" / "css" / "style.css"
SIGNAL_STYLE = ROOT / "app" / "static" / "css" / "signal-river.css"


class SemanticTemplateParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.controls: list[tuple[str, dict[str, str | None], bool, int]] = []
        self.headings: list[tuple[int, int]] = []
        self.label_fors: set[str] = set()
        self.main_count = 0
        self.tables_without_focusable_container: list[int] = []
        self.action_class_links: list[tuple[list[str], str | None, int]] = []
        self._label_depth = 0
        self._stack: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)

        if tag == "label":
            self._label_depth += 1
            if attributes.get("for"):
                self.label_fors.add(str(attributes["for"]))

        if tag in {"input", "select", "textarea"} and attributes.get("type") != "hidden":
            self.controls.append(
                (tag, attributes, self._label_depth > 0, self.getpos()[0])
            )

        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append((int(tag[1]), self.getpos()[0]))

        if tag == "main":
            self.main_count += 1

        if tag == "table":
            has_focusable_container = any(
                ancestor_attrs.get("tabindex") == "0"
                for _, ancestor_attrs in self._stack
            )
            if not has_focusable_container:
                self.tables_without_focusable_container.append(self.getpos()[0])

        if tag == "a":
            classes = str(attributes.get("class") or "").split()
            if any(class_name.endswith("-action") for class_name in classes):
                self.action_class_links.append(
                    (classes, attributes.get("data-action"), self.getpos()[0])
                )

        self._stack.append((tag, attributes))

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self._label_depth:
            self._label_depth -= 1

        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break


def _parse(path: Path) -> SemanticTemplateParser:
    parser = SemanticTemplateParser()
    source = path.read_text()
    source = re.sub(
        r"{%\s*call\s+data_surfaces\.responsive_table\([^%]+\)\s*%}",
        '<div tabindex="0">',
        source,
    )
    source = source.replace("{% endcall %}", "</div>")
    parser.feed(source)
    return parser


def test_all_form_controls_have_programmatic_names() -> None:
    failures: list[str] = []

    for path in sorted(TEMPLATES.rglob("*.html")):
        parser = _parse(path)
        for tag, attrs, nested_in_label, line in parser.controls:
            control_id = attrs.get("id")
            has_name = (
                nested_in_label
                or bool(attrs.get("aria-label"))
                or bool(attrs.get("aria-labelledby"))
                or bool(control_id and control_id in parser.label_fors)
            )
            if not has_name:
                failures.append(
                    f"{path.relative_to(ROOT)}:{line} "
                    f"<{tag}> id={control_id!r} name={attrs.get('name')!r}"
                )

    assert failures == [], "Unlabelled form controls:\n" + "\n".join(failures)


def test_templates_define_one_top_level_main_landmark() -> None:
    main_locations = [
        path.relative_to(ROOT)
        for path in sorted(TEMPLATES.rglob("*.html"))
        if _parse(path).main_count
    ]

    assert main_locations == [Path("app/templates/base.html")]
    assert _parse(TEMPLATES / "base.html").main_count == 1


def test_page_heading_levels_do_not_skip() -> None:
    failures: list[str] = []

    for path in sorted(TEMPLATES.glob("*.html")):
        previous_level: int | None = None
        for level, line in _parse(path).headings:
            if previous_level is not None and level > previous_level + 1:
                failures.append(
                    f"{path.relative_to(ROOT)}:{line} "
                    f"h{previous_level} followed by h{level}"
                )
            previous_level = level

    assert failures == [], "Skipped heading levels:\n" + "\n".join(failures)


def test_metric_values_are_not_headings() -> None:
    metric_heading = re.compile(
        r'class="stat-content"[^>]*>\s*<h[1-6]\b',
    )
    failures = [
        str(path.relative_to(ROOT))
        for path in sorted(TEMPLATES.rglob("*.html"))
        if metric_heading.search(path.read_text())
    ]

    assert failures == []
    assert ".stat-content .stat-value {" in STYLE.read_text()


def test_table_action_headers_are_named() -> None:
    empty_header = re.compile(r"<th(?:\s[^>]*)?>\s*</th>", flags=re.DOTALL)
    failures = [
        str(path.relative_to(ROOT))
        for path in sorted(TEMPLATES.rglob("*.html"))
        if empty_header.search(path.read_text())
    ]

    assert failures == []
    assert '<span class="visually-hidden">Actions</span>' in (
        TEMPLATES / "iac_run.html"
    ).read_text()
    assert '<span class="visually-hidden">Actions</span>' in (
        TEMPLATES / "iac_repositories.html"
    ).read_text()


def test_action_class_links_declare_explicit_action_semantics() -> None:
    failures: list[str] = []

    for path in sorted(TEMPLATES.rglob("*.html")):
        for classes, action, line in _parse(path).action_class_links:
            if not action:
                failures.append(
                    f"{path.relative_to(ROOT)}:{line} classes={' '.join(classes)!r}"
                )

    assert failures == [], "Action-class links without data-action:\n" + "\n".join(
        failures
    )


def test_data_tables_have_keyboard_focusable_scroll_containers() -> None:
    failures: list[str] = []

    for path in sorted(TEMPLATES.rglob("*.html")):
        if path.name.endswith("_export.html"):
            continue
        for line in _parse(path).tables_without_focusable_container:
            failures.append(f"{path.relative_to(ROOT)}:{line}")

    assert failures == [], "Tables without focusable scroll containers:\n" + "\n".join(
        failures
    )

    signal_styles = SIGNAL_STYLE.read_text()
    assert ".signal-river-layout .table-container:focus-visible," in signal_styles
    assert "outline: 2px solid var(--accent-primary);" in signal_styles


def test_target360_timeline_flow_is_keyboard_scrollable() -> None:
    template = (TEMPLATES / "repository_360.html").read_text()
    styles = SIGNAL_STYLE.read_text()

    assert (
        '<ol class="target360-flow" tabindex="0" '
        'aria-label="Execution steps for {{ execution.correlation_key }}">'
    ) in template
    assert ".signal-river-layout .target360-flow:focus-visible {" in styles
