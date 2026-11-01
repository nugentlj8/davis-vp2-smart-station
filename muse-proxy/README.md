# Muse proxy — an allow-list gateway between an AI assistant and Home Assistant

A tiny, standard-library-only Python proxy that lets an AI assistant (here, Muse, via a
home-network tunnel) read the weather station and control a few lights and fans, **without
ever seeing the Home Assistant token** and without being able to touch anything not listed.

- **Control:** lights and fans only (`light.turn_on/turn_off`, `fan.turn_on/turn_off/set_percentage`).
- **Read only:** station sensors, long-term history, the archived records, the NWS forecast, thermostat status.
- **The thermostat cannot be changed** — there are no climate services in the code at all.
- Everything it can touch is listed in `allowed.json`; edits are picked up within seconds.

Full setup, security model and troubleshooting: [`docs/muse-proxy-setup.md`](../docs/muse-proxy-setup.md).

Quick start: copy `.env.example` to `.env`, add your token, edit `allowed.json`, then `docker compose up -d --build`.
