"""
The orchestrator: topic in, sourced report out.

Plan on a strong model, gather with free tools, synthesise on a strong model.
The expensive pools are touched twice - at the start and at the end - and every
page in between is fetched for nothing.
"""

from __future__ import annotations

import time
import json
import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .gateway import gateway
from .corpus import Corpus
from .ladder import read as read_page
from .extract import summarize

log = logging.getLogger("rig.research")

PLAN_SYSTEM = (
    "You plan web research. Given a topic, you produce focused search queries "
    "that together cover it. You return only JSON."
)

REPORT_SYSTEM = (
    "You write research reports from collected source material. Every claim is "
    "grounded in the sources given to you. You cite with the source URL. If the "
    "sources do not answer something, you say so plainly rather than guessing."
)


def plan(topic: str, n_queries: int = 6) -> list[str]:
    """Break a topic into search queries."""
    try:
        data = gateway.json(
            [
                {"role": "system", "content": PLAN_SYSTEM},
                {"role": "user", "content":
                 f'Topic: "{topic}"\n\nProduce {n_queries} search queries that '
                 f'together cover this topic well. Return '
                 f'{{"queries": ["...", "..."]}}'},
            ],
            job="plan",
        )
        queries = data.get("queries") if isinstance(data, dict) else data
        if isinstance(queries, list) and queries:
            return [str(q) for q in queries][:n_queries]
    except Exception as exc:
        log.warning("planning failed (%s) - falling back to the bare topic", exc)
    return [topic]


# One request at a time per host, with a gap between them. Politeness is what
# keeps a parallel fetcher from looking like an attack.
_HOST_LOCKS: dict[str, threading.Lock] = defaultdict(threading.Lock)
_HOST_LAST: dict[str, float] = {}
PER_HOST_DELAY = 1.5


def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0].lower()


def _polite_read(url: str):
    """Fetch a page, serialised per host with a delay between hits."""
    host = _host(url)
    with _HOST_LOCKS[host]:
        gap = time.monotonic() - _HOST_LAST.get(host, 0.0)
        if gap < PER_HOST_DELAY:
            time.sleep(PER_HOST_DELAY - gap)
        try:
            return read_page(url)
        finally:
            _HOST_LAST[host] = time.monotonic()


def gather(topic: str, queries: list[str], per_query: int = 5,
           max_pages: int = 25, workers: int = 6) -> Corpus:
    """
    Search, then fetch every result through the ladder into the corpus.

    Fetching runs in parallel - serial fetching was the single biggest cost in a
    run, since a tier 3 or 4 page can take 10-30 seconds on its own. Different
    hosts go at once; the same host is still hit one at a time, politely.
    """
    corpus = Corpus(topic)
    seen: set[str] = set()
    targets: list[dict] = []

    for q in queries:
        try:
            hits = gateway.search(q, max_results=per_query)
        except Exception as exc:
            log.warning("search failed for %r: %s", q, exc)
            continue
        for hit in hits:
            url = hit.get("url", "")
            if not url or url in seen or corpus.has(url):
                continue
            seen.add(url)
            targets.append(hit)
            if len(targets) >= max_pages:
                break
        if len(targets) >= max_pages:
            break

    if not targets:
        return corpus

    fetched = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_polite_read, h["url"]): h for h in targets}
        for fut in as_completed(futures):
            hit = futures[fut]
            url = hit["url"]
            try:
                page = fut.result()
            except Exception as exc:
                log.info("skipped %s - %s", url, exc)
                continue

            if not page.ok:
                log.info("skipped %s - %s", url, page.error)
                continue

            # The same article on three mirrors should be stored once.
            if corpus.has_content(page.markdown):
                log.info("duplicate content, skipping %s", url)
                continue

            # Search snippets are free context - keep them alongside the page.
            body = page.markdown
            if hit.get("snippet"):
                body = f"> Search snippet: {hit['snippet']}\n\n{body}"

            corpus.save_page(url, body, page.tier,
                             title=page.title or hit.get("title", ""))
            fetched += 1
            log.info("[%d/%d] %s (%s)", fetched, len(targets), url, page.tier)

    corpus.flush()
    return corpus


SUMMARISE_OVER = 15_000     # chars; longer pages are condensed before synthesis


def _condense(corpus: Corpus, topic: str, workers: int = 6) -> str:
    """
    Summarise long pages on the cheap pool before the expensive pool reads them.

    Without this, write_report sent the entire raw corpus to the synthesis
    model - slow, costly, and liable to overflow the context on a big gather.
    """
    paths = sorted(corpus.pages.glob("*.md"))
    if not paths:
        return ""

    def one(path: Path) -> str:
        text = path.read_text()
        if len(text) <= SUMMARISE_OVER:
            return text
        head, _, body = text.partition("---\n\n")
        try:
            return head + "---\n\n" + summarize(body, focus=topic, max_words=500)
        except Exception as exc:
            log.warning("summarise failed for %s (%s) - using a truncation", path.name, exc)
            return text[:SUMMARISE_OVER]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        parts = list(pool.map(one, paths))
    return "\n\n---\n\n".join(parts)


def write_report(corpus: Corpus, topic: str, max_chars: int = 300_000) -> str:
    """Condense the pile, then write the report."""
    material = _condense(corpus, topic)[:max_chars]
    if not material.strip():
        return f"# {topic}\n\nNo sources were collected. Nothing to report."

    return gateway.chat(
        [
            {"role": "system", "content": REPORT_SYSTEM},
            {"role": "user", "content":
             f"Topic: {topic}\n\nWrite a thorough report on this topic using only "
             f"the sources below. Use markdown headings. Cite source URLs inline. "
             f"End with a Sources list.\n\n=== SOURCES ===\n\n{material}"},
        ],
        job="synthesis",
        temperature=0.3,
    )


def research(topic: str, max_pages: int = 25, n_queries: int = 6) -> dict:
    """The whole loop. This is what the MCP tool `research` calls."""
    log.info("planning: %s", topic)
    queries = plan(topic, n_queries=n_queries)

    log.info("gathering (%d queries, up to %d pages)", len(queries), max_pages)
    corpus = gather(topic, queries, max_pages=max_pages)

    log.info("writing report")
    report = write_report(corpus, topic)

    path = corpus.dir / "report.md"
    path.write_text(f"# {topic}\n\n{report}\n")

    return {
        "ok": True,
        "topic": topic,
        "queries": queries,
        "report_path": str(path),
        "report": report,
        "corpus": corpus.summary(),
    }
