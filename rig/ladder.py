"""
The escalation ladder: fetch a page with the cheapest thing that works.

Tier 2  plain HTTP with a real browser's TLS fingerprint   free
Tier 3  stealth browser (Camoufox) - clears Cloudflare      free
Tier 4  real Chrome with a saved login                      free

Callers just say read(url). The ladder decides how hard to try, and reports back
which tier actually succeeded so you can see what a site costs you.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("rig.ladder")

SESSIONS = Path.home() / "Desktop" / "research-rig" / "sessions"

# Sites known to need a logged-in real browser. Anything here skips straight to
# tier 4 rather than wasting two failed attempts first. Matched on the hostname,
# never as a substring - "x.com" is inside "netflix.com", "fox.com" and
# "linux.com", all of which would otherwise pop a browser window.
NEEDS_LOGIN = ("linkedin.com", "x.com", "twitter.com", "facebook.com", "instagram.com")


def _hostname(url: str) -> str:
    h = url.split("//", 1)[-1].split("/", 1)[0].split(":")[0].lower()
    return h[4:] if h.startswith("www.") else h


def _needs_login(url: str) -> bool:
    host = _hostname(url)
    return any(host == d or host.endswith("." + d) for d in NEEDS_LOGIN)


@dataclass
class Page:
    url: str
    markdown: str
    title: str
    tier: str
    ok: bool = True
    error: str = ""

    def __len__(self) -> int:
        return len(self.markdown)


def _too_thin(md: str) -> bool:
    """A JS shell or bot wall returns almost no prose. Some real pages are just
    short, so this only means 'worth trying a harder tier', never 'failed'."""
    return len(md.strip()) < 500


def _blocked(md: str) -> bool:
    low = md.lower()
    return any(s in low for s in (
        "just a moment", "checking your browser", "enable javascript",
        "access denied", "are you a robot", "unusual traffic",
    ))


def read(url: str, max_tier: int = 4, force_tier: int | None = None,
         headless: bool = True) -> Page:
    """
    Fetch one page as markdown, escalating only as far as necessary.

    max_tier caps how hard the ladder will try. force_tier skips straight to one
    rung, which is what you want when you already know a site is difficult.
    headless=False shows the browser, which only login() normally wants.
    """
    try:
        from scrapling.fetchers import Fetcher, StealthyFetcher, DynamicFetcher
    except ImportError as exc:                       # pragma: no cover
        return Page(url, "", "", "none", ok=False,
                    error=f"Scrapling not installed ({exc}). Run: uv sync")

    if force_tier is None and _needs_login(url):
        force_tier = 4

    tiers = [force_tier] if force_tier else [t for t in (2, 3, 4) if t <= max_tier]
    last_error = ""
    best: Page | None = None       # thin-but-real result, kept in case nothing beats it

    for tier in tiers:
        try:
            if tier == 2:
                page = Fetcher.get(url, stealthy_headers=True)
            elif tier == 3:
                page = StealthyFetcher.fetch(url, headless=True, network_idle=True)
            else:
                page = DynamicFetcher.fetch(
                    url,
                    headless=headless,   # windowed only when a human must watch
                    network_idle=True,
                    user_data_dir=str(_profile_for(url)),
                )
        except Exception as exc:
            last_error = f"tier {tier}: {exc}"
            log.warning("tier %d failed for %s: %s", tier, url, exc)
            continue

        md = _to_markdown(page)
        title = _title(page)

        if _blocked(md):
            last_error = f"tier {tier}: blocked ({len(md)} chars)"
            log.info("tier %d blocked for %s - escalating", tier, url)
            continue

        if _too_thin(md):
            # Could be a JS shell, could just be a small page. Remember it and
            # try harder; if nothing better turns up, this is the honest answer.
            last_error = f"tier {tier}: only {len(md)} chars"
            if best is None and md.strip():
                best = Page(url, md, title, f"tier{tier}")
            log.info("tier %d thin for %s (%d chars) - escalating", tier, url, len(md))
            continue

        log.info("tier %d ok for %s (%d chars)", tier, url, len(md))
        return Page(url, md, title, f"tier{tier}")

    if best is not None:
        log.info("returning thin result for %s from %s", url, best.tier)
        return best

    return Page(url, "", "", "none", ok=False,
                error=last_error or "all tiers failed")


def _to_markdown(page) -> str:
    """Scrapling's LLM-ready markdown, falling back to plain text if the
    optional markdownify extra is missing."""
    value = getattr(page, "markdown", None)
    if value is not None:
        try:
            return value() if callable(value) else str(value)
        except Exception as exc:                     # e.g. scrapling[rag] absent
            log.debug("markdown conversion unavailable (%s) - using text", exc)

    for attr in ("get_all_text", "text"):
        value = getattr(page, attr, None)
        if value is not None:
            try:
                return value() if callable(value) else str(value)
            except Exception:
                continue
    return str(page)


def _title(page) -> str:
    for getter in (lambda: page.css_first("title::text"),
                   lambda: page.css_first("title").text,
                   lambda: page.css_first("h1::text")):
        try:
            value = getter()
            if value:
                return str(value).strip()[:200]
        except Exception:
            continue
    try:
        import re
        m = re.search(r"<title[^>]*>(.*?)</title>",
                      str(page.html_content), re.I | re.S)
        if m:
            return m.group(1).strip()[:200]
    except Exception:
        pass
    return ""


def _profile_for(url: str) -> Path:
    """One persistent Chrome profile per site, so logins survive between runs."""
    host = url.split("//", 1)[-1].split("/", 1)[0].replace("www.", "")
    d = SESSIONS / host
    d.mkdir(parents=True, exist_ok=True)
    return d


def login(site: str, minutes: int = 5) -> str:
    """
    Open a real browser window so you can log in by hand, once.

    This is the only correct way in - "Sign in with Google" cannot be automated,
    Google blocks automated browsers on purpose. You log in; the profile keeps
    the session; every later run is already signed in.
    """
    try:
        from scrapling.fetchers import DynamicFetcher
    except ImportError as exc:
        return f"Scrapling not installed ({exc}). Run: uv sync"

    url = site if site.startswith("http") else f"https://{site}"
    profile = _profile_for(url)

    DynamicFetcher.fetch(
        url,
        headless=False,
        user_data_dir=str(profile),
        wait=minutes * 60_000,   # human time: password, 2FA, captcha, email code
        network_idle=True,
    )
    return (
        f"Session for {site} saved to {profile}.\n"
        "Log in inside that window if you have not already - it stays signed in "
        "for future runs."
    )
