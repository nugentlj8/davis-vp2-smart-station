# Historical data format

`scripts/import_history_to_ha.py` reads an Excel workbook named
`Weather_History_Log.xlsx` and imports it into Home Assistant as long-term
**external statistics**, so decades of manually-logged records show up on the
dashboard next to the live data.

The workbook has up to four tabs; fill in whichever granularities you have:

| Tab | Row label (col A) | Example columns |
|---|---|---|
| `Years`  | year (e.g. `2018`)       | Temp High, Temp Low, Wind High, Highest Rain Rate, Total Rain |
| `Months` | `YYYY-MM` (e.g. `2024-06`) | + Humidity High/Low, Avg Barometer |
| `Days`   | `YYYY-MM-DD`             | same as Months |
| `Hours`  | `YYYY-MM-DD HH:00`       | Temp, Humidity, Barometer, Wind High |

- The header text drives the metric (see the `METRICS` map in the import script).
- Blank cells are skipped.
- Re-running overwrites the same points (no duplicates).

A real workbook is intentionally **not** committed (see `.gitignore`) — drop
your own `Weather_History_Log.xlsx` in the repo root and run the script.
