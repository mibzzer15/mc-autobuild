# RLM (Realism Location Marker) API — Research Findings

**Status:** Confirmed. This environment's network egress policy blocked
`realism-location-marker.com` during initial research, but access opened up later and the API
turned out to expose its own OpenAPI schema (`/openapi.json`) plus a public map webapp (`/map`)
with unminified client JS — both gave a direct, authoritative look at the real contract, no
guessing required. Everything below is confirmed against live responses (2026-07).

## Sources

- GitHub repo <https://github.com/Norbit-Online/Realism-Location-Marker> (loader scripts + wiki)
  — confirms the endpoint list and request-header scheme, but not the actual query/response
  shapes (see "What the public repo doesn't contain" below).
- `https://realism-location-marker.com/map` — RLM's public, no-login-required web map. Its
  (unminified) client-side JS makes real calls to `/api/pois`, `/api/poi-types`, and
  `/api/poi-stats`, which is what nailed down the actual contract.
- `https://realism-location-marker.com/openapi.json` — the backend is FastAPI and auto-exposes
  its full OpenAPI schema (also browsable at `/docs`, `/redoc`). This gave exact, typed parameter
  definitions for every endpoint, including several the map page doesn't even use
  (`/api/dispatch-centers`, `/api/building-types`, `/api/game-config`, etc.).
- Live test calls made directly against the production API (small, targeted, low-volume — see
  the "Being a good citizen" note at the end).

## What the public GitHub repo doesn't contain

The three loader variants (`loader.stable.user.js`/`.beta.`/`.dev.`) are Tampermonkey bootstrap
scripts. None of them contain POI-fetching logic — they only detect the current MissionChief
domain, set `window.RLMConfig`, build tracking headers (see below), and fetch+`eval()` a
server-side "entry point" script that isn't in the repo and changes independently of it. The
actual client logic lives in that dynamic script and in the public map page's own JS, not in
version control.

## Base URL and request headers

Base URL: `https://realism-location-marker.com`. No MissionChief session cookie or CSRF token is
ever required — confirmed by calling every endpoint below with a plain, cookie-less `curl`.

From the userscript loader's `getCommonHeaders()`, the in-game script additionally sends
attribution headers (not required, but good practice to send if we have the values):

```js
{
  'X-RLM-Script': 'true',
  'X-RLM-Version': '<script version>',
  'X-RLM-UserID': '<MissionChief user_id, if known>',
  'X-RLM-Username': '<MissionChief username, if known>',
  'X-RLM-AllianceID': '<MissionChief alliance_id, if known>',
}
```

The public map page sends none of these — plain requests work fine.

## Confirmed endpoints

### `GET /api/pois` — the POI database itself

Parameters (from the OpenAPI schema):

| param | required | type | notes |
|---|---|---|---|
| `poi_type` | **yes** | string | one of the `table_name` values from `/api/poi-types`, e.g. `poi_fire_station` |
| `page` | no | integer, default `1` | 1-indexed |
| `page_size` | no | integer, default `10000` | the public map page always requests the full default; there's no documented max, but see the rate-limiting note below |
| `north` | no | number | bounding-box latitude |
| `south` | no | number | bounding-box latitude |
| `east` | no | number | bounding-box longitude |
| `west` | no | number | bounding-box longitude |

**Bounding box filtering is real and confirmed working** — `north`/`south`/`east`/`west` together
narrow results to that box (tested: a global `poi_fire_station` query has 141,621 indexed
results; the same query with a small Bay Area bbox returned exactly the ~10 stations in that box).
This is exactly what our `rlm_client.py` needs for "region: bbox" queries. All four params are
optional and independent — you can also omit them entirely and just paginate through everything
of a given `poi_type` worldwide (which is what the public map page does; not recommended for us —
prefer a bbox to avoid pulling and caching far more than a given run's region needs).

Response shape:

```json
{
  "total_count": 320,
  "pois": [
    {
      "id": 1,
      "name": "Incident Control Point",
      "lat": -43.2088427,
      "lng": 171.7146897,
      "address": "BMX/Pump Track, Castle Hill Village, Selwyn District, Canterbury, 7580, New Zealand / Aotearoa",
      "osm_id": 961854893,
      "osm_type": "way",
      "status": "operational"
    }
  ]
}
```

**Important gotcha — coordinate field names are not consistent across `poi_type` values.** A
`poi_control_centre` query returned `lat`/`lng`; a `poi_fire_station` query returned
`latitude`/`longitude` for the same conceptual field:

```json
{"id":19415,"name":"Alameda County Fire Station 6","latitude":37.7027953,"longitude":-122.0536777,
 "address":"Alameda County Fire Station 6, ... United States","osm_id":544634297,"osm_type":"way",
 "status":"operational"}
```

The OpenAPI spec documents this endpoint's response as an untyped `{}` (no Pydantic response
model), consistent with each `poi_type` being backed by its own DB table with independently
evolved column names. **Our `rlm_client.py` must read coordinates defensively** — try `lat`/`lng`
first, fall back to `latitude`/`longitude`. The public map page's own JS does exactly this
(`poi.lon || poi.lng`), which is what tipped us off.

Other fields seen but not present on every record: `needs_review` (0/1 — the map page treats a
missing/placeholder name, e.g. `"To Be Confirmed"`, as needing review), and separately
`city`/`state`/`country` on some records vs. a single combined `address` string on others (the map
page's popup builder defensively joins whichever of `address`/`city`/`state`/`country` are
present and non-empty).

`osm_id`/`osm_type` are direct references back to OpenStreetMap (`way`/`node`) — useful as a
stable external id for our own dedupe/audit trail, and lets us construct an
`openstreetmap.org/{osm_type}/{osm_id}` link for a human to double-check a POI.

### `GET /api/poi-types` — the `poi_type` taxonomy

No parameters. Returns the full list of queryable POI types (confirmed 10 as of this capture):

```json
[
  {"table_name": "poi_fire_station", "friendly_name": "Fire Stations", "emoji": "🚒", "osm_tag": "emergency=fire_station"},
  {"table_name": "poi_police", "friendly_name": "Police Stations", "emoji": "👮", "osm_tag": "amenity=police"},
  {"table_name": "poi_ambulance_station", "friendly_name": "Ambulance Stations", "emoji": "🚑", "osm_tag": "emergency=ambulance_station"},
  {"table_name": "poi_disaster_response", "friendly_name": "Disaster Response", "emoji": "🚨", "osm_tag": "emergency=disaster_response"},
  {"table_name": "poi_control_centre", "friendly_name": "Control Centers", "emoji": "📡", "osm_tag": "emergency=control_centre"},
  {"table_name": "poi_hospital", "friendly_name": "Hospitals", "emoji": "🏥", "osm_tag": "amenity=hospital"},
  {"table_name": "poi_prison", "friendly_name": "Prisons", "emoji": "🔒", "osm_tag": "amenity=prison"},
  {"table_name": "poi_lifeguard", "friendly_name": "Lifeguard Stations", "emoji": "🏊", "osm_tag": "emergency=lifeguard"},
  {"table_name": "poi_mountain_rescue", "friendly_name": "Mountain Rescue", "emoji": "⛰️", "osm_tag": "emergency=mountain_rescue"},
  {"table_name": "poi_doctors", "friendly_name": "Doctors/Clinics", "emoji": "👨‍⚕️", "osm_tag": "amenity=doctors"}
]
```

`osm_tag` shows each type is sourced from that literal OpenStreetMap tag — RLM's database is
essentially a curated, MissionChief-flavored view over OSM emergency/amenity data. Notably there's
no dedicated `poi_type` for "water rescue" or "riot police" here either — consistent with what we
found on the MissionChief side (docs/missionchief-api.md): those are probably building
*extensions* rather than distinct POI/building categories, on both sides of this integration.

### `GET /api/poi-stats` — coverage stats, not needed for building automation

No parameters. Per-type indexing coverage (how much of OSM's data RLM has ingested/reviewed):

```json
{"type":"poi_control_centre","name":"Dispatch Centres","osm_total":614,"indexed_count":317,"percentage":51.63,"last_updated":"2025-09-09 05:13:31"}
```

Useful for an informational dashboard note ("RLM has X% coverage for this POI type") but not
needed for the planner itself.

### `GET /api/building-types` — POI type → MissionChief `building_type` id, **per game server**

Parameters: `game_world` (optional but you need it — see below), `type` (optional, filter to one
POI type's friendly name).

**Confirmed per-server, exactly as the loader's "server-specific building IDs" comment implied.**
Calling this with no `game_world` returns an unfiltered dump — many rows with the *same* `name`
but *different* `building_id` values and no field telling you which server each belongs to,
useless on its own. Filtering by `game_world` (using the loader's country codes, e.g. `US`, `DE`)
gives a clean, per-server mapping:

```
GET /api/building-types?game_world=US
[{"building_id":0,"name":"Fire Station","is_small":false},
 {"building_id":5,"name":"Police Station","is_small":false},
 {"building_id":3,"name":"Ambulance Station","is_small":false},
 {"building_id":2,"name":"Hospitals","is_small":false},
 {"building_id":16,"name":"Ambulance Station","is_small":true},
 {"building_id":13,"name":"Fire Station","is_small":true}]
```

**This exactly matches our independently-confirmed MissionChief building type enum** in
docs/missionchief-api.md (Fire station=0, Police station=5, Ambulance station=3, Hospital=2,
Ambulance small=16, Fire small=13) — strong cross-confirmation that both docs are right, and that
this is the correct way to translate an RLM POI type into a `building[building_type]` value for
`POST /buildings`.

```
GET /api/building-types?game_world=DE
[{"building_id":0,"name":"Fire Station","is_small":false},
 {"building_id":6,"name":"Police Station","is_small":false},
 {"building_id":2,"name":"Ambulance Station","is_small":false},
 {"building_id":19,"name":"Police Station","is_small":true},
 {"building_id":18,"name":"Fire Station","is_small":true}]
```

Confirms the IDs genuinely differ by server (DE's Police Station is `6`, not `5`; DE's Ambulance
Station is `2`, the same id US uses for Hospital!). **`rlm_client.py` must always pass the
correct `game_world` for the account's actual server** (derived the same way the loader does it —
matching the MissionChief domain to a country code) and must never assume US's numbering applies
elsewhere, or even that a type present on one server exists on another (this DE response has no
Hospital entry at all, unlike US).

Not yet confirmed: the complete list of valid `game_world` values (presumably the loader's full
country-code list — `US, UK, AU, DE, PT, ES, NL, PL, SE, IT, FR, RU, CZ, DK, JP, KR, NO, RO, SK,
TR, BR, MX, UA, FI` — only `US` and `DE` were actually tested here) and the exhaustive `building_id`
→ `name` mapping for every server. Also unconfirmed: how "water rescue"/"riot police"-style
extensions map through here, since they don't appear as their own `poi_type` or `building_type`
name — needs a real per-account test build against Phase 4 to nail down.

### `GET /api/dispatch-centers?game_world=<code>` — a naming reference table, not location data

```
GET /api/dispatch-centers?game_world=US
[{"id":1,"dispatch_id":1,"name":"Dispatch Center"}]
```

`game_world` is **required** (omitting it returns a `422` validation error). This is *not* a list
of real-world dispatch center locations — it's a minimal per-server naming/id reference (for US,
just one generic "Dispatch Center" type). **Actual real-world dispatch center locations come from
`GET /api/pois?poi_type=poi_control_centre`** instead (confirmed above — real lat/lng records like
"JRCC Tahiti", "CROSS La Runion"). Don't conflate the two: use `poi_control_centre` POIs for
"where are real dispatch centers," and this endpoint only if we ever need the server's own
dispatch-center building-type id/name.

### `GET /api/reverse-geocode?lat=<lat>&lon=<lon>`

Confirmed to exist and take `lat`/`lon` (both required, per the OpenAPI schema). Not yet given a
live test call in this pass (kept total request volume low) — described as getting "address
details for coordinates from our database," i.e. reverse geocoding against RLM's own indexed
data rather than a third-party geocoder. MissionChief's own `GET /reverse_address` (confirmed in
docs/missionchief-api.md) is a same-origin fallback/alternative if this one is ever slow or
unavailable.

### `GET /api/version`

```json
{"success":true,"version":"7.0.2","status":"Production","api_version":"1.0","build_date":"2025-09-20","features":["POI","Building Types","Dispatch Centers","MCID Verification"]}
```

Useful for a startup sanity check / cache-invalidation signal.

### `GET /api/game-config?domain=<domain>` — attempted, not working yet, not required

Meant to return "game-specific configuration including building types and translations" for a
given domain. Tried `domain=www.missionchief.com` and `domain=missionchief.com` — both returned
`{"detail":"404: Game server not found"}`. The exact expected domain identifier format is
unconfirmed. Not needed for our purposes since `/api/building-types?game_world=US` already gives
us the mapping we need; not worth further trial-and-error against the live API for a
nice-to-have.

### Many other endpoints exist but are out of scope

The OpenAPI spec lists ~50 paths total — loader/script-serving endpoints, Discord/MCID
verification, feedback submission, Stripe webhooks, badge images, loader usage stats, etc. None
of these are relevant to reading POI data or building stations; omitted here for brevity.

## Being a good citizen of this API

This is a community-run, unauthenticated API with no visible rate-limit headers in responses
tested so far. All research calls made here were small (single-digit to low-double-digit request
count, mostly `page_size` capped well under the 10,000 default, several endpoints called exactly
once). `rlm_client.py` must still:

- Always pass a real bounding box for the region being processed rather than paging through an
  entire worldwide `poi_type` (the public map page does the latter, but it's a human-driven,
  occasional action — our tool runs unattended and must be more conservative).
- Cache responses locally per region with a configurable TTL, and never re-fetch within that TTL
  even across repeated runs (per the project's "one fetch per region per run" requirement).
- Send the `X-RLM-*` attribution headers when we have real MissionChief account context, matching
  what the official loader does.

## What's still open for later phases

- Full enumeration of valid `game_world` codes and their complete `building_type` mappings
  (only US and DE spot-checked here).
- How "water rescue"/"riot police"-equivalent extensions surface through this API, if at all —
  may simply not be modeled by RLM and would need to come from a different source or manual
  config.
- A live test of `/api/reverse-geocode`'s exact response shape.
- Whether repeated/bulk querying triggers any rate limiting in practice (no evidence either way
  yet — treat conservatively regardless, per above).
