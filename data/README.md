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

## Try it without building a workbook

[`Weather_History_Log.example.xlsx`](Weather_History_Log.example.xlsx) is a small
**synthetic** workbook — five years, six months, a week of days, and twelve hours of
invented readings — that exercises all four tabs and every metric column. Point the
script at it to confirm the import path works before committing to a real spreadsheet:

```python
XLSX_PATH = r"data/Weather_History_Log.example.xlsx"
```

It imports 25 statistics / 177 data points. The numbers are made up; delete the
resulting `weather:*` statistics from HA afterwards if you don't want them sitting
alongside real data.

A real workbook is intentionally **not** committed (see `.gitignore`) — drop
your own `Weather_History_Log.xlsx` in the repo root and run the script.

> **Row labels:** the `Hours` tab accepts either a real Excel datetime cell or plain
> text in `YYYY-MM-DD HH:00` form. Same for the other tabs — `2018`, `2024-06`,
> `2024-06-15`. Anything the script can't read names the tab and the expected format
> rather than failing anonymously.
