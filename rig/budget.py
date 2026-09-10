"""
Action budget: the thing that tells you before you get banned, not after.

LinkedIn's practical ceiling is around 150 actions per account per 24 hours, and
more than 100-150 profile loads in an hour is the single most common trigger for
a lock. Nothing in a scraper naturally knows this, so the count lives here.

Every action against a rate-limited site goes through spend(). It warns as you
approach the limit and refuses once you cross it - you have to pass
force=True to keep going, which is a decision, not an accident.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("rig.budget")

STATE = Path.home() / "Desktop" / "research-rig" / "sessions" / "budget.json"

# Per-site daily ceilings. Deliberately below the real limits, because the real
# limits are where the ban starts, not where it is safe to sit.
LIMITS = {
    "linkedin.com": {"day": 120, "hour": 60},   # real ceiling ~150/day
    "instagram.com": {"day": 200, "hour": 80},
    "x.com": {"day": 200, "hour": 80},
    "facebook.com": {"day": 200, "hour": 80},
}
DEFAULT = {"day": 1000, "hour": 400}


class BudgetExceeded(RuntimeError):
    """Raised when an action would cross a site's daily or hourly ceiling."""


@dataclass
class Spend:
    site: str
    day_used: int
    day_limit: int
    hour_used: int
    hour_limit: int

    @property
    def day_left(self) -> int:
        return max(0, self.day_limit - self.day_used)

    @property
    def hour_left(self) -> int:
        return max(0, self.hour_limit - self.hour_used)

    @property
    def level(self) -> str:
        pct = self.day_used / self.day_limit if self.day_limit else 0
        if pct >= 1.0 or self.hour_left == 0:
            return "STOP"
        if pct >= 0.8:
            return "WARN"
        if pct >= 0.5:
            return "NOTICE"
        return "OK"

    def message(self) -> str:
        if self.level == "STOP":
            return (f"STOP - {self.site}: {self.day_used}/{self.day_limit} actions "
                    f"today, {self.hour_used}/{self.hour_limit} this hour. "
                    f"Continuing now risks an account lock. Resume tomorrow, or "
                    f"pass force=True if you accept the risk.")
        if self.level == "WARN":
            return (f"WARNING - {self.site}: {self.day_used}/{self.day_limit} "
                    f"actions today. {self.day_left} left before the rig stops. "
                    f"Consider finishing this session and resuming tomorrow.")
        if self.level == "NOTICE":
            return (f"{self.site}: {self.day_used}/{self.day_limit} actions today "
                    f"({self.day_left} left).")
        return f"{self.site}: {self.day_used}/{self.day_limit} today."


def _host(target: str) -> str:
    h = target.split("//", 1)[-1].split("/", 1)[0].lower()
    if h.startswith("www."):
        h = h[4:]
    return h


def _limits_for(host: str) -> dict:
    for site, lim in LIMITS.items():
        if host == site or host.endswith("." + site):
            return lim
    return DEFAULT


def _load() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {}


def _save(data: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data, indent=2))


def _prune(stamps: list[str]) -> list[str]:
    """Keep only the last 24 hours of action timestamps."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    out = []
    for s in stamps:
        try:
            if datetime.fromisoformat(s) >= cutoff:
                out.append(s)
        except ValueError:
            continue
    return out


def check(target: str) -> Spend:
    """What has been spent against this site, without spending anything."""
    host = _host(target)
    site = next((s for s in LIMITS if host == s or host.endswith("." + s)), host)
    lim = _limits_for(host)

    data = _load()
    stamps = _prune(data.get(site, []))
    hour_cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    hour_used = sum(1 for s in stamps
                    if datetime.fromisoformat(s) >= hour_cutoff)

    return Spend(site, len(stamps), lim["day"], hour_used, lim["hour"])


def spend(target: str, n: int = 1, force: bool = False) -> Spend:
    """
    Record n actions against a site, refusing once a ceiling is crossed.

    Raises BudgetExceeded rather than quietly continuing - the whole point is
    that you find out before the account is locked, not after.
    """
    host = _host(target)
    site = next((s for s in LIMITS if host == s or host.endswith("." + s)), host)
    lim = _limits_for(host)

    data = _load()
    stamps = _prune(data.get(site, []))

    now = datetime.now(timezone.utc)
    hour_cutoff = now - timedelta(hours=1)
    hour_used = sum(1 for s in stamps if datetime.fromisoformat(s) >= hour_cutoff)

    over_day = len(stamps) + n > lim["day"]
    over_hour = hour_used + n > lim["hour"]

    if (over_day or over_hour) and not force:
        s = Spend(site, len(stamps), lim["day"], hour_used, lim["hour"])
        raise BudgetExceeded(s.message())

    stamps.extend(now.isoformat(timespec="seconds") for _ in range(n))
    data[site] = stamps
    _save(data)

    result = Spend(site, len(stamps), lim["day"],
                   hour_used + n, lim["hour"])
    if result.level in ("WARN", "STOP"):
        log.warning(result.message())
    return result


def report() -> str:
    """Human-readable view of every site's remaining budget."""
    data = _load()
    if not data:
        return "No rate-limited actions recorded yet."
    lines = []
    for site in sorted(data):
        s = check(site)
        lines.append(f"{s.level:6} {s.message()}")
    return "\n".join(lines)


def reset(site: str = "") -> str:
    """Clear the counter. Only honest if the day really has rolled over."""
    data = _load()
    if site:
        data.pop(_host(site), None)
        _save(data)
        return f"Cleared budget for {site}."
    _save({})
    return "Cleared all budgets."
