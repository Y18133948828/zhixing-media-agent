from html import escape

import bleach
from markdown_it import MarkdownIt


def reference_sources(state):
    sources = {k["id"]: k for k in state.get("context", {}).get("knowledge", []) if k.get("id")}
    sources.update({f"input-{i + 1}": s for i, s in enumerate(state["task_input"].get("sources", []))})
    return sources


def format_wechat(article: dict, sources=None) -> str:
    # Raw HTML disabled and no external images: the draft can be copied without hidden markup.
    rendered = MarkdownIt("commonmark", {"html": False}).disable("image").render(article["markdown"])
    safe = bleach.clean(
        rendered,
        tags=[
            "p",
            "h1",
            "h2",
            "h3",
            "h4",
            "strong",
            "em",
            "ul",
            "ol",
            "li",
            "blockquote",
            "pre",
            "code",
            "a",
            "hr",
            "br",
        ],
        attributes={"a": ["href", "title"]},
        protocols=["http", "https"],
        strip=True,
    )
    styles = {
        "p": "margin:16px 0;line-height:1.85;",
        "h2": "margin:28px 0 14px;color:#126653;font-size:22px;",
        "h3": "margin:24px 0 12px;font-size:19px;",
        "pre": "padding:16px;background:#f3f6f5;white-space:pre-wrap;word-break:break-all;font-size:13px;",
        "blockquote": "border-left:4px solid #25a881;padding-left:16px;color:#52615c;",
    }
    for tag, style in styles.items():
        safe = safe.replace(f"<{tag}>", f'<{tag} style="{style}">')
    if article.get("citations"):
        entries = []
        for citation in article["citations"]:
            source = (sources or {}).get(citation["source_id"], {})
            title = escape(source.get("title", citation["source_id"]))
            url = source.get("source_url") or source.get("url")
            if url and str(url).startswith(("https://", "http://")):
                title = f'<a href="{escape(str(url), quote=True)}">{title}</a>'
            entries.append(f"<li>{title}：{escape(citation['claim'])}</li>")
        safe += "<h2>参考资料</h2><ul>" + "".join(entries) + "</ul>"
    return (
        '<section style="font-family:system-ui,sans-serif;font-size:16px;color:#26362f;">'
        f'<h1 style="font-size:26px;line-height:1.45;">{escape(article["title"])}</h1>'
        f"{safe}</section>"
    )
