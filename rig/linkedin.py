"""
LinkedIn: scroll, extract, and say honestly how much was missed.

Nothing off-the-shelf covers LinkedIn - gallery-dl's 390 sites do not include
it - so the pagination is written here. Two things make this different from a
plain fetch:

  * it scrolls until the page stops growing, instead of taking the first screen
  * it reports a completeness estimate, so a caller can tell "I got all 52" from
    "I got the first 12 and the page quietly stopped loading"

Every load is charged against the action budget, which refuses to continue past
a daily ceiling rather than letting you find out from a locked account.
"""

from __future__ import annotations

import re
import time
import html as _html
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import budget
from .ladder import _profile_for

log = logging.getLogger("rig.linkedin")

# How long to let the page settle after each scroll. Deliberately unhurried -
# a fast scroll is one of the clearest bot signals there is.
SCROLL_PAUSE_MS = 1800
MAX_SCROLLS = 60
STALL_LIMIT = 3          # consecutive scrolls with no new posts before stopping


@dataclass
class Post:
    text: str = ""
    images: list[str] = field(default_factory=list)
    date: str = ""
    link: str = ""
    documents: list[str] = field(default_factory=list)
    author: str = ""


@dataclass
class Harvest:
    url: str
    posts: list[Post]
    scrolls: int
    claimed_total: int | None      # what the page says it has, if it says
    stalled: bool                  # stopped because nothing new loaded
    budget_note: str = ""

    @property
    def completeness(self) -> str:
        got = len(self.posts)
        if self.claimed_total:
            pct = round(100 * got / self.claimed_total)
            if pct >= 95:
                return f"COMPLETE - collected {got} of ~{self.claimed_total} posts"
            return (f"INCOMPLETE - collected {got} of ~{self.claimed_total} posts "
                    f"({pct}%). Run again to continue; LinkedIn may have throttled.")
        if self.stalled and self.scrolls >= MAX_SCROLLS:
            return (f"UNCERTAIN - collected {got} posts and hit the scroll limit. "
                    f"There are probably more; run again.")
        if self.stalled:
            return (f"LIKELY COMPLETE - collected {got} posts; the page stopped "
                    f"loading new ones.")
        return f"UNCERTAIN - collected {got} posts."

    def to_markdown(self) -> str:
        head = (f"# LinkedIn: {self.url}\n\n"
                f"_{self.completeness}_\n"
                f"_{self.scrolls} scrolls_\n")
        if self.budget_note:
            head += f"_{self.budget_note}_\n"
        body = []
        for i, p in enumerate(self.posts, 1):
            block = [f"\n## Post {i}"]
            if p.date:
                block.append(f"_{p.date}_")
            if p.text:
                block.append(p.text)
            if p.images:
                block.append("\n".join(f"![image]({u})" for u in p.images))
            if p.documents:
                block.append("Documents: " + ", ".join(p.documents))
            if p.link:
                block.append(f"[permalink]({p.link})")
            body.append("\n\n".join(block))
        return head + "\n".join(body)


def _scroller(max_scrolls: int, pause_ms: int, stats: dict):
    """
    A page_action for Scrapling: scroll to the bottom repeatedly until the post
    count stops rising.

    The count goes into `stats`, a dict the caller owns - attributes set on the
    page object do not survive Scrapling's response wrapper, so an earlier
    version always reported zero scrolls.
    """
    def action(page):
        seen, stalls, scrolls = 0, 0, 0
        for _ in range(max_scrolls):
            page.mouse.wheel(0, 20000)
            page.wait_for_timeout(pause_ms)
            scrolls += 1

            # Dismiss the sign-in modal LinkedIn throws at scrolling sessions.
            try:
                btn = page.query_selector("button[aria-label*='Dismiss']")
                if btn:
                    btn.click()
                    page.wait_for_timeout(400)
            except Exception:
                pass

            count = len(page.query_selector_all(
                "div.feed-shared-update-v2, li.profile-creator-shared-feed-update__container, "
                "div[data-urn], article"
            ))
            if count <= seen:
                stalls += 1
                if stalls >= STALL_LIMIT:
                    break
            else:
                stalls = 0
                seen = count
        stats["scrolls"] = scrolls
        stats["stalled"] = stalls >= STALL_LIMIT
        return page
    return action


def _claimed_total(html: str) -> int | None:
    """LinkedIn often states a post or follower count in the page text."""
    for pat in (r"([\d,]+)\s+posts?\b", r"\"numPosts\"\s*:\s*(\d+)"):
        m = re.search(pat, html, re.I)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def _is_authwall(html: str) -> bool:
    low = html.lower()
    return ("welcome back" in low and "password" in low) or "authwall" in low


# Interface chrome LinkedIn puts inside every post card. None of it is content.
_NOISE = re.compile(
    r"^(feed post number \d+|verified|• ?3rd\+?|• ?2nd|• ?1st|following|follow|"
    r"like|comment|repost|send|show more|see more|…more|"
    r"visible to anyone on or off linkedin|"
    r"\d[\d,]* (?:reactions?|comments?|reposts?|likes?)|"
    r"activate to view larger image.*|report this post.*)$",
    re.I,
)


def _clean(text: str) -> str:
    """
    Strip LinkedIn's duplicated chrome and leave the post body.

    LinkedIn repeats the author name and headline two or three times inside each
    card for screen readers, and wraps the body in interface labels. Raw text is
    mostly boilerplate, which wastes tokens and buries the actual content.
    """
    lines, seen_run = [], ""
    for raw in text.splitlines():
        line = " ".join(raw.split()).strip()
        if not line or _NOISE.match(line):
            continue
        # LinkedIn emits the same string 2-3x in a row; keep the first.
        if line == seen_run:
            continue
        seen_run = line
        lines.append(line)

    # Drop any line that appears more than twice overall - that is chrome, not prose.
    counts: dict[str, int] = {}
    for l in lines:
        counts[l] = counts.get(l, 0) + 1
    out = [l for l in lines if counts[l] <= 2 or len(l) > 120]

    # Collapse a repeated leading author block.
    while len(out) > 1 and out[0] == out[1]:
        out.pop(0)
    return "\n".join(out).strip()


def _body_text(node) -> str:
    """Prefer LinkedIn's own post-body containers over the whole card."""
    for sel in ("div.update-components-text",
                "div.feed-shared-inline-show-more-text",
                "span.break-words"):
        try:
            found = node.css(sel)
        except Exception:
            found = []
        if found:
            joined = "\n".join((f.get_all_text() or "") for f in found)
            cleaned = _clean(joined)
            if cleaned:
                return cleaned
    return _clean(node.get_all_text() or "")


def _first_line(node, selectors: tuple[str, ...]) -> str:
    for sel in selectors:
        try:
            f = node.css_first(sel)
        except Exception:
            continue
        if f is None:
            continue
        cleaned = _clean(f.get_all_text() or "")
        if cleaned:
            return cleaned.splitlines()[0].strip(" •")
    return ""


def _author(node) -> str:
    """Author lives in the actor block, which _body_text deliberately excludes."""
    return _first_line(node, (
        "span.update-components-actor__title",
        "span.update-components-actor__name",
        "a.update-components-actor__meta-link span[aria-hidden='true']",
    ))


# "2d", "1mo", "3 months ago", "12 Aug 2026" - LinkedIn uses several shapes.
_DATE = re.compile(
    r"\b(\d+\s*(?:s|m|h|d|w|mo|yr)\b|\d+\s+(?:second|minute|hour|day|week|month|year)s?\s+ago"
    r"|\d{1,2}\s+\w{3,9}\s+\d{4})",
    re.I,
)


def _date(node) -> str:
    """
    Read the timestamp off the card's sub-description.

    It cannot come from the post body - _body_text strips the actor block, which
    is where the date lives, so an earlier version returned empty for every post.
    """
    line = _first_line(node, (
        "span.update-components-actor__sub-description",
        "time",
        "span.update-components-actor__description",
    ))
    if line:
        m = _DATE.search(line)
        if m:
            return m.group(1).strip()
    try:
        m = _DATE.search(node.get_all_text() or "")
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def _extract_posts(page) -> list[Post]:
    """Pull posts out of the fully-scrolled DOM."""
    posts: list[Post] = []
    selectors = (
        "div.feed-shared-update-v2",
        "li.profile-creator-shared-feed-update__container",
        "div[data-urn]",
        "article",
    )
    nodes = []
    for sel in selectors:
        try:
            nodes = page.css(sel)
        except Exception:
            nodes = []
        if nodes:
            break

    for node in nodes:
        try:
            html = str(node.html_content)
        except Exception:
            continue

        text = _body_text(node)
        # Unescape: LinkedIn writes &amp; in the src, and the signed URL carries
        # its signature in &v=beta&t=... - leave the entities in and the CDN
        # returns 403 on every image.
        images = [_html.unescape(u)
                  for u in re.findall(r'src="(https://media[^"]+)"', html)
                  if "profile-displayphoto" not in u]
        docs = [_html.unescape(u) for u in
                re.findall(r'href="(https://[^"]*document[^"]*)"', html)]
        docs += [_html.unescape(u) for u in
                 re.findall(r'"(https://media\.licdn\.com/dms/document/[^"]+)"', html)]
        if not docs and ('document-s-container' in html
                         or 'carousel-container' in html
                         or 'native-document' in html):
            docs.append("(document post detected - open the permalink to download)")
        date = _date(node)
        link = ""
        m = re.search(r'href="(https://www\.linkedin\.com/(?:feed/update|posts)/[^"?]+)', html)
        if m:
            link = _html.unescape(m.group(1))
        else:
            # Every card carries its own id; build the permalink from that.
            m = re.search(r'data-urn="(urn:li:activity:\d+)"', html) or \
                re.search(r'(urn:li:activity:\d+)', html)
            if m:
                link = f"https://www.linkedin.com/feed/update/{m.group(1)}/"

        if text or images:
            posts.append(Post(text=text[:6000], images=images[:20],
                              date=date, link=link, documents=docs[:5],
                              author=_author(node)))
    return posts


def harvest_posts(url: str, max_scrolls: int = MAX_SCROLLS,
                  force: bool = False) -> Harvest:
    """
    Scroll a LinkedIn page and collect every post it will give up.

    Requires login('linkedin.com') to have been run once. Charged against the
    action budget - this refuses rather than risking the account silently.
    """
    try:
        from scrapling.fetchers import DynamicFetcher
    except ImportError as exc:
        raise RuntimeError(f"Scrapling not installed ({exc}). Run: uv sync") from exc

    # Each scroll loads more content, so budget for the whole session up front.
    spend = budget.spend(url, n=max(1, max_scrolls // 4), force=force)
    note = spend.message()

    stats: dict = {"scrolls": 0, "stalled": False}
    page = DynamicFetcher.fetch(
        url,
        headless=False,                 # LinkedIn is markedly harsher on headless
        network_idle=True,
        timeout=180_000,
        user_data_dir=str(_profile_for(url)),
        page_action=_scroller(max_scrolls, SCROLL_PAUSE_MS, stats),
    )

    try:
        html = str(page.html_content)
    except Exception:
        html = ""

    if _is_authwall(html):
        raise RuntimeError(
            "Not signed in - LinkedIn served a login wall instead of the page. "
            "Run login('linkedin.com'), sign in, and let the window close itself.")

    posts = _extract_posts(page)

    # On an activity feed every card has the same author and the actor block is
    # often absent, so fall back to the profile name from the page title.
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    owner = ""
    if m:
        # Titles look like "Activity | Urja SGGSCC" or "Urja SGGSCC | LinkedIn".
        # Take the longest segment that is not a LinkedIn section label.
        parts = [x.strip() for x in re.split(r"\s*\|\s*", m.group(1).strip()) if x.strip()]
        parts = [x for x in parts
                 if x.lower() not in ("linkedin", "activity", "posts", "articles", "feed")]
        owner = max(parts, key=len) if parts else ""
    if owner:
        for post in posts:
            if not post.author:
                post.author = owner

    scrolls = int(stats.get("scrolls", 0))
    stalled = bool(stats.get("stalled", False))

    result = Harvest(url=url, posts=posts, scrolls=scrolls,
                     claimed_total=_claimed_total(html), stalled=stalled,
                     budget_note=note)
    log.info("%s -> %d posts, %d scrolls. %s",
             url, len(posts), scrolls, result.completeness)
    return result


def company_url(name_or_url: str) -> str:
    """Accept 'urja-sggscc', a full URL, or a company slug."""
    s = name_or_url.strip()
    if s.startswith("http"):
        return s
    slug = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return f"https://www.linkedin.com/company/{slug}/posts/"
