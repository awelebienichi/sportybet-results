#!/usr/bin/env python3
"""Extra basketball checks:
- ESPN NBA injuries, weighed by each missing player's points per game (ESPN player overview)
- rest: a team on its 2nd game in 2 days (from Sportradar match times); measured on 5,343 games: about -1 point for that team
- Flashscore home/away form in points (the home team's home games, the away team's away games)
- Basketball-Reference NBA team ratings (attack / defence per 100 possessions, pace) -> a second prediction for totals and margins
Each check returns (support, against) notes for a leg; nothing here may block the builder for long (time limits)."""
import json, os, re, subprocess, time
from concurrent.futures import ThreadPoolExecutor
import scores, extra
HERE = os.path.dirname(os.path.abspath(__file__)); CACHE = os.path.join(HERE, "cache", "bb"); os.makedirs(CACHE, exist_ok=True)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"

def _curl(url, secs=30, *h):
    r = subprocess.run(["curl", "-s", "--compressed", "-m", str(secs), "-A", UA, *h, url], capture_output=True, text=True, errors="ignore")
    return r.stdout

def _cached(name, hours, fn):
    f = os.path.join(CACHE, name)
    if os.path.exists(f) and time.time() - os.path.getmtime(f) < hours * 3600:
        try: return json.load(open(f))
        except Exception: pass
    v = fn()
    if v:
        tmp = f"{f}.{os.getpid()}.tmp"
        try: json.dump(v, open(tmp, "w")); os.replace(tmp, f)
        except OSError: pass
    return v

# ---------------- ESPN NBA injuries ----------------
def _ppg(aid):
    """a player's points per game over his last 5 games and when he last played (ESPN game log) – a player who has been out
    for weeks is already reflected in his team's recent scores, so only recent absences count"""
    def get():
        try:
            gl = json.loads(_curl(f"https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{aid}/overview", 20))["gameLog"]
            st = next(x for x in gl["statistics"] if x.get("displayName") == "totals"); i = st["names"].index("points")
            pts = [float(e["stats"][i]) for e in st["events"]]
            import datetime as _dt
            last = max(_dt.datetime.fromisoformat(e["gameDate"].replace("Z", "+00:00")).timestamp() for e in gl["events"].values())
            return {"ppg": sum(pts) / len(pts) if pts else 0.0, "last": last}
        except Exception: return {"ppg": 0.0, "last": 0}
    return _cached(f"ppg2_{aid}.json", 6, get)

def injuries():
    """{ESPN team name: [(player, status, points per game)]} – players ruled Out or Doubtful"""
    def get():
        try: d = json.loads(_curl("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries", 30))
        except Exception: return {}
        rows = []
        for t in d.get("injuries", []):
            for i in t.get("injuries", []):
                if i.get("status") not in ("Out", "Doubtful"): continue
                href = next((l["href"] for l in i["athlete"].get("links", []) if "/id/" in l.get("href", "")), "")
                m = re.search(r"/id/(\d+)", href)
                if m: rows.append((t["displayName"], i["athlete"]["displayName"], i["status"], m.group(1)))
        with ThreadPoolExecutor(8) as ex: P = list(ex.map(lambda r: _ppg(r[3]), rows))
        out = {}
        for (team, name, st, _), p in zip(rows, P):
            if time.time() - p.get("last", 0) <= 7 * 86400:          # played in the last week: a new absence the team's form doesn't show yet
                out.setdefault(team, []).append((name, st, p.get("ppg", 0.0)))
        return out or {"_none": []}
    return _cached("nba_injuries2.json", 1, get)

def missing(team):
    """points per game of an NBA team's players who are out, with their names"""
    I = injuries() or {}
    best = max(I, key=lambda k: scores.sim(team, k), default=None)
    if not best or scores.sim(team, best) < .8: return 0.0, []
    L = sorted(I[best], key=lambda x: -x[2])
    return sum(x[2] for x in L), [f"{n} ({p:.0f} pts)" for n, _, p in L if p >= 5]

# ---------------- rest ----------------
def last_played(tid):
    """time of a team's most recent finished game (Sportradar team history already cached by the builder)"""
    try:
        d = json.load(open(os.path.join(HERE, "cache", f"{tid}.json")))["doc"][0]["data"]["matches"]
        return max(m["time"]["uts"] for m in d if (m.get("result") or {}).get("home") is not None)
    except Exception: return None

def tired(tid, ts):
    lp = last_played(tid)
    return lp is not None and 0 < ts - lp < 30 * 3600

# ---------------- Flashscore home/away form ----------------
_FS = {"at": 0, "rows": []}
def fs_list():
    if time.time() - _FS["at"] < 30 * 60 and _FS["rows"]: return _FS["rows"]
    with ThreadPoolExecutor(3) as ex: feeds = list(ex.map(lambda off: scores.fs(f"f_3_{off}_3_en_1", 6), (0, 1, 2)))
    rows = [dict(id=r["AA"], h=r.get("AE", ""), a=r.get("AF", ""), t=int(r.get("AD", 0) or 0)) for f in feeds for r in scores.recs(f) if "AA" in r]
    if rows: _FS.update(at=time.time(), rows=rows)
    return _FS["rows"]

def fs_match(g):
    best = None
    for r in fs_list():
        if abs(r["t"] - g["ts"]) > 40 * 60: continue
        s = min(scores.sim(g["h"], r["h"]), scores.sim(g["a"], r["a"]))
        if s >= .72 and (best is None or s > best[0]): best = (s, r["id"])
    return best[1] if best else None

def venue_form(g):
    fid = fs_match(g)
    if not fid: return None
    F = extra.fs_form(fid)
    H, A = F.get("home_home") or [], F.get("away_away") or []
    return (H, A) if len(H) >= 4 and len(A) >= 4 else None

# ---------------- Basketball-Reference NBA ratings ----------------
def ratings():
    """{team: (attack per 100, defence per 100, pace)} for the current NBA season"""
    def get():
        yr = time.gmtime().tm_year + (1 if time.gmtime().tm_mon >= 9 else 0)
        s = _curl(f"https://www.basketball-reference.com/leagues/NBA_{yr}.html", 40)
        i = s.find('id="advanced-team"')
        if i < 0: return {}
        out = {}
        for m in re.finditer(r'data-stat="team"[^>]*>(?:<a[^>]*>)?([^<]+)(?:(?!</tr>).)*?data-stat="off_rtg"[^>]*>([\d.]+)(?:(?!</tr>).)*?data-stat="def_rtg"[^>]*>([\d.]+)(?:(?!</tr>).)*?data-stat="pace"[^>]*>([\d.]+)', s[i:], re.S):
            if "League" in m.group(1): continue
            out[m.group(1).strip()] = [float(m.group(2)), float(m.group(3)), float(m.group(4))]
        return out
    return _cached("nba_ratings.json", 12, get) or {}

def ratings_expect(g):
    """NBA: expected home / away points from the season ratings (+ home edge), or None"""
    R = ratings()
    def find(n):
        k = max(R, key=lambda x: scores.sim(n, x), default=None)
        return R[k] if k and scores.sim(n, k) >= .8 else None
    h, a = find(g["h"]), find(g["a"])
    if not h or not a: return None
    poss = (h[2] + a[2]) / 2
    return poss * (h[0] + a[1]) / 200 + 1.8, poss * (a[0] + h[1]) / 200 - 1.8

# ---------------- the check for one leg ----------------
def check(g, leg, nba):
    mid, spec, oid = leg["mid"], leg["spec"], leg["oid"]
    v = float(spec.split("=")[1].split("|")[0]) if "=" in spec else None
    sup, bad = [], []
    home_side = (mid == "219" and oid == "4") or (mid == "223" and oid == "1714") or mid == "227"
    away_side = (mid == "219" and oid == "5") or (mid == "223" and oid == "1715") or mid == "228"
    over = oid == "12"
    # 1) injuries (NBA)
    if nba:
        mh, nh = missing(g["h"]); ma, na = missing(g["a"])
        if mid in ("219", "223"):
            me, them, names = (mh, ma, nh) if home_side else (ma, mh, na)
            if me >= 10 and me > them + 5: bad.append(f"injuries: without {', '.join(names[:3])}")
            elif them >= 10 and them > me + 5: sup.append(f"rival missing {them:.0f} pts a game")
        if mid == "225":
            if over and mh + ma >= 15: bad.append(f"injuries: {mh + ma:.0f} pts a game missing")
            if not over and mh + ma >= 15: sup.append(f"{mh + ma:.0f} pts a game missing through injury")
        if mid == "236" and over and mh + ma >= 15: bad.append(f"injuries: {mh + ma:.0f} pts a game missing")
        if mid in ("227", "228"):
            me = mh if mid == "227" else ma
            if over and me >= 10: bad.append(f"injuries: team missing {me:.0f} pts a game")
    if mid in ("236", "756", "757"): return sup, bad                           # quarter lines: full-game form and ratings don't apply
    # 2) Flashscore home/away form
    vf = venue_form(g)
    if vf:
        H, A = vf
        rate = lambda L, c: sum(1 for x in L if c(*x)) / len(L)
        if mid == "225": r = (rate(H, lambda x, y: (x + y > v) == over) + rate(A, lambda x, y: (x + y > v) == over)) / 2
        elif mid == "227": r = rate(H, lambda x, y: (x > v) == over)
        elif mid == "228": r = rate(A, lambda x, y: (x > v) == over)
        elif mid == "219": r = (rate(H, lambda x, y: x > y) + rate(A, lambda x, y: x < y)) / 2 if home_side else (rate(H, lambda x, y: x < y) + rate(A, lambda x, y: x > y)) / 2
        else:                                   # handicap: home margin + v > 0 (home side), < -v ... (away side)
            ok = (lambda d: d + v > 0) if home_side else (lambda d: d + v < 0)
            r = (rate(H, lambda x, y: ok(x - y)) + rate(A, lambda x, y: ok(y - x))) / 2
        if r < .5: bad.append(f"Flashscore home/away form only {r:.0%}")
        elif r >= .7: sup.append(f"Flashscore home/away form {r:.0%}")
    # 3) NBA ratings (pace, attack, defence)
    if nba:
        re_ = ratings_expect(g)
        if re_:
            rh, ra = re_
            if mid == "225": gap = (rh + ra - v) if over else (v - rh - ra); what = f"ratings say {rh + ra:.0f} points"
            elif mid == "227": gap = (rh - v) if over else (v - rh); what = f"ratings say {rh:.0f}"
            elif mid == "228": gap = (ra - v) if over else (v - ra); what = f"ratings say {ra:.0f}"
            elif mid == "223": gap = (rh - ra + v) if home_side else -(rh - ra + v); what = f"ratings margin {rh - ra:+.0f}"
            else: gap = (rh - ra) if home_side else (ra - rh); what = f"ratings margin {rh - ra:+.0f}"
            if gap < 3: bad.append(f"NBA ratings too close/against ({what})")
            elif gap >= 8: sup.append(f"NBA {what}")
    return sup, bad

def warm():
    """listener warm-up (runs in the background): injuries, ratings, Flashscore basketball list"""
    try: return dict(injured_teams=len(injuries() or {}), rated=len(ratings()), flashscore=len(fs_list()))
    except Exception as e: return str(e)
