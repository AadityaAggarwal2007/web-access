"""
Media harvesting: photos and video, with no AI involved at all.

gallery-dl covers ~390 sites (Instagram, X, Pinterest, Behance, Flickr and the
rest); yt-dlp covers video almost everywhere. Both are deterministic - they get
everything and tell you the count, where an agent quietly stops early.
"""

from __future__ import annotations

import os
import json
import shutil
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("rig.media")

VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "tiktok.com", "dailymotion.com")

# Sites that serve nothing at all to a logged-out client. Instagram replies
# "Requested user could not be found" to anonymous requests, which reads like a
# missing account rather than a missing login - hence this list.
NEEDS_COOKIES = ("instagram.com", "facebook.com", "x.com", "twitter.com")

# Where cookies come from. A browser name uses that browser's live cookie store;
# a path is a Netscape-format cookies.txt exported once.
COOKIE_SOURCE = os.environ.get("RIG_COOKIES", "")


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def harvest(target: str, dest: Path, kind: str = "auto", limit: int = 0) -> dict:
    """
    Download everything at `target` into `dest`.

    target may be a URL or a bare handle. kind is auto | gallery | video.
    Returns a summary rather than raising, so a partial failure in one source
    never takes down a whole research run.
    """
    dest.mkdir(parents=True, exist_ok=True)
    before = _count(dest)

    if kind == "auto":
        kind = "video" if any(h in target for h in VIDEO_HOSTS) else "gallery"

    if kind == "video":
        result = _yt_dlp(target, dest, limit)
    else:
        result = _gallery_dl(target, dest, limit)

    result["files_added"] = _count(dest) - before
    result["dest"] = str(dest)
    return result


SESSIONS = Path.home() / "Desktop" / "research-rig" / "sessions"


def _rig_profile(target: str) -> Path | None:
    """
    The browser profile login() created for this site, if there is one.

    This is what makes login() a single flow for every site: sign in once in the
    window it opens, and gallery-dl reads the cookies straight out of that same
    profile - no separate cookie export, and your real browser is never touched.
    """
    host = target.split("//", 1)[-1].split("/", 1)[0].lower()
    if host.startswith("www."):
        host = host[4:]
    for candidate in (host, ".".join(host.split(".")[-2:])):
        d = SESSIONS / candidate
        if (d / "Default" / "Cookies").is_file():
            return d
    return None


def _cookie_args(target: str) -> list[str]:
    """
    Cookies for sites that show nothing when logged out.

    Preference order: an explicit RIG_COOKIES (a browser name, or a path to a
    cookies.txt), then the profile login() made for this site.
    """
    if not any(h in target for h in NEEDS_COOKIES):
        return []
    if COOKIE_SOURCE:
        if Path(COOKIE_SOURCE).expanduser().is_file():
            return ["--cookies", str(Path(COOKIE_SOURCE).expanduser())]
        return ["--cookies-from-browser", COOKIE_SOURCE]
    profile = _rig_profile(target)
    if profile:
        return ["--cookies-from-browser", f"chromium:{profile}"]
    return []


def _needs_cookies(target: str) -> bool:
    return (any(h in target for h in NEEDS_COOKIES)
            and not COOKIE_SOURCE and _rig_profile(target) is None)


def _gallery_dl(target: str, dest: Path, limit: int) -> dict:
    if not _have("gallery-dl"):
        return {"ok": False, "tool": "gallery-dl",
                "error": "gallery-dl not installed. Run: uv sync"}

    if _needs_cookies(target):
        return {"ok": False, "tool": "gallery-dl", "error": (
            "This site serves nothing to a logged-out client, and no cookie "
            "source is set. Instagram in particular replies 'user could not be "
            "found', which looks like a missing account but is a missing login.\n"
            "Fix, easiest first:\n"
            "  1. login('instagram.com') - sign in once in the window it opens, "
            "then re-run this. Nothing else to configure.\n"
            "  2. export RIG_COOKIES=chrome - use your real browser's cookies.\n"
            "  3. export RIG_COOKIES=/path/to/cookies.txt")}

    cmd = ["gallery-dl", "--dest", str(dest), "--write-metadata", "--no-part"]
    cmd += _cookie_args(target)
    if limit:
        cmd += ["--range", f"1-{limit}"]
    cmd.append(target)
    return _run(cmd, "gallery-dl")


def _yt_dlp(target: str, dest: Path, limit: int) -> dict:
    if not _have("yt-dlp"):
        return {"ok": False, "tool": "yt-dlp",
                "error": "yt-dlp not installed. Run: uv sync"}

    cmd = [
        "yt-dlp",
        "-o", str(dest / "%(title).80s-%(id)s.%(ext)s"),
        "--write-info-json", "--write-thumbnail",
        "--write-subs", "--sub-langs", "en.*", "--no-warnings",
    ]
    cmd += _cookie_args(target)
    if limit:
        cmd += ["--playlist-end", str(limit)]
    cmd.append(target)
    return _run(cmd, "yt-dlp")


def _run(cmd: list[str], tool: str) -> dict:
    log.info("%s: %s", tool, " ".join(cmd[:4]) + " ...")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return {"ok": False, "tool": tool, "error": "timed out after 30 minutes"}

    ok = proc.returncode == 0
    return {
        "ok": ok,
        "tool": tool,
        "returncode": proc.returncode,
        "stderr": proc.stderr.strip()[-800:] if not ok else "",
    }


def _count(d: Path) -> int:
    return sum(1 for p in d.rglob("*") if p.is_file() and not p.name.startswith("."))


def read_pdf(path: Path, max_chars: int = 200_000) -> str:
    """
    Pull the text out of a PDF - how organisations publish magazines and annual
    reports on LinkedIn. Falls back to PyMuPDF when pdfplumber struggles.
    """
    try:
        import pdfplumber
        chunks = []
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                t = page.extract_text() or ""
                if t.strip():
                    chunks.append(f"## Page {i}\n\n{t}")
                if sum(len(c) for c in chunks) > max_chars:
                    break
        text = "\n\n".join(chunks)
        if text.strip():
            return text
    except Exception as exc:
        log.warning("pdfplumber failed on %s: %s", path, exc)

    try:
        import fitz                                   # PyMuPDF
        doc = fitz.open(path)
        out = [f"## Page {i}\n\n{p.get_text()}" for i, p in enumerate(doc, 1)]
        return "\n\n".join(out)[:max_chars]
    except Exception as exc:
        return f"[could not read PDF {path.name}: {exc}]"
