#!/usr/bin/env python3
"""
Weekly data refresh for the NHL Shooter Atlas  ->  https://hockey.ridgelinemaps.com/shooting-atlas/

Runs inside the cedarcanvas/hockey-maps repo (GitHub Actions, .github/workflows/
weekly-shooter-refresh.yml) or locally from a checkout:

    python3 _pipeline/update_atlas.py            # build data files
    python3 _pipeline/update_atlas.py status     # show the last run
    python3 _pipeline/update_atlas.py rebuild-history

What it writes (index.html is never touched — edit the page by hand as usual):
    shooting-atlas/data-hexes.js          window.HD_META  (hex aggregates, season list, metadata)
    shooting-atlas/data-players-1..3.js   completed seasons' per-hex cells — static all season
    shooting-atlas/data-players-current.js  names/teams/bios/totals + current-season cells
The page deep-merges the HD_P chunks, so the large history chunks only change
once a year and weekly commits stay small.

Pipeline
  1. History: completed seasons live in _pipeline/history/shots_compact.csv.gz.
     Any completed season missing from it is downloaded from MoneyPuck once and
     appended, so season rollovers take care of themselves.
  2. Current season: shots_<season>.zip is re-downloaded every run (MoneyPuck
     refreshes nightly), plus allPlayersLookup.csv for rookie bios.
  3. Aggregate into 4-ft pointy-top hexes, per season + an all-seasons scope,
     by situation (all/ES/PP/SH) and game type (reg/playoffs).  Best shooter per
     hex = highest empirical-Bayes Sh%, prior k=30 toward the hex's own league
     rate, g>=1 required, shot threshold stepping 8 -> 1.
  4. Sanity-check against the last good run (state.json); refuse to write if the
     data shrank or seasons vanished, so a bad MoneyPuck file can't break the site.

Standard library only.
"""
import csv, datetime as dt, gzip, io, json, math, sys, time
import urllib.error, urllib.request, zipfile
from collections import defaultdict
from email.utils import parsedate_to_datetime
from pathlib import Path

csv.field_size_limit(sys.maxsize)

HERE      = Path(__file__).resolve().parent
REPO      = HERE.parent
SITE_DIR  = REPO / "shooting-atlas"
HIST_DIR  = HERE / "history"
HIST_GZ   = HIST_DIR / "shots_compact.csv.gz"
HIST_META = HIST_DIR / "player_meta.json"
BIO_CACHE = HIST_DIR / "allPlayersLookup.csv"
STATE     = HERE / "state.json"
LOG       = HERE / "update_log.md"
TMP = Path("/tmp/shooter_atlas"); TMP.mkdir(parents=True, exist_ok=True)

FIRST_SEASON = 2018
SHOTS_URL    = "https://peter-tanner.com/moneypuck/downloads/shots_{}.zip"
BIO_URL      = "https://moneypuck.com/moneypuck/playerData/playerBios/allPlayersLookup.csv"
UA           = "RidgelineMaps-ShooterAtlas/1.0"
N_HIST_CHUNKS = 3        # must match the data-players-N.js <script> tags in index.html

# geometry / model (must match the JS in shooting-atlas/index.html)
HEX_SIZE, SQRT3 = 4.0, math.sqrt(3)
GOAL_LINE_X, RINK_HALF_W = 89.0, 42.5
ZONE_MIN, ZONE_MAX = -11.0, 75.0
PRIMARY_MIN_SHOTS, MIN_SHOTS_FLOOR, PRIOR_K = 8, 1, 30
PX_PER_FT, SVG_W, SVG_H, GOAL_LINE_PX_Y = 10.0, 850, 750, 110
DEFAULT_SEASON_MIN_SHOTS = 40000   # ~1/3 season; until then the page opens on the previous season
SIT_OFF = {"es": 2, "pp": 4, "sh": 6}
SITMAP = {"e": "es", "p": "pp", "s": "sh"}


# ----------------------------------------------------------------------------- utils
def log(msg):
    print(msg, flush=True)

def season_label(s):
    s = int(s); return f"{s}-{str(s + 1)[-2:]}"

def current_season(today=None):
    today = today or dt.date.today()
    return today.year if today.month >= 9 else today.year - 1

def http(url, method="GET", timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, method=method, headers={"User-Agent": UA}),
                                  timeout=timeout)

def download(url, dest, tries=3):
    last = None
    for i in range(tries):
        try:
            with http(url, timeout=180) as r:
                lm = r.headers.get("Last-Modified")
                dest.write_bytes(r.read())
                return lm
        except urllib.error.HTTPError as e:
            if e.code == 404: raise
            last = e
        except Exception as e:
            last = e
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"download failed: {url}: {last}")

def remote_exists(url):
    try:
        with http(url, method="HEAD", timeout=30) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False

def load_state():
    try: return json.loads(STATE.read_text())
    except Exception: return {}

def save_state(st):
    STATE.write_text(json.dumps(st, indent=2) + "\n")

def append_log(line):
    new = not LOG.exists()
    with LOG.open("a") as f:
        if new: f.write("# Shooter Atlas weekly refresh log\n\n")
        f.write(f"- {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC — {line}\n")

def dumps(obj):
    # sorted keys + no whitespace -> byte-identical output when nothing changed
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False).replace("</", "<\\/")

def write_text(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8"); tmp.replace(path)


# ----------------------------------------------------------------------------- raw -> compact rows
COMPACT_HDR = ["season", "pid", "x", "y", "g", "sit", "pof"]
NEED = ["xCordAdjusted", "yCordAdjusted", "goal", "season", "shooterPlayerId", "shooterName",
        "isPlayoffGame", "homeSkatersOnIce", "awaySkatersOnIce", "isHomeTeam", "homeTeamCode", "awayTeamCode"]

def compact_rows_from_csv(fobj, meta):
    """Yield compact rows from a MoneyPuck shots CSV; fill meta[pid] = [name, team, season]."""
    rdr = csv.reader(fobj)
    ix = {n: i for i, n in enumerate(next(rdr))}
    missing = [n for n in NEED if n not in ix]
    if missing:
        raise RuntimeError(f"MoneyPuck schema changed — missing columns: {missing}")
    I = [ix[n] for n in NEED]
    for row in rdr:
        try:
            xc, yc, g, se, pid, nm, pof, hsk, ask, ih, ht, at = (row[i] for i in I)
            pid = int(float(pid)); s = int(float(se))
            home = ih in ("1", "1.0")
            my, th = (int(float(hsk)), int(float(ask))) if home else (int(float(ask)), int(float(hsk)))
        except (ValueError, IndexError):
            continue
        sit = "p" if my > th else ("s" if my < th else "e")
        m = meta.get(pid)
        if m is None or s >= m[2]:
            meta[pid] = [nm, ht if home else at, s]
        yield (s, pid, xc, yc, 1 if g in ("1", "1.0") else 0, sit, 1 if pof in ("1", "1.0") else 0)

def season_zip_rows(season, meta):
    z = TMP / f"shots_{season}.zip"
    lm = download(SHOTS_URL.format(season), z)
    with zipfile.ZipFile(z) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".csv"))
        with zf.open(name) as raw:
            rows = list(compact_rows_from_csv(io.TextIOWrapper(raw, encoding="utf-8", errors="replace"), meta))
    return rows, lm

def read_history():
    rows, seasons = [], set()
    if HIST_GZ.exists():
        with gzip.open(HIST_GZ, "rt") as f:
            rdr = csv.reader(f); next(rdr)
            for s, pid, x, y, g, sit, pof in rdr:
                r = (int(s), int(pid), x, y, int(g), sit, int(pof)); rows.append(r); seasons.add(r[0])
    meta = {int(k): v for k, v in json.loads(HIST_META.read_text()).items()} if HIST_META.exists() else {}
    return rows, seasons, meta

def write_history(rows, meta):
    HIST_DIR.mkdir(exist_ok=True)
    tmp = HIST_GZ.with_suffix(".tmp")
    with gzip.GzipFile(tmp, "wb", compresslevel=6, mtime=0) as gz:      # mtime=0 -> reproducible bytes
        with io.TextIOWrapper(gz, encoding="utf-8", newline="") as f:
            w = csv.writer(f); w.writerow(COMPACT_HDR)
            for r in sorted(rows, key=lambda r: r[0]): w.writerow(r)
    tmp.replace(HIST_GZ)
    HIST_META.write_text(json.dumps({str(k): v for k, v in sorted(meta.items())}, separators=(",", ":")))

def ensure_history(cur, rebuild=False):
    """Make sure every completed season FIRST_SEASON..cur-1 is in the compact cache."""
    rows, have, meta = ([], set(), {}) if rebuild else read_history()
    missing = sorted(set(range(FIRST_SEASON, cur)) - have)
    if not missing:
        return rows, meta, []
    for s in missing:
        log(f"  downloading completed season {season_label(s)} …")
        srows, _ = season_zip_rows(s, meta); rows.extend(srows)
    write_history(rows, meta)
    return rows, meta, missing


# ----------------------------------------------------------------------------- hex geometry
def axial_round(qf, rf):
    sf = -qf - rf
    rq, rr, rs = round(qf), round(rf), round(sf)
    dq, dr, ds = abs(rq - qf), abs(rr - rf), abs(rs - sf)
    if dq > dr and dq > ds: rq = -rr - rs
    elif dr > ds:           rr = -rq - rs
    return int(rq), int(rr)

def xy_to_axial(x, y):
    return axial_round((SQRT3 / 3 * x - y / 3) / HEX_SIZE, (2 / 3 * y) / HEX_SIZE)

def axial_to_xy(q, r):
    return HEX_SIZE * (SQRT3 * q + SQRT3 / 2 * r), HEX_SIZE * 1.5 * r

def hex_path(cx, cy):
    s = HEX_SIZE; h = s * SQRT3 / 2
    pts = [(SVG_W / 2 + (cx + dx) * PX_PER_FT, GOAL_LINE_PX_Y + (cy + dy) * PX_PER_FT)
           for dx, dy in [(0, s), (h, s / 2), (h, -s / 2), (0, -s), (-h, -s / 2), (-h, s / 2)]]
    return "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts) + " Z"


# ----------------------------------------------------------------------------- aggregation
def load_bios(path):
    bios = {}
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            try: pid = int(float(row["playerId"]))
            except (ValueError, KeyError): continue
            b = {}
            if row.get("birthDate"):     b["born"] = row["birthDate"]
            if row.get("height"):        b["ht"] = row["height"]
            if row.get("weight"):
                try: b["wt"] = int(float(row["weight"]))
                except ValueError: pass
            if row.get("nationality"):   b["nat"] = row["nationality"]
            if row.get("shootsCatches"): b["shoots"] = row["shootsCatches"]
            if row.get("primaryNumber"): b["num"] = row["primaryNumber"]
            bios[pid] = ((row.get("name") or "").strip(), (row.get("team") or "").strip(),
                         (row.get("primaryPosition") or row.get("position") or "").strip(), b)
    return bios

def aggregate(rows, meta, bios):
    nd = lambda f: defaultdict(f)
    hex_stats  = nd(lambda: nd(lambda: nd(lambda: nd(lambda: [0, 0]))))              # hk>scope>gt>sit
    player_hex = nd(lambda: nd(lambda: nd(lambda: nd(lambda: nd(lambda: [0, 0])))))  # hk>scope>gt>sit>pid
    cells = nd(lambda: nd(lambda: nd(lambda: [0] * 16)))                             # pid>season>hex
    season_totals, playoff_totals = defaultdict(int), defaultdict(int)

    for s, pid, x, y, g, sit, pof in rows:
        if pid <= 0: continue                    # MoneyPuck's placeholder for an unidentified shooter
        try: depth, lateral = GOAL_LINE_X - float(x), -float(y)
        except ValueError: continue
        if not (ZONE_MIN <= depth <= ZONE_MAX) or abs(lateral) > RINK_HALF_W:
            continue
        q, r = xy_to_axial(lateral, depth)
        hk, se, sk, gt = (q, r), str(s), SITMAP[sit], ("pof" if pof else "reg")
        for scope in (se, "all"):
            hs = hex_stats[hk][scope][gt]
            hs["all"][0] += 1; hs["all"][1] += g; hs[sk][0] += 1; hs[sk][1] += g
            ph = player_hex[hk][scope][gt]
            ph["all"][pid][0] += 1; ph["all"][pid][1] += g; ph[sk][pid][0] += 1; ph[sk][pid][1] += g
        c = cells[pid][se][f"{q},{r}"]; o = SIT_OFF[sk]
        c[0] += 1; c[1] += g; c[o] += 1; c[o + 1] += g
        if pof: c[8] += 1; c[9] += g; c[8 + o] += 1; c[9 + o] += g
        season_totals[se] += 1
        if pof: playoff_totals[se] += 1

    seasons = sorted(season_totals)

    def who(pid):
        if pid in bios and bios[pid][0]: return bios[pid][:3]
        m = meta.get(pid)
        return (m[0], m[1], "") if m else (str(pid), "", "")

    def best(cands, lg_sh):
        for th in range(PRIMARY_MIN_SHOTS, MIN_SHOTS_FLOOR - 1, -1):
            el = [(pid, s, g) for pid, (s, g) in cands.items() if s >= th and g >= 1]
            if not el: continue
            pid, s, g, sh, shp = min(((pid, s, g, g / s, (g + PRIOR_K * lg_sh) / (s + PRIOR_K)) for pid, s, g in el),
                                     key=lambda t: (-t[4], -t[3], -t[1], t[0]))
            n, t, p = who(pid)
            return {"id": pid, "n": n, "t": t, "p": p, "s": s, "g": g,
                    "sh": round(sh, 4), "shp": round(shp, 4), "th": th}
        return None

    def scope_record(sg, pmap):
        if sg["all"][0] == 0: return None
        rec = {"all": {"ls": sg["all"][0], "lg": sg["all"][1]}, "sit": {}}
        t = best(pmap.get("all", {}), sg["all"][1] / sg["all"][0])
        if t: rec["all"]["top"] = t
        for sk in ("es", "pp", "sh"):
            s, g = sg.get(sk, (0, 0))
            if not s: continue
            rec["sit"][sk] = {"ls": s, "lg": g}
            t = best(pmap.get(sk, {}), g / s)
            if t: rec["sit"][sk]["top"] = t
        return rec

    hexes = []
    for hk in sorted(hex_stats):
        q, r = hk; cx, cy = axial_to_xy(q, r)
        by_s = {}
        for scope in seasons + ["all"]:
            if scope not in hex_stats[hk]: continue
            gts = hex_stats[hk][scope]
            comb_sg = defaultdict(lambda: [0, 0]); comb_p = defaultdict(lambda: defaultdict(lambda: [0, 0]))
            for gt in ("reg", "pof"):
                for sk, (s, g) in gts.get(gt, {}).items():
                    comb_sg[sk][0] += s; comb_sg[sk][1] += g
                for sk, pm in player_hex[hk][scope].get(gt, {}).items():
                    for pid, (s, g) in pm.items():
                        comb_p[sk][pid][0] += s; comb_p[sk][pid][1] += g
            rec = scope_record(comb_sg, comb_p)
            if not rec: continue
            by_g = {}
            for gt in ("reg", "pof"):
                if gt in gts:
                    gr = scope_record(gts[gt], player_hex[hk][scope][gt])
                    if gr: by_g[gt] = gr
            if by_g: rec["byG"] = by_g
            by_s[scope] = rec
        hexes.append({"q": q, "r": r, "cx": round(cx, 2), "cy": round(cy, 2), "d": hex_path(cx, cy), "byS": by_s})

    players = {}
    for pid in sorted(cells):
        by_se = cells[pid]
        all_s = all_g = 0; bh, bg = "", -1
        for se in sorted(by_se):
            for k in sorted(by_se[se]):
                c = by_se[se][k]; all_s += c[0]; all_g += c[1]
                if c[1] > bg: bg, bh = c[1], k
        if not all_s: continue
        n, t, p = who(pid)
        players[str(pid)] = {"n": n, "t": t, "p": p, "s": all_s, "g": all_g, "bh": bh,
                             "hs": {se: dict(by_se[se]) for se in sorted(by_se)},
                             "bio": bios.get(pid, (None, None, None, {}))[3]}
    return seasons, season_totals, playoff_totals, hexes, players


# ----------------------------------------------------------------------------- output
def split_players(players, cur):
    """History chunks: completed seasons' cells only (static). Current chunk: everything else."""
    hist, current = {}, {}
    for pid, p in players.items():
        old = {se: v for se, v in p["hs"].items() if int(se) < cur}
        new = {se: v for se, v in p["hs"].items() if int(se) >= cur}
        if old: hist[pid] = {"hs": old}
        rec = {k: p[k] for k in ("n", "t", "p", "s", "g", "bh", "bio")}
        if new: rec["hs"] = new
        current[pid] = rec
    # contiguous, roughly equal-size history chunks (sorted pids -> deterministic boundaries)
    ids = sorted(hist, key=int)
    sizes = [len(dumps(hist[i])) for i in ids]
    total, target = sum(sizes), sum(sizes) / N_HIST_CHUNKS
    chunks, acc, cur_chunk = [], 0, {}
    for i, sz in zip(ids, sizes):
        if acc >= target * (len(chunks) + 1) and len(chunks) < N_HIST_CHUNKS - 1:
            chunks.append(cur_chunk); cur_chunk = {}
        cur_chunk[i] = hist[i]; acc += sz
    chunks.append(cur_chunk)
    while len(chunks) < N_HIST_CHUNKS: chunks.append({})
    return chunks, current

def merged(chunks, current):
    """Python mirror of the page's deep merge — used to self-test the split."""
    out = {}
    for ch in chunks + [current]:
        for pid, p in ch.items():
            if pid not in out: out[pid] = dict(p); continue
            hs = dict(out[pid].get("hs", {})); hs.update(p.get("hs", {}))
            out[pid] = {**out[pid], **p, "hs": hs}
    return out


# ----------------------------------------------------------------------------- build
def build():
    t0 = time.time()
    st_all = load_state()
    cur = current_season()
    if not remote_exists(SHOTS_URL.format(cur)):
        log(f"shots_{cur}.zip not published yet; treating {season_label(cur - 1)} as current"); cur -= 1
    log(f"Current season: {season_label(cur)}")

    hist_rows, meta, added = ensure_history(cur)
    if added: log(f"  history cache gained: {', '.join(season_label(s) for s in added)}")
    log(f"  history rows: {len(hist_rows):,}")
    cur_rows, lm = season_zip_rows(cur, meta)
    through = parsedate_to_datetime(lm).date().isoformat() if lm else dt.date.today().isoformat()
    log(f"  {season_label(cur)} rows: {len(cur_rows):,} (MoneyPuck file dated {through})")

    try:
        tmp_bio = BIO_CACHE.with_suffix(".tmp")          # same filesystem as the cache -> atomic replace
        download(BIO_URL, tmp_bio)
        if tmp_bio.stat().st_size < 50_000: raise RuntimeError("bio file suspiciously small")
        tmp_bio.replace(BIO_CACHE)
    except Exception as e:
        log(f"  bio download failed ({e}); using cached copy")
    bios = load_bios(BIO_CACHE)

    seasons, st, pt, hexes, players = aggregate(hist_rows + cur_rows, meta, bios)
    latest = seasons[-1]
    default = latest if st[latest] >= DEFAULT_SEASON_MIN_SHOTS or len(seasons) < 2 else seasons[-2]
    meta_out = {
        "primaryMinShots": PRIMARY_MIN_SHOTS, "minShotsFloor": MIN_SHOTS_FLOOR, "priorK": PRIOR_K,
        "totalShots": sum(st.values()), "totalPlayers": len(players), "viewBox": [0, 0, SVG_W, SVG_H],
        "seasons": seasons, "seasonLabels": {s: season_label(s) for s in seasons},
        "seasonTotals": dict(st), "playoffTotals": dict(pt), "playerCellLen": 16,
        "defaultSeason": default, "dataThrough": through, "hexes": hexes,
    }

    # ---- sanity checks against the last good run
    prev = st_all.get("lastBuild", {})
    problems = []
    if not 150 <= len(hexes) <= 300: problems.append(f"hex count {len(hexes)} out of range")
    if prev.get("totalShots") and meta_out["totalShots"] < 0.98 * prev["totalShots"]:
        problems.append(f"total shots fell {prev['totalShots']:,} -> {meta_out['totalShots']:,}")
    if prev.get("seasons") and not set(prev["seasons"]) <= set(seasons):
        problems.append(f"seasons disappeared: {sorted(set(prev['seasons']) - set(seasons))}")
    if not any("all" in h["byS"] for h in hexes): problems.append("all-seasons scope missing")
    chunks, current = split_players(players, cur)
    if merged(chunks, current) != players: problems.append("player chunk split failed self-test")

    st_all["lastRun"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    if problems:
        st_all["lastRun_result"] = "rejected: " + "; ".join(problems)
        save_state(st_all)
        msg = "BUILD REJECTED (site left unchanged): " + "; ".join(problems)
        append_log(msg); log(msg); sys.exit(2)

    SITE_DIR.mkdir(exist_ok=True)
    write_text(SITE_DIR / "data-hexes.js", "window.HD_META=" + dumps(meta_out) + ";\n")
    for i, ch in enumerate(chunks, 1):
        write_text(SITE_DIR / f"data-players-{i}.js", "(window.HD_P=window.HD_P||[]).push(" + dumps(ch) + ");\n")
    write_text(SITE_DIR / "data-players-current.js", "(window.HD_P=window.HD_P||[]).push(" + dumps(current) + ");\n")

    st_all["lastBuild"] = {"totalShots": meta_out["totalShots"], "players": len(players), "hexes": len(hexes),
                           "seasons": seasons, "current": str(cur), "currentShots": st.get(str(cur), 0),
                           "dataThrough": through, "defaultSeason": default}
    st_all["lastRun_result"] = "ok"
    save_state(st_all)
    line = (f"{meta_out['totalShots']:,} shots, {len(players):,} shooters; {season_label(cur)}: "
            f"{st.get(str(cur), 0):,} shots through {through}; page opens on {season_label(default)} "
            f"({time.time() - t0:.0f}s)")
    append_log(line); log(line)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "build": build()
    elif cmd == "status": print(json.dumps(load_state(), indent=2))
    elif cmd == "rebuild-history":
        rows, meta, added = ensure_history(current_season(), rebuild=True)
        log(f"history rebuilt: {len(rows):,} rows, seasons {sorted({r[0] for r in rows})}")
    else: sys.exit(__doc__)
