# MissionChief API — Research Findings

**Status:** Partially confirmed. Read endpoints, the username/password sign-in form, and
building a new station (`POST /buildings`, including a real successful build) are confirmed
against the live site. Several write actions for later phases (expand, vehicle purchase, hiring,
personnel assignment, service toggle, dispatch reassignment) are **not yet captured** — see "Not
yet captured" below before implementing Phase 5.

## Source

Captured via a browser HAR export from the user's own authenticated MissionChief session. The
account already has the community tool **LSS-Manager** installed (confirmed by an
`X-LSS-Manager: Stable undefined` request header sent on `/api/buildings` and `/api/vehicles`),
which independently validates that these are the correct, established endpoints for external
tooling to read a player's own data.

Direct live requests to `missionchief.com` from the automation sandbox used to research this are
blocked by that environment's network egress policy — all findings below come from the supplied
capture, not from requests made by this app.

## Authentication

MissionChief is a standard Rails app and uses two cooperating mechanisms:

1. **Session cookie** — a normal Rails session cookie (`HttpOnly`), obtained by logging in
   normally (browser or Playwright) or supplied directly as `MC_SESSION_COOKIE`. Required on every
   request.
2. **CSRF token** — Rails' `authenticity_token`. Observed as a single stable value for the whole
   session, sent as:
   - hidden form field `authenticity_token` on HTML forms (e.g. `/buildings/new`), and
   - `X-CSRF-Token` request header on XHR/fetch calls — including plain `GET`s like
     `/api/buildings` and `/api/vehicles` (the game's frontend JS attaches it to all AJAX calls
     uniformly, not just state-changing ones).

   Example observed value: `7yGvlmZ8Kx53fKHTvzkG1tjYJWi86lnphVdjOIW/kHM=` (same value used
   identically as both the form's hidden field and the header across every request in the
   capture).

   **Implementation plan:** on startup/re-auth, `GET /` (or any authenticated page) and parse
   `<meta name="csrf-token" content="...">` from the HTML; attach it as `X-CSRF-Token` on every
   subsequent request. If a write request comes back `422 Unprocessable Entity` (invalid
   authenticity token), treat that as a session-expiry signal, refetch the token once, and retry;
   if it still fails, prompt for re-authentication rather than failing silently.

3. **`GET /profile/external_secret_key/:user_id`** → `{"code": "en_US-109450-<hex>"}`. An
   account-linked secret code, presumably used by third-party manager tools for their own
   external sync — not resent to missionchief.com anywhere in the capture. Not required for our
   auth flow; documented for completeness only.

### Username/password login form — **confirmed**

`GET https://www.missionchief.com/users/sign_in` (captured 2026-07) returns a standard
Devise-style sign-in form:

```html
<meta content="authenticity_token" name="csrf-param" />
<meta content="Wkp3S9SICwpH8VkEqAUni2J1DOhWgW30dG+v19vTDS4=" name="csrf-token" />

<form accept-charset="UTF-8" action="/users/sign_in" class="simple_form form-horizontal"
      id="new_user" method="post" novalidate="novalidate">
  <input name="utf8" type="hidden" value="&#x2713;" />
  <input name="authenticity_token" type="hidden" value="..." />
  <input id="user_email" name="user[email]" type="email" value="" />
  <input id="user_password" name="user[password]" type="password" />
  <input name="user[remember_me]" type="hidden" value="0" />
  <input id="user_remember_me" name="user[remember_me]" type="checkbox" value="1" />
  <input name="commit" type="submit" value="Login" />
</form>
```

**Important gotcha found from this capture:** the page's `<meta>` tags put `content` *before*
`name` — `<meta content="..." name="csrf-token" />`, not the more commonly assumed
`<meta name="csrf-token" content="...">`. An attribute-order-sensitive regex silently fails to
extract the token from this. `auth.py`'s `extract_csrf_token` uses BeautifulSoup instead of a
regex specifically because of this.

`POST /users/sign_in` with `user[email]`, `user[password]`, and the `authenticity_token` returns
a `302` redirect to `/` on success (confirmed from a real login's request log: `POST
/users/sign_in HTTP/1.1" 302`, followed by the redirect being auto-followed to `GET / HTTP/1.1"
200`). A failed login re-renders the same form at `/users/sign_in` with a `200` status instead of
redirecting — that's the signal `auth.py`'s `_has_password_form` check relies on to detect
failure.

`auth.py`'s `login_with_credentials` still scrapes the form fields at runtime rather than
hardcoding these names, since that's more robust to the page changing later, but the values it
discovers now match a real, confirmed capture rather than an assumption.

## Confirmed read endpoints

### `GET /api/buildings`

Headers seen: `X-CSRF-Token`, `X-Requested-With: XMLHttpRequest`, `Accept: */*`. (`X-LSS-Manager`
was also present but appears to be that other tool's own cosmetic identifier — not required.)

Returns a JSON array, one object per building owned by the account:

```json
{
  "id": 778773,
  "personal_count": 400,
  "level": 12,
  "building_type": 0,
  "caption": "ACFD Station 10",
  "latitude": 37.70879027778312,
  "longitude": -122.18151211651276,
  "extensions": [
    { "caption": "Water Rescue Extension", "available": true, "enabled": false, "type_id": 2 }
  ],
  "storage_upgrades": [],
  "leitstelle_building_id": 376606,
  "small_building": false,
  "enabled": true,
  "personal_count_target": 400,
  "hiring_phase": 3,
  "hiring_automatic": true,
  "updated_iso": "2023-04-12T23:39:39-04:00",
  "complex_type": "building",
  "generates_mission_categories": ["fire"]
}
```

Field notes:
- `building_type` — int, matches the enum in "Building type enum" below.
- `enabled` — the station's in-service/out-of-service state (what our `service` command toggles).
- `leitstelle_building_id` — the dispatch center this building is currently assigned to (FK to
  another building of type `1`/Dispatch Center). This is what `dispatch` reassignment needs to
  change.
- `extensions` — array of purchasable extensions (e.g. "Water Rescue Extension" on a Fire
  station), each with `available`/`enabled`/`type_id`. **This strongly suggests "water rescue" is
  an extension on a Fire station rather than its own `building_type`** — see the caveat under
  Building type enum.
- `academies` (Fire/Police/EMS academy buildings) additionally include a `schoolings` array (seen
  empty in this capture; schema not yet confirmed with real contents).
- Response is large (1.4 MB+ for ~1,000 buildings) — cache and diff locally rather than
  re-fetching per action.

### `GET /api/buildings/:id`

Same schema as above, single object. Useful for refreshing one building's state after an action
without re-pulling the whole list.

### `GET /api/vehicles`

Returns a JSON array, one object per vehicle across all the account's buildings (17 MB+ for
~4,000 vehicles in this capture):

```json
{
  "id": 780883,
  "building_id": 376637,
  "vehicle_type": 10,
  "working_hour_start": 0,
  "working_hour_end": 0,
  "alarm_delay": 0,
  "max_personnel_override": 2,
  "assigned_personnel_count": 0,
  "ignore_aao": false,
  "tractive_vehicle_id": null,
  "tractive_random": false,
  "queued_mission_id": null,
  "fms_real": 2,
  "fms_show": 2,
  "prisoner_transportation_delay": null,
  "hospital_automatic": false,
  "hospital_own": false,
  "hospital_right_building_extension": false,
  "hospital_automatic_return": false,
  "hospital_max_price": 50,
  "hospital_max_distance": 100,
  "hospital_free_space": 0,
  "equipments": [],
  "assigned_equipments": [],
  "caption": "Patrol Car - 1 - FPD",
  "vehicle_type_caption": "",
  "target_type": null,
  "target_id": null
}
```

Notes:
- `assigned_personnel_count` is per-vehicle, but the **required** staffing count per
  `vehicle_type` is not in this payload — that appears to be static game data. We'll need either
  a reference table captured from the vehicle-purchase page or another endpoint (not yet seen)
  before implementing auto-assign-personnel and education-requirement checks.
- `fms_real`/`fms_show` are the game's radio status codes (in service, out of service, etc.) —
  useful for `service` state logic once we confirm the code meanings against the game UI.

### `GET /building/buildings_json?load_vehicles=false&limit=3500`

An alternate/legacy endpoint (referenced by `assets/workers/buildings_worker-*.js`, i.e. used by a
map-rendering web worker). Response body was not retained in the capture (>1 MB bodies get
dropped by Chrome's DevTools by default). **Schema unconfirmed** — recommend using `/api/buildings`
instead, since its schema is confirmed and it's what the established community tool
(LSS-Manager) relies on.

### Live credit balance — confirmed via a real account (2026-07)

There's no dedicated API endpoint for this, but it doesn't need one — the current credit balance
is embedded directly in the HTML of **every** authenticated page load, including the plain
homepage (`GET /`) already fetched for the CSRF token. Getting this right took two passes:

**First pass (wrong):** DevTools "Inspect Element" on the nav bar showed
`<span class="credits-value">2,456,656,440</span>` and it looked like a simple text-content
read. It isn't — that's the *live DOM after JavaScript runs*, not the raw HTML. A real `GET /`
capture (`view-source`-equivalent) showed the same span **empty**:

```html
<img class="navbar-icon" src="/images/mc_credits_flat.png">
<span class="credits-value"></span>
```

**Second pass (confirmed working):** searching the same raw HTML for the word "credit" turned up
an inline script near the bottom of the page:

```html
<script> $(function() { creditsUpdate(2456738985); coinsUpdate(193); messageUnreadUpdate(0); }); </script>
```

`creditsUpdate(<n>)` is what actually populates that span client-side — but critically, this
call itself is **server-rendered with the real current value at request time**, not delivered
later via AJAX/websocket. So a plain `GET /` already contains the live number; it's just in this
inline script, not the span. `mc_client.parse_credits_balance` regexes `creditsUpdate\((\d+)\)`
out of the raw HTML directly, ignoring the (empty) span entirely.

**Lesson for future endpoint/parsing work on this game:** DevTools' Elements/Inspect panel shows
the post-JavaScript DOM, which can differ from what a plain HTTP client actually receives — when
a value looks server-rendered but a parser can't find it, check whether it's actually populated
by inline JS elsewhere in the same response before assuming a different endpoint is needed.

`get_credits_balance()` must still be fetched as a plain navigation request, not through this
client's default AJAX headers (`X-Requested-With`/`Accept: application/json`) — those are
confirmed (see the CSRF-token section above) to make MissionChief respond differently to the same
URL, so it bypasses `_request()` and goes through the session directly, the same way `auth.py`'s
CSRF-token fetch does.

The nav bar's `href="/credits"` suggests a dedicated credits/finance page exists too, with
presumably more detail (transaction history?) — not investigated, since `creditsUpdate(...)` is
all the budget safety checks need.

### `GET /reverse_address?latitude=<lat>&longitude=<lng>`

MissionChief's own reverse-geocoding endpoint. Returns a plain-text address string, e.g.:

```
Morgan Territory Road, 94157, Contra Costa County
```

A same-origin alternative/fallback to RLM's `/api/reverse-geocode` — no extra auth beyond the
normal session.

### `POST /buildings/vehiclesMap` (form-encoded, not needed for automation but documented)

Body: `building_ids[]=<id>&building_ids[]=<id>...`. Response is `text/html` but is actually a
stream of JS calls like `vehicleMarkerAdd({"id":...,"b":<building_id>,"fms":2,"c":"<caption>","t":<vehicle_type>});`
— used to populate the live map. Not needed for our CLI/planner.

### `GET /driving/vehicles?include_alliance=1` and `POST /faye`

Real-time vehicle position/route polling and a Bayeux/CometD push channel, respectively. Neither
is needed — our app polls on its own rate-limited schedule instead.

## Confirmed write endpoint: build a new station

### `GET /buildings/new`

Returns an HTML fragment with the building-creation form:

```html
<form action="/buildings" method="post" id="new_building" class="... building-form">
  <input type="hidden" name="authenticity_token" value="...">
  <select name="building[building_type]">...</select>
  <input name="building[name]" maxlength="40">
  <input type="hidden" name="building[latitude]">
  <input type="hidden" name="building[longitude]">
  <input type="hidden" name="build_with_coins">
  <input type="hidden" name="build_as_alliance">
  <select name="building[leitstelle_building_id]">...</select>  <!-- optional: existing dispatch centers -->

  <!-- one "detail_<building_type>" block per type, e.g. for Fire station (type 0): -->
  <select name="building[start_vehicle_feuerwache]">
    <option value="0">Type 1 fire engine</option>
    <option value="1">Type 2 fire engine</option>
    <option value="13">Quint</option>
    <option value="18">Rescue Engine</option>
  </select>
  <input type="submit" name="commit" value="Build 2,066,894 Credits">
  <input type="submit" name="commit" value="Build 15 Coins">
</form>
```

- **`POST /buildings`** with: `authenticity_token`, `building[building_type]`, `building[name]`,
  `building[latitude]`, `building[longitude]`, optionally `building[leitstelle_building_id]`, the
  type-specific `building[start_vehicle_*]` field where applicable, and
  `commit=Build <price> Credits` to pay with in-game currency.
- **Never use the `Build <n> Coins` submit value** — Coins are real-money premium currency. Our
  app must only ever submit the Credits button, and should treat any Coins-price fallback as a
  hard stop, not something to automate around.
- **Update (Phase 4, real test build against a live account):** a successful `POST /buildings`
  returns `302` redirecting to `/buildings`. `mc_client.py`'s `create_building` deliberately does
  **not** follow that redirect (`allow_redirects=False`) — the redirect target's own real
  behavior/response shape is still unconfirmed, and `requests` following it by default meant a
  perfectly successful build could get misreported as a failure if that target page ever returns
  a non-2xx status for any reason. Success is instead verified independently: `GET
  /api/buildings` is read before and after the POST, and a new entry of the right
  `building_type` appearing is what counts as success. The error-response shape (e.g.
  insufficient funds) is still unconfirmed — `create_building` doesn't try to parse one, it just
  reports "no new building appeared" and asks you to check manually.
- Each building type has its own `detail_<building_type>` block with its own field name for the
  starting-vehicle select (e.g. `start_vehicle_feuerwache` for a regular Fire station,
  `start_vehicle_feuerwache_kleinwache` for the small variant) — the exact field name per type
  needs enumerating fully (we only captured Fire station and Fire station (Small) in detail; other
  types may have analogous `start_vehicle_*` fields or none at all, e.g. Dispatch Center and
  Staging area had no such field and a flat "Build 0 Credits").
- **Credit prices are dynamic** (scale with account/alliance progression) — e.g. Fire station
  showed `2,066,894 Credits` and Police station `1,662,756 Credits` in this account, not the
  round numbers you'd expect from a fixed price table. Never hardcode prices; always re-fetch
  `/buildings/new` immediately before submitting to read the current price and confirm it's
  within the run's remaining budget/reserve.

### Building type enum (confirmed from `/buildings/new`, captured 2026-07-05)

| id | name |
|----|------|
| 1  | Dispatch Center |
| 0  | Fire station |
| 13 | Fire station (Small station) |
| 4  | Fire academy |
| 3  | Ambulance station |
| 16 | Ambulance station (Small station) |
| 2  | Hospital |
| 14 | Clinic |
| 19 | Rescue (EMS) academy |
| 7  | Police academy |
| 5  | Police station |
| 15 | Police station (Small station) |
| 8  | Police Aviation |
| 6  | Medical helicopter station |
| 12 | Rescue boat dock |
| 11 | Fire boat dock |
| 23 | Coastal Rescue Station |
| 26 | Lifeguard Post |
| 24 | Coastal Rescue School |
| 25 | Coastal Air Station |
| 9  | Staging area |
| 17 | Firefighting plane station |
| 18 | Federal Police Station |
| 22 | Fire Marshal's Office |
| 10 | Prison |
| 27 | Tow Truck Station |
| 28 | Mountain Rescue Station |

**Caveat — "water rescue" and "riot police" are not distinct building types here.** The task
brief lists both as buildable station types, but this dropdown has no such options. The most
likely explanation, based on a real building's `extensions` array containing a `"Water Rescue
Extension"` entry (`type_id: 2`), is that these are **extensions purchasable on an existing
station** (e.g. a Fire station), not separate buildings created via `/buildings/new`. This needs
confirming against another `/buildings/:id` sample with the extension enabled, or the game's own
help text, before the planner treats them as build targets vs. expansion targets.

### Existing dispatch centers (sample, for `building[leitstelle_building_id]`)

The dropdown lists the account's existing dispatch centers by name → id, e.g. `376606` → "ACRECC",
`2018584` → "US Dispatch 1", etc. Confirms dispatch-center assignment *at creation time* is just
this form field; reassignment of an *already-built* station to a different center is a separate,
not-yet-captured action.

## Phase 5 write actions (confirmed, 2026-07-06 HAR captures)

Two HAR captures from the same real session: the first only covered personnel assignment; a
second, fuller one (recorded from before each page was opened) also covered expand, service
toggle, vehicle purchase, and hiring. All confirmed against `building_id=5558174` ("Alameda County
Fire Department Station 1", a Fire station) and its two vehicles (`14577420`, `14578509`).

### Station expansion — `GET /buildings/<id>/expand_do/credits?level=<n>`

- `GET /buildings/<id>/expand` lists every level with its Credits/Coins price as plain links, e.g.
  `<a href="/buildings/5558174/expand_do/credits?level=0">Expand (10,000 Credits)</a>` and a
  parallel `expand_do/coins?level=0` link ("25 Coins" — **never automate this one**, same
  Credits-only rule as building creation). Price scales per level exactly like build prices
  (10,000 → 60,000 → 160,000 → ... → 3,760,000 by level 38 in this account) — always re-fetch
  `/buildings/<id>/expand` live immediately before submitting, never cache/hardcode.
- Submitting the credits link is a plain `GET` (not a form POST) with no CSRF header — confirmed
  `302` redirect to `/buildings/<id>` on success. `level` is 0-indexed and must match one of the
  levels actually listed on the expand page (not just "current level + 1" — confirm from the page).
- Verify success via `GET /api/buildings/<id>`'s `level` field (confirmed present) increasing,
  same before/after-diff pattern as `create_building`.

### Service-state toggle — `GET /buildings/<id>/active`

- Plain `GET`, no params, no CSRF header, confirmed `302` redirect back to `/buildings/<id>`.
  Captured twice in the same session (disable, then re-enable) — same URL both directions, a pure
  toggle.
- Verify via `/api/buildings/<id>`'s `enabled` field (confirmed present) flipping.

### Vehicle purchase — `GET /buildings/<id>/vehicle/<id>/<vehicle_type_id>/credits?building=<id>&return_tab=<tab>`

- `GET /buildings/<id>/vehicles/new` lists every purchasable vehicle type for that station type,
  grouped into tabs (`return_tab` values seen: `fire_engine`, `ambulance`, `airport`, `brush`,
  `tanker`, `trailer`, `tow_trucks`, `water_rescue`, `coastal_rescue`, `fire_investigation`,
  `fire_support`, `firefighting_other`, `disaster_response`, `water_damage_and_flood`, `container`
  — station-type-dependent). Each vehicle is a `<div class="vehicle_type well">` with an `<h3>`
  name (e.g. "Type 1 fire engine"), stats (Max Crew, Capacity, water/foam/pump for engines), and
  paired Credits/Coins purchase links, e.g. `<a href="/buildings/5558174/vehicle/5558174/0/credits?building=5558174&return_tab=fire_engine">5,000 Credits</a>`
  — the `0` here is a **vehicle-type id local to the purchase catalog** (distinct from the
  building-type enum), confirmed values in this capture: `0` = Type 1 fire engine (5,000cr),
  `1` = a second Type 1 variant (5,000cr), `13` = Type 1 (19,000cr), others unseen. Always parse
  the live page rather than hardcoding these ids/prices.
- Submitting the credits link is a plain `GET`, confirmed `302` redirect to
  `/buildings/<id>/vehicles/new` (not `/buildings/<id>` — different from expand/build/toggle).
- Verify success via `GET /api/vehicles`, diffing for a new entry with matching `building_id`
  (same before/after pattern as `create_building`/`get_buildings`).

### Hiring — `GET /buildings/<id>/hire_do/<days>`

- `GET /buildings/<id>/hire` shows: instant paid options (`hire_do/coins` "Recruit now (5 coins)",
  `hire_do/coins_multiple` "Recruit 5 now (20 Coins)" — **never automate these, Coins-only**), and
  free day-based recruiting phase links — confirmed `hire_do/1`, `hire_do/2`, `hire_do/3` ("Recruit
  1/2/3 day(s)") in this account. The task brief mentions 1/3/7-day options elsewhere, so **parse
  the actual links present on the live `/hire` page** rather than assuming a fixed set — this
  account only offered 1/2/3.
- Once a recruiting phase is active, the page instead shows "The recruiting phase still runs for N
  day(s)" and a `hire_do/0` link ("Cancel recruitment phase") in place of the day-options.
- Submitting a day-option is a plain `GET`, confirmed `302` redirect back to `/buildings/<id>/hire`.
- Verify via `/api/buildings/<id>`'s `hiring_phase` field (confirmed present) changing — it does
  **not** immediately add personnel; it starts/extends a timed phase, matching the "Every night
  it's possible to add a person ... when a promotion is active" copy on the page. Actual new
  personnel arriving is a separate, later, passive event — not something a single `hire` call can
  verify synchronously the way `create_building`/expand/vehicle-purchase can.
- `hire_with_education` (`POST`, separate form on the same page, `education` field like
  `feuerwehrschule,0` = HazMat) was visible but never submitted in this capture — still unconfirmed.

### Personnel-to-vehicle assignment — `POST /vehicles/<vehicle_id>/zuweisungDo/<personnel_id>`

- `GET /buildings/<id>/personals` is the roster page confirming how to discover personnel_ids:
  a `#personal_table` with one `<tr>` per employee, `<input type="checkbox" ... value="<personal_id>">`,
  name, education, **currently-assigned vehicle caption** (e.g. "Type 1 fire engine"), and a
  real-time duty-status column (`Available` / `In a Vehicle: <link>`).
- `GET /vehicles/<vehicle_id>/zuweisung` is the per-vehicle assign/unassign page: same roster,
  filtered to one `<table id="personal_table">`, each row's action `<td>` is either `<a
  class="btn btn-success" href="/vehicles/<id>/zuweisungDo/<personal_id>">Assign vehicle</a>` (not
  yet bound to *this* vehicle) or, once bound, `<a class="btn btn-default btn-assigned"
  href="...">Remove binding</a>` — **confirms `zuweisungDo` is a toggle whose direction depends on
  current binding state**, discoverable by which link text/class is present before clicking.
- **The earlier "open oddity" is resolved.** The Status column (`Available` / `In a Vehicle: X`) is
  that employee's *live mission-dispatch status* — whether they're currently out on a call in some
  vehicle — which is entirely independent of the *permanent crew-roster binding* `zuweisungDo`
  controls. A person can show "In a Vehicle: X" (mid-call) while their assign/unassign link for a
  completely different vehicle Y still says "Assign vehicle", because roster binding and real-time
  dispatch occupancy are unrelated axes. Don't conflate them; verify assignment success via
  `/api/vehicles`'s `assigned_personnel_count` (confirmed field), not the Status column.
- `GET /vehicles/<id>/update_required_personnel_alert` fires after each assignment (empty `200`
  body) — a UI badge refresh, not load-bearing.
- Education/training requirement surfacing on assignment (e.g. a vehicle requiring a trained
  operator) was never observed — still unconfirmed, needed to implement "skip and log" behavior
  for personnel who don't qualify.

### Confirmed API field additions

- `GET /api/buildings/<id>` (single-building detail): `personal_count`, `personal_count_target`,
  `hiring_phase`, `hiring_automatic`, `enabled`, `level`, `leitstelle_building_id`.
- `GET /api/vehicles` (list): `assigned_personnel_count`, `vehicle_type`, `building_id`, `caption`,
  `fms_real`/`fms_show` (status codes, meaning unconfirmed).

## Not yet captured

- **Resolved — `building[name]` has a hard 40-character limit.** The repeated real failure on
  "Union City Police Department- Fremont, CA" (41 characters) is now explained: the captured
  failed-POST response body (`response_text`, added after the first investigation attempt) shows
  the form re-rendered with `<input ... id="building_name" maxlength="40" ...><span
  class="label label-danger">is too long (maximum is 40 characters)</span>`, i.e. a genuine
  `200`-with-validation-error, not a diff/timing bug. Every other station in the same runs built
  fine because their rendered names happened to be ≤40 characters. Fixed in `planner.render_name`:
  if the rendered name exceeds `MAX_BUILDING_NAME_LENGTH = 40`, the `{poi_name}` portion is
  shortened first (not the `{city}` suffix, which matters more for keeping stations organized by
  region), with a final hard truncation as a safety net.
- The error-response shape for other kinds of `POST /buildings` failures (insufficient funds,
  etc.) is still unconfirmed — `create_building` keeps the raw failed-POST response body on
  `BuildResult.response_text` (empty on success) so any future failure can be diagnosed from the
  log instead of guessing at markup that's never been seen.
- Dispatch-center creation, and **re-assigning an already-built station** to a different center —
  no reassignment UI/endpoint appeared anywhere in either capture, including on `/buildings/<id>`
  itself. Still needs its own capture.
- `hire_with_education` (paid/trained hiring) — form seen, never submitted.
- Education/training gating on personnel assignment.
- Equipment purchase/assignment.

Still need captures covering: `/buildings/:id/expand` (or equivalent), the vehicle-purchase UI,
the hiring UI, the `/vehicles/:id/zuweisung` personnel-listing page, toggling a station out of
service and back, and (if reachable) reassigning a station's dispatch center.
