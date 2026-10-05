# Shooter Atlas data pipeline

Rebuilds the data behind https://hockey.ridgelinemaps.com/shooting-atlas/ from
[MoneyPuck](https://moneypuck.com/data.htm) shot data.

**Automatic:** `.github/workflows/weekly-shooter-refresh.yml` runs every Monday
at 5:23 am Mountain time. It downloads the current season, rebuilds the data files,
commits them, and GitHub Pages republishes. You get an email from GitHub if a run fails.
In that case the site keeps last week's data.

**Manual run:** in the repo's **Actions** tab, open *Weekly shooter data refresh*
and click **Run workflow**.

## What gets written
| File | Changes |
|---|---|
| `shooting-atlas/data-hexes.js` | weekly (hex aggregates, season list, data-through date) |
| `shooting-atlas/data-players-current.js` | weekly (names, teams, bios, career totals, current season) |
| `shooting-atlas/data-players-1..3.js` | once a year (completed seasons only) |
| `_pipeline/history/` | once a year (compact copy of every completed season's shots) |
| `_pipeline/state.json`, `update_log.md` | every run (keeps the scheduled workflow from going dormant) |

`shooting-atlas/index.html` is **never** touched by the pipeline. You can edit the page freely.

## Safeguards
A build is rejected and nothing is written if any of these happen:
- the total shot count drops more than 2% from the last good run
- a season disappears
- the hex count falls outside 150–300
- the all-seasons aggregate is missing
- the player-file split fails its self-test
- MoneyPuck's CSV columns change

## Season rollover
Nothing to do. When the next season's `shots_YYYY.zip` appears on MoneyPuck,
the finished season is appended to `history/` automatically. The page keeps
opening on the previous season until the new one has about 40,000 shots
(`DEFAULT_SEASON_MIN_SHOTS`).

## Method (matches the page footer)
- Offensive-zone shots are binned into 4-ft pointy-top hexes, per season plus an
  all-seasons scope.
- Each hex is split by situation (all, ES, PP, SH) and game type (regular season, playoffs).
- The best shooter in a hex is the player with the highest empirical-Bayes Sh%,
  `(g + 30·lg) / (s + 30)`. Here `lg` is that hex's own league rate for the same
  scope.
- A player needs at least 1 goal from the hex to qualify. The minimum shot count
  starts at 8 and steps down to 1.
