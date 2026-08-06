# Canonical data model

The checked-in CSV files remain source-compatible staging data for the static
site. `canonical_data.py` is the authoritative semantic layer: it converts
those rows into constrained player, ranking, match, and tournament records and
validates the complete project with:

```text
python canonical_data.py validate --data-dir data
```

## Tables and constraints

- `players(player_key, display_name, presentation_name, wta_id, itf_id, bjkc_id, ...)`
  - `player_key` is stable and required.
  - Every source ID maps to exactly one player.
  - WTA and ITF IDs must use their numeric source formats.
  - Canonical display names are unique identity labels after case/accent normalization.
  - Optional `presentation_name` stores the public name whenever the unique
    identity label needs a country, birth year, or source-ID qualifier.
  - A name shared by multiple players is marked ambiguous and is never used to
    select one by input order.
  - Additional source IDs are retained explicitly for merged source profiles.
- `rankings(week_date, player_key, rank, points, ...)`
  - `(week_date, player_key)` is unique.
  - The source WTA ID must resolve to a canonical player.
- `matches(source, source_match_key, tournament_key, ...)`
  - `(source, source_match_key)` is unique.
  - Match IDs are interpreted in their source context; `matchId` alone is not a
    project-wide key.
- `tournaments(tournament_key, source, source_id, season, name)`
  - The key includes the source namespace, source ID, season, and normalized
    event name. This handles source IDs that have been reused for another event.

## Match natural keys

| Source | Natural key fields |
| --- | --- |
| WTA | tournament ID, season, match ID |
| ITF | match ID |
| Grand Slam | match ID, draw, round, winner ID, loser ID |
| BJK Cup | tie/tournament ID, season, round, match ID, participants |
| United Cup | season, round, match ID, participants |
| Olympics/manual | tournament ID, date, round, match ID, participants |

All current loaders call the shared key builders. The scheduled workflow runs
the regression suite and validates the full canonical model both before and
after refreshing the site. Ranking and match loaders also register newly seen
WTA/ITF IDs; a colliding name is qualified by its source ID instead of being
merged automatically.

`migrate_canonical_data.py` is idempotent and can be used to upgrade an older
snapshot. It merges the verified identity duplicates, fills missing displays,
adds ranking-only WTA identities, removes duplicate ranking snapshots, and
normalizes the documented match anomalies.
