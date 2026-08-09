# Time handling

The project follows three rules:

1. Real instants, including cache and fetch timestamps, are timezone-aware UTC.
2. Tournament dates and ranking weeks are date-only values; they are not midnight timestamps.
3. A named `ZoneInfo` timezone is applied only where a business rule depends on local civil time.

The shared implementation is `time_utils.py`:

- `utc_now()` returns an aware UTC `datetime`.
- `utc_timestamp()` writes the existing `YYYY-MM-DDTHH:MM:SSZ` wire format.
- `parse_utc_timestamp()` parses and normalizes timestamps to aware UTC.
- `madrid_now()` / `madrid_today()` define the site's operating-calendar boundary.
- `new_york_now()` / `new_york_today()` define the WTA ranking-publication boundary.
  A current-week ranking is first checked at or after Monday 12:00 in
  `America/New_York`. If it still matches the prior week then, the week is
  accepted as frozen; an unavailable or invalid response remains pending and is
  retried by a later update.

Do not use naive `datetime.now()`, `datetime.utcnow()`, `datetime.today()`, or
`date.today()` in application modules. Do not calculate daylight-saving offsets
by month. `ZoneInfo` supplies the historical and future transition rules.
