# AI-assistant gateway (muse-proxy)

An optional, tiny allow-list proxy (`muse-proxy/`, standard-library Python) that lets an AI
assistant — here, Muse — read the weather station and switch a few lights and fans, **without
ever seeing your Home Assistant token** and without being able to touch anything not listed.

- **Control:** lights and fans only (`light.turn_on/turn_off`, `fan.turn_on/turn_off/set_percentage`).
- **Read only:** station sensors, long-term history, the imported archive, the NWS forecast, thermostat status.
- **The thermostat cannot be changed** — no climate services exist in the code.

## 1. Create a restricted HA user and token
In Home Assistant: *Settings → People → Users →* add a **non-admin** user (e.g. `muse`). Log in as
that user, open its profile → *Security* → **Long-lived access tokens**, and create one.

## 2. Configure
In `muse-proxy/`:
1. Copy `.env.example` to `.env` and paste the token.
2. Set `MUSE_URL_SECRET` to a random word. It becomes part of every URL (`/<secret>/help`); anything
   without it is a 404. PowerShell can make one:
   `-join ((48..57)+(97..122) | Get-Random -Count 12 | % {[char]$_})`
3. Set `HA_URL` in `docker-compose.yml` (try `http://host.docker.internal:8123` if the container
   can't reach HA).
4. Edit `allowed.json` to your own entity IDs (*Settings → Devices & services → Entities*).
   Each entry is `"short_id": {"entity": "domain.entity_id", "name": "Friendly name"}`; lights and
   fans also need a `"room"`. Sections: `lights`, `fans`, `sensors`, `weather`, `thermostats`.
   Changes apply within seconds, no rebuild; a typo keeps the last good config
   (see `docker logs muse-proxy`). Add `"history": false` to a sensor to hide its history.

## 3. Run
    docker compose up -d --build

Then open `http://<host>:8765/<secret>/help`. Try `/weather`, `/forecast`, `/thermostat`, `/lights`.

## 4. Connect the assistant
Give it the base URL including the secret and tell it to read `/help` first; that lists every
endpoint plus your lights, fans, sensors and rooms. After editing `allowed.json`, have it re-read `/help`.
If the assistant is outside your network, reach the proxy through a tunnel (Cloudflare Tunnel,
Tailscale, etc.). **Do not port-forward 8765.**

## Weather history: two sources
| Endpoint | Source | Covers |
|---|---|---|
| `/history/<day\|week\|month\|quarter\|year\|max>` | HA long-term statistics of the live `sensor.*` entities | From when the integration was added |
| `/archive/<year\|month\|day\|hour>` | The `weather:*` statistics written by [`scripts/import_history_to_ha.py`](../scripts/import_history_to_ha.py) | Hand-logged console records back to 2005 |

`/history` can't see the imported archive, which is why `/archive` exists — run the history import
first or it will be empty. Archive rows are single values per period (record highs/lows, rain
totals, peak wind), not raw readings.

## Security model
- The token lives only in `.env` on the server; the assistant never sees it.
- Use a non-admin HA user so even a bug here can't do much.
- Only the five light/fan services above can ever be called; unlisted entities are 404.
- The URL secret is obscurity, not authentication — treat it like a password.

## Troubleshooting
- `{"ok": false, "error": "not found"}` on `/help` → you set a URL secret; use `/<secret>/help`.
- 502 "Could not reach Home Assistant" → fix `HA_URL`. 502 "HTTP 401" → bad token.
- `docker logs muse-proxy` shows each request (token and secret are never logged).
