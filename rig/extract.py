"""
Tier 5: the page loaded fine, but its structure is chaos. The model reads it.

Costs about a cent a page, so this is not the default - it is what you reach for
when writing selectors by hand would be genuinely painful.
"""

from __future__ import annotations

import logging
from typing import Any

from .gateway import gateway
from .ladder import read as read_page

log = logging.getLogger("rig.extract")

CHUNK = 60_000   # comfortably inside a single request on any pool


def extract(url: str, want: str | dict, markdown: str | None = None) -> dict:
    """
    Pull structured data out of a page.

    `want` is either a plain-English description of what you need, or a dict
    showing the JSON shape you want back. The shape form is much more reliable.
    """
    if markdown is None:
        page = read_page(url)
        if not page.ok:
            return {"ok": False, "url": url, "error": page.error}
        markdown = page.markdown
        tier = page.tier
    else:
        tier = "supplied"

    if isinstance(want, dict):
        instruction = (
            "Fill in this JSON shape from the page. Use null for anything the "
            "page does not state. Invent nothing.\n\n"
            f"{want}"
        )
    else:
        instruction = (
            f"Extract the following from the page, as JSON: {want}\n"
            "Use null for anything absent. Invent nothing."
        )

    body = markdown[:CHUNK]
    try:
        data = gateway.json(
            [
                {"role": "system", "content":
                 "You extract structured data from web pages. You return only "
                 "JSON. You never invent facts that are not in the page."},
                {"role": "user", "content": f"{instruction}\n\n---\n\n{body}"},
            ],
            job="extract",
        )
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}

    return {"ok": True, "url": url, "tier": tier, "data": data}


def summarize(text: str, focus: str = "", max_words: int = 400) -> str:
    """Condense a long page. Used to keep a big corpus inside a context window."""
    ask = f"Summarise the following in at most {max_words} words."
    if focus:
        ask += f" Focus on: {focus}."
    ask += " Keep concrete facts, names, dates and figures. Drop navigation and boilerplate."

    return gateway.chat(
        [{"role": "user", "content": f"{ask}\n\n---\n\n{text[:CHUNK]}"}],
        job="bulk",
    )
