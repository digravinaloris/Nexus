import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from audit_visuals import build_sparkline_svg



def test_sparkline_empty_series_returns_empty_svg():
    assert build_sparkline_svg([]) == ""


def test_sparkline_renders_scaled_bars_and_labels():
    svg = build_sparkline_svg([("Mon", 2), ("Tue", 4)], width=100, height=60)
    assert svg.startswith('<svg viewBox="0 0 100 60"')
    assert '<title>Mon: 2</title>' in svg
    assert '<title>Tue: 4</title>' in svg
    assert svg.count("<rect ") == 2
