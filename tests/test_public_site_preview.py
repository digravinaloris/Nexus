"""Contract checks for the isolated public-site preview artifact."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PREVIEW = ROOT / "docs" / "design" / "public-site-preview.html"


def test_public_preview_is_a_standalone_html_document():
    html = PREVIEW.read_text(encoding="utf-8")
    assert re.search(r"<!doctype html>", html, re.IGNORECASE)
    assert re.search(r'<html lang="en">', html)
    assert "</html>" in html


def test_preview_keeps_primary_navigation_targets():
    html = PREVIEW.read_text(encoding="utf-8")
    for href in ("/dashboard", "/status", "#features", "#about"):
        assert f'href="{href}"' in html


def test_preview_uses_responsive_layout_and_no_gradient_backgrounds():
    html = PREVIEW.read_text(encoding="utf-8")
    assert "@media" in html
    assert "grid-template-columns" in html
    assert not re.search(r"\b(?:linear|radial|conic)-gradient\s*\(", html, re.IGNORECASE)
