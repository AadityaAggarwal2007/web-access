# Research Rig

Gives your AI eyes on the web. Thirteen tools, one gateway, cheapest method first.

Your AI never learns Scrapling or gallery-dl. It calls `read(url)` and the rig
decides whether that needs plain HTTP, a stealth browser, or real Chrome with a
saved login.

---

## Setup

You have `uv`, which brings its own Python — your system Python is 3.9 and too
old for Scrapling. Don't use `pip` against it.

```bash
cd ~/Desktop/research-rig
uv sync
uv run scrapling install     # fetches the Camoufox stealth browser, once
```

Optional extras, only when you need them:

```bash
uv sync --extra agent        # browser-use, for sites that need clicking
uv sync --extra forms        # Skyvern, for filling forms
```

Point it at your gateway (the key is read from the environment, never committed):

```bash
export RIG_GATEWAY_URL=http://127.0.0.1:8000/v1
export RIG_GATEWAY_KEY=your-gateway-key
```

Check everything is wired up:

```bash
uv run python -c "from rig.gateway import gateway; print(gateway.health())"
```

---

## Point your AI at it

Add to your MCP client config (Claude Desktop, Claude Code, Cursor — same shape):

```json
{
  "mcpServers": {
    "research-rig": {
      "command": "uv",
      "args": ["run", "--directory", "/Users/aadityaaggarwal/Desktop/research-rig", "mcp_server.py"],
      "env": {
        "RIG_GATEWAY_URL": "http://127.0.0.1:8000/v1",
        "RIG_GATEWAY_KEY": "your-gateway-key"
      }
    }
  }
}
```

That's it. Your AI now has thirteen tools.

---

## The thirteen tools

| Tool | What your AI says | Cost |
|---|---|---|
| `research(topic)` | "Research Urja SGGSCC" — the whole loop, returns a sourced report | plan + synthesis only |
| `search(query)` | Cheapest way into a topic. Always first. | Tavily credit |
| `read(url)` | Page → markdown. Auto-escalates through the tiers. | free |
| `extract(url, want)` | Structured JSON from a messy page | ~$0.01 |
| `harvest(target, topic)` | All photos/video from a profile — 390 sites | free |
| `login(site)` | Opens a window so you sign in by hand, once | free |
| `browse(goal)` | Agent that clicks. Last resort. | ~$0.05/page |
| `read_pdf(path)` | Magazines, reports, LinkedIn document posts | free |
| `harvest_org(name, ...)` | Instagram + LinkedIn + website in one call | free |
| `linkedin_posts(page)` | Scrolls LinkedIn, reports how complete it is | free |
| `budget(site)` | Actions spent today, before you get locked out | free |
| `corpus(topic)` | What's already collected | free |
| `status()` | Gateway up? Models live? Tools installed? | free |

---

## The ladder

`read()` climbs only as far as it must:

```
tier 2   plain HTTP + real TLS fingerprint      most sites
tier 3   stealth browser (Camoufox)             Cloudflare, bot walls
tier 4   real Chrome + saved login              LinkedIn, Instagram
```

LinkedIn, Instagram, X and Facebook skip straight to tier 4 — no point failing
twice first. Every page records which tier it needed, so you can see what a site
actually costs you.

---

## Your gateway

`rig/gateway.py` handles everything specific to your pool setup:

- **503 is retried**, because on this gateway it's usually the 120s upstream cap
  wearing a 503 costume — not an exhausted pool.
- **Empty replies are retried** (~4% of calls come back `content: ""`).
- **Model list fetched at startup**, never hardcoded — the roster changes under
  you, as it did when `minimax-m3` vanished.
- **Pools are budgeted**: bulk work → `gemini`, planning and synthesis →
  `claude`, `router` left alone, and **`nvidia` is never touched automatically**
  because it never refills. Turn it on per-instance with
  `Gateway(allow_finite_pool=True)`.
- **Intent aliases preferred** (`fast`, `best`, `reasoning`) over concrete model
  names, so nothing breaks when models come and go.
- `model_not_found` raises immediately instead of burning three retries.

---

## Logins

"Sign in with Google" **cannot be automated** — Google blocks automated browsers
deliberately, and no tool gets around it. The working pattern:

```bash
uv run python -c "from rig.ladder import login; print(login('linkedin.com'))"
```

A window opens. You log in — password, 2FA, captcha, whatever. The profile keeps
the session, and every run afterwards is already signed in.

**Use a spare LinkedIn account.** The practical ceiling is ~150 actions per
account per 24 hours, and more than 100–150 profile loads in an hour is the most
common trigger for a lock.

### Instagram, Facebook, LinkedIn

Full walkthrough in **[LOGIN.md](LOGIN.md)**. The short version:

```bash
uv run python -c "from rig.ladder import login; print(login('instagram.com', minutes=8))"
```

Sign in in the window that opens, then leave it — it closes itself and saves the
session. `harvest()` picks those cookies up automatically, no extra config.

### Or use your own browser's cookies

gallery-dl gets everything from Instagram in one command — but only once logged
in. Anonymous requests get `Requested user could not be found`, which reads like
a missing account and is really a missing login. Point the rig at a cookie
source:

```bash
export RIG_COOKIES=chrome          # or firefox / safari / edge / brave
# or, to keep your main browser out of it:
export RIG_COOKIES=~/ig-cookies.txt
```

Without it, `harvest()` refuses with an explanation rather than silently
downloading nothing.

### The action budget

Every hit on a rate-limited site is counted and persisted. The rig warns at 50%,
warns harder at 80%, and **refuses at 120 actions/day** for LinkedIn — well under
the ~150 where locks start. Override deliberately with `force=True`.

```bash
uv run python -c "from rig import budget; print(budget.report())"
```

This is the difference between finding out you are near the limit and finding
out from a locked account.

---

## Where things land

```
corpus/<topic>/
  pages/         one markdown file per page, with source + tier in the header
  media/         photos and video from harvest()
  files/         PDFs and anything else
  manifest.json  every item, where it came from, which tier fetched it
  report.md      the written report
```

Nothing is re-fetched if the manifest already has it.

---

## Running it without an AI

```bash
uv run python -c "
from rig.research import research
r = research('Urja SGGSCC', max_pages=20)
print(r['report_path'])
"
```
