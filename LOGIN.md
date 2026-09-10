# Logging in

Instagram, Facebook and LinkedIn show almost nothing to a logged-out client.
Instagram is the worst of them: it replies **`Requested user could not be
found`**, which reads like the account does not exist and actually means you are
not signed in.

You log in **once per site, by hand**. The session is saved and reused forever
after.

---

## The short version

```bash
cd ~/Desktop/research-rig

uv run python -c "from rig.ladder import login; print(login('instagram.com', minutes=8))"
uv run python -c "from rig.ladder import login; print(login('facebook.com',  minutes=8))"
uv run python -c "from rig.ladder import login; print(login('linkedin.com',  minutes=8))"
```

Each opens a real Chrome window. Sign in. **Then leave the window alone** — it
closes itself and saves the session. Do not close it yourself, and do not kill
the terminal: cookies are written on a clean shutdown, and a forced close loses
the login.

That is the whole setup. `harvest()` and `linkedin_posts()` pick the session up
automatically.

---

## Rules that will save you time

### Never use "Sign in with Google"

Google blocks automated browsers deliberately. You will get:

> **Couldn't sign you in** — This browser or app may not be secure.

No tool gets around this. Use the site's own **email + password** form.

If you created an account *with* the Google button you have no password yet —
set one first, in your normal browser:

* Instagram — <https://www.instagram.com/accounts/password/reset/>
* Facebook — <https://www.facebook.com/login/identify/>
* LinkedIn — <https://www.linkedin.com/uas/request-password-reset>

### Use burner accounts

Scraping from an account you care about is how you lose an account you care
about. Make throwaways with their own email addresses.

Give a new account an hour before you scrape with it, and add a name and a
photo. Brand-new empty profiles get flagged fastest.

### Let the window close itself

The single most common failure. `login()` holds the browser open for `minutes`
and then shuts it down cleanly, flushing cookies to disk. Kill it early and you
get a **"Welcome back"** re-authentication screen on the next run — the profile
remembers who you are but has no valid session.

Need longer? `login('instagram.com', minutes=15)`.

---

## Checking it worked

```bash
uv run python -c "
from rig.media import _rig_profile
for s in ('instagram.com','facebook.com','linkedin.com'):
    print(f'{s:16}', 'signed in' if _rig_profile('https://'+s) else 'NOT set up')
"
```

Then actually use it:

```bash
# Instagram - photos, videos, thumbnails, captions, metadata
uv run python -c "
from rig.media import harvest
from pathlib import Path
print(harvest('https://www.instagram.com/nasa/', Path('/tmp/ig-test'), limit=3))
"
```

`files_added: 3` means you are done. If it refuses, the message tells you which
step is missing.

---

## Where the session lives

```
~/Desktop/research-rig/sessions/
  instagram.com/     a full Chrome profile - cookies, local storage
  facebook.com/
  linkedin.com/
```

These are **live logins**. They are in `.gitignore` and must stay there —
committing one hands the account to anyone who clones the repo.

To sign out of a site, delete its folder:

```bash
rm -rf ~/Desktop/research-rig/sessions/instagram.com
```

---

## Using your real browser instead

If you would rather not sign in again, point the rig at a browser you are
already logged into:

```bash
export RIG_COOKIES=chrome        # or firefox / safari / edge / brave
```

This reads that browser's **entire** cookie store, every site included. It is
the fast path, not the careful one. The per-site profiles above keep your real
browser out of it entirely, which is why they are the default.

A middle option — export just the cookies you need to a `cookies.txt` with a
browser extension:

```bash
export RIG_COOKIES=~/ig-cookies.txt
```

---

## Rate limits

Every hit on these sites is counted and persisted. The rig warns at 50%, warns
harder at 80%, and **refuses** at the daily ceiling.

| Site | Rig stops at | Real trouble starts near |
|---|---|---|
| LinkedIn | 120 / day | ~150 / day |
| Instagram | 200 / day | varies, less predictable |
| Facebook | 200 / day | varies |

```bash
uv run python -c "from rig import budget; print(budget.report())"
```

Override deliberately with `force=True` if you accept the risk. That is a
decision, not something you should hit by accident.
