#!/usr/bin/env python3
"""
Research Rig - MCP server.

This is the whole point of the project: your AI does not learn Scrapling, or
gallery-dl, or which stealth engine beats Cloudflare. It sees nine tools, calls
them in plain English, and the rig picks the cheapest thing that works.

Run it:      uv run mcp_server.py
Register it: see README.md
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from rig.gateway import gateway
from rig import ladder, media, budget as budget_mod
from rig import linkedin as li
from rig import extract as extract_mod
from rig import agent as agent_mod
from rig import research as research_mod
from rig.corpus import Corpus, list_topics

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

mcp = MCPServer(
    "research-rig",
    instructions=(
        "Web access for research. Try tools in cost order: search first, then "
        "read, then extract; use harvest for bulk photos/video, and browse only "
        "when a site genuinely needs clicking. If a page needs a login, call "
        "login() and tell the user to sign in in the window that opens."
    ),
)


# ─────────────────────────────────────────────────────────── the one-call tool

@mcp.tool()
def research(topic: str, max_pages: int = 25) -> str:
    """
    Research a topic end to end and return a sourced report.

    Plans the searches, fetches every result with the cheapest method that
    works, saves everything to a corpus folder, then writes the report. Use
    this when the user asks a broad research question.

    Args:
        topic: What to research, in plain English.
        max_pages: Cap on pages fetched. 25 is a good default; raise for depth.
    """
    result = research_mod.research(topic, max_pages=max_pages)
    return (
        f"{result['report']}\n\n---\n"
        f"Saved to: {result['report_path']}\n"
        f"Corpus: {json.dumps(result['corpus'], indent=2)}"
    )


# ───────────────────────────────────────────────────────────────── primitives

@mcp.tool()
def search(query: str, max_results: int = 8) -> str:
    """
    Search the web. The cheapest way into any topic - always try this first.

    Returns titles, URLs and snippets. Follow up with `read` on whichever
    results look worth the full page.
    """
    try:
        hits = gateway.search(query, max_results=max_results)
    except Exception as exc:
        return f"Search failed: {exc}"
    if not hits:
        return "No results."
    return "\n\n".join(
        f"{i}. {h['title']}\n   {h['url']}\n   {h['snippet']}"
        for i, h in enumerate(hits, 1)
    )


@mcp.tool()
def read(url: str, max_chars: int = 20000) -> str:
    """
    Read a web page as clean markdown.

    Escalates automatically: plain HTTP, then a stealth browser for bot walls,
    then real Chrome with a saved login. You do not choose - it reports which
    tier it needed. If a site needs a login, call `login` for it first.

    Args:
        url: The page to read.
        max_chars: Truncate the returned text at this length.
    """
    page = ladder.read(url)
    if not page.ok:
        return (f"Could not read {url}\n{page.error}\n\n"
                "If this site needs a login, call login() for it first.")
    body = page.markdown[:max_chars]
    tail = "" if len(page.markdown) <= max_chars else \
        f"\n\n[truncated - {len(page.markdown)} chars total]"
    return f"# {page.title or url}\n_source: {url} · fetched at {page.tier}_\n\n{body}{tail}"


@mcp.tool()
def extract(url: str, want: str) -> str:
    """
    Pull structured JSON out of a page whose layout is messy.

    Use when `read` gives you the content but you need specific fields.

    Args:
        url: The page to extract from.
        want: e.g. "every event name, date and venue", or a JSON shape to fill.
    """
    result = extract_mod.extract(url, want)
    if not result.get("ok"):
        return f"Extraction failed for {url}: {result.get('error')}"
    return json.dumps(result["data"], indent=2)


@mcp.tool()
def harvest(target: str, topic: str, kind: str = "auto", limit: int = 0) -> str:
    """
    Download all photos or videos from a profile, gallery or channel.

    Covers roughly 390 sites - Instagram, X, Pinterest, Behance, Flickr - plus
    video from almost anywhere. No AI is involved and nothing is charged. Use
    this rather than `browse` whenever the goal is "get all their images".

    Args:
        target: A profile URL, gallery URL, or video/playlist URL.
        topic: Which corpus folder to file the media under.
        kind: auto, gallery or video.
        limit: Max items; 0 means everything.
    """
    corpus = Corpus(topic)
    result = media.harvest(target, corpus.media, kind=kind, limit=limit)
    corpus.record(kind="media", url=target, tool=result.get("tool"),
                  files_added=result.get("files_added", 0), ok=result.get("ok"))
    if not result.get("ok"):
        return (f"Harvest failed ({result.get('tool')}): "
                f"{result.get('error') or result.get('stderr')}")
    return (f"Downloaded {result['files_added']} files from {target}\n"
            f"Saved to: {result['dest']}")


@mcp.tool()
def login(site: str) -> str:
    """
    Open a real browser window so the user can sign in to a site by hand, once.

    Required for LinkedIn, Instagram and anything behind a wall. "Sign in with
    Google" cannot be automated - Google blocks automated browsers on purpose -
    so a human does it once and the session is reused forever after.

    Tell the user to complete the login in the window that opens.

    Args:
        site: e.g. "linkedin.com"
    """
    return ladder.login(site)


@mcp.tool()
def browse(goal: str, start_url: str = "") -> str:
    """
    Last resort: an agent that looks at the screen and clicks through a site.

    Slow and the most expensive tool here. Try `read`, `extract` and `harvest`
    first. Worth it only for flows that genuinely need interaction.

    Args:
        goal: What to accomplish, in plain English.
        start_url: Optional page to start from.
    """
    result = agent_mod.browse(goal, start_url or None)
    if not result.get("ok"):
        return f"Agent failed: {result.get('error')}"
    return result["result"]


@mcp.tool()
def read_pdf(path: str) -> str:
    """
    Read a PDF into text - reports, magazines, LinkedIn document posts.

    Args:
        path: Path to a PDF on disk.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        return f"No such file: {p}"
    return media.read_pdf(p)




# ────────────────────────────────────────────────── organisations & LinkedIn

@mcp.tool()
def harvest_org(name: str, instagram: str = "", linkedin: str = "",
                website: str = "", max_scrolls: int = 40) -> str:
    """
    Collect everything about one organisation: Instagram, LinkedIn and website.

    This is the tool for "get me everything about <org>". It runs each source
    with the right method - gallery-dl for Instagram photos, a scroll-and-extract
    pass for LinkedIn, the ladder for the website - and files it all under one
    corpus. Every source reports separately, so one failure never hides the rest.

    Pass whichever handles you know; leave the others blank to skip them.

    Args:
        name: The organisation, used as the corpus name.
        instagram: Handle or profile URL.
        linkedin: Company slug or full LinkedIn URL.
        website: Their website URL.
        max_scrolls: How far to scroll LinkedIn. 40 covers ~80-150 posts.
    """
    corpus = Corpus(name)
    report: list[str] = [f"# Harvest: {name}\n"]

    if instagram:
        target = instagram if instagram.startswith("http") else \
            f"https://www.instagram.com/{instagram.lstrip('@')}/"
        r = media.harvest(target, corpus.media, kind="gallery")
        corpus.record(kind="media", source="instagram", url=target,
                      files_added=r.get("files_added", 0), ok=r.get("ok"))
        report.append(f"## Instagram\n{'OK' if r.get('ok') else 'FAILED'} - "
                      f"{r.get('files_added', 0)} files\n"
                      f"{r.get('error') or r.get('stderr') or ''}")
    else:
        report.append("## Instagram\nskipped (no handle given)")

    if linkedin:
        url = li.company_url(linkedin)
        try:
            h = li.harvest_posts(url, max_scrolls=max_scrolls)
            corpus.save_page(url, h.to_markdown(), "linkedin",
                             title=f"{name} - LinkedIn posts")
            report.append(f"## LinkedIn\n{h.completeness}\n{h.budget_note}")
        except budget_mod.BudgetExceeded as exc:
            report.append(f"## LinkedIn\nHELD BACK - {exc}")
        except Exception as exc:
            report.append(f"## LinkedIn\nFAILED - {exc}\n"
                          f"If this is an auth error, run login('linkedin.com') first.")
    else:
        report.append("## LinkedIn\nskipped (no page given)")

    if website:
        page = ladder.read(website)
        if page.ok:
            corpus.save_page(website, page.markdown, page.tier, title=page.title)
            report.append(f"## Website\nOK - {len(page.markdown)} chars "
                          f"at {page.tier}")
        else:
            report.append(f"## Website\nFAILED - {page.error}")
    else:
        report.append("## Website\nskipped (no URL given)")

    corpus.flush()
    report.append(f"\n---\nCorpus: {json.dumps(corpus.summary(), indent=2)}")
    return "\n\n".join(report)


@mcp.tool()
def linkedin_posts(page: str, max_scrolls: int = 40, force: bool = False) -> str:
    """
    Scroll a LinkedIn company or profile page and collect every post.

    Unlike `read`, which takes only the first screen, this scrolls until the page
    stops loading new posts, then reports how complete the result is - so you can
    tell "got all 52" from "got the first 12 and LinkedIn throttled".

    Requires login('linkedin.com') to have been run once. Charged against the
    action budget, which refuses past a daily ceiling rather than risking a lock.

    Args:
        page: Company slug (e.g. "urja-sggscc") or a full LinkedIn URL.
        max_scrolls: Scroll passes. 40 covers roughly 80-150 posts.
        force: Continue even if the action budget says stop. Use deliberately.
    """
    url = li.company_url(page)
    try:
        h = li.harvest_posts(url, max_scrolls=max_scrolls, force=force)
    except budget_mod.BudgetExceeded as exc:
        return f"HELD BACK\n{exc}"
    except Exception as exc:
        return (f"Failed: {exc}\n\nIf this looks like an auth wall, run "
                f"login('linkedin.com') and sign in, then try again.")
    return h.to_markdown()


@mcp.tool()
def budget(site: str = "") -> str:
    """
    How many rate-limited actions have been spent, and how many are left.

    Check this before a big LinkedIn or Instagram job. LinkedIn locks accounts
    at roughly 150 actions per 24 hours; the rig stops at 120 to leave room.

    Args:
        site: Optional, e.g. "linkedin.com". Blank shows every site.
    """
    if site:
        return budget_mod.check(site).message()
    return budget_mod.report()


# ───────────────────────────────────────────────────────────────────── corpus

@mcp.tool()
def corpus(topic: str = "") -> str:
    """
    See what has already been collected.

    With no topic, lists every research folder. With a topic, summarises that
    one. Check here before re-fetching something.

    Args:
        topic: Optional topic name.
    """
    if not topic:
        topics = list_topics()
        if not topics:
            return "Nothing collected yet."
        return "\n".join(f"- {t['topic']} ({t['items']} items) - {t['dir']}"
                         for t in topics)
    return json.dumps(Corpus(topic).summary(), indent=2)


@mcp.tool()
def status() -> str:
    """
    Check the gateway: is it up, which models are live, which tools installed.

    Call this if anything behaves oddly.
    """
    import shutil

    models = gateway.models(refresh=True)
    pools: dict[str, int] = {}
    for m in models:
        pool = m.split("/")[0] if "/" in m else "alias"
        pools[pool] = pools.get(pool, 0) + 1

    tools = {t: bool(shutil.which(t)) for t in ("gallery-dl", "yt-dlp")}
    for mod in ("scrapling", "browser_use", "pdfplumber"):
        try:
            __import__(mod)
            tools[mod] = True
        except ImportError:
            tools[mod] = False

    return json.dumps({
        "gateway": gateway.base_url,
        "models_live": len(models),
        "by_pool": pools,
        "picked_for": {job: gateway.pick(job)
                       for job in ("bulk", "extract", "plan", "synthesis")},
        "tools_installed": tools,
        "health": gateway.health(),
    }, indent=2)


if __name__ == "__main__":
    mcp.run()
