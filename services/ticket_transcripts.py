"""HTML rendering and retrieval helpers for ticket transcripts."""


def _render_ticket_transcript(channel_name, messages):
    """Render a self-contained, Discord-inspired HTML transcript."""
    import html as _html

    safe_channel = _html.escape(str(channel_name), quote=True)
    rows = []
    for message in messages:
        author = _html.escape(str(getattr(message.author, "display_name", message.author)), quote=True)
        avatar_url = _html.escape(str(getattr(getattr(message.author, "display_avatar", None), "url", "")), quote=True)
        timestamp = message.created_at.strftime("%b %d, %Y at %H:%M UTC")
        safe_timestamp = _html.escape(timestamp, quote=True)
        content = _html.escape(message.content or "")
        content_html = content.replace("\n", "<br>") if content else ""
        if not content_html and not getattr(message, "attachments", None) and not getattr(message, "embeds", None):
            content_html = '<span class="muted">This message had no text content.</span>'

        avatar = (
            f'<img class="avatar" src="{avatar_url}" alt="" loading="lazy">'
            if avatar_url else '<div class="avatar avatar-fallback">?</div>'
        )
        bot_badge = '<span class="bot-badge">BOT</span>' if getattr(message.author, "bot", False) else ""
        edited = ' <span class="edited">(edited)</span>' if getattr(message, "edited_at", None) else ""

        attachment_html = []
        for attachment in getattr(message, "attachments", []):
            filename = _html.escape(str(attachment.filename), quote=True)
            url = _html.escape(str(attachment.url), quote=True)
            content_type = str(getattr(attachment, "content_type", "") or "")
            if content_type.startswith("image/"):
                attachment_html.append(
                    f'<a class="attachment image-attachment" href="{url}" target="_blank" rel="noopener noreferrer">'
                    f'<img src="{url}" alt="{filename}" loading="lazy"><span>{filename}</span></a>'
                )
            else:
                attachment_html.append(
                    f'<a class="attachment file-attachment" href="{url}" target="_blank" rel="noopener noreferrer">'
                    f'<span class="file-icon">📎</span><span>{filename}</span></a>'
                )

        embed_html = []
        for embed in getattr(message, "embeds", []):
            title = _html.escape(str(embed.title), quote=True) if embed.title else ""
            description = _html.escape(str(embed.description), quote=True).replace("\n", "<br>") if embed.description else ""
            if title or description:
                embed_html.append(
                    f'<div class="discord-embed">{f"<strong>{title}</strong>" if title else ""}'
                    f'{f"<div>{description}</div>" if description else ""}</div>'
                )

        rows.append(
            f'<article class="message">{avatar}<div class="message-body">'
            f'<div class="message-meta"><span class="author">{author}</span>{bot_badge}'
            f'<time datetime="{message.created_at.isoformat()}">{safe_timestamp}</time>{edited}</div>'
            f'<div class="message-content">{content_html}</div>'
            f'{"".join(attachment_html)}{"".join(embed_html)}</div></article>'
        )

    count = len(messages)
    noun = "message" if count == 1 else "messages"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light"><title>Transcript — {safe_channel}</title>
<style>
:root {{ color-scheme: dark; --bg:#313338; --panel:#2b2d31; --text:#dbdee1; --muted:#949ba4; --line:#3f4147; --link:#00a8fc; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:28px 16px 48px; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:920px; margin:0 auto; }}
header {{ padding:20px 22px; margin-bottom:18px; background:var(--panel); border-radius:8px; }}
h1 {{ margin:0 0 4px; font-size:21px; }}
.summary {{ color:var(--muted); font-size:13px; }}
.message {{ display:flex; gap:16px; padding:12px 18px; border-radius:6px; }}
.message:hover {{ background:rgba(0,0,0,.08); }}
.avatar {{ width:40px; height:40px; flex:0 0 40px; border-radius:50%; object-fit:cover; background:#404249; }}
.avatar-fallback {{ display:grid; place-items:center; color:var(--muted); font-weight:700; }}
.message-body {{ min-width:0; flex:1; }}
.message-meta {{ display:flex; align-items:baseline; flex-wrap:wrap; gap:8px; }}
.author {{ color:#f2f3f5; font-weight:600; }}
time,.edited,.muted {{ color:var(--muted); font-size:12px; }}
.bot-badge {{ padding:1px 4px; border-radius:3px; background:#5865f2; color:white; font-size:9px; font-weight:700; }}
.message-content {{ margin-top:3px; overflow-wrap:anywhere; white-space:normal; }}
.attachment {{ display:inline-flex; align-items:center; gap:9px; max-width:100%; margin:8px 8px 0 0; padding:8px; border:1px solid var(--line); border-radius:6px; color:var(--link); text-decoration:none; }}
.file-attachment {{ min-width:190px; }}
.file-icon {{ font-size:20px; }}
.image-attachment {{ flex-direction:column; align-items:flex-start; }}
.image-attachment img {{ max-width:min(100%,420px); max-height:320px; border-radius:4px; object-fit:contain; }}
.discord-embed {{ margin-top:8px; padding:10px 12px; border-left:4px solid #5865f2; border-radius:4px; background:var(--panel); overflow-wrap:anywhere; }}
.discord-embed strong {{ display:block; margin-bottom:4px; }}
@media(max-width:520px) {{ body {{ padding:12px 6px 28px; }} .message {{ gap:10px; padding:10px 8px; }} .avatar {{ width:32px; height:32px; flex-basis:32px; }} header {{ padding:16px; }} }}
</style></head><body><main>
<header><h1>#{safe_channel}</h1><div class="summary">{count} {noun} · Ticket transcript</div></header>
<section aria-label="Ticket messages">{"".join(rows)}</section>
</main></body></html>"""


async def _generate_ticket_transcript(channel):
    """Fetch the ticket history and render it in chronological order."""
    messages = [message async for message in channel.history(limit=500, oldest_first=True)]
    return _render_ticket_transcript(channel.name, messages)



