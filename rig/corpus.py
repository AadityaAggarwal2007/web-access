"""
Where everything the rig collects lands.

One folder per topic. Text as markdown, media as files, and a manifest that
records where each item came from and which tier fetched it - so a later run can
tell what it already has, and a report can cite its sources.
"""

from __future__ import annotations

import os
import re
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("RIG_CORPUS", Path.home() / "Desktop" / "research-rig" / "corpus"))


def slug(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:limit] or "untitled").strip("-")


class Corpus:
    """A single topic's collected material."""

    def __init__(self, topic: str):
        self.topic = topic
        self.dir = ROOT / slug(topic)
        self.pages = self.dir / "pages"
        self.media = self.dir / "media"
        self.files = self.dir / "files"
        for d in (self.pages, self.media, self.files):
            d.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / "manifest.json"
        # Held in memory: has() ran a full manifest read per URL, which made a
        # gather O(n^2) in disk I/O.
        self._m = self._read_manifest()
        self._urls = {i.get("url") for i in self._m["items"] if i.get("url")}
        self._hashes = {i.get("sha") for i in self._m["items"] if i.get("sha")}

    # ----------------------------------------------------------------- manifest

    def _read_manifest(self) -> dict:
        if self.manifest_path.exists():
            try:
                return json.loads(self.manifest_path.read_text())
            except Exception:
                pass
        return {"topic": self.topic, "created": _now(), "items": []}

    def manifest(self) -> dict:
        return self._m

    def flush(self) -> None:
        """Write the manifest once, rather than on every recorded item."""
        self.manifest_path.write_text(json.dumps(self._m, indent=2))

    def record(self, flush: bool = True, **item: Any) -> dict:
        item.setdefault("collected", _now())
        self._m["items"].append(item)
        if item.get("url"):
            self._urls.add(item["url"])
        if item.get("sha"):
            self._hashes.add(item["sha"])
        if flush:
            self.flush()
        return item

    def has(self, url: str) -> bool:
        return url in self._urls

    def has_content(self, text: str) -> bool:
        """Same article on three mirrors should be stored once, not three times."""
        return _sha(text) in self._hashes

    # -------------------------------------------------------------------- pages

    def save_page(self, url: str, markdown: str, tier: str, title: str = "") -> Path:
        name = f"{hashlib.sha1(url.encode()).hexdigest()[:10]}-{slug(title or url, 40)}.md"
        path = self.pages / name
        header = (
            f"---\nurl: {url}\ntitle: {title}\ntier: {tier}\n"
            f"collected: {_now()}\n---\n\n"
        )
        path.write_text(header + markdown)
        self.record(kind="page", url=url, title=title, tier=tier, sha=_sha(markdown),
                    path=str(path.relative_to(self.dir)), chars=len(markdown))
        return path

    # -------------------------------------------------------------------- stats

    def summary(self) -> dict:
        items = self.manifest()["items"]
        kinds: dict[str, int] = {}
        for i in items:
            kinds[i.get("kind", "?")] = kinds.get(i.get("kind", "?"), 0) + 1
        return {
            "topic": self.topic,
            "dir": str(self.dir),
            "total_items": len(items),
            "by_kind": kinds,
            "pages_chars": sum(i.get("chars", 0) for i in items if i.get("kind") == "page"),
            "media_files": _count_files(self.media),
        }

    def read_all_text(self, max_chars: int = 400_000) -> str:
        """Every collected page, concatenated - the pile the AI reads at the end."""
        out, used = [], 0
        for p in sorted(self.pages.glob("*.md")):
            t = p.read_text()
            if used + len(t) > max_chars:
                out.append(t[: max_chars - used])
                break
            out.append(t)
            used += len(t)
        return "\n\n---\n\n".join(out)


def _sha(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8", "ignore")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _count_files(d: Path) -> int:
    return sum(1 for p in d.rglob("*") if p.is_file())


def list_topics() -> list[dict]:
    if not ROOT.exists():
        return []
    out = []
    for d in sorted(ROOT.iterdir()):
        mf = d / "manifest.json"
        if mf.is_file():
            m = json.loads(mf.read_text())
            out.append({"topic": m.get("topic", d.name), "dir": str(d),
                        "items": len(m.get("items", []))})
    return out
