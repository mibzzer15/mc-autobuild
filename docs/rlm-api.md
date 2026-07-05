# RLM (Realism Location Marker) API — Research Findings

**Status:** Endpoint list and request-header scheme are confirmed from source. The actual
`/api/pois` query contract and response schemas are **not yet confirmed** — this needs a live
capture before Phase 3 (planner + dry-run against RLM data) can be implemented against real data.

## Sources checked

- GitHub repo <https://github.com/Norbit-Online/Realism-Location-Marker>, cloned and inspected in
  full: `loader.stable.user.js`, `loader.beta.user.js`, `loader.dev.user.js`, `README.md`, and the
  full wiki (`Home`, `Installation-Guide`, `Troubleshooting`, `Loader-Comparison`, `Roadmap`,
  `Changelog`, `Feature-Requests`).
- Direct live requests to `https://realism-location-marker.com` — **blocked** by this automation
  environment's network egress policy (403 on the HTTPS CONNECT, both via direct request and via
  the web-fetch tool). Not something that could be routed around from here.
- A browser HAR capture of a real, logged-in MissionChief session, supplied by the user — this
  session did **not** trigger any `realism-location-marker.com` requests (RLM's POI display
  apparently wasn't invoked during that capture), so it didn't fill this gap either.

## What the public repo actually contains

The three loader variants are nearly identical Tampermonkey bootstrap scripts. None of them
contain POI-fetching logic. Each one only:

1. Detects which MissionChief-family domain/country it's running on (the game has 30+ localized
   clones — `missionchief.com`, `leitstellenspiel.de`, `missionchief.co.uk`, etc. — each mapped to
   an ISO country code in a `domainMapping` table).
2. Sets `window.RLMConfig` with `apiBaseUrl: 'https://realism-location-marker.com'` and the
   endpoint paths below.
3. Builds request headers via `getCommonHeaders()` (see below).
4. Fetches `GET https://realism-location-marker.com/api/{stable|beta|dev}-entry-point?_t=<ts>`
   and `eval()`s the response text. **This is where all the real POI-fetching/rendering/dropdown
   logic lives**, and it is not in the public repo — it's served dynamically from the RLM server
   on every page load, so static analysis of the GitHub repo cannot recover it.

The wiki is entirely user-facing install/support documentation. It confirms the *existence* of
POIs, building types, and dispatch centers conceptually (e.g. the Roadmap notes "229,221 POIs
need review" and a planned in-game POI-editing UI) but gives no request/response schema anywhere.

## Declared API surface (from `config.apiEndpoints` in the loader)

Base URL: `https://realism-location-marker.com`

| Path | Purpose (per loader config key / context) |
|---|---|
| `/api/pois` | POI/station database — the actual data source we need |
| `/api/poi-types` | POI type taxonomy |
| `/api/building-types` | Building type taxonomy — the loader's own version comment says "server-specific building IDs", implying this endpoint maps POI types to **per-game-server** MissionChief `building_type` ids (plausible, since MissionChief's own building_type numbering could differ across the 30+ localized game servers) |
| `/api/dispatch-centers` | Dispatch center data |
| `/api/reverse-geocode` | Reverse geocoding |
| `/api/version` | Version/update check |
| `/api/{stable,beta,dev}-entry-point` | Dynamically-loaded JS bundle with the actual client logic — not statically analyzable, changes server-side independent of the public repo |

## Request headers (confirmed from `getCommonHeaders()` in the loader source)

```js
{
  'X-RLM-Script': 'true',
  'X-RLM-Version': GM_info.script.version,        // e.g. "7.3.0"
  'X-RLM-UserID': window.user_id,                 // only if present on the MissionChief page
  'X-RLM-Username': window.username,              // only if present
  'X-RLM-AllianceID': window.alliance_id,         // only if present
}
```

The dev-loader variant additionally adds `X-RLM-Timestamp` and `X-RLM-Origin`.

**Important:** no MissionChief session cookie or CSRF token is ever sent to
`realism-location-marker.com` — these `X-RLM-*` headers are RLM's own attribution/analytics
(which MissionChief account is asking), not authentication. This means our `rlm_client.py` does
**not** need any MissionChief auth to talk to RLM; at most we send the MissionChief `user_id` /
`username` as an optional courtesy header, matching upstream behavior, and treat the RLM API as
effectively anonymous/open access. This also means we should be conservative and cache
aggressively regardless — being a good citizen of a community-run, unauthenticated API matters
more here, not less.

## Still unknown — blocked, needs a live capture

Because neither this environment nor the supplied HAR could observe `/api/pois` actually being
invoked, these remain **unconfirmed** and must not be guessed at in code:

- Exact query parameters for `/api/pois` — candidates based on the endpoint's evident purpose:
  bounding box (`bbox`/`north,south,east,west`?), center + radius (`lat`,`lng`,`radius`?),
  `country` (matches the loader's per-domain country code), `poi_type` filter, pagination
  (`page`/`limit`/`offset`?). All unconfirmed.
- Response schema per POI: field names for latitude/longitude, display name, POI type id,
  address, any source/confidence/verification flag, external/stable id for dedupe.
- `/api/poi-types` and `/api/building-types` response shape, and specifically the POI-type →
  MissionChief `building_type` id mapping (and whether it's genuinely per-server, per the
  loader's comment).
- `/api/dispatch-centers` response shape.
- Any rate-limit/caching signals the API advertises itself (e.g. `Cache-Control`, `ETag`,
  `Retry-After`) — directly relevant to our "one fetch per region per run, cached for a
  configurable TTL" requirement; absent explicit guidance we'll default to a conservative TTL and
  a single in-flight request at a time regardless.

## What we need next

A capture (HAR or the same text-paste format used for the MissionChief side) of a real RLM
session where POIs are visibly loaded on the map — pan/zoom to a region so `/api/pois` actually
fires — plus opening whatever RLM UI element lists POI types, building types, and dispatch
centers, so those endpoints fire too. This is required before Phase 3 (planner + dry-run against
real RLM data); Phases 1–2 (auth + read-only sync of the player's own MissionChief buildings)
don't depend on it and can proceed now.
