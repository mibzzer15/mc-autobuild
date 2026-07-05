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
- **Dry-run is the plan for every write-capable command** (not built yet — see Status below).
  Nothing in the current build spends in-game currency or modifies your account; it only reads
  `/api/buildings`.
- Your session cookie is equivalent to your password for this game. Treat `.env` and
  `storage_state.json` like credentials: never commit them, never share them.

## Status

This is being built in phases; only what's actually implemented is documented below.

| Phase | What | Status |
|---|---|---|
| 0 | Research: RLM + MissionChief API docs | Done — `docs/rlm-api.md`, `docs/missionchief-api.md` |
| 1 | Auth + read-only building sync | **Done** — `mc-autobuilder login` / `sync` |
| 2 | Config schema + dedupe planner against RLM data | Not started (blocked on confirming RLM's `/api/pois` contract) |
| 3 | Build execution (single test station) | Not started |
| 4 | Expand / vehicles / hire / personnel / service / dispatch write actions | Not started |
| 5 | Web dashboard | Not started |

Commands that don't exist yet: `plan`, `build`, `expand`, `vehicles`, `hire`, `assign`, `service`,
`dispatch`, `run`. Don't expect `config.yaml` yet either — it lands with the planner in Phase 2.

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

### 7. Re-authenticating when your session expires

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
  auth.py        # cookie / Playwright auth, CSRF token handling, session-expiry detection
  mc_client.py   # rate-limited MissionChief API client
  models.py      # SQLite/SQLAlchemy local cache
  cli.py         # typer CLI (`login`, `sync`)
docs/
  rlm-api.md            # RLM API research findings
  missionchief-api.md   # MissionChief API research findings
tests/
```

## Credits

Real-world station location data comes from the
[Realism Location Marker](https://github.com/Norbit-Online/Realism-Location-Marker) project
(GPL-3.0-or-later), by Richard Cameron (Madpugs) / Norbit.Online. This project is not affiliated
with RLM, MissionChief, or Leitstellenspiel.
