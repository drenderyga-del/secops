from pathlib import Path
import math
import re
import textwrap


ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "app" / "static" / "css"


def _read(name):
    return (CSS / name).read_text(encoding="utf-8")


CONTROL_SELECTOR = """body.layout.signal-river-layout :where(
    .btn,
    button,
    input:not([type="checkbox"]):not([type="radio"]):not(.signal-select-native),
    select:not(.signal-select-native),
    textarea,
    .side-link,
    .side-sublink,
    .module-tab,
    .signal-select-trigger,
    .signal-select-option,
    .pagination a,
    .table-actions a
)"""

ICON_CONTROL_SELECTOR = """body.layout.signal-river-layout :where(
    .sidebar-toggle,
    .filter-clear,
    .gitlab-remove-instance,
    .icon-button,
    .btn-icon
)"""


def _rule_body(source, selector):
    start = source.index(f"{selector} {{") + len(selector)
    opening_brace = source.index("{", start)
    closing_brace = source.index("}", opening_brace)
    return source[opening_brace + 1 : closing_brace]


def test_global_tokens_have_one_owner_and_load_before_route_styles():
    tokens = _read("tokens.css")
    legacy = _read("style.css")
    signal = _read("signal-river.css")
    base = (ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")

    assert tokens.count(":root {") == 1
    assert ":root {" not in legacy
    assert ":root {" not in signal
    assert base.index("tokens.css") < base.index("style.css") < base.index("signal-river.css")


def test_semantic_type_scale_meets_operational_readability_floor():
    tokens = _read("tokens.css")

    expected = {
        "--type-label": "0.75rem",
        "--type-table": "0.8125rem",
        "--type-mono": "0.75rem",
        "--type-body": "0.875rem",
        "--type-action": "0.875rem",
        "--type-heading-sm": "1rem",
        "--type-heading-md": "1.125rem",
        "--type-heading-lg": "1.75rem",
    }
    for name, value in expected.items():
        assert f"{name}: {value};" in tokens

    route_css = _read("style.css") + _read("signal-river.css")
    literal_rems = [
        float(value)
        for value in re.findall(r"font-size:\s*(0?\.\d+)rem", route_css)
    ]
    assert literal_rems
    assert min(literal_rems) >= 0.75


def _oklch_to_linear_srgb(value):
    lightness, chroma, hue = value
    hue_radians = math.radians(hue)
    a = chroma * math.cos(hue_radians)
    b = chroma * math.sin(hue_radians)
    l_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_ = lightness - 0.0894841775 * a - 1.291485548 * b
    l, m, s = l_**3, m_**3, s_**3
    channels = (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
    )
    return tuple(max(0, min(1, channel)) for channel in channels)


def _contrast_ratio(foreground, background):
    def luminance(color):
        red, green, blue = _oklch_to_linear_srgb(color)
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue

    brighter, darker = sorted(
        (luminance(foreground), luminance(background)),
        reverse=True,
    )
    return (brighter + 0.05) / (darker + 0.05)


def test_operational_text_tokens_keep_wcag_aa_contrast_on_shared_surfaces():
    text_colors = {
        "primary": (0.965, 0.008, 286),
        "secondary": (0.76, 0.018, 286),
        "muted": (0.67, 0.022, 286),
    }
    surfaces = {
        "primary": (0.115, 0.015, 286),
        "secondary": (0.135, 0.018, 286),
        "tertiary": (0.155, 0.019, 286),
    }

    for text_name, text_color in text_colors.items():
        for surface_name, surface_color in surfaces.items():
            assert _contrast_ratio(text_color, surface_color) >= 4.5, (
                f"{text_name} text does not meet WCAG AA on {surface_name}"
            )


def test_shared_control_contract_covers_navigation_filters_and_table_actions():
    tokens = _read("tokens.css")
    styles = _read("signal-river.css")

    assert "--control-height: 40px;" in tokens
    assert "--control-height-touch: 44px;" in tokens
    assert "--icon-control-size: 40px;" in tokens
    media_marker = "@media (max-width: 900px), (pointer: coarse)"
    desktop_styles, touch_styles = styles.rsplit(media_marker, 1)
    touch_styles = textwrap.dedent(
        touch_styles[touch_styles.index("{") + 1 : touch_styles.rfind("}")]
    )
    desktop_controls = _rule_body(desktop_styles, CONTROL_SELECTOR)
    desktop_icons = _rule_body(desktop_styles, ICON_CONTROL_SELECTOR)
    touch_controls = _rule_body(touch_styles, CONTROL_SELECTOR)
    touch_icons = _rule_body(touch_styles, ICON_CONTROL_SELECTOR)

    # Specificity (0, 2, 1) ties the strongest legacy control selector
    # (`.signal-river-layout .filter-field input`) and wins by source order.
    assert CONTROL_SELECTOR.split(" :where(", 1)[0].count(".") == 2
    assert desktop_styles.index(CONTROL_SELECTOR) > desktop_styles.index(
        ".signal-river-layout .filter-field input"
    )
    assert desktop_styles.index(CONTROL_SELECTOR) > desktop_styles.index(
        ".signal-river-layout .btn {"
    )
    assert desktop_styles.index(CONTROL_SELECTOR) > desktop_styles.index(
        ".signal-river-layout .side-link {"
    )
    assert "min-height: var(--control-height);" in desktop_controls
    assert "font-size: var(--type-action);" in desktop_controls
    for declaration in (
        "width: var(--icon-control-size);",
        "min-width: var(--icon-control-size);",
        "height: var(--control-height);",
        "min-height: var(--control-height);",
        "padding: 0;",
    ):
        assert declaration in desktop_icons

    assert "min-height: var(--control-height-touch);" in touch_controls
    for declaration in (
        "width: var(--control-height-touch);",
        "min-width: var(--control-height-touch);",
        "height: var(--control-height-touch);",
        "min-height: var(--control-height-touch);",
    ):
        assert declaration in touch_icons


def test_focus_motion_and_accidental_accent_regressions_stay_closed():
    legacy = _read("style.css")
    signal = _read("signal-river.css")
    combined = legacy + signal

    assert ":focus-visible" in signal
    assert "outline: 2px solid var(--accent-primary);" in signal
    assert "@media (prefers-reduced-motion: reduce)" in signal
    assert "transition: all" not in combined
    transition_values = re.findall(
        r"transition(?:-property)?:\s*([^;]+);",
        combined,
    )
    layout_properties = re.compile(
        r"\b(?:left|right|top|bottom|width|height|margin|padding)\b"
    )
    assert not [
        value for value in transition_values if layout_properties.search(value)
    ]
    for selector in (
        ".audit-legend-item",
        ".audit-metric",
        ".iac-finding-result",
        ".cscan-finding-card",
        ".cscan-finding-compact",
    ):
        rule = legacy.split(f"{selector} {{", 1)[1].split("}", 1)[0]
        assert "border-left" not in rule
