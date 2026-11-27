"""Pure SVG audit visual helpers without bot or database dependencies."""
def build_sparkline_svg(series, width=640, height=120):
    """Génère un petit graphe en barres en SVG pur — évite de charger une
    librairie JS externe juste pour ça (et marche même hors ligne)."""
    if not series:
        return ""
    max_val = max(v for _, v in series) or 1
    bar_gap = 2
    bar_width = max(2, (width - bar_gap * len(series)) / len(series))
    bars = []
    for i, (label, value) in enumerate(series):
        bar_h = (value / max_val) * (height - 20)
        x = i * (bar_width + bar_gap)
        y = height - bar_h
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_h:.1f}" '
            f'rx="2" fill="var(--raspberry)" opacity="0.85"><title>{label}: {value}</title></rect>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'preserveAspectRatio="none" role="img">{"".join(bars)}</svg>'
    )
