# AI Weather Ticker — Setup

Goal: every ~20 min, a small local LLM reads your live Davis data + a real NWS
forecast and writes a friendly 1–2 sentence ticker line ("Hot and dry at 99°F,
humidity down to 11% — bring water, the calm holds into the evening").

Runs **locally** via Ollama — free, private, no cloud API. Developed on an RTX 2060 Super (6 GB);
any GPU with ~6 GB of VRAM will do, and CPU-only works too if you don't mind slower replies.

---

## 1. Install Ollama (Windows, on the HA server box)

1. Download and install Ollama for Windows from https://ollama.com/download
2. It runs as a background service on `http://localhost:11434`.
3. Pull a model (open PowerShell):
   - **Fast, fits easily in 6 GB (recommended start):**
     `ollama pull llama3.2:3b`
   - **Nicer prose, still fits 6 GB at 4-bit:**
     `ollama pull qwen2.5:7b`  (or `ollama pull llama3.1:8b`)
4. Test it: `ollama run llama3.2:3b "Say hello in one sentence."`

> The GPU is used automatically. A 3B model answers in ~1–2 s; a 7–8B in a few
> seconds. Either is fine for a 20-minute cadence.

### Let Home Assistant reach Ollama
HA runs in Docker, so `localhost` inside the container is NOT the Windows host.
Use the host IP instead. Two options:
- Point HA at `http://192.168.1.100:11434`, AND
- Make Ollama listen on all interfaces: set a Windows environment variable
  `OLLAMA_HOST=0.0.0.0` (System Properties → Environment Variables), then restart
  the Ollama service. (By default it only listens on localhost.)

---

## 2. Add the Ollama integration + AI Task in HA

1. Settings → Devices & Services → **Add Integration → Ollama**.
2. URL: `http://192.168.1.100:11434`  → pick your model (e.g. `llama3.2:3b`).
3. This creates an **AI Task** entity, typically `ai_task.ollama` (check
   Settings → Devices & Services → Ollama → entities for the exact name; use it
   in the automation below).

---

## 3. Add a real forecast (free, no key)

Settings → Devices & Services → **Add Integration → National Weather Service (NWS)**
(US). It creates a `weather.` entity — note its exact name (e.g.
`weather.nws_yourstation` or `weather.home`). Met.no also works if you prefer.

---

## 4. Create the text helper the ticker reads

Add to `configuration.yaml` (or Settings → Devices & Services → Helpers → Text):

```yaml
input_text:
  weather_ai_summary:
    name: Weather AI Summary
    max: 255
```

Restart / reload. (Your ticker already prefers this when it has content, and
falls back to the decoded forecast sentence when it's empty.)

---

## 5. The automation

Settings → Automations → Create → (top-right menu) **Edit in YAML**, paste this,
and fix the two names marked `# <-- EDIT`:

> This is the **minimal** version, kept short so the moving parts are visible.
> [`../homeassistant/ai_ticker_automation.yaml`](../homeassistant/ai_ticker_automation.yaml)
> is the version actually running: it adds NWS alert handling, rain-intensity branches,
> a temperature-band calibration table, and anti-repetition rules. Start here to confirm
> the plumbing works, then swap in the full one.

```yaml
alias: Weather AI ticker summary
triggers:
  - trigger: time_pattern
    minutes: "/20"
  - trigger: homeassistant
    event: start
actions:
  - action: weather.get_forecasts
    target:
      entity_id: weather.home            # <-- EDIT to your NWS entity
    data:
      type: daily
    response_variable: fc
  - variables:
      today: "{{ fc[ 'weather.home' ].forecast[0] }}"   # <-- EDIT entity id here too
  - action: ai_task.generate_data
    data:
      task_name: weather_ticker
      entity_id: ai_task.ollama          # <-- EDIT to your AI Task entity
      instructions: >
        You narrate a backyard weather station in Phoenix, Arizona.
        Right now it is {{ now().strftime('%A, %B %-d, %Y at %-I:%M %p') }}.
        Write ONE or TWO short, friendly sentences for a scrolling ticker
        (under 200 characters total). Describe what the weather is doing right
        now and add a brief practical heads-up only when it fits (bring water if
        hot, watch for rain/clouds, etc.). Use ONLY the numbers below. Do not
        invent values. No emojis. No preamble — just the sentence(s).

        LIVE NOW:
        - Temp {{ states('sensor.davis_vantage_pro2_outside_temperature') }} F
          (feels like {{ states('sensor.wind_chill') }} F)
        - Humidity {{ states('sensor.davis_vantage_pro2_outside_humidity') }} %,
          dew point {{ states('sensor.dew_point') }} F
        - Wind {{ states('sensor.davis_vantage_pro2_wind_speed') }} mph from
          {{ states('sensor.wind_cardinal') }}
        - Barometer {{ states('sensor.davis_vantage_pro2_barometer') }} inHg and
          {{ states('sensor.davis_vantage_pro2_barometer_trend') }}
        - Today's high/low so far
          {{ state_attr('sensor.temp_high_low_today','high') }}/{{ state_attr('sensor.temp_high_low_today','low') }} F
        - Rain today {{ states('sensor.davis_vantage_pro2_rain_today') }} in
        - Console forecast: {{ states('sensor.detailed_forecast') }}

        OFFICIAL NWS FORECAST FOR TODAY:
        - {{ today.condition }}, high {{ today.temperature }},
          low {{ today.templow | default('n/a') }}
    response_variable: result
  - action: input_text.set_value
    target:
      entity_id: input_text.weather_ai_summary
    data:
      value: "{{ result.data }}"
```

Save. Run it once manually (the ▶ button) to test — the ticker should pick up
the new line within a few seconds.

---

## Notes / tuning
- **Cadence:** every 20 min is gentle. Lower it if you want, but there's no point
  faster than the data meaningfully changes.
- **Length:** `input_text` caps at 255 chars; the prompt asks for under 200.
- **Wrong/odd output:** lower the model's creativity in the Ollama integration
  options (temperature ~0.3), or switch to the 7B model for steadier wording.
- **Date:** the prompt injects the real date/time so the model doesn't guess it.

---

## Possible extension — historical context
A scheduled script could read the long-term statistics (the `weather:*` history from
the importer) and write a compact comparison into a second helper, e.g.
`input_text.weather_ai_history` = "June avg high 104F vs 107F last year; hottest
112F on the 18th". Adding that block to the prompt would let the ticker say things
like "running milder than last summer." Not implemented yet.
