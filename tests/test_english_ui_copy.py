import re
from pathlib import Path

from jinja2 import Template


CYRILLIC = re.compile(r"[\u0400-\u052f]")
HTML_COMMENT = re.compile(r"<!--.*?-->|{#.*?#}", re.DOTALL)
INLINE_SCRIPT = re.compile(
    r"(?P<open><script\b[^>]*>)(?P<body>.*?)(?P<close></script\s*>)",
    re.IGNORECASE | re.DOTALL,
)
JS_TOKEN_OR_COMMENT = re.compile(
    r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)"""
    r"""|(/\*.*?\*/|//[^\n]*)""",
    re.DOTALL,
)
UNICODE_ESCAPE = re.compile(
    r"(?<!\\)(?P<escaped_pairs>(?:\\\\)*)\\u(?P<codepoint>[0-9a-fA-F]{4})"
)
HTML_NUMERIC_ENTITY = re.compile(
    r"&#(?:(?:x(?P<hex>[0-9a-fA-F]+))|(?P<decimal>[0-9]+));?",
    re.IGNORECASE,
)
STRING_FROM_CODE_POINT = re.compile(
    r"\bString\s*\.\s*fromCodePoint\s*\((?P<arguments>[^()]*)\)",
    re.DOTALL,
)
JS_INTEGER_LITERAL = re.compile(
    r"(?:0[xX][0-9a-fA-F](?:_?[0-9a-fA-F])*"
    r"|0[bB][01](?:_?[01])*"
    r"|0[oO][0-7](?:_?[0-7])*"
    r"|[0-9](?:_?[0-9])*)"
)
SCRIPT_SUFFIXES = {".js", ".mjs"}


def _without_js_comments(match):
    token, comment = match.groups()
    if token is not None:
        return token
    return "\n" * comment.count("\n")


def _without_html_comments(match):
    return "\n" * match.group(0).count("\n")


def _without_inline_js_comments(match):
    body = JS_TOKEN_OR_COMMENT.sub(_without_js_comments, match.group("body"))
    return f"{match.group('open')}{body}{match.group('close')}"


def _decode_unicode_escape(match):
    rendered = chr(int(match.group("codepoint"), 16))
    if not CYRILLIC.fullmatch(rendered):
        return match.group(0)
    return f"{match.group('escaped_pairs')}{rendered}"


def _decode_html_numeric_entity(match):
    raw_codepoint = match.group("hex") or match.group("decimal")
    base = 16 if match.group("hex") is not None else 10
    codepoint = int(raw_codepoint, base)
    if codepoint > 0x10FFFF:
        return match.group(0)
    rendered = chr(codepoint)
    return rendered if CYRILLIC.fullmatch(rendered) else match.group(0)


def _parse_js_integer_literal(value):
    normalized = value.strip().replace("_", "")
    if not JS_INTEGER_LITERAL.fullmatch(value.strip()):
        return None
    if normalized.lower().startswith("0x"):
        return int(normalized[2:], 16)
    if normalized.lower().startswith("0b"):
        return int(normalized[2:], 2)
    if normalized.lower().startswith("0o"):
        return int(normalized[2:], 8)
    return int(normalized, 10)


def _expose_static_code_points(match):
    rendered = []
    for argument in match.group("arguments").split(","):
        codepoint = _parse_js_integer_literal(argument)
        if codepoint is not None and codepoint <= 0x10FFFF:
            character = chr(codepoint)
            if CYRILLIC.fullmatch(character):
                rendered.append(character)
    return f"{match.group(0)}{''.join(rendered)}"


def _expose_static_js_code_points(source):
    parts = []
    cursor = 0
    for match in JS_TOKEN_OR_COMMENT.finditer(source):
        parts.append(
            STRING_FROM_CODE_POINT.sub(
                _expose_static_code_points,
                source[cursor : match.start()],
            )
        )
        parts.append(match.group(0))
        cursor = match.end()
    parts.append(
        STRING_FROM_CODE_POINT.sub(_expose_static_code_points, source[cursor:])
    )
    return "".join(parts)


def _expose_inline_static_code_points(match):
    body = _expose_static_js_code_points(match.group("body"))
    return f"{match.group('open')}{body}{match.group('close')}"


def _rendered_copy_view(source):
    source = UNICODE_ESCAPE.sub(_decode_unicode_escape, source)
    return HTML_NUMERIC_ENTITY.sub(_decode_html_numeric_entity, source)


def _text_source_offenders(path, source):
    suffix = path.suffix.lower()
    if suffix == ".html":
        source = HTML_COMMENT.sub(_without_html_comments, source)
        source = INLINE_SCRIPT.sub(_without_inline_js_comments, source)
        static_copy = INLINE_SCRIPT.sub(_expose_inline_static_code_points, source)
    elif suffix in SCRIPT_SUFFIXES:
        source = JS_TOKEN_OR_COMMENT.sub(_without_js_comments, source)
        static_copy = _expose_static_js_code_points(source)
    else:
        static_copy = source

    rendered_copy = _rendered_copy_view(static_copy)
    return [
        f"{path}:{line_number}: {original_line.strip()}"
        for line_number, (original_line, rendered_line) in enumerate(
            zip(source.splitlines(), rendered_copy.splitlines(), strict=True),
            1,
        )
        if CYRILLIC.search(rendered_line)
    ]


def _operator_ui_sources():
    script_root = Path("app/static/js")
    return sorted(
        [
            *Path("app/templates").rglob("*.html"),
            *(
                path
                for path in script_root.rglob("*")
                if path.is_file() and path.suffix.lower() in SCRIPT_SUFFIXES
            ),
        ],
        key=str,
    )


def _find_cyrillic_ui_copy(paths):
    offenders = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        offenders.extend(_text_source_offenders(path, source))
    return offenders


def test_operator_ui_authored_copy_is_english_only():
    offenders = _find_cyrillic_ui_copy(_operator_ui_sources())

    assert not offenders, "Cyrillic remains in authored operator UI copy:\n" + "\n".join(offenders)


def test_english_only_guard_ignores_comments_docstrings_and_runtime_user_data(tmp_path):
    template_path = tmp_path / "sample.html"
    template_path.write_text(
        "{# заметка разработчика #}\n"
        "<!-- ещё одна заметка -->\n"
        "<p>{{ operator_value }}</p>\n",
        encoding="utf-8",
    )
    script_path = tmp_path / "sample.js"
    script_path.write_text(
        "// заметка разработчика\n"
        "/* ещё одна заметка */\n"
        "const label = 'Ready';\n",
        encoding="utf-8",
    )

    assert _find_cyrillic_ui_copy([template_path, script_path]) == []

    rendered = Template(template_path.read_text(encoding="utf-8")).render(
        operator_value="Проект Альфа"
    )
    assert "Проект Альфа" in rendered


def test_english_only_guard_ignores_inline_script_comments(tmp_path):
    template_path = tmp_path / "sample.html"
    template_path.write_text(
        "<script>\n"
        "  // заметка разработчика\n"
        "  const label = 'Ready'; /* ещё одна заметка */\n"
        "  const marker = '// Ошибка загрузки';\n"
        "  const example = 'String.fromCodePoint(0x41e)';\n"
        "</script>\n",
        encoding="utf-8",
    )

    assert _find_cyrillic_ui_copy([template_path]) == [
        f"{template_path}:4: const marker = '// Ошибка загрузки';"
    ]


def test_english_only_guard_reports_statically_encoded_rendered_copy(tmp_path):
    template_path = tmp_path / "sample.html"
    template_path.write_text(
        r"""<p>{{ "\u041e\u0448\u0438\u0431\u043a\u0430" }}</p>
<p>&#1054;&#1096;&#1080;&#1073;&#1082;&#1072;</p>
<p>&#x41e;&#x448;&#x438;&#x431;&#x43a;&#x430;</p>
<script>const label = String.fromCodePoint(0x41e, 0x448, 1080, 1073, 1082, 1072);</script>
""",
        encoding="utf-8",
    )
    script_path = tmp_path / "sample.js"
    script_path.write_text(
        r"""const label = "\u041e\u0448\u0438\u0431\u043a\u0430";
""",
        encoding="utf-8",
    )

    assert _find_cyrillic_ui_copy([template_path, script_path]) == [
        f'{template_path}:1: <p>{{{{ "\\u041e\\u0448\\u0438\\u0431\\u043a\\u0430" }}}}</p>',
        f"{template_path}:2: <p>&#1054;&#1096;&#1080;&#1073;&#1082;&#1072;</p>",
        f"{template_path}:3: <p>&#x41e;&#x448;&#x438;&#x431;&#x43a;&#x430;</p>",
        (
            f"{template_path}:4: <script>const label = "
            "String.fromCodePoint(0x41e, 0x448, 1080, 1073, 1082, 1072);</script>"
        ),
        f'{script_path}:1: const label = "\\u041e\\u0448\\u0438\\u0431\\u043a\\u0430";',
    ]


def test_operator_ui_sources_discovers_javascript_suffixes_case_insensitively(
    tmp_path, monkeypatch
):
    script_root = tmp_path / "app/static/js"
    script_root.mkdir(parents=True)
    for name in ("lower.js", "module.mjs", "upper.JS", "mixed.MjS"):
        (script_root / name).write_text(
            "const label = 'Ошибка';\n",
            encoding="utf-8",
        )
    for name in ("unsupported.jsx", "notes.txt"):
        (script_root / name).write_text("const label = 'Ready';\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert [path.name for path in _operator_ui_sources()] == [
        "lower.js",
        "mixed.MjS",
        "module.mjs",
        "upper.JS",
    ]
    assert len(_find_cyrillic_ui_copy(_operator_ui_sources())) == 4


def test_english_only_guard_reports_authored_cyrillic_copy(tmp_path):
    template_path = tmp_path / "sample.html"
    template_path.write_text("<p>Ошибка загрузки</p>\n", encoding="utf-8")
    script_path = tmp_path / "sample.js"
    script_path.write_text("const error = 'Ошибка загрузки';\n", encoding="utf-8")

    offenders = _find_cyrillic_ui_copy([template_path, script_path])

    assert offenders == [
        f"{template_path}:1: <p>Ошибка загрузки</p>",
        f"{script_path}:1: const error = 'Ошибка загрузки';",
    ]
