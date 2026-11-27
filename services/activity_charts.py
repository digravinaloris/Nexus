"""Chart rendering for Nexus moderation activity."""
import io

import matplotlib
matplotlib.use("Agg")  # Headless rendering for bot and web processes.
import matplotlib.pyplot as plt

from stores.sanctions_store import get_sanction_stats


def _generate_activity_chart(guild_id):
    """Render the last seven days of moderation activity as a PNG buffer.

    This CPU-bound helper is called through asyncio.to_thread by its caller.
    """
    stats = get_sanction_stats(guild_id, days=7)
    labels = [label for label, _ in stats]
    values = [count for _, count in stats]
    fig, ax = plt.subplots(figsize=(6, 2.2), dpi=120)
    ax.bar(labels, values, color="#3399ff")
    ax.set_title("Moderation activity · last 7 days", fontsize=10, color="#333333")
    ax.spines[["top", "right"]].set_visible(False)
    ax.tick_params(labelsize=8)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    buf.seek(0)
    return buf
