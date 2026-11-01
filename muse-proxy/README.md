# Muse proxy — an allow-list gateway between an AI assistant and Home Assistant

A tiny, standard-library-only Python proxy that lets an AI assistant (here, Muse, via a
home-network tunnel) read the weather station and control a few lights and fans, **without
ever seeing the Home Assistant token** and without being able to touch anything not listed.

- **Control:** lights and fans only (`light.turn_on/turn_off`, `fan.turn_on/turn_off/set_percentage`).
- **Read only:** station sensors, long-term history, the archived records, the NWS forecast, thermostat status.
- **The thermostat cannot be changed** — there are no climate services in the code at all.
- Everything it can touch is listed in `allowed.json`; edits are picked up within seconds.

## Setup
1. In Home Assistant create a **non-admin** user (e.g. `muse`), log in as it, and create a
   long-lived access token (profile → Security).
2. Copy `.env.example` to `.env` and paste the token. Optionally set `MUSE_URL_SECRET`
   to a random word; every URL then needs that prefix (`/<secret>/help`) and anything else is a 404.
3. Edit `allowed.json` to your entity IDs (Settings → Devices & services → Entities). Set `HA_URL` in
   `docker-compose.yml`.
4. `docker compose up -d --build`, then open `http://<host>:8765/<secret>/help`.

## Connecting the assistant
Give it the base URL, including the secret (`http://<host>:8765/<secret>`), and tell it to read
`/help` first; that response documents every endpoint and lists your lights, fans, sensors and rooms.
If the assistant lives outside your network, reach the proxy through a tunnel (Cloudflare Tunnel,
Tailscale, or whatever your assistant's gadget uses). **Do not port-forward 8765.** After editing
`allowed.json`, ask the assistant to re-read `/help`.

## Security model
- The HA token exists only in `.env` on the server. The assistant never sees it.
- Use a **non-admin** HA user for the token, so even a bug here can't do much.
- Only these services can ever be called: `light.turn_on/off`, `fan.turn_on/off/set_percentage`.
- Entities not in `allowed.json` return 404. Read endpoints are filtered to listed entities.
- The URL secret is obscurity, not authentication; treat it as a password and keep it off public pages.

## Weather history: two sources
| Endpoint | Source | Covers |
|---|---|---|
| `/history/<day\|week\|month\|quarter\|year\|max>` | HA long-term statistics of the live `sensor.*` entities | From when the integration was added |
| `/archive/<year\|month\|day\|hour>` | The imported `weather:*` statistics written by [`scripts/import_history_to_ha.py`](../scripts/import_history_to_ha.py) | Hand-logged console records back to 2005 |

`/history` only knows live sensors, so on its own it can't see the imported archive — `/archive`
exists for that. Archive rows are single values per period (record highs/lows, rain totals, peak
wind, etc.), not raw readings. Add `?sensors=a,b` to `/history` to select sensors.

## Adding things
Add entries to `allowed.json` as `"short_id": {"entity": "domain.entity_id", "name": "Friendly name"}`
(lights and fans also need a `"room"`). Sections: `lights`, `fans`, `sensors`, `weather`, `thermostats`.
Changes apply within seconds; a typo keeps the last good config (see `docker logs muse-proxy`).
Set `"history": false` on a sensor to hide its history.

## Troubleshooting
- `{"ok": false, "error": "not found"}` on `/help` → you set a URL secret; use `/<secret>/help`.
- 502 "Could not reach Home Assistant" → fix `HA_URL`. 502 "HTTP 401" → bad token.
- `docker logs muse-proxy` shows each request (token and secret are never logged).
