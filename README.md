# mc-autobuilder

Automates MissionChief (missionchief.com and its localized clones) station management —
building stations, managing dispatch centers, vehicles, personnel, and more — using the
[Realism Location Marker (RLM)](https://github.com/Norbit-Online/Realism-Location-Marker)
community database of real-world station locations as the source of truth for where to build.

RLM is a GPL-3.0, community-maintained project by Norbit.Online — credit to them for the POI
database this tool builds on. See `docs/rlm-api.md` for what we've confirmed about their API.

## ⚠️ Before you use this

- **This automates actions in a browser game against the developer's ToS.** MissionChief/
  Leitstellenspiel's terms generally prohibit botting/automation. Running this tool is your own
  decision and risk — it may get your account warned, suspended, or banned. Nothing here is legal
  advice.
- **`build` and `run` spend real in-game credits.** Both are dry-run by default (show what would
  be submitted and the current price, spend nothing) and require both an explicit `--execute`
  flag *and* an interactive confirmation before submitting anything — `build` confirms one
  station, `run` shows the full batch and its total cost and asks once for the whole run. `run`
  also stops immediately (not just skips ahead) if your live balance would drop below
  `budget.credit_reserve`, a build fails to confirm, or `budget.max_credits_per_run` is used up.
  `sync` and `plan` are fully read-only.
- Your session cookie is equivalent to your password for this game. Treat `.env` and
  `storage_state.json` like credentials: never commit them, never share them.

## Status

This is being built in phases; only what's actually implemented is documented below.

| Phase | What | Status |
|---|---|---|
| 0 | Research: RLM + MissionChief API docs | Done — `docs/rlm-api.md`, `docs/missionchief-api.md` |
| 1 | Auth + read-only building sync | **Done** — `mc-autobuilder login` / `sync` |
| 2–3 | Config schema + dedupe planner against real RLM data | **Done** — `mc-autobuilder plan` |
| 4 | Build execution (one station, or a whole plan) | **Done** — `mc-autobuilder build` / `run` |
| 5 | Expand / vehicles / hire / personnel / service / dispatch write actions | **Implemented, not yet tested** — `expand`, `toggle-service`, `buy-vehicle`, `hire`, `assign-personnel`, `set-dispatch-center` are all confirmed against real HAR captures and unit-tested, but none have been exercised against a real account yet (unlike Phases 1-4, which were verified live) |
| 6 | Web dashboard | **Done (MVP)** — `mc-autobuilder serve`; this is how Phase 5 is meant to get tested |

Phase 5 commands, all dry-run by default with `--execute` + confirmation before anything changes
(same safety pattern as `build`/`run`):

- `mc-autobuilder expand --building-id <id> --level <n>` — pay Credits to expand a station.
- `mc-autobuilder toggle-service --building-id <id>` — take a station out of service / back in. Free.
- `mc-autobuilder buy-vehicle --building-id <id> --vehicle-type <n>` — buy a vehicle. Pay Credits.
- `mc-autobuilder hire --building-id <id> --days <n>` — start a free day-based recruiting phase
  (does not add personnel immediately — see `docs/missionchief-api.md`).
- `mc-autobuilder assign-personnel --vehicle-id <id> --personal-id <id>` — toggle a person's crew
  binding to a vehicle (personnel ids come from `/buildings/<id>/personals` in the game; no CLI
  command surfaces that roster yet).
- `mc-autobuilder set-dispatch-center --building-id <id> --leitstelle-id <id>` — reassign a
  station's dispatch center (`--leitstelle-id 0` to unassign).

Not yet implemented: `hire_with_education` (paid/trained hiring), equipment purchase.

## Web dashboard

`mc-autobuilder serve` runs a full control panel covering everything above — view synced
buildings and the current plan, and trigger every write action from a browser instead of the
CLI. It's gated behind its own password (separate from your MissionChief login, since this
dashboard can spend real credits):

```bash
pip install -e ".[web]"
echo "DASHBOARD_PASSWORD=$(openssl rand -hex 16)" >> .env
grep DASHBOARD_PASSWORD .env   # note this down, you'll need it to log in
mc-autobuilder serve
```

That's it — it binds every network interface by default, so any device on your local network
(your Windows PC, phone, etc.) can browse straight to it at **`http://<server's-LAN-IP>:8000`**
(the command prints the exact URL on startup). No SSH tunnel required. The password screen is
what actually protects it, so make sure `DASHBOARD_PASSWORD` is a long random value, not something
guessable.

If your server has `ufw` enabled, you'll need to open the port once: `sudo ufw allow 8000/tcp`.

**Never port-forward this out to the public internet** without your own reverse proxy adding TLS
— plain HTTP sends the dashboard password in cleartext, which is fine on a trusted home LAN but
not over the open internet. If you'd rather it not be reachable on the LAN at all (e.g. you only
ever want to reach it via SSH tunnel yourself), pass `--host 127.0.0.1` instead.

### Running it persistently (systemd)

By default `serve` only runs as long as its terminal session is open. To have it run in the
background permanently and restart automatically (on crash or server reboot), install it as a
systemd service — a template is at `deploy/mc-autobuilder.service`:

```bash
sudo cp deploy/mc-autobuilder.service /etc/systemd/system/
sudo nano /etc/systemd/system/mc-autobuilder.service   # fix the User=/WorkingDirectory=/ExecStart= paths for your setup
sudo systemctl daemon-reload
sudo systemctl enable --now mc-autobuilder
sudo systemctl status mc-autobuilder     # check it started
journalctl -u mc-autobuilder -f          # follow its logs
```

- Add `DASHBOARD_SECRET_KEY=<a long random value>` to `.env` so logins survive a service restart
  (otherwise a fresh key is generated every time it starts, which just logs everyone out — not a
  security issue, just an inconvenience).
- After a `git pull` with code changes, restart it: `sudo systemctl restart mc-autobuilder`.

Safety rules match the CLI exactly regardless of how you run it: money-spending actions (expand,
buy vehicle) are a genuine two-step preview → confirm flow, never a single click; free actions
(toggle service, hire, assign personnel, set dispatch center) are a single deliberate form submit
whose button states the exact consequence. Every action verifies success the same way its CLI
equivalent does.

### Presets

The dashboard's **Presets** page lets you configure, per building type, what a station of that
type should always end up looking like:

- **Expand to level** — a specific target level (1-39), not just "max". Re-applying only buys
  the rungs still needed to reach it.
- **Service state** — a single selector: don't manage / keep in service / keep out of service.
- **Hiring** — a free 1/2/3-day recruiting phase, **or "Auto"** (premium continuous hiring toward
  the staffing target, confirmed from a real capture — see `docs/missionchief-api.md`). If the
  account isn't premium, applying the preset reports that auto-hire couldn't be enabled instead of
  silently doing nothing. You can also set a **Personnel (desired) target** — the staffing level
  auto-hire fills toward.
- **Vehicles** — up to 15 rows of catalog `vehicle_type_id` + target count (the edit page shows
  a live catalog with real names/prices if you've already synced or built a station of that
  type), each optionally with a **personnel-per-vehicle** count — after buying a vehicle, that
  many currently-unassigned personnel from the station's roster get assigned as its crew
  automatically.
- **Dispatch center** — optionally assign every new station of this type to one of your dispatch
  centers (picked from a dropdown of your synced Dispatch Center buildings). New stations are
  assigned right after they're built; re-applying to an existing station reassigns it too.

How it runs:
- **Applied automatically** right after a station is built from the plan (CLI `build`/`run`, or
  the dashboard's Plan page) — no extra step needed.
- **Re-appliable anytime** from any station's detail page via "Apply preset now" (or `mc-autobuilder
  apply-preset --building-id <id>` from the CLI) — useful after changing a preset, or to backfill
  stations built before the preset existed.
- **Idempotent and safe to re-run**: only takes the actions still needed (won't re-expand past the
  target level, won't toggle service state if it already matches, skips hiring if a phase is
  already running, won't over-buy vehicles past each type's target count, and won't assign the
  same person to two different vehicles in one application).
- **Expands straight to the target level in a single request** rather than buying one rung at a
  time — MissionChief's expand page lets you click directly to any reachable level, so reaching
  level 39 is one purchase, not 39. Combined with a small default rate-limit delay (see
  `rate_limiting` in `config.yaml`, editable on the Config page), a full station build-out
  finishes in tens of seconds. Raise the delays there if you'd rather go gentler on the site.
- **Runs in the background** on the dashboard with **live progress** — the building's page streams
  each preset action as it happens (no manual refresh), and the Plan page's "Run entire plan" shows
  a progress bar with a per-station log. The CLI version runs synchronously and prints progress too.
- **A station only counts as "done" once its build AND preset have both finished.** If a preset is
  interrupted (session expiry, an unconfirmed action), the station is recorded as built but not
  done, and the next run resumes just the preset — it never re-builds (that would spend credits on
  a duplicate). The Plan page shows the three buckets: to build, built-but-preset-unfinished, and
  fully done.
- **Fast**: crew assignment verifies via a small per-vehicle page instead of re-downloading the
  whole vehicle list, and a vehicle shopping list reuses one vehicle snapshot across purchases, so
  staffing and buying take seconds rather than minutes.
- **Scope, for now**: only actions already confirmed against a real account are covered (expand,
  service toggle, vehicles, crew assignment, hiring). Station extensions, equipment purchase, and
  "auto" hiring are **not** included yet — those were never captured live (see
  `docs/missionchief-api.md`), so adding preset support for them now would mean guessing at
  unconfirmed endpoints.

### Config editor

The dashboard's **Config** page edits the same `config.yaml` the CLI's `plan` command reads —
regions, building-type→RLM mappings, dedupe radius, naming template, budget, RLM cache, and rate
limiting — all from the browser instead of hand-editing YAML. Saving **overwrites the file**
(comments in an existing hand-edited `config.yaml` won't be preserved) but keeps the exact same
schema, so the CLI and dashboard stay interchangeable.

The same page also has a **MissionChief account** section for your login credentials (auth mode,
username, password, session cookie, server base URL) — these actually live in `.env`, not
`config.yaml`, since that's what the CLI already reads. Password/session-cookie fields are never
echoed back once set (leave them blank to keep the current value); saving them takes effect
immediately, no server restart needed.

### Generating a plan from the dashboard

The **Plan** page has a "Generate plan" button — it re-reads whatever's currently in
`config.yaml` (edit it on the Config page, or by hand) and rewrites `plan.json`, exactly like
running `mc-autobuilder plan` on the server: fetches RLM candidates for every configured region,
dedupes against your synced buildings, and applies your naming template/budget/caps. Runs in the
background (RLM fetches across several regions plus a live price check can take a while,
especially on a cold cache) — refresh the page to see the result once it finishes. The CLI and
dashboard share the exact same plan-generation code, so results are identical either way.

If the plan comes back with **0 stations to build**, the Plan page's "Why this plan?" card
explains where the candidates went: 0 RLM candidates points at a bad region bbox or `poi_type`
mapping, while lots of candidates but 0 to build points at dedupe/cap/budget (most often
everything got rejected by your `max_credits_per_run` cap — the card shows the cap next to the
cheapest rejected station's cost so you know what to raise it to).

### Running a whole plan from the dashboard

Once a plan looks right, the **Plan** page's "Run entire plan" button builds every not-yet-built
station in one go and applies each station's preset (expand, service state, hiring, vehicles +
crew) immediately after it's built. You get a confirmation screen first with the estimated total
spend and a warning if any stations lack a preset (they'd build "bare" at level 1). The run
happens in the background and **aborts on the first hard problem** — an unconfirmed build, an
expired session, or reaching your `max_credits_per_run` budget — so a partial run stops cleanly
instead of compounding errors. Refresh the Plan page to watch a per-station result log build up.
Everything is idempotent: already-built stations are skipped, so it's always safe to re-run.

Building a single station (the per-row "Build" button) works the same way — it applies that
station's preset right after building, and now tells you explicitly when no preset is configured
for that building type (which is why a freshly-built station would otherwise sit at level 1 with
no vehicles).

### Map and sortable tables

The **Plan** page shows every pending station on a map (green markers) alongside your already-
synced stations (grey markers) — powered by a locally-vendored copy of Leaflet (no CDN calls);
map tiles themselves load from OpenStreetMap directly in your browser when you view the page.
Every table across the dashboard (buildings, plan, presets, vehicle/expand catalogs, preset
action logs, ...) has clickable, sortable column headers.

## Requirements

- **Python 3.11+**
- `git`
- (Optional, for Playwright auth mode) a display/GUI, or a local machine to capture the session
  on and copy it over — see the Playwright caveat below.

## Installing on a fresh Ubuntu server

These steps assume a clean Ubuntu 22.04 or 24.04 server with only SSH access (no GUI).

### 1. System packages

```bash
sudo apt update
sudo apt install -y git python3-pip python3-venv
```

Check your Python version:

```bash
python3 --version
```

- **Ubuntu 24.04** ships Python 3.12 by default — you're already good, skip to step 2.
- **Ubuntu 22.04** ships Python 3.10, which is too old. Install 3.11 from the deadsnakes PPA:

  ```bash
  sudo apt install -y software-properties-common
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt update
  sudo apt install -y python3.11 python3.11-venv
  ```

  Use `python3.11` in place of `python3` in the commands below.

### 2. Create a dedicated user (recommended)

Don't run this as root. A session cookie leak from a root-owned process is worse than from a
scoped one:

```bash
sudo adduser --disabled-password --gecos "" mcbot
sudo su - mcbot
```

Run everything below as this user.

### 3. Clone and install

```bash
git clone https://github.com/mibzzer15/mc-autobuild.git
cd mc-autobuild
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

This installs the `mc-autobuilder` command into `.venv/bin`. Confirm it works:

```bash
mc-autobuilder --help
```

### 4. Configure `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` and set your auth mode:

```bash
nano .env
```

There are three modes (`MC_AUTH_MODE`):

- **`credentials`** (default, simplest) — give it your username/password directly:

  ```dotenv
  MC_AUTH_MODE=credentials
  MC_USERNAME=you@example.com
  MC_PASSWORD=your-password
  MC_BASE_URL=https://www.missionchief.com
  ```

  The app logs in itself (scraping and submitting the real sign-in form) and caches the
  resulting session in `MC_STORAGE_STATE_PATH` (default `storage_state.json`) so it doesn't have
  to log in again every run — it only re-logs-in automatically once that cached session expires.

  **Security tradeoff:** this stores your password in plaintext in `.env`. It's gitignored and
  `chmod 600` locks it to your user, but it's still plaintext on disk. If that's not acceptable
  for your setup, use cookie mode instead.

  Verified against a real login on `missionchief.com`: it scrapes the actual sign-in form at
  runtime (rather than hardcoding field names) and confirms success by checking for a redirect
  away from the sign-in page.

- **`cookie`** — paste a session cookie you copied yourself:

  ```dotenv
  MC_AUTH_MODE=cookie
  MC_SESSION_COOKIE=paste_your_full_cookie_header_here
  MC_BASE_URL=https://www.missionchief.com
  ```

  Get it from your browser: log into MissionChief, open DevTools (F12) → Network tab, click any
  request to `missionchief.com`, find the `Cookie` request header under Headers, and copy its
  full value (looks like `_missionchief_session=...; other_cookie=...`).

- **`playwright`** — interactive browser login, see the next section.

If you're on a different localized MissionChief domain (Leitstellenspiel, missionchief.co.uk,
etc.), change `MC_BASE_URL` to match, for any auth mode.

### 5. About Playwright mode on a headless server

`MC_AUTH_MODE=playwright` opens a real Chromium window for you to log in manually
(`mc-autobuilder login`) — that needs a display, which a bare server doesn't have. Two ways to
use it anyway:

- **Don't** — just use cookie mode (step 4). It's simpler and is all a headless server needs.
- Or run `mc-autobuilder login` on your own desktop (where Playwright can open a real window),
  then `scp` the resulting `storage_state.json` to the server and point
  `MC_STORAGE_STATE_PATH` at it, with `MC_AUTH_MODE=playwright` in the server's `.env`.

If you do want Playwright mode locally, install the extra and its browser first:

```bash
pip install -e ".[playwright]"
playwright install --with-deps chromium
```

### 6. Run it

```bash
mc-autobuilder sync
```

This pulls every building on your account from `GET /api/buildings`, caches it in a local SQLite
file (`mc_autobuilder.db` by default), and prints a report: total buildings, in-service vs.
out-of-service counts, a breakdown by building type, and how many dispatch centers are
referenced. A structured log for the run is written to `logs/sync_<timestamp>.log`.

Re-running `sync` is safe any time — it's read-only and upserts by building id, so it never
duplicates local records.

### 7. Set up `config.yaml` and generate a build plan

```bash
cp config.example.yaml config.yaml
nano config.yaml
```

`config.example.yaml` is fully commented — set `mission_chief.game_world` to your server (RLM's
code for it, e.g. `US` or `DE` — see `docs/rlm-api.md`), list the real-world `regions` you want to
pull candidate stations from (a bounding box, a city name, or a center point + radius), and map
each RLM `poi_type` you care about to your server's MissionChief `building_type` id.

Then, with `sync` already run at least once (so there's something to dedupe against):

```bash
mc-autobuilder plan
```

This fetches candidate stations from RLM's public POI database for each configured region,
skips any that are within `dedupe.radius_m` of a building you already have of the same type,
respects each type's `max_per_run` cap and the overall `budget.max_credits_per_run`, and fetches
your account's *current* build prices (which scale with progression, so they're never
hardcoded) to estimate total cost. It writes the full result to `plan.json` and prints a summary
table. **This is read-only** — it never builds anything or spends credits.

RLM's API responses are cached locally under `.rlm_cache/` per `rlm_cache.ttl_hours` in your
config, so re-running `plan` doesn't re-fetch a region's data on every run.

### 8. Build one station from the plan

Every entry in `plan.json`'s `to_build` list has a `poi_id`. Pick one and preview it:

```bash
mc-autobuilder build --poi-id 12345
```

With no `--execute`, this only prints the station name, location, and current price — it
submits nothing. When you're ready to actually build it:

```bash
mc-autobuilder build --poi-id 12345 --execute
```

This re-checks the live price immediately before submitting (prices drift over time), asks you
to confirm the exact station and cost interactively, and only then submits the build. It records
what it built locally, so running the same `--poi-id` again just reports "already built" instead
of building a duplicate.

### 9. Build a whole plan at once

To build everything in `plan.json`'s `to_build` list instead of one station at a time, preview
the batch first:

```bash
mc-autobuilder run
```

With no `--execute`, this just lists every not-yet-built station and the total estimated cost.
When you're ready:

```bash
mc-autobuilder run --execute
```

You get **one** confirmation covering the whole batch (total station count and cost), then it
builds through all of them — with the same rate-limit delays between each as everything else in
this tool. Before each station it checks your **live** credit balance (read from the nav bar,
same as the game shows you) and stops immediately, without building that station, if doing so
would drop your balance below `budget.credit_reserve`. It also stops if `budget.max_credits_per_run`
for the run is used up, if a build ever fails to confirm success, or if your session expires —
"stop and tell you," never "skip ahead and keep spending." Already-built stations (e.g. from a
previous interrupted run) are skipped rather than repeated.

### 10. Re-authenticating when your session expires

If `sync` fails with an authentication error (expired/invalid session), it will tell you plainly
instead of failing silently. Fix it by:

- **Credentials mode:** nothing to do — it re-logs-in automatically using `MC_USERNAME`/
  `MC_PASSWORD` when the cached session expires. If it still fails, the credentials themselves
  are likely wrong, or MissionChief is blocking the automated login (see the note above).
- **Cookie mode:** grab a fresh `Cookie` header value from your browser and update
  `MC_SESSION_COOKIE` in `.env`.
- **Playwright mode:** re-run `mc-autobuilder login` (on a machine with a display) to refresh
  `storage_state.json`.

## Running unattended (optional)

Since only the read-only `sync` command exists today, a periodic cron job is low-risk if you want
your local cache to stay fresh:

```bash
crontab -e
```

```cron
0 * * * * cd /home/mcbot/mc-autobuild && .venv/bin/mc-autobuilder sync >> /home/mcbot/mc-autobuild/cron.log 2>&1
```

Don't extend this to future write-capable commands (`build`, `vehicles`, `hire`, etc.) without
reviewing their dry-run output first — this project's design intentionally requires an explicit
`--execute` or interactive confirmation before anything that spends credits or changes your
account, and unattended cron isn't the place to skip that review.

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Project layout

```
src/mc_autobuilder/
  auth.py         # cookie / credentials / Playwright auth, CSRF token handling, session-expiry detection
  mc_client.py    # rate-limited MissionChief API client (buildings, vehicles, expand, hire, dispatch, ...)
  rlm_client.py   # RLM POI database client (bbox queries, disk caching, city geocoding)
  planner.py      # pure dedupe/build-planning logic (haversine distance, budget/caps)
  presets.py      # idempotent per-building-type preset application (expand/service/hire/vehicles)
  config.py       # config.yaml loading and validation
  web_config.py   # dashboard password/secret-key loading
  models.py       # SQLite/SQLAlchemy local cache, completed-action + preset-action idempotency logs
  cli.py          # typer CLI (`login`, `sync`, `plan`, `build`, `run`, Phase 5 actions, `serve`)
  web/            # FastAPI dashboard (app.py, templates/, static/) — see `mc-autobuilder serve`
deploy/
  mc-autobuilder.service  # systemd unit template for running the dashboard persistently
config.example.yaml
docs/
  rlm-api.md            # RLM API research findings
  missionchief-api.md   # MissionChief API research findings
tests/
  fixtures/             # real (scrubbed) captures used by several tests
```

## Credits

Real-world station location data comes from the
[Realism Location Marker](https://github.com/Norbit-Online/Realism-Location-Marker) project
(GPL-3.0-or-later), by Richard Cameron (Madpugs) / Norbit.Online. This project is not affiliated
with RLM, MissionChief, or Leitstellenspiel.
