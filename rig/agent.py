"""
Tier 6, the last rung: an agent that actually looks at the screen and clicks.

Roughly $0.05 a page and slow, so nothing calls this automatically. It exists
for flows the ladder cannot do at all - multi-step navigation, a form that has
to be filled before content appears, a site that only reveals itself to clicks.
"""

from __future__ import annotations

import asyncio
import logging

from .gateway import BASE_URL, API_KEY, gateway

log = logging.getLogger("rig.agent")


def browse(goal: str, start_url: str | None = None, max_steps: int = 25) -> dict:
    """
    Hand a plain-English goal to browser-use and let it drive.

    Points at the local gateway through browser-use's OpenAI-compatible client,
    so it runs on the same pools as everything else.
    """
    try:
        from browser_use import Agent
        from browser_use.llm import ChatOpenAI
    except ImportError as exc:
        return {"ok": False, "error": f"browser-use not installed ({exc}). Run: uv sync"}

    task = goal if not start_url else f"Go to {start_url}. Then: {goal}"

    llm = ChatOpenAI(
        model=gateway.pick("vision"),
        base_url=BASE_URL,
        api_key=API_KEY,
    )

    async def _run():
        agent = Agent(task=task, llm=llm)
        return await agent.run(max_steps=max_steps)

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        log.exception("agent failed")
        return {"ok": False, "goal": goal, "error": str(exc)}

    return {
        "ok": True,
        "goal": goal,
        "result": str(getattr(result, "final_result", None) or result)[:8000],
    }


def fill_form(url: str, data: dict, submit: bool = False) -> dict:
    """
    Fill a form with Skyvern, which benchmarks first on write tasks.

    submit is False by default on purpose: the rig fills the form and stops, so
    you can look at it before anything is actually sent.
    """
    try:
        from skyvern import Skyvern
    except ImportError as exc:
        return {"ok": False, "error": f"skyvern not installed ({exc}). "
                                      "Run: uv sync --extra forms"}

    fields = "\n".join(f"  {k}: {v}" for k, v in data.items())
    prompt = (
        f"Fill in the form on this page with the following values:\n{fields}\n\n"
        + ("Then submit it." if submit
           else "Do NOT submit. Fill the fields and stop so a human can review.")
    )

    try:
        client = Skyvern(base_url=BASE_URL, api_key=API_KEY)
        task = client.run_task(prompt=prompt, url=url)
    except Exception as exc:
        log.exception("skyvern failed")
        return {"ok": False, "url": url, "error": str(exc)}

    return {"ok": True, "url": url, "submitted": submit, "result": str(task)[:4000]}
