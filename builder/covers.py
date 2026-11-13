#!/usr/bin/env python3
"""OddsShark basketball stats – OddsShark's pages now live on Covers.com (oddsshark.com redirects there).
For every NBA and college (NCAAB) game: each team's season record, against-the-spread (ATS) record and Over/Under record, and the
last 10 head-to-head meetings with their Over/Under results. Used as a backing check on basketball legs (bbsources.check)."""
import datetime as dt, json, os, re, subprocess, time
from concurrent.futures import ThreadPoolExecutor
import scores
HERE = os.path.dirname(os.path.abspath(__file__)); CACHE = os.path.join(HERE, "cache", "covers"); os.makedirs(CACHE, exist_ok=True)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
ET = dt.timezone(dt.timedelta(hours=-5))

def _get(url, secs=30):
    return subprocess.run(["curl", "-sL", "--compressed", "-m", str(secs), "-A", UA, url], capture_output=True, text=True, errors="ignore").stdout

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

def day(league, date):
    """[{id, home, away}] for a league ("nba" / "ncaab") on a US date (YYYY-MM-DD)"""
    def get():
        s = _get(f"https://www.covers.com/sports/{league}/matchups?selectedDate={date}", 40)
        out = []
        for m in re.finditer(r'<article id="(?:nba|ncaab)-(\d+)"[^>]*>', s):
            tag = m.group(0)
            a = re.search(r"data-away-team-fullname=(.*?) data-away-team-name=", tag); h = re.search(r"data-home-team-fullname=(.*?) data-home-team-name=", tag)
            if a and h: out.append(dict(id=m.group(1), home=h.group(1).strip(), away=a.group(1).strip()))
        return out
    return _cached(f"day_{league}_{date}.json", 3, get) or []

def _rec(x):
    try: return [int(v) for v in x.split("-")]
    except Exception: return None

def matchup(mid, league):
    """{"home": {...}, "away": {...}, "h2h_ou": [over, under, push]} from the matchup page's records table"""
    def get():
        s = _get(f"https://www.covers.com/sport/basketball/{league}/matchup/{mid}", 40)
        t = re.sub(r"<style.*?</style>", " ", s, flags=re.S); t = re.sub(r"<script.*?</script>", " ", t, flags=re.S)
        t = re.sub(r"<[^>]+>", "|", t); t = re.sub(r"\|[\s|]*", "|", t)
        i = t.find("Team Table|Team|Record|ATS|O/U|Away|Home")
        if i < 0: return {}
        rows = re.findall(r"\|([A-Z0-9&.']{2,6})\|(\d+-\d+)\|(\d+-\d+-\d+)\|(\d+-\d+-\d+)\|(\d+-\d+)\|(\d+-\d+)", t[i:i + 1500])
        if len(rows) < 2: return {}
        tm = lambda r: dict(code=r[0], rec=_rec(r[1]), ats=_rec(r[2]), ou=_rec(r[3]), away=_rec(r[4]), home=_rec(r[5]))
        out = dict(away=tm(rows[0]), home=tm(rows[1]))                       # the page lists the away team first
        j = t.find("Head-To-Head|Last 10")
        if j >= 0:
            m = re.search(r"Over / Under\|(\d+-\d+-\d+)", t[j:j + 600])
            if m: out["h2h_ou"] = _rec(m.group(1))
        return out
    return _cached(f"m_{mid}.json", 6, get) or {}

def find(g):
    """the Covers matchup for a SportyBet basketball game (NBA / NCAA only), or None"""
    lg = "nba" if "NBA" in g["lg"] and "G League" not in g["lg"] else "ncaab" if "NCAA" in g["lg"] else None
    if not lg: return None
    d = dt.datetime.fromtimestamp(g["ts"], ET).date()
    best = None
    for date in (d, d - dt.timedelta(days=1)):
        for x in day(lg, date.isoformat()):
            s = min(scores.sim(g["h"], x["home"]), scores.sim(g["a"], x["away"]))
            if s >= .6 and (best is None or s > best[0]): best = (s, x["id"])
    return (lg, best[1]) if best else None

def check(g, leg):
    """(support, against) from OddsShark/Covers records for a basketball leg"""
    f = find(g)
    if not f: return [], []
    M = matchup(f[1], f[0])
    if not M: return [], []
    mid, oid = leg["mid"], leg["oid"]; sup, bad = [], []
    share = lambda r: r[0] / (r[0] + r[1]) if r and r[0] + r[1] >= 6 else None
    if mid in ("225", "236"):
        oh, oa = share(M["home"]["ou"]), share(M["away"]["ou"])
        if oh is not None and oa is not None:
            r = (oh + oa) / 2 if oid == "12" else 1 - (oh + oa) / 2
            if r <= .35: bad.append(f"OddsShark O/U records only {r:.0%} this way")
            elif r >= .6: sup.append(f"OddsShark O/U records {r:.0%} ({M['home']['code']} {M['home']['ou'][0]}-{M['home']['ou'][1]}, {M['away']['code']} {M['away']['ou'][0]}-{M['away']['ou'][1]})")
        h2 = M.get("h2h_ou")
        if h2 and h2[0] + h2[1] >= 5:
            r = h2[0] / (h2[0] + h2[1]); r = r if oid == "12" else 1 - r
            if r <= .3: bad.append(f"OddsShark h2h O/U only {r:.0%}")
            elif r >= .7: sup.append(f"OddsShark h2h O/U {r:.0%} of last {h2[0] + h2[1]}")
    if mid == "223":
        me = M["home"] if oid == "1714" else M["away"]; r = share(me["ats"])
        if r is not None:
            if r <= .35: bad.append(f"OddsShark: {me['code']} covers the spread only {r:.0%}")
            elif r >= .6: sup.append(f"OddsShark: {me['code']} covers the spread {r:.0%}")
    if mid == "219":
        me = M["home"] if oid == "4" else M["away"]; r = share(me["rec"])
        if r is not None and r <= .35: bad.append(f"OddsShark: {me['code']} wins only {r:.0%}")
    return sup, bad

def warm():
    """listener warm-up: today's and tomorrow's NBA / college matchups and their records"""
    n = 0
    for lg in ("nba", "ncaab"):
        for k in (-1, 0, 1):
            date = (dt.datetime.now(ET).date() + dt.timedelta(days=k)).isoformat()
            L = day(lg, date)
            with ThreadPoolExecutor(6) as ex: n += sum(1 for x in ex.map(lambda x: matchup(x["id"], lg), L) if x)
    return n
