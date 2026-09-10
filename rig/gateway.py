"""
Client for Aaditya's local model gateway.

Everything the gateway does differently from a plain OpenAI endpoint is handled
here, so nothing else in the rig has to think about it:

  * 120s upstream cap surfaces as a 503 that is really a timeout -> retried
  * ~4% of successful calls return empty content -> retried
  * the model list changes underneath you -> fetched at startup, never hardcoded
  * four pools with very different budgets -> traffic is routed by pool, and the
    finite pool is never used unless explicitly asked for
"""

from __future__ import annotations

import os
import time
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

log = logging.getLogger("rig.gateway")

BASE_URL = os.environ.get("RIG_GATEWAY_URL", "http://127.0.0.1:8000/v1")
# No default: a key belongs in the environment, not in a file that gets pushed.
API_KEY = os.environ.get("RIG_GATEWAY_KEY", "")

# Pool budgets, from the gateway's own description of itself.
#   claude  17 models  47 keys  weekly refill
#   gemini   8 models  51 keys  weekly refill
#   router   8 models  11 keys  DAILY refill, ~50 req/day - scarce
#   nvidia  18 models   6 keys  NEVER refills - finite
POOL_POLICY = {
    "gemini": "bulk",      # default workhorse
    "claude": "quality",   # planning, synthesis, hard reasoning
    "router": "sparing",   # occasional only
    "nvidia": "never",     # finite; opt-in per call, never automatic
}

# What the rig asks for by job, in preference order. Intent aliases first so the
# gateway picks; concrete models are fallbacks if aliases are ever removed.
JOB_MODELS = {
    "bulk":      ["fast", "cheap", "gemini/gemini-3.5-flash-lite"],
    "extract":   ["fast", "gemini/gemini-3.7-flash"],
    "plan":      ["reasoning", "best", "claude/claude-opus-4-6-thinking"],
    "synthesis": ["best", "smart", "claude/claude-opus-4-6-thinking"],
    "vision":    ["smart", "gemini/gemini-3.7-flash"],
}

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class GatewayError(RuntimeError):
    """Raised when the gateway fails in a way retrying will not fix."""


@dataclass
class Gateway:
    base_url: str = BASE_URL
    api_key: str = API_KEY
    timeout: float = 135.0          # just above the gateway's 120s upstream cap;
                                    # 180s x 3 retries meant a 9-minute worst case
    max_retries: int = 3
    allow_finite_pool: bool = False  # nvidia stays off unless you turn it on

    _models: list[str] = field(default_factory=list)
    _client: httpx.Client | None = None

    # ---------------------------------------------------------------- plumbing

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def _post(self, path: str, payload: dict) -> dict:
        """POST with retries that understand this gateway's failure modes."""
        last: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                r = self.client.post(path, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                log.warning("transport error on %s (attempt %d): %s", path, attempt, exc)
                time.sleep(min(2 ** attempt, 8))
                continue

            if r.status_code == 200:
                return r.json()

            body = _safe_json(r)
            err = (body or {}).get("error", {})
            etype = err.get("type", "")
            msg = err.get("message", r.text[:300])

            # model_not_found is a real, permanent error - do not burn retries.
            if etype == "model_not_found":
                raise GatewayError(f"model not found: {payload.get('model')} - {msg}")

            # A 503 here is usually the 120s upstream cap, not an exhausted pool.
            if r.status_code in RETRYABLE_STATUS:
                last = GatewayError(f"{r.status_code} {etype}: {msg}")
                log.warning(
                    "retryable %s on %s (attempt %d/%d): %s",
                    r.status_code, path, attempt, self.max_retries, msg,
                )
                time.sleep(min(2 ** attempt, 8))
                continue

            raise GatewayError(f"{r.status_code} {etype}: {msg}")

        raise GatewayError(f"{path} failed after {self.max_retries} attempts: {last}")

    # ------------------------------------------------------------------ models

    def models(self, refresh: bool = False) -> list[str]:
        """Live model list. Never hardcode - the roster changes underneath you."""
        if self._models and not refresh:
            return self._models
        try:
            r = self.client.get("/models")
            r.raise_for_status()
            data = r.json().get("data", [])
            self._models = [m["id"] for m in data if isinstance(m, dict) and "id" in m]
        except Exception as exc:                       # gateway down at startup
            log.warning("could not fetch model list: %s", exc)
            self._models = []
        return self._models

    def pick(self, job: str = "bulk") -> str:
        """Choose a model for a job, preferring intent aliases the gateway owns."""
        wanted = JOB_MODELS.get(job, JOB_MODELS["bulk"])
        available = set(self.models())

        if not available:                # gateway unreachable; let it decide
            return wanted[0]

        for candidate in wanted:
            if candidate in available:
                if not self.allow_finite_pool and candidate.startswith("nvidia/"):
                    continue
                return candidate

        # Nothing matched. Fall back to any model from a refillable pool.
        for m in sorted(available):
            pool = m.split("/")[0] if "/" in m else ""
            if POOL_POLICY.get(pool) in ("bulk", "quality"):
                return m
        return wanted[0]

    # -------------------------------------------------------------------- chat

    def chat(
        self,
        messages: list[dict],
        job: str = "bulk",
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        """
        One chat call, returning text. Empty responses (~4% on this gateway) are
        retried rather than handed back as a silently successful empty string.
        """
        chosen = model or self.pick(job)
        payload: dict[str, Any] = {
            "model": chosen,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        for attempt in range(1, self.max_retries + 1):
            data = self._post("/chat/completions", payload)
            try:
                content = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError):
                content = ""

            if content.strip():
                return content

            # Re-sending an identical payload tends to reproduce an identical
            # empty reply, so nudge the sampling on each retry.
            payload["temperature"] = min(1.0, payload.get("temperature", 0.2) + 0.3)
            log.warning(
                "empty reply from %s (attempt %d/%d) - retrying at temperature %.1f",
                chosen, attempt, self.max_retries, payload["temperature"],
            )
            time.sleep(1)

        raise GatewayError(f"{chosen} returned empty content {self.max_retries} times")

    def json(self, messages: list[dict], job: str = "extract", **kw) -> Any:
        """Chat that must come back as JSON. Tolerates fenced output."""
        raw = self.chat(messages, job=job, json_mode=True, **kw)
        return _loads_loose(raw)

    # ------------------------------------------------------------------ search

    def search(self, query: str, max_results: int = 8) -> list[dict]:
        """
        The gateway's built-in web search (Tavily-backed, finite credits).
        Returns [{title, url, snippet}]. This is the cheapest way into a topic -
        use it before reaching for any crawler.
        """
        try:
            r = self.client.get("/search", params={"q": query})
            if r.status_code == 405:                  # some builds are POST-only
                r = self.client.post("/search", json={"query": query})
            r.raise_for_status()
            body = r.json()
        except Exception as exc:
            raise GatewayError(f"search failed: {exc}") from exc

        raw = body.get("results") or body.get("data") or []
        out = []
        for item in raw[:max_results]:
            if not isinstance(item, dict):
                continue
            out.append({
                "title": item.get("title") or item.get("name") or "",
                "url": item.get("url") or item.get("link") or "",
                "snippet": (item.get("content") or item.get("snippet")
                            or item.get("description") or "")[:600],
            })
        return out

    # ------------------------------------------------------------------ health

    def health(self) -> dict:
        try:
            r = self.client.get(
                self.base_url.rstrip("/").rsplit("/v1", 1)[0] + "/health"
            )
            return r.json()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------- helpers

def _safe_json(r: httpx.Response) -> dict | None:
    try:
        return r.json()
    except Exception:
        return None


def _loads_loose(raw: str) -> Any:
    """Parse JSON that may arrive wrapped in a markdown fence."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        s = s.rsplit("```", 1)[0]
    s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start = min((i for i in (s.find("{"), s.find("[")) if i != -1), default=-1)
        end = max(s.rfind("}"), s.rfind("]"))
        if start != -1 and end > start:
            return json.loads(s[start:end + 1])
        raise


# A single shared instance is all the rig ever needs.
gateway = Gateway()
