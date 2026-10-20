import pytest

from app.routes.appearance import DEFAULT_APPEARANCE, clean_hex_color


@pytest.mark.parametrize("value, expected", [("#147471", "#147471"), ("#ABCDEF", "#abcdef"), (" #0e4f4d ", "#0e4f4d")])
def test_clean_hex_color_accepts_six_digit_hex(value, expected):
    assert clean_hex_color(value, "#000000") == expected


@pytest.mark.parametrize("value", ["", None, "red", "#fff", "#1234567", "#14747g", "#147471;}body{display:none"])
def test_clean_hex_color_falls_back_on_invalid(value):
    assert clean_hex_color(value, "#172033") == "#172033"


def test_theme_defaults_match_stylesheet():
    assert DEFAULT_APPEARANCE["theme_sidebar_color"] == "#172033"
    assert DEFAULT_APPEARANCE["theme_accent_color"] == "#2563eb"
