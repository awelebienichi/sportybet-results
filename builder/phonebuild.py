#!/usr/bin/env python3
"""Phone ticket builder: run by .github/workflows/build.yml for each request sent from build.html (via ntfy).
usage: phonebuild.py '{"type":"fav","legs":40,"tickets":1,"until":"sun"}'
types: fav (strong favourites to win), smallfav (smallest-odds strong favourites), o15, o25 (high-scoring leagues),
       o25yw (youth & women's leagues), u25 (low-scoring leagues)
Rules are the user's standing rules: favourites must pass form + h2h checks AND be clear bookmaker favourites
(opponent's odds 1.5x+), goal picks need both teams' last-10 + h2h to agree, no game repeated across tickets,
every code reloaded and checked before it is reported, then added to the tracker."""
import json, os, re, subprocess, sys, time, collections, threading, datetime as dt
from concurrent.futures import ThreadPoolExecutor
HERE = os.path.dirname(os.path.abspath(__file__)); SITE = os.path.dirname(HERE)
CACHE = os.path.join(HERE, "cache"); os.makedirs(CACHE, exist_ok=True)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
LAGOS = dt.timezone(dt.timedelta(hours=1))
def hm(ts): return dt.datetime.fromtimestamp(ts, LAGOS).strftime("%a %H:%M")

def curl(args, timeout=60):
    for i in range(3):
        try:
            r = subprocess.run(["curl", "-s", "--compressed", "-m", str(timeout), "-A", UA, *args], capture_output=True, text=True).stdout
            if r: return json.loads(r)
        except Exception: pass
        time.sleep(2)
def get(u, ref="https://www.sportybet.com/ng/"): return curl(["-H", "Accept: application/json", "-H", "Referer: " + ref, u])
def post(sel):
    for i in range(4):
        r = curl(["-X", "POST", "-H", "Content-Type: application/json", "-H", "Accept: application/json", "-H", "Origin: https://www.sportybet.com",
                  "-H", "Referer: https://www.sportybet.com/ng/", "--data", json.dumps({"selections": sel}), "https://www.sportybet.com/api/ng/orders/share"])
        if r and (r.get("data") or {}).get("shareCode"): return r["data"]["shareCode"]
        time.sleep(2)
def load(code):
    for i in range(5):
        r = get("https://www.sportybet.com/api/ng/orders/share/" + code)
        if r and (r.get("data") or {}).get("outcomes"): return r["data"]["outcomes"]
        time.sleep(2)
    return []

# ---------- games ----------
def games(lo, hi):
    """every real (non-virtual) game kicking off in [lo, hi] with 1X2 and Over/Under 1.5/2.5 prices.
    1X2 and Over/Under are listed separately: asking for both at once makes the reply too big and it gets cut off."""
    G = {}; tl = max(24, int((hi - time.time()) / 3600) + 2)
    for mk in ("1", "18"):
        p = 1
        while True:
            r = curl(["-H", "Accept: application/json", "-H", "Referer: https://www.sportybet.com/ng/",
                      f"https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A1&marketId={mk}&pageSize=100&pageNum={p}&timeline={tl}"], 180)
            d = (r or {}).get("data")
            if not d: break
            for t in d["tournaments"]:
                for e in t["events"]:
                    s = e["estimateStartTime"] / 1000; cat = e["sport"]["category"]["name"]; trn = e["sport"]["category"]["tournament"]["name"]
                    if not (lo <= s <= hi) or "Simulated" in cat or "SRL" in trn: continue
                    g = G.setdefault(e["eventId"], dict(eid=e["eventId"], ts=s, h=e["homeTeamName"], a=e["awayTeamName"], lg=cat + " - " + trn,
                                     hid=e["homeTeamId"].split(":")[-1], aid=e["awayTeamId"].split(":")[-1], x={}, ou={}))
                    for m in e.get("markets", []):
                        for o in m.get("outcomes", []):
                            if not o.get("isActive"): continue
                            if m["id"] == "1": g["x"][o["id"]] = float(o["odds"])
                            if m["id"] == "18" and m.get("specifier") in ("total=1.5", "total=2.5"): g["ou"][(m["specifier"], o["id"])] = float(o["odds"])
            if p * 100 >= d["totalNum"]: break
            p += 1
    return list(G.values())

_GAMES = {"at": 0, "hi": 0, "list": []}
def games_cached(lo, hi, fresh=15 * 60):
    """reuse the game list fetched in the last 15 minutes (the listener keeps a 7-day list warm); prices are re-checked when the code is reloaded"""
    if time.time() - _GAMES["at"] > fresh or _GAMES["hi"] < hi:
        far = max(hi, time.time() + 7 * 86400)
        _GAMES.update(at=time.time(), hi=far, list=games(time.time() + 5 * 60, far))
    return [g for g in _GAMES["list"] if lo <= g["ts"] <= hi]

def lastx(t):
    f = os.path.join(CACHE, t + ".json")
    if not (os.path.exists(f) and time.time() - os.path.getmtime(f) < 6 * 3600):
        d = get(f"https://stats.fn.sportradar.com/sportybet/en/Europe:Berlin/gismo/stats_team_lastx/{t}/10", "https://www.sportybet.com/")
        if d:   # atomic, and a temp name of its own: the listener's warm-up thread may fetch the same team at the same moment
            tmp = f"{f}.{os.getpid()}.{threading.get_ident()}.tmp"
            try: json.dump(d, open(tmp, "w")); os.replace(tmp, f)
            except OSError: pass
    try: d = json.load(open(f))["doc"][0]["data"]
    except Exception: return []
    ms = [m for m in ((d.get("matches") if isinstance(d, dict) else None) or []) if isinstance(m, dict) and (m.get("result") or {}).get("home") is not None and not m.get("canceled")]
    ms.sort(key=lambda m: -m["time"]["uts"]); out = []
    for m in ms[:10]:
        home = m["teams"]["home"]["uid"] == int(t); r = m["result"]; p1 = (m.get("periods") or {}).get("p1")
        out.append(dict(id=m["_id"], gf=r["home"] if home else r["away"], ga=r["away"] if home else r["home"], home=home,   # always from this team's side
                        h1f=(p1["home"] if home else p1["away"]) if p1 else None, h1a=(p1["away"] if home else p1["home"]) if p1 else None))
        o = out[-1]; o["h2f"] = o["gf"] - o["h1f"] if p1 else None; o["h2a"] = o["ga"] - o["h1a"] if p1 else None
    return out
def h2h(h, a):
    d = get(f"https://stats.fn.sportradar.com/sportybet/en/Europe:Berlin/gismo/stats_team_versusrecent/{h}/{a}", "https://www.sportybet.com/")
    out = []
    try:
        for m in d["doc"][0]["data"]["matches"][:8]:
            r = m.get("result") or {}
            if r.get("home") is None: continue
            hh = m["teams"]["home"]["uid"] == int(h); out.append((r["home"], r["away"]) if hh else (r["away"], r["home"]))   # (home team goals, away team goals)
    except Exception: pass
    return out
def fetch_teams(G):
    T = sorted({t for g in G for t in (g["hid"], g["aid"])})
    with ThreadPoolExecutor(16) as ex: return dict(zip(T, ex.map(lastx, T)))

YW = re.compile(r"U1[5-9]|U2[0-3]|Youth|Junior|Juniori|Primavera|Reserve|Women|Femin|Frauen|Damallsvenskan|Elitettan|Toppserien|Next Gen|Development", re.I)

# ---------- pickers: each returns a ranked list of legs {eid, mid, spec, oid, pick, odds, why, ts, game, lg} ----------
def pick_fav(G, LX, smallest=False):
    C = []
    for g in G:
        for side, oid, other in (("home", "1", "3"), ("away", "3", "1")):
            od = g["x"].get(oid); oo = g["x"].get(other, 99)
            if not od or od > 2.6 or oo < 1.5 * od: continue                    # clear bookmaker favourite only
            me, op = (g["hid"], g["aid"]) if side == "home" else (g["aid"], g["hid"])
            F, O = LX.get(me, []), LX.get(op, [])
            if len(F) < 6 or len(O) < 6: continue
            fw = sum(m["gf"] > m["ga"] for m in F) / len(F); fl = sum(m["gf"] < m["ga"] for m in F) / len(F)
            ol = sum(m["gf"] < m["ga"] for m in O) / len(O); gd = sum(m["gf"] - m["ga"] for m in F) / len(F)
            V = [m for m in F if m["home"] == (side == "home")]; vw = sum(m["gf"] > m["ga"] for m in V) / len(V) if len(V) >= 3 else fw
            if fw < .6 or fl > .2 or vw < .5 or ol < .3 or gd < .6: continue
            C.append(dict(g=g, side=side, oid=oid, od=od, fw=fw, fl=fl, ol=ol, vw=vw, gd=gd))
    with ThreadPoolExecutor(8) as ex: H = list(ex.map(lambda c: h2h(c["g"]["hid"], c["g"]["aid"]), C))
    out = []
    for c, hh in zip(C, H):
        if c["side"] == "away": hh = [(b, a) for a, b in hh]
        hw = sum(a > b for a, b in hh) / len(hh) if hh else None; hl = sum(a < b for a, b in hh) / len(hh) if hh else None
        if len(hh) >= 3 and (hl > .25 or hw < .5): continue
        s = c["fw"]*.35 + c["vw"]*.2 + c["ol"]*.15 + (1-c["fl"])*.1 + min(c["gd"], 3)/3*.1 + (hw if len(hh) >= 3 else c["fw"])*.1
        g = c["g"]; team = g["h"] if c["side"] == "home" else g["a"]
        out.append(dict(eid=g["eid"], mid="1", spec="", oid=c["oid"], pick=f"{team} to win", odds=c["od"], ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"],
                        s=-c["od"] if smallest else s, why=f"won {c['fw']:.0%} lost {c['fl']:.0%} of last 10, rival lost {c['ol']:.0%}" + (f", won {hw:.0%} of {len(hh)} meetings" if len(hh) >= 3 else "")))
    return sorted(out, key=lambda r: -r["s"])

def league_stats(G, LX):
    L = collections.defaultdict(dict)
    for g in G:
        for t in (g["hid"], g["aid"]):
            for m in LX.get(t, []): L[g["lg"]][m["id"]] = m["gf"] + m["ga"]
    return {k: (sum(v.values())/len(v), sum(x >= 3 for x in v.values())/len(v), sum(x <= 1 for x in v.values())/len(v)) for k, v in L.items() if len(v) >= 15}

def pick_goals(G, LX, kind):
    LS = league_stats(G, LX)
    if kind == "o25": ok_lg = lambda k: LS[k][0] >= 3.1 and LS[k][1] >= .6
    elif kind == "o25yw": ok_lg = lambda k: YW.search(k) and LS[k][0] >= 2.9
    elif kind == "u25": ok_lg = lambda k: LS[k][0] <= 2.3 and LS[k][2] >= .3
    else: ok_lg = lambda k: LS[k][0] >= 2.6                                 # o15
    spec, oid, line, over = {"o25": ("total=2.5", "12", 3, True), "o25yw": ("total=2.5", "12", 3, True), "u25": ("total=2.5", "13", 3, False), "o15": ("total=1.5", "12", 2, True)}[kind]
    need, hneed = {"o25": (.6, .5), "o25yw": (.6, .5), "u25": (.6, .5), "o15": (.8, .7)}[kind]
    C = []
    for g in G:
        if g["lg"] not in LS or not ok_lg(g["lg"]) or (spec, oid) not in g["ou"]: continue
        H, A = LX.get(g["hid"], []), LX.get(g["aid"], [])
        if len(H) < 6 or len(A) < 6: continue
        hit = (lambda n: n >= line) if over else (lambda n: n < line)
        hu = sum(hit(m["gf"] + m["ga"]) for m in H) / len(H); au = sum(hit(m["gf"] + m["ga"]) for m in A) / len(A)
        if min(hu, au) < need: continue
        if kind == "u25":   # user's rule: no Under on a strong favourite or a high-scoring side
            if min(g["x"].get("1", 9), g["x"].get("3", 9)) < 1.6 or max(sum(m["gf"] for m in H)/len(H), sum(m["gf"] for m in A)/len(A)) >= 1.6: continue
        C.append((g, hu, au, hit))
    with ThreadPoolExecutor(8) as ex: HH = list(ex.map(lambda c: h2h(c[0]["hid"], c[0]["aid"]), C))
    out = []
    for (g, hu, au, hit), hh in zip(C, HH):
        hr = sum(hit(a + b) for a, b in hh) / len(hh) if len(hh) >= 3 else None
        if hr is not None and hr < hneed: continue
        s = min(hu, au)*.4 + (hu+au)/2*.3 + (hr if hr is not None else (hu+au)/2)*.2 + (min(LS[g["lg"]][0], 4.5)/4.5 if over else (3 - min(LS[g["lg"]][0], 3))/3)*.1
        word = {"o25": "Over 2.5", "o25yw": "Over 2.5", "u25": "Under 2.5", "o15": "Over 1.5"}[kind]
        out.append(dict(eid=g["eid"], mid="18", spec=spec, oid=oid, pick=word, odds=g["ou"][(spec, oid)], ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"], s=s,
                        why=f"{word} in {hu:.0%} / {au:.0%} of the teams' last 10" + (f", {hr:.0%} of {len(hh)} meetings" if hr is not None else "") + f", league avg {LS[g['lg']][0]:.1f} goals"))
    return sorted(out, key=lambda r: -r["s"])

def event_markets(eid):
    d = curl(["-H", "Accept: application/json", "-H", "Referer: https://www.sportybet.com/ng/", f"https://www.sportybet.com/api/ng/factsCenter/event?eventId={eid}&productId=3"], 180)
    return ((d or {}).get("data") or {}).get("markets", [])

def pick_fav1h(G, LX, half=1):
    """strong side to score in the 1st half (1st half team Over 0.5: market 69 home / 70 away): a clear favourite that scored
    before half-time in 80%+ of its recent games, against a side that concedes before half-time 60%+ of the time"""
    C = []
    for g in G:
        for side, oid, other in (("home", "1", "3"), ("away", "3", "1")):
            od = g["x"].get(oid); oo = g["x"].get(other, 99)
            if not od or od > 2.2 or oo < 1.5 * od: continue
            me, op = (g["hid"], g["aid"]) if side == "home" else (g["aid"], g["hid"])
            kf, ka = f"h{half}f", f"h{half}a"
            F = [m for m in LX.get(me, []) if m[kf] is not None]; O = [m for m in LX.get(op, []) if m[ka] is not None]
            if len(F) < 6 or len(O) < 6: continue
            fs = sum(m[kf] > 0 for m in F) / len(F); oc = sum(m[ka] > 0 for m in O) / len(O)
            if fs < .8 or oc < .6: continue
            C.append(dict(g=g, side=side, fs=fs, oc=oc, nf=len(F), no=len(O), od=od))
    C.sort(key=lambda c: -(c["fs"] * .6 + c["oc"] * .4)); C = C[:80]
    def price(c):
        mid = ({1: "69", 2: "91"} if c["side"] == "home" else {1: "70", 2: "92"})[half]
        for m in event_markets(c["g"]["eid"]):
            if m["id"] == mid and m.get("specifier") == "total=0.5":
                for o in m["outcomes"]:
                    if o["id"] == "12" and o.get("isActive"): return mid, float(o["odds"])
        return mid, None
    with ThreadPoolExecutor(8) as ex: PR = list(ex.map(price, C))
    out = []
    for c, (mid, odds) in zip(C, PR):
        if not odds: continue
        g = c["g"]; team = g["h"] if c["side"] == "home" else g["a"]; opp = g["a"] if c["side"] == "home" else g["h"]
        out.append(dict(eid=g["eid"], mid=mid, spec="total=0.5", oid="12", pick=f"{team} to score in {'1st' if half == 1 else '2nd'} half", odds=odds, ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"],
                        s=c["fs"] * .6 + c["oc"] * .4 - odds * .02,
                        why=f"{team} scored in the {'1st' if half == 1 else '2nd'} half in {c['fs']:.0%} of last {c['nf']}; {opp} conceded in it in {c['oc']:.0%} of last {c['no']}; win odds {c['od']}"))
    return sorted(out, key=lambda r: -r["s"])

def pick_h1o05(G, LX, half=1):
    """1st half Over 0.5 (market 68): a goal before half-time in 85%+ of both teams' recent games"""
    C = []
    for g in G:
        kf, ka = f"h{half}f", f"h{half}a"
        H = [m for m in LX.get(g["hid"], []) if m[kf] is not None]; A = [m for m in LX.get(g["aid"], []) if m[kf] is not None]
        if len(H) < 6 or len(A) < 6: continue
        hr = sum(m[kf] + m[ka] > 0 for m in H) / len(H); ar = sum(m[kf] + m[ka] > 0 for m in A) / len(A)
        if min(hr, ar) < .85: continue
        C.append((g, hr, ar, len(H), len(A)))
    C.sort(key=lambda c: -(min(c[1], c[2]) * .6 + (c[1] + c[2]) / 2 * .4)); C = C[:80]
    def price(c):
        for m in event_markets(c[0]["eid"]):
            if m["id"] == {1: "68", 2: "90"}[half] and m.get("specifier") == "total=0.5":
                for o in m["outcomes"]:
                    if o["id"] == "12" and o.get("isActive"): return float(o["odds"])
    with ThreadPoolExecutor(8) as ex: PR = list(ex.map(price, C))
    out = []
    for (g, hr, ar, nh, na), odds in zip(C, PR):
        if not odds: continue
        out.append(dict(eid=g["eid"], mid={1: "68", 2: "90"}[half], spec="total=0.5", oid="12", pick=f"{'1st' if half == 1 else '2nd'} half Over 0.5", odds=odds, ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"],
                        s=min(hr, ar) * .6 + (hr + ar) / 2 * .4 - odds * .02,
                        why=f"goal in the {'1st' if half == 1 else '2nd'} half in {hr:.0%} of {g['h']}'s last {nh} and {ar:.0%} of {g['a']}'s last {na}"))
    return sorted(out, key=lambda r: -r["s"])

def pick_yw_strong(G, LX):
    """Over 2.5 in youth & women's games where a strong side plays: a clear favourite (win odds 1.80 or less, rival 2.5x+) that won 60%+
    and averages 2.0+ goals, whose games went over 2.5 in 70%+ of its last 10, against a rival that concedes 1.2+ a game"""
    C = []
    for g in G:
        if not YW.search(g["lg"]) or ("total=2.5", "12") not in g["ou"]: continue
        for side, oid, other in (("home", "1", "3"), ("away", "3", "1")):
            od = g["x"].get(oid); oo = g["x"].get(other, 99)
            if not od or od > 1.8 or oo < 2.5 * od: continue
            me, op = (g["hid"], g["aid"]) if side == "home" else (g["aid"], g["hid"])
            F, O = LX.get(me, []), LX.get(op, [])
            if len(F) < 6 or len(O) < 6: continue
            fo = sum(m["gf"] + m["ga"] >= 3 for m in F) / len(F); fg = sum(m["gf"] for m in F) / len(F)
            oc = sum(m["ga"] for m in O) / len(O); oo25 = sum(m["gf"] + m["ga"] >= 3 for m in O) / len(O)
            fw = sum(m["gf"] > m["ga"] for m in F) / len(F)
            if fo < .7 or fg < 2.0 or oc < 1.2 or fw < .6: continue
            C.append(dict(g=g, side=side, od=od, fo=fo, fg=fg, oc=oc, oo25=oo25, nf=len(F), no=len(O)))
    with ThreadPoolExecutor(8) as ex: HH = list(ex.map(lambda c: h2h(c["g"]["hid"], c["g"]["aid"]), C))
    out = []
    for c, hh in zip(C, HH):
        hr = sum(a + b >= 3 for a, b in hh) / len(hh) if len(hh) >= 3 else None
        if hr is not None and hr < .5: continue
        g = c["g"]; team = g["h"] if c["side"] == "home" else g["a"]; opp = g["a"] if c["side"] == "home" else g["h"]
        out.append(dict(eid=g["eid"], mid="18", spec="total=2.5", oid="12", pick="Over 2.5", odds=g["ou"][("total=2.5", "12")], ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"],
                        s=c["fo"] * .35 + min(c["fg"], 4) / 4 * .25 + min(c["oc"], 3) / 3 * .2 + c["oo25"] * .1 + (hr if hr is not None else c["fo"]) * .1,
                        why=f"strong side {team} (win {c['od']}) scores {c['fg']:.1f} a game, {c['fo']:.0%} of its games over 2.5; {opp} concedes {c['oc']:.1f} a game" + (f"; {hr:.0%} of {len(hh)} meetings over" if hr is not None else "")))
    return sorted(out, key=lambda r: -r["s"])

# ---------- multi-market: GG, tracker's best markets, safest mix ----------
MK = {  # key: (market id, specifier, outcome id, label)
    "win_h": ("1", "", "1", "{h} to win"), "win_a": ("1", "", "3", "{a} to win"),
    "up_h": ("60200", "", "1", "{h} to win (1UP)"), "up_a": ("60200", "", "3", "{a} to win (1UP)"),
    "dc_x2": ("10", "", "11", "Double chance {a} or draw"),
    "h1dc_1x": ("63", "", "9", "1st half double chance {h} or draw"), "h1dc_x2": ("63", "", "11", "1st half double chance {a} or draw"),
    "o15": ("18", "total=1.5", "12", "Over 1.5"), "gb25": ("450001", "", "25", "Goal bounds 2-5+"),
    "h1o05": ("68", "total=0.5", "12", "1st half Over 0.5"), "h2o05": ("90", "total=0.5", "12", "2nd half Over 0.5"),
    "gg": ("29", "", "74", "GG (both teams score)"), "hgg": ("860", "", "74", "{h} or GG"),
}
SETS = {"gg": ["gg"], "best": ["up_h", "up_a", "h1dc_1x", "hgg", "gb25"],
        "mix": ["win_h", "win_a", "up_h", "up_a", "dc_x2", "h1dc_1x", "h1dc_x2", "o15", "h1o05", "h2o05", "gg", "hgg"]}

def rates(g, LX, hh):
    """chance-like rates from both teams' last 10 (and meetings for full-time markets); keys as in MK"""
    H, A = LX.get(g["hid"], []), LX.get(g["aid"], [])
    if len(H) < 6 or len(A) < 6: return {}
    f = lambda L, c: sum(1 for m in L if c(m)) / len(L)
    def both(ch, ca, hc=None):
        r = (f(H, ch) + f(A, ca)) / 2
        if hc and len(hh) >= 3: r = r * .7 + sum(1 for x in hh if hc(*x)) / len(hh) * .3
        return r
    H1, A1 = [m for m in H if m["h1f"] is not None], [m for m in A if m["h1f"] is not None]
    h1 = lambda ch, ca: (f(H1, ch) + f(A1, ca)) / 2 if len(H1) >= 6 and len(A1) >= 6 else 0
    xh, xa = g["x"].get("1", 99), g["x"].get("3", 99)
    R = {"o15": both(lambda m: m["gf"] + m["ga"] >= 2, lambda m: m["gf"] + m["ga"] >= 2, lambda a, b: a + b >= 2),
         "gb25": both(lambda m: m["gf"] + m["ga"] >= 2, lambda m: m["gf"] + m["ga"] >= 2, lambda a, b: a + b >= 2),
         "gg": both(lambda m: m["gf"] > 0 and m["ga"] > 0, lambda m: m["gf"] > 0 and m["ga"] > 0, lambda a, b: a > 0 and b > 0),
         "hgg": both(lambda m: m["gf"] > m["ga"] or (m["gf"] > 0 and m["ga"] > 0), lambda m: m["gf"] < m["ga"] or (m["gf"] > 0 and m["ga"] > 0), lambda a, b: a > b or (a > 0 and b > 0)),
         "h1o05": h1(lambda m: m["h1f"] + m["h1a"] > 0, lambda m: m["h1f"] + m["h1a"] > 0),
         "h2o05": h1(lambda m: m["h2f"] + m["h2a"] > 0, lambda m: m["h2f"] + m["h2a"] > 0),
         "h1dc_1x": h1(lambda m: m["h1f"] >= m["h1a"], lambda m: m["h1f"] <= m["h1a"]) if xh <= xa * 1.3 else 0,
         "h1dc_x2": h1(lambda m: m["h1f"] <= m["h1a"], lambda m: m["h1f"] >= m["h1a"]) if xa <= xh * 1.3 else 0}
    if xh <= 1.8 and xa >= 1.5 * xh:   # home is a clear favourite
        R["win_h"] = both(lambda m: m["gf"] > m["ga"], lambda m: m["gf"] < m["ga"], lambda a, b: a > b)
        R["up_h"] = both(lambda m: m["gf"] > m["ga"] or (m["gf"] > 0 and m["gf"] == m["ga"]) * .5, lambda m: m["gf"] < m["ga"] or (m["ga"] > 0 and m["gf"] == m["ga"]) * .5)
    if xa <= 1.8 and xh >= 1.5 * xa:   # away is a clear favourite
        R["win_a"] = both(lambda m: m["gf"] < m["ga"], lambda m: m["gf"] > m["ga"], lambda a, b: b > a)
        R["up_a"] = both(lambda m: m["gf"] < m["ga"] or (m["ga"] > 0 and m["gf"] == m["ga"]) * .5, lambda m: m["gf"] > m["ga"] or (m["gf"] > 0 and m["gf"] == m["ga"]) * .5)
        R["dc_x2"] = both(lambda m: m["gf"] <= m["ga"], lambda m: m["gf"] >= m["ga"], lambda a, b: b >= a)
    return R

def pick_multi(G, LX, kind, want):
    keys = SETS[kind]; floor = {"gg": .68, "best": .8, "mix": .82}[kind]
    pre = []
    for g in G:
        R = rates(g, LX, [])
        best = max(((R[k], k) for k in keys if R.get(k, 0) >= floor), default=None)
        if best: pre.append((best[0], g))
    pre.sort(key=lambda x: -x[0]); pre = [g for _, g in pre[:max(60, want * 3)]]
    def detail(g):
        hh = h2h(g["hid"], g["aid"])
        mk = {(m["id"], m.get("specifier") or "", o["id"]): float(o["odds"]) for m in event_markets(g["eid"]) for o in m.get("outcomes", []) if o.get("isActive")}
        return g, hh, mk
    with ThreadPoolExecutor(8) as ex: D = list(ex.map(detail, pre))
    out = []
    for g, hh, mk in D:
        R = rates(g, LX, hh); opts = []
        for k in keys:
            mid, spec, oid, lab = MK[k]; od = mk.get((mid, spec, oid))
            if not od or R.get(k, 0) < floor or od < 1.06: continue      # 1.01-1.05 prices add nothing to a ticket
            opts.append((R[k] - od * .015, R[k], k, od))
        if not opts: continue
        sc, r, k, od = max(opts); mid, spec, oid, lab = MK[k]
        out.append(dict(eid=g["eid"], mid=mid, spec=spec, oid=oid, pick=lab.format(h=g["h"], a=g["a"]), odds=od, ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"], s=sc,
                        why=f"stats rate {r:.0%} from both teams' last 10" + (f" and {len(hh)} meetings" if len(hh) >= 3 and mid in ("1", "10", "18", "29", "860", "450001") else "")))
    return sorted(out, key=lambda r: -r["s"])

def finish(T, kind, names, note, req):
    """book every ticket, reload and check it, report"""
    res = []
    for t in T:
        t.sort(key=lambda p: p["ts"])
        code = post([dict(eventId=p["eid"], marketId=p["mid"], specifier=p["spec"], outcomeId=p["oid"]) for p in t])
        if not code: note.append("SportyBet refused one booking."); continue
        time.sleep(2); o = load(code)
        okc = len(o) == len(t) and len({e["eventId"] for e in o}) == len(o) and all(e["estimateStartTime"]/1000 > time.time() for e in o)
        odds = 1.0
        for e in o: odds *= float(e["markets"][0]["outcomes"][0]["odds"])
        if not okc: note.append(f"{code} failed the reload check and was not used."); continue
        res.append(dict(code=code, odds=round(odds, 2), legs=[dict(t=hm(p["ts"]), game=p["game"], lg=p["lg"], pick=p["pick"], odds=p["odds"], why=p["why"],
                                                                        ts=p["ts"], eid=p["eid"], mid=p["mid"], spec=p["spec"], oid=p["oid"],
                                                                        k=f"{p['eid'].split(':')[-1]}|{p['mid']}|{p['spec']}|{p['oid']}") for p in t]))
    if kind == "u25": note.append("Under 2.5 has been the weakest market in your tracker.")
    return dict(ok=bool(res), kind=names[kind], req=req, tickets=res, note=note, first=min((p["ts"] for t in T for p in t), default=None))

# ---------- main ----------
def main(req):
    kind = req.get("type", "fav"); legs = max(1, min(int(req.get("legs", 10)), 50)); nt = max(1, min(int(req.get("tickets", 1)), 10))
    now = time.time(); today = dt.datetime.now(LAGOS).date()
    until = req.get("until", "today")
    WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    if until in WD: days = (WD.index(until) - today.weekday()) % 7          # up to the end of the next such day (today's name = tonight)
    else: days = {"today": 0, "tomorrow": 1}.get(until, 0)
    hi = dt.datetime.combine(today + dt.timedelta(days=days), dt.time(23, 59), LAGOS).timestamp()
    lo = now + 20 * 60 if until != "tomorrow" or req.get("from_now", True) else now
    pick_days = sorted({d for d in (req.get("days") or []) if re.fullmatch(r"\d{4}-\d\d-\d\d", str(d))})
    if pick_days:   # ticked days on build.html: only games on those dates (Lagos time), in any combination
        hi = dt.datetime.combine(dt.date.fromisoformat(pick_days[-1]), dt.time(23, 59), LAGOS).timestamp()
        G = [g for g in games_cached(lo, hi) if dt.datetime.fromtimestamp(g["ts"], LAGOS).date().isoformat() in pick_days]
        span = ", ".join(dt.date.fromisoformat(d).strftime("%a %d") for d in pick_days)
    else:
        G = games_cached(lo, hi); span = "up to " + hm(hi)
    LX = fetch_teams(G)
    P = pick_fav(G, LX, kind == "smallfav") if kind in ("fav", "smallfav") else pick_fav1h(G, LX) if kind == "fav1h" else pick_fav1h(G, LX, 2) if kind == "fav2h" else pick_h1o05(G, LX) if kind == "h1o05" else pick_h1o05(G, LX, 2) if kind == "h2o05" else pick_yw_strong(G, LX) if kind == "o25yw" else pick_multi(G, LX, kind, legs * nt) if kind in SETS else pick_goals(G, LX, kind)
    seen = set(); P = [p for p in P if not (p["eid"] in seen or seen.add(p["eid"]))]   # one leg per game
    names = {"gg": "GG (both teams score)", "best": "your tracker's best markets", "mix": "safest market per game", "h2o05": "2nd half Over 0.5", "fav2h": "strong team to score in the 2nd half", "h1o05": "1st half Over 0.5", "fav1h": "strong team to score in the 1st half", "fav": "strong favourites to win", "smallfav": "smallest-odds favourites to win", "o15": "Over 1.5", "o25": "Over 2.5 (high-scoring leagues)",
             "o25yw": "Over 2.5 (strong teams in youth & women's leagues)", "u25": "Under 2.5 (low-scoring leagues)"}
    note = []
    target = float(req.get("target") or 0)
    if target > 1:   # fill each ticket with the strongest legs until it reaches the target odds (legs = the most allowed)
        T, i = [], 0
        for _ in range(nt):
            t, prod = [], 1.0
            while i < len(P) and prod < target and len(t) < legs: t.append(P[i]); prod *= P[i]["odds"]; i += 1
            if t and prod >= target * .9: T.append(t)
        if not T:
            return dict(ok=False, msg=f"Could not reach {target:g} odds with {legs} legs or fewer from the {len(P)} games that pass the checks for {names[kind]} ({span}). Allow more legs or a lower target.")
        if len(T) < nt: note.append(f"Only enough strong games for {len(T)} ticket(s) at about {target:g} odds with no game repeated (you asked for {nt}).")
        return finish(T, kind, names, note, req)
    can = len(P) // legs
    if can == 0:
        return dict(ok=False, msg=f"Only {len(P)} games pass the checks for {names[kind]} ({span}). Not enough for {legs} legs. Ask for {len(P)} legs or fewer.")
    if can < nt: note.append(f"Only {len(P)} games qualified, enough for {can} ticket(s) of {legs} with no game repeated (you asked for {nt}).")
    nt = min(nt, can); use = P[:nt*legs]
    T = [[] for _ in range(nt)]
    for i, p in enumerate(use):                 # snake split so every ticket gets a fair share of the strongest legs
        r, k = divmod(i, nt); T[k if r % 2 == 0 else nt-1-k].append(p)
    return finish(T, kind, names, note, req)

if __name__ == "__main__":
    req = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {"type": "fav", "legs": 10}
    out = main(req); out["built"] = time.time()
    print(json.dumps(out, indent=1))
    json.dump(out, open(os.path.join(HERE, "last.json"), "w"))
