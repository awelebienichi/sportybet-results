#!/usr/bin/env python3
"""Whole-season data from Sportradar (covers lower divisions, reserves, youth, women's): league table rows per team
(played, W/D/L, goals for/against, home and away). Used by three ticket types (goal-fest, always-beaten, draw & low-scoring)
and as an extra check on every leg."""
import json, os, subprocess, time
from concurrent.futures import ThreadPoolExecutor
HERE = os.path.dirname(os.path.abspath(__file__)); CACHE = os.path.join(HERE, "cache", "season"); os.makedirs(CACHE, exist_ok=True)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
BASE = "https://stats.fn.sportradar.com/sportybet/en/Europe:Berlin/gismo/"

def _get(path):
    for i in range(3):
        r = subprocess.run(["curl", "-s", "--compressed", "-m", "40", "-A", UA, "-H", "Referer: https://www.sportybet.com/", BASE + path], capture_output=True, text=True).stdout
        try: return json.loads(r)
        except Exception: time.sleep(2)
    return {}

def _cached(name, hours, fn):
    f = os.path.join(CACHE, name)
    if os.path.exists(f) and time.time() - os.path.getmtime(f) < hours * 3600:
        try: return json.load(open(f))
        except Exception: pass
    v = fn()
    if v is not None:
        tmp = f"{f}.{os.getpid()}.tmp"
        try: json.dump(v, open(tmp, "w")); os.replace(tmp, f)
        except OSError: pass
    return v

def season_of(eid):
    sid = eid.split(":")[-1]
    def get():
        d = _get(f"match_info/{sid}")
        try: return str(d["doc"][0]["data"]["match"]["_seasonid"])
        except Exception: return ""
    return _cached(f"sid_{sid}.json", 72, get) or ""

def table(season):
    """{team uid: row} for the season (the first table when a league has groups)"""
    def get():
        d = _get(f"stats_season_tables/{season}")
        out = {}
        try:
            for t in d["doc"][0]["data"]["tables"]:
                for r in t.get("tablerows", []):
                    u = str(r["team"]["uid"]); n = r.get("total") or 0
                    if not n: continue
                    out[u] = dict(name=r["team"]["name"], pos=r.get("pos"), n=n, w=r.get("winTotal", 0), d=r.get("drawTotal", 0), l=r.get("lossTotal", 0),
                                  gf=r.get("goalsForTotal", 0), ga=r.get("goalsAgainstTotal", 0), pts=r.get("pointsTotal", 0),
                                  nh=(r.get("winHome", 0) + r.get("drawHome", 0) + r.get("lossHome", 0)), lh=r.get("lossHome", 0), wh=r.get("winHome", 0),
                                  na=(r.get("winAway", 0) + r.get("drawAway", 0) + r.get("lossAway", 0)), la=r.get("lossAway", 0), wa=r.get("winAway", 0),
                                  size=len(t.get("tablerows", [])))
        except Exception: pass
        return out
    return _cached(f"tab_{season}.json", 6, get) or {}

def teams(g):
    """(home row, away row) for a game, or (None, None)"""
    s = season_of(g["eid"])
    if not s: return None, None
    T = table(s)
    return T.get(g["hid"]), T.get(g["aid"])

def prefetch(G, n=80):
    with ThreadPoolExecutor(8) as ex: list(ex.map(teams, G[:n]))

def rates(r):
    """per-team season numbers"""
    n = max(r["n"], 1)
    return dict(n=r["n"], win=r["w"] / n, draw=r["d"] / n, loss=r["l"] / n, gpg=(r["gf"] + r["ga"]) / n, gf=r["gf"] / n, ga=r["ga"] / n, ppg=r["pts"] / n,
                hloss=(r["lh"] / r["nh"]) if r["nh"] else None, aloss=(r["la"] / r["na"]) if r["na"] else None, pos=r["pos"], size=r["size"])

def check(leg, g):
    """season-based (support, against) for a leg: never 'to win' for a side that loses most games, never 'goals' between two
    season-long low scorers, never 'under' between two goal-fest sides"""
    H, A = teams(g)
    if not H or not A or min(H["n"], A["n"]) < 5: return [], []
    h, a = rates(H), rates(A)
    mid, oid, spec = leg["mid"], leg["oid"], leg.get("spec") or ""
    ln = float(spec.split("=")[1]) if spec.startswith("total=") else None
    sup, bad = [], []
    side = "h" if (mid in ("1", "60200") and oid == "1") else "a" if (mid in ("1", "60200") and oid == "3") else None
    if side:
        me, them = (h, a) if side == "h" else (a, h)
        if me["loss"] >= .5: bad.append(f"season: lost {me['loss']:.0%} of {me['n']}")
        elif me["win"] >= .6 and them["loss"] >= .5: sup.append(f"season: won {me['win']:.0%}, rival lost {them['loss']:.0%}")
    g2 = (h["gpg"] + a["gpg"]) / 2
    if mid == "18" and ln is not None:
        if oid == "12" and ln >= 2.5 and g2 < 2.3: bad.append(f"season: only {g2:.1f} goals a game")
        if oid == "12" and g2 >= ln + 1.2: sup.append(f"season: {g2:.1f} goals a game")
        if oid == "13" and g2 >= ln + .6: bad.append(f"season: {g2:.1f} goals a game")
        if oid == "13" and g2 <= ln - .8: sup.append(f"season: {g2:.1f} goals a game")
    if mid in ("69", "91", "19") and oid == "12" and h["gf"] < .8: bad.append(f"season: home scores {h['gf']:.1f} a game")
    if mid in ("70", "92", "20") and oid == "12" and a["gf"] < .8: bad.append(f"season: away scores {a['gf']:.1f} a game")
    if mid == "29" and oid == "74" and min(h["gf"], a["gf"]) < .9: bad.append("season: one side rarely scores")
    return sup, bad

def check_leg(leg):
    """season check for a leg, finding its game (team ids) in the builder's game list"""
    try:
        import phonebuild
        g = next((x for x in phonebuild._GAMES["list"] if x["eid"] == leg["eid"]), None)
        if not g:
            ev, _ = phonebuild.event_game(leg["eid"]); g = ev
        return check(leg, g) if g else ([], [])
    except Exception: return [], []
