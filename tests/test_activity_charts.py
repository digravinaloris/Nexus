"""Tests for moderation activity chart rendering."""
from services import activity_charts


def test_generate_activity_chart_renders_seven_day_stats(monkeypatch):
    calls = {}
    closed = []

    class Spines:
        def __getitem__(self, names):
            calls["spines"] = names
            return self

        def set_visible(self, visible):
            calls["spines_visible"] = visible

    class Axes:
        spines = Spines()

        def bar(self, labels, values, color):
            calls["bar"] = (labels, values, color)

        def set_title(self, title, **kwargs):
            calls["title"] = (title, kwargs)

        def tick_params(self, **kwargs):
            calls["ticks"] = kwargs

    class Figure:
        def tight_layout(self):
            calls["tight_layout"] = True

        def savefig(self, buffer, **kwargs):
            calls["savefig"] = kwargs
            buffer.write(b"png-data")

    figure = Figure()
    axes = Axes()

    monkeypatch.setattr(
        activity_charts,
        "get_sanction_stats",
        lambda guild_id, days: calls.update(stats=(guild_id, days)) or [
            ("Mon", 2), ("Tue", 4)
        ],
    )
    monkeypatch.setattr(
        activity_charts.plt,
        "subplots",
        lambda **kwargs: calls.update(subplots=kwargs) or (figure, axes),
    )
    monkeypatch.setattr(
        activity_charts.plt,
        "close",
        lambda fig: closed.append(fig),
    )

    result = activity_charts._generate_activity_chart(1234)

    assert result.read() == b"png-data"
    assert calls["stats"] == (1234, 7)
    assert calls["subplots"] == {"figsize": (6, 2.2), "dpi": 120}
    assert calls["bar"] == (["Mon", "Tue"], [2, 4], "#3399ff")
    assert calls["savefig"] == {"format": "png", "transparent": True}
    assert calls["spines"] == ["top", "right"]
    assert calls["spines_visible"] is False
    assert closed == [figure]
