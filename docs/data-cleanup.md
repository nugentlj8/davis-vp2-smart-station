# Removing bad "sentinel" readings from HA statistics

The Davis console uses special values to mean **"no data"**, and before the
firmware guards were added these got published as if they were real readings:

| Field | Sentinel published as | What it really means |
|---|---|---|
| Wind speed / gust | **255 mph** | raw byte `0xFF` = no data |
| Rain rate | **655.35 in/hr** | raw `0xFFFF` / 100 = no data |
| Rain totals | 655.35 in | raw `0xFFFF` |
| Barometer | 65.535 inHg | raw `0xFFFF` |
| Inside temp | 3276.7 F | raw `32767` |

The firmware now rejects all of these, so no new ones can be recorded. But any
already written to Home Assistant's **long-term statistics** have to be deleted
from the recorder database — there's no per-point editor in the HA UI.

Deleting from the hourly `statistics` table fixes **every** chart at once
(daily, 30-day, monthly, yearly, Records), because HA computes all those
timeframes on the fly from that one table.

This assumes the default **SQLite** recorder (`<config>/home-assistant_v2.db`).
If you use MariaDB/Postgres, the same SQL works via that database's client.

---

## Steps (Windows GUI: DB Browser for SQLite)

1. **Back up first.** Copy `home-assistant_v2.db` somewhere safe.
2. **Stop Home Assistant** (stop the Docker container) so the DB isn't in use.
3. Open **DB Browser for SQLite** (https://sqlitebrowser.org/) ->
   **File -> Open Database** -> `home-assistant_v2.db`.
4. Go to the **Execute SQL** tab and run the blocks below.
5. Click **Write Changes**, close, then **start Home Assistant**.

### Wind spikes (>= 150 mph is impossible)

```sql
DELETE FROM statistics
WHERE metadata_id IN (
  SELECT id FROM statistics_meta WHERE statistic_id IN (
    'sensor.davis_vantage_pro2_wind_speed',
    'sensor.davis_vantage_pro2_wind_speed_10_min_avg',
    'sensor.wind_gust_today'
  )
)
AND (max >= 150 OR mean >= 150 OR state >= 150);

DELETE FROM statistics_short_term
WHERE metadata_id IN (
  SELECT id FROM statistics_meta WHERE statistic_id IN (
    'sensor.davis_vantage_pro2_wind_speed',
    'sensor.davis_vantage_pro2_wind_speed_10_min_avg',
    'sensor.wind_gust_today'
  )
)
AND (max >= 150 OR mean >= 150 OR state >= 150);
```

### Rain spikes (>= 50 in/hr or >= 100 in total is impossible)

```sql
DELETE FROM statistics
WHERE metadata_id IN (
  SELECT id FROM statistics_meta WHERE statistic_id IN (
    'sensor.davis_vantage_pro2_rain_rate',
    'sensor.davis_vantage_pro2_rain_today',
    'sensor.davis_vantage_pro2_storm_rain',
    'sensor.davis_vantage_pro2_rain_this_month',
    'sensor.davis_vantage_pro2_rain_this_year'
  )
)
AND (max >= 50 OR mean >= 50 OR state >= 50 OR sum >= 500);

DELETE FROM statistics_short_term
WHERE metadata_id IN (
  SELECT id FROM statistics_meta WHERE statistic_id IN (
    'sensor.davis_vantage_pro2_rain_rate',
    'sensor.davis_vantage_pro2_rain_today',
    'sensor.davis_vantage_pro2_storm_rain',
    'sensor.davis_vantage_pro2_rain_this_month',
    'sensor.davis_vantage_pro2_rain_this_year'
  )
)
AND (max >= 50 OR mean >= 50 OR state >= 50 OR sum >= 500);
```

### Barometer / inside temp (only if you ever see them)

```sql
DELETE FROM statistics
WHERE metadata_id IN (
  SELECT id FROM statistics_meta WHERE statistic_id IN (
    'sensor.davis_vantage_pro2_barometer',
    'sensor.davis_vantage_pro2_inside_temperature'
  )
)
AND (max >= 60 OR mean >= 60);
```

---

## Notes
- Thresholds are set well above anything real (150 mph wind, 50 in/hr rain,
  60 inHg pressure), so nothing legitimate is deleted.
- If a rain **total** spike wrecked a `total_increasing` sum, deleting the bad
  rows lets HA recompute; if the running total still looks wrong afterward, the
  sensor will re-baseline at the next reset (midnight / month / year rollover).
- Always **back up** and **stop HA** before editing the database.

## No-SQL alternative
Install the **Spook** integration (HACS) — it adds recorder actions to delete
statistics from the HA UI. Less surgical than the SQL above, but no DB editing.
