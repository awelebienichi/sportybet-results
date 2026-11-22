#!/usr/bin/env python3
"""Phone ticket builder: run by .github/workflows/build.yml for each request sent from build.html (via ntfy).
usage: phonebuild.py '{"type":"fav","legs":40,"tickets":1,"until":"sun"}'
types: fav (strong favourites to win), smallfav (smallest-odds strong favourites), o15, o25 (high-scoring leagues),
       o25yw (youth & women's leagues), u25 (low-scoring leagues)
Rules are the user's standing rules: favourites must pass form + h2h checks AND be clear bookmaker favourites
(opponent's odds 1.5x+), goal picks need both teams' last-10 + h2h to agree, no game repeated across tickets,
every code reloaded and checked before it is reported, then added to the tracker."""
import json, math, os, re, subprocess, sys, time, collections, threading, datetime as dt
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
# games already on this person's earlier tickets that haven't kicked off (set by poll.py for each pre-match build; user, 17 Nov:
# "some games appearing 3 to 4 times on different tickets") – every build ranks strongest first, so without this the same top games
# came back on every new ticket
AVOID = {"eids": set(), "hit": set()}

def games_cached(lo, hi, fresh=15 * 60):
    """reuse the game list fetched in the last 15 minutes (the listener keeps a 7-day list warm); prices are re-checked when the code is reloaded"""
    if time.time() - _GAMES["at"] > fresh or _GAMES["hi"] < hi:
        far = max(hi, time.time() + 7 * 86400)
        _GAMES.update(at=time.time(), hi=far, list=games(time.time() + 5 * 60, far))
    out = [g for g in _GAMES["list"] if lo <= g["ts"] <= hi]
    if AVOID["eids"]:
        AVOID["hit"] |= {g["eid"] for g in out if g["eid"] in AVOID["eids"]}
        out = [g for g in out if g["eid"] not in AVOID["eids"]]
    return out

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

ST = {"L": 0.1}     # strictness: strict 0, normal 0.1 (default), relaxed 0.2; lowers the stats bars by about that much
YW = re.compile(r"U1[5-9]|U2[0-3]|Youth|Junior|Juniori|Primavera|Reserve|Women|Femin|Frauen|Damallsvenskan|Elitettan|Toppserien|Next Gen|Development", re.I)

# ---------- pickers: each returns a ranked list of legs {eid, mid, spec, oid, pick, odds, why, ts, game, lg} ----------
def pick_fav(G, LX, smallest=False):
    C = []
    for g in G:
        for side, oid, other in (("home", "1", "3"), ("away", "3", "1")):
            od = g["x"].get(oid); oo = g["x"].get(other, 99)
            if not od or od > 2.6 or oo < (1.5 - ST["L"] * 1.5) * od: continue     # clear bookmaker favourite only
            me, op = (g["hid"], g["aid"]) if side == "home" else (g["aid"], g["hid"])
            F, O = LX.get(me, []), LX.get(op, [])
            if len(F) < 6 or len(O) < 6: continue
            fw = sum(m["gf"] > m["ga"] for m in F) / len(F); fl = sum(m["gf"] < m["ga"] for m in F) / len(F)
            ol = sum(m["gf"] < m["ga"] for m in O) / len(O); gd = sum(m["gf"] - m["ga"] for m in F) / len(F)
            V = [m for m in F if m["home"] == (side == "home")]; vw = sum(m["gf"] > m["ga"] for m in V) / len(V) if len(V) >= 3 else fw
            L = ST["L"]
            if fw < .6 - L or fl > .2 + L or vw < .5 - L or ol < .3 - L or gd < .6 - 3 * L: continue
            C.append(dict(g=g, side=side, oid=oid, od=od, fw=fw, fl=fl, ol=ol, vw=vw, gd=gd))
    with ThreadPoolExecutor(8) as ex: H = list(ex.map(lambda c: h2h(c["g"]["hid"], c["g"]["aid"]), C))
    out = []
    for c, hh in zip(C, H):
        if c["side"] == "away": hh = [(b, a) for a, b in hh]
        hw = sum(a > b for a, b in hh) / len(hh) if hh else None; hl = sum(a < b for a, b in hh) / len(hh) if hh else None
        if len(hh) >= 3 and (hl > .25 + ST["L"] or hw < .5 - ST["L"]): continue
        s = c["fw"]*.35 + c["vw"]*.2 + c["ol"]*.15 + (1-c["fl"])*.1 + min(c["gd"], 3)/3*.1 + (hw if len(hh) >= 3 else c["fw"])*.1
        g = c["g"]; team = g["h"] if c["side"] == "home" else g["a"]
        out.append(dict(eid=g["eid"], mid="1", spec="", oid=c["oid"], pick=f"{team} to win", odds=c["od"], ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"],
                        rate=(c["fw"] + c["ol"] + c["vw"]) / 3, s=-c["od"] if smallest else s, why=f"won {c['fw']:.0%} lost {c['fl']:.0%} of last 10, rival lost {c['ol']:.0%}" + (f", won {hw:.0%} of {len(hh)} meetings" if len(hh) >= 3 else "")))
    return sorted(out, key=lambda r: -r["s"])

def league_stats(G, LX):
    L = collections.defaultdict(dict)
    for g in G:
        for t in (g["hid"], g["aid"]):
            for m in LX.get(t, []): L[g["lg"]][m["id"]] = m["gf"] + m["ga"]
    return {k: (sum(v.values())/len(v), sum(x >= 3 for x in v.values())/len(v), sum(x <= 1 for x in v.values())/len(v)) for k, v in L.items() if len(v) >= 15}

O25_MAX = {0.0: 1.50, 0.1: 1.60, 0.2: 1.70}   # Over 2.5 price cap by strictness: tracked legs at 1.70+ won 35-39% vs 46-56% implied
U35_FAV = 1.60   # no Under 3.5 when either side is priced under this to win: Under 3.5 came true 42% (favourite <1.30) / 58-61% (1.30-1.60)
                 # vs 67-70% (1.60+) on 612 priced favourites – blowouts (Boca 5-0, Canuelas 4-0, PAOK 4-0) were most of the losses
U35_MAX, U35_EXP = 1.25, 2.8   # Under 3.5 (17 Nov, 4,149 replayed games): came true 76% when expected goals < 2.4, 64% at 2.8-3.2,
                                # 53% at 3.6+; tracked legs priced 1.25-1.35 won 65% vs 77% implied
def exp_goals(H, A):
    """expected match goals: each team's scoring against the other's conceding, over their last 10"""
    gf = lambda L: sum(m["gf"] for m in L) / len(L); ga = lambda L: sum(m["ga"] for m in L) / len(L)
    return (gf(H) + ga(A)) / 2 + (gf(A) + ga(H)) / 2
O15_MAX = 1.25        # Over 1.5 above this price won 70% of 87 tracked legs when the odds said 76%
def o15_top(H, A):
    """the strongest Over 1.5 games: both teams had 2+ goals in 90%+ of their last 10, or 80%+ and in every one of their last 5.
    Replayed on 4,149 finished games: 87-89% came true (the old rule alone: 82-85%); on 107 tracked legs 90% won vs 84% implied"""
    o = lambda L: sum(m["gf"] + m["ga"] >= 2 for m in L) / len(L)
    lo = min(o(H), o(A))
    return lo >= .9 or (lo >= .8 and min(o(H[-5:]), o(A[-5:])) >= 1)

def pick_goals(G, LX, kind):
    LS = league_stats(G, LX)
    if kind == "o25": ok_lg = lambda k: LS[k][0] >= 3.1 - 3 * ST["L"] and LS[k][1] >= .6 - ST["L"]
    elif kind == "o25yw": ok_lg = lambda k: YW.search(k) and LS[k][0] >= 2.9
    elif kind == "u25": ok_lg = lambda k: LS[k][0] <= 2.3 and LS[k][2] >= .3
    else: ok_lg = lambda k: LS[k][0] >= 2.6                                 # o15
    spec, oid, line, over = {"o25": ("total=2.5", "12", 3, True), "o25yw": ("total=2.5", "12", 3, True), "u25": ("total=2.5", "13", 3, False), "o15": ("total=1.5", "12", 2, True)}[kind]
    need, hneed = {"o25": (.6, .5), "o25yw": (.6, .5), "u25": (.6, .5), "o15": (.8, .7)}[kind]
    need, hneed = need - ST["L"], hneed - ST["L"]
    C = []
    for g in G:
        if (kind != "o25" and (g["lg"] not in LS or not ok_lg(g["lg"]))) or (spec, oid) not in g["ou"]: continue
        H, A = LX.get(g["hid"], []), LX.get(g["aid"], [])
        if len(H) < 6 or len(A) < 6: continue
        if kind == "o15" and g["ou"][(spec, oid)] > O15_MAX: continue
        if kind == "o25":
            # Over 2.5 is PRICE-LED (16 Nov test, 1,002 priced candidates + 581 tracked legs): teams on hot Over 2.5 runs did WORSE
            # than their odds (both 70%+ of last 10: won 60% vs 67% implied; 80%+: 51% vs 68%) – the bookmaker already prices the
            # run in. The price itself was honest (1.50 or less: 70-73% won), so legs are taken lowest price first, under a cap.
            if g["ou"][(spec, oid)] > O25_MAX.get(round(ST["L"], 1), 1.60): continue
            C.append((g, sum(m["gf"] + m["ga"] >= 3 for m in H) / len(H), sum(m["gf"] + m["ga"] >= 3 for m in A) / len(A), lambda n: n >= 3)); continue
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
        if kind == "o25":
            if hr is not None and len(hh) >= 5 and hr < .4: continue              # meetings mostly under 2.5: 47% won vs 53% implied
            od = g["ou"][(spec, oid)]; imp = 1 / (od * 1.06)
            out.append(dict(eid=g["eid"], mid="18", spec=spec, oid=oid, pick="Over 2.5", odds=od, ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"], s=imp, rate=imp,
                            why=f"Over 2.5 at {od:.2f}: SportyBet's own price gives {imp:.0%}" + (f", {hr:.0%} of {len(hh)} meetings" if hr is not None else "") +
                                f"; teams' last 10 {hu:.0%} / {au:.0%}"))
            continue
        if hr is not None and hr < hneed: continue
        s = min(hu, au)*.4 + (hu+au)/2*.3 + (hr if hr is not None else (hu+au)/2)*.2 + (min(LS[g["lg"]][0], 4.5)/4.5 if over else (3 - min(LS[g["lg"]][0], 3))/3)*.1
        top = kind == "o15" and o15_top(LX[g["hid"]], LX[g["aid"]])
        if top: s += 1                                                          # top tier first: used up before any other Over 1.5
        word = {"o25": "Over 2.5", "o25yw": "Over 2.5", "u25": "Under 2.5", "o15": "Over 1.5"}[kind]
        rate = (hu + au) / 2 * .7 + (hr if hr is not None else (hu + au) / 2) * .3
        out.append(dict(eid=g["eid"], mid="18", spec=spec, oid=oid, pick=word, odds=g["ou"][(spec, oid)], ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"], s=s, rate=rate, top=top,
                        why=("TOP TIER: " if top else "") + f"{word} in {hu:.0%} / {au:.0%} of the teams' last 10" + (f", {hr:.0%} of {len(hh)} meetings" if hr is not None else "") + f", league avg {LS[g['lg']][0]:.1f} goals"))
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
            if not od or od > 2.2 or oo < (1.5 - ST["L"] * 1.5) * od: continue
            me, op = (g["hid"], g["aid"]) if side == "home" else (g["aid"], g["hid"])
            kf, ka = f"h{half}f", f"h{half}a"
            F = [m for m in LX.get(me, []) if m[kf] is not None]; O = [m for m in LX.get(op, []) if m[ka] is not None]
            if len(F) < 6 or len(O) < 6: continue
            fs = sum(m[kf] > 0 for m in F) / len(F); oc = sum(m[ka] > 0 for m in O) / len(O)
            if fs < .8 - ST["L"] or oc < .6 - ST["L"]: continue
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

H1O05_MAX = 1.25      # 1st half Over 0.5: tracked legs at 1.20-1.30 won 75% vs 80% implied, 1.30+ only 58-65% vs 68-75%
def pick_h1o05(G, LX, half=1):
    """1st half Over 0.5 (market 68): a goal before half-time in 85%+ of both teams' recent games"""
    C = []
    for g in G:
        kf, ka = f"h{half}f", f"h{half}a"
        H = [m for m in LX.get(g["hid"], []) if m[kf] is not None]; A = [m for m in LX.get(g["aid"], []) if m[kf] is not None]
        if len(H) < 6 or len(A) < 6: continue
        hr = sum(m[kf] + m[ka] > 0 for m in H) / len(H); ar = sum(m[kf] + m[ka] > 0 for m in A) / len(A)
        if min(hr, ar) < .85 - ST["L"]: continue
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
        if not odds or (half == 1 and odds > H1O05_MAX): continue
        # top tier (16 Nov, 2,421 replayed games): a 1st-half goal in 90%+ of both teams' last 10, or 80%+ and in all of their last 5:
        # 86% came true (all games 73%); used up before any other 1st-half Over 0.5
        kf, ka = f"h{half}f", f"h{half}a"
        H = [m for m in LX[g["hid"]] if m[kf] is not None]; A = [m for m in LX[g["aid"]] if m[kf] is not None]
        l5 = min(sum(m[kf] + m[ka] > 0 for m in H[-5:]) / len(H[-5:]), sum(m[kf] + m[ka] > 0 for m in A[-5:]) / len(A[-5:]))
        top = half == 1 and (min(hr, ar) >= .9 or (min(hr, ar) >= .8 and l5 >= 1))
        # 15 Nov review of 334 settled legs: 1st-half Over 0.5 came true 65% when the odds said 76% – the stats alone were too
        # optimistic, so the stats (the weaker side's rate) and the bookmaker's price must agree
        pb = (min(hr, ar) + 1 / (odds * 1.06)) / 2
        if pb < .80 - ST["L"] / 2: continue
        out.append(dict(eid=g["eid"], mid={1: "68", 2: "90"}[half], spec="total=0.5", oid="12", pick=f"{'1st' if half == 1 else '2nd'} half Over 0.5", odds=odds, ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"],
                        s=min(hr, ar) * .6 + (hr + ar) / 2 * .4 - odds * .02 + top, rate=min(hr, ar) * .6 + (hr + ar) / 2 * .4, top=top,
                        why=("TOP TIER: " if top else "") + f"goal in the {'1st' if half == 1 else '2nd'} half in {hr:.0%} of {g['h']}'s last {nh} and {ar:.0%} of {g['a']}'s last {na}"))
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
            L = ST["L"]
            if fo < .7 - L or fg < 2.0 - 3 * L or oc < 1.2 - 2 * L or fw < .6 - L: continue
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
        "mix": ["win_h", "win_a", "up_h", "up_a", "dc_x2", "h1dc_1x", "h1dc_x2", "o15", "h1o05", "h2o05", "gg", "hgg",
                "u35", "h1u25", "m5x", "m10x"]}      # every game gets whichever of these suits it best

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

# ---------- picking the RIGHT market for a game: value over the bookmaker, and nothing that contradicts the game ----------
FEW = {"m5x", "m10x", "u35", "u45", "u55", "xg_5", "h1u25", "wu25_h", "wu25_a", "xg1_3", "h1gg_n", "xgh_3", "xga_3"}           # "few goals" picks
MANY = {"o25", "gg", "h1gg_y", "gb25", "o15"}                                                                     # "goals" picks
def edge(rate, odds):
    """how far the stats beat the price: stats rate minus the bookmaker's de-margined chance"""
    return rate - 1 / (odds * 1.06)
MAIN = {"h1u25", "o15", "o25", "gg", "win_h", "win_a", "up_h", "up_a", "dc_1x", "dc_x2", "dc_12", "h1o05", "h2o05", "u35", "hgg", "h1dc_1x", "h1dc_x2"}
def value(k, rate, odds):
    """what the picker maximises: value over the price, with a nudge towards the main markets over exotic ones"""
    return edge(rate, odds) + (.04 if k in MAIN else 0)
def profile(g, LX):
    """'high' if both teams' games went over 2.5 in 70%+ of their last 10 (or a heavy favourite could run up the score),
    'low' if both stayed under 2.5 in 70%+"""
    if min(g["x"].get("1", 99), g["x"].get("3", 99)) <= 1.35: return "high"     # blowout risk: no few-goals picks
    H, A = LX.get(g["hid"], []), LX.get(g["aid"], [])
    if len(H) < 6 or len(A) < 6: return ""
    o = lambda L: sum(m["gf"] + m["ga"] >= 3 for m in L) / len(L)
    if o(H) >= .7 and o(A) >= .7: return "high"
    if o(H) <= .3 and o(A) <= .3: return "low"
    return ""
def side_ok(k, g):
    """never lean on the weaker side: 'Home or GG' only when the home team is the favourite or close to it"""
    xh, xa = g["x"].get("1", 99), g["x"].get("3", 99)
    if k in ("ah_h_m15", "eh_h_01") and xh >= xa: return False                  # win by 2+: the favourite only
    if k in ("ah_a_m15", "eh_a_10") and xa >= xh: return False
    return not (k == "hgg" and xh > xa * 1.2)
SAFE = {0.0: (1.60, .02), 0.1: (1.75, -.03), 0.2: (2.00, -.08)}   # strictness -> (max odds, shift of the market's bar)
# what "strong" means differs by market: a side winning 80% of games is rare, Over 1.5 at 80% is ordinary.
# The pair (both teams' last 10 + meetings) must reach BAR + shift; each team on its own BAR + shift - 0.07.
BAR = {"o15": .88, "o25": .78, "gg": .75, "win_h": .70, "win_a": .70, "up_h": .72, "up_a": .72, "dc_1x": .88, "dc_x2": .88, "dc_12": .85,
       "hgg": .85, "gb25": .85, "h1o05": .85, "h2o05": .88, "htx": .55, "h1dc_1x": .85, "h1dc_x2": .85, "wu25_h": .85, "wu25_a": .85,
       "h1u25": .90, "u35": .85, "u45": .92, "u55": .96, "h1gg_y": .50, "h1gg_n": .85,
       "m5x": .88, "m10x": .78, "ah_h_m15": .62, "ah_a_m15": .62, "eh_h_01": .62, "eh_a_10": .62, "ah_h_p15": .88, "ah_a_p15": .88, "eh_h_20": .88, "eh_a_02": .88, "ah_h_p25": .94, "ah_a_p25": .94}
LONG = {"htx": .9, "h1gg_y": 1.2, "ah_h_m15": .3, "ah_a_m15": .3, "eh_h_01": .3, "eh_a_10": .3}           # markets that are never short prices: their price cap is raised by this much
GAP = {"win_h": .15, "win_a": .15, "up_h": .15, "up_a": .15, "ah_h_m15": .15, "ah_a_m15": .15, "eh_h_01": .15, "eh_a_10": .15}   # result markets: one side wins most games, the other loses a fair share – a smaller share is enough
def bar(k): return BAR.get(k, .85) + SAFE.get(round(ST["L"], 1), SAFE[0.1])[1]
def safe_enough(k, g, LX, rate, odds):
    """a leg only goes on a ticket if BOTH teams are strong for this market on their own, the pair is strong too, and the price
    is not a coin flip. Strict: 1.60 max and the market's bar +2 points; Normal: 1.75, -3; Relaxed: 2.00, -8.
    Value only decides between legs that already pass this."""
    mx = SAFE.get(round(ST["L"], 1), SAFE[0.1])[0] + LONG.get(k, 0)
    if k == "o15": mx = min(mx, O15_MAX)
    if k == "h1o05": mx = min(mx, H1O05_MAX)
    if k == "u35":
        mx = min(mx, U35_MAX)
        H_, A_ = LX.get(g["hid"], []), LX.get(g["aid"], [])
        if len(H_) < 6 or len(A_) < 6 or exp_goals(H_, A_) >= U35_EXP: return False   # no Under 3.5 between teams that score a lot
        if min(g["x"].get("1", 99), g["x"].get("3", 99)) < U35_FAV: return False       # nor with a strong favourite (blowouts)
    if k == "o25": mx = min(mx, O25_MAX.get(round(ST["L"], 1), 1.60))
    if odds > mx or rate < bar(k): return False
    try:
        import rollover
        mid, spec, oid, _ = RB.get(k, MK.get(k, (None, None, None, None)))
        tr = rollover.team_rates(dict(mid=mid, spec=spec, oid=oid), g, LX) if mid else None
    except Exception: tr = None
    if tr and None not in tr and min(tr) < bar(k) - GAP.get(k, .07): return False
    return True
def fits(k, prof):
    if prof == "high" and k in FEW: return False
    if prof == "low" and k in ("o25", "gg", "h1gg_y"): return False
    return True

def pick_multi(G, LX, kind, want):
    keys = SETS[kind]; floor = {"gg": .68, "best": .8, "mix": .82}[kind] - ST["L"]
    pre = []
    for g in G:
        R = rebuild_rates(g, LX, [])
        best = max((R.get(k, 0) - bar(k) for k in keys), default=-1)
        if best >= -.1: pre.append((best, g))
    pre.sort(key=lambda x: -x[0]); pre = [g for _, g in pre[:max(200, want * 6)]]
    def detail(g):
        hh = h2h(g["hid"], g["aid"])
        mk = {(m["id"], m.get("specifier") or "", o["id"]): float(o["odds"]) for m in event_markets(g["eid"]) for o in m.get("outcomes", []) if o.get("isActive")}
        return g, hh, mk
    with ThreadPoolExecutor(8) as ex: D = list(ex.map(detail, pre))
    out = []
    for g, hh, mk in D:
        R = rebuild_rates(g, LX, hh); opts = []; prof = profile(g, LX)
        for k in keys:
            mid, spec, oid, lab = RB[k]; od = mk.get((mid, spec, oid))
            if not od or od < 1.06 or not fits(k, prof) or not side_ok(k, g) or not safe_enough(k, g, LX, R.get(k, 0), od): continue
            pb = (R[k] + 1 / (od * 1.06)) / 2                                     # stats and the bookmaker's price must agree (15 Nov review)
            if pb < bar(k) - .06: continue
            opts.append((value(k, R[k], od), R[k], k, od))
        if not opts: continue
        sc, r, k, od = max(opts); mid, spec, oid, lab = RB[k]
        out.append(dict(eid=g["eid"], mid=mid, spec=spec, oid=oid, pick=lab.format(h=g["h"], a=g["a"]), odds=od, ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"], s=sc, rate=r,
                        why=f"stats rate {r:.0%} from both teams' last 10" + (f" and {len(hh)} meetings" if len(hh) >= 3 and mid in ("1", "10", "18", "29", "860", "450001") else "")))
    return sorted(out, key=lambda r: -r["s"])

def h2h_ht(h, a):
    """half-time scores of the teams' meetings (from today's home side), where the feed has them"""
    d = get(f"https://stats.fn.sportradar.com/sportybet/en/Europe:Berlin/gismo/stats_team_versusrecent/{h}/{a}", "https://www.sportybet.com/")
    out = []
    try:
        for m in d["doc"][0]["data"]["matches"][:10]:
            p = (m.get("periods") or {}).get("p1")
            if not p: continue
            hh = m["teams"]["home"]["uid"] == int(h); out.append((p["home"], p["away"]) if hh else (p["away"], p["home"]))
    except Exception: pass
    return out

def pick_draw_weak(G, LX, half=False):
    """full-time (half=False) or half-time (half=True) draw between two WEAK teams. Measured on 4,088 finished games (15 Nov):
    weak teams alone draw no more than anyone (25%); both weak (won 35% or less of last 10) + both drawing 30%+ + low scoring
    drew 37% (n=49, a small sample); at half-time, weak teams that are often level at the break were level 41%. A draw leg is a
    1-in-3 (full-time) or 2-in-5 (half-time) bet – best for short tickets at bigger odds."""
    C = []
    for g in G:
        H, A = LX.get(g["hid"], []), LX.get(g["aid"], [])
        if len(H) < 6 or len(A) < 6: continue
        w = lambda L: sum(m["gf"] > m["ga"] for m in L) / len(L)
        if max(w(H), w(A)) > .35 + ST["L"] / 2: continue                       # both weak
        xh, xa = g["x"].get("1", 99), g["x"].get("3", 99)
        if max(xh, xa) / max(1.01, min(xh, xa)) > 1.8: continue                  # evenly matched: no clear favourite
        if not half:
            d = lambda L: sum(m["gf"] == m["ga"] for m in L) / len(L)
            gpg = (sum(m["gf"] + m["ga"] for m in H) / len(H) + sum(m["gf"] + m["ga"] for m in A) / len(A)) / 2
            if min(d(H), d(A)) < .3 - ST["L"] / 2 or gpg > 2.5 + ST["L"] * 2: continue
            od = g["x"].get("2")
            if not od or not 2.6 <= od <= 4.2: continue
            rate = .37 if min(d(H), d(A)) >= .3 and gpg <= 2.4 else .32
            C.append(dict(eid=g["eid"], mid="1", spec="", oid="2", pick="Draw", odds=od, ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"], rate=rate,
                          s=(d(H) + d(A)) / 2 - gpg / 10, why=f"two weak sides (won {w(H):.0%} / {w(A):.0%} of last 10), draw {d(H):.0%} / {d(A):.0%}, {gpg:.1f} goals a game"))
        else:
            H1 = [m for m in H if m["h1f"] is not None]; A1 = [m for m in A if m["h1f"] is not None]
            if len(H1) < 6 or len(A1) < 6: continue
            lv = lambda L: sum(m["h1f"] == m["h1a"] for m in L) / len(L)
            if min(lv(H1), lv(A1)) < .5 - ST["L"] / 2: continue
            C.append(dict(g=g, why=f"two weak sides (won {w(H):.0%} / {w(A):.0%} of last 10), level at half-time {lv(H1):.0%} / {lv(A1):.0%}", s=(lv(H1) + lv(A1)) / 2))
    if not half: return sorted(C, key=lambda r: -r["s"])
    C.sort(key=lambda c: -c["s"]); C = C[:60]
    def price(c):
        for m in event_markets(c["g"]["eid"]):
            if m["id"] == "60":
                for o in m["outcomes"]:
                    if o["id"] == "2" and o.get("isActive"): return float(o["odds"])
    with ThreadPoolExecutor(8) as ex: PR = list(ex.map(price, C))
    out = []
    for c, od in zip(C, PR):
        if not od or od < 1.8: continue
        g = c["g"]
        out.append(dict(eid=g["eid"], mid="60", spec="", oid="2", pick="Half-time draw", odds=od, ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"], rate=.41, s=c["s"], why=c["why"]))
    return sorted(out, key=lambda r: -r["s"])

def pick_htdraw(G, LX):
    """1st half 1X2 Draw (market 60, outcome 2): both teams level at half-time in 60%+ of recent games (the usual rate is about 40%), in a league where
    half-time draws are common, no big favourite (they tend to score early), and meetings agree where known"""
    L = collections.defaultdict(dict)
    for g in G:
        for t in (g["hid"], g["aid"]):
            for m in LX.get(t, []):
                if m["h1f"] is not None: L[g["lg"]][m["id"]] = m["h1f"] == m["h1a"]
    LR = {k: sum(v.values()) / len(v) for k, v in L.items() if len(v) >= 15}
    C = []
    for g in G:
        if min(g["x"].get("1", 9), g["x"].get("3", 9)) < 1.6: continue
        H = [m for m in LX.get(g["hid"], []) if m["h1f"] is not None]; A = [m for m in LX.get(g["aid"], []) if m["h1f"] is not None]
        if len(H) < 6 or len(A) < 6: continue
        hr = sum(m["h1f"] == m["h1a"] for m in H) / len(H); ar = sum(m["h1f"] == m["h1a"] for m in A) / len(A)
        lr = LR.get(g["lg"])
        if min(hr, ar) < .6 - ST["L"] / 2 or (lr is not None and lr < .42 - ST["L"] / 2): continue
        C.append(dict(g=g, hr=hr, ar=ar, lr=lr, nh=len(H), na=len(A), h0=sum(m["h1f"] + m["h1a"] == 0 for m in H + A) / len(H + A)))
    C.sort(key=lambda c: -(min(c["hr"], c["ar"]) * .6 + (c["lr"] or .45) * .4)); C = C[:70]
    def detail(c):
        g = c["g"]; hh = h2h_ht(g["hid"], g["aid"]); od = None
        for m in event_markets(g["eid"]):
            if m["id"] == "60":
                for o in m["outcomes"]:
                    if o["id"] == "2" and o.get("isActive"): od = float(o["odds"])
        return c, hh, od
    with ThreadPoolExecutor(8) as ex: D = list(ex.map(detail, C))
    out = []
    for c, hh, od in D:
        if not od: continue
        hd = sum(x == y for x, y in hh) / len(hh) if len(hh) >= 3 else None
        if hd is not None and hd < .4: continue      # meetings must not be against it (the usual half-time draw rate is ~40%)
        g = c["g"]; avg = (c["hr"] + c["ar"]) / 2
        out.append(dict(eid=g["eid"], mid="60", spec="", oid="2", pick="Half-time draw", odds=od, ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"],
                        s=min(c["hr"], c["ar"]) * .45 + avg * .25 + (c["lr"] or avg) * .15 + (hd if hd is not None else avg) * .15,
                        why=f"level at half-time in {c['hr']:.0%} of {g['h']}'s last {c['nh']} and {c['ar']:.0%} of {g['a']}'s last {c['na']} ({c['h0']:.0%} were 0-0)"
                            + (f"; league {c['lr']:.0%} half-time draws" if c["lr"] is not None else "") + (f"; {hd:.0%} of {len(hh)} meetings level at half-time" if hd is not None else "")))
    return sorted(out, key=lambda r: -r["s"])

# ---------- rebuild a booking code: same games, the markets the user ticks, aiming at their target odds ----------
RB = dict(MK, **{"o25": ("18", "total=2.5", "12", "Over 2.5"), "htx": ("60", "", "2", "Half-time draw"),
                 "dc_1x": ("10", "", "9", "Double chance {h} or draw"), "dc_12": ("10", "", "10", "Double chance {h} or {a}"),
                 "wu25_h": ("19", "total=2.5", "13", "{h} Under 2.5 goals"), "wu25_a": ("20", "total=2.5", "13", "{a} Under 2.5 goals"),
                 "h1u25": ("68", "total=2.5", "13", "1st half Under 2.5"), "u35": ("18", "total=3.5", "13", "Under 3.5"),
                 "u45": ("18", "total=4.5", "13", "Under 4.5"), "u55": ("18", "total=5.5", "13", "Under 5.5"),
                 "h1gg_y": ("75", "", "74", "1st half GG – Yes"), "h1gg_n": ("75", "", "76", "1st half GG – No")})
# Asian handicap removed 11 Nov (user's choice); handicap (14, "hcp=home:away" head start; 1711 home, 1713 away)
RB.update({"eh_h_01": ("14", "hcp=0:1", "1711", "Handicap 0:1 – {h}"), "eh_a_10": ("14", "hcp=1:0", "1713", "Handicap 1:0 – {a}"),
           "eh_h_20": ("14", "hcp=2:0", "1711", "Handicap 2:0 – {h}"), "eh_a_02": ("14", "hcp=0:2", "1713", "Handicap 0:2 – {a}")})
# draw in the opening minutes (SportyBet opens these mostly on match day, mainly bigger leagues): 0-0 or level after 5 / 10 minutes
RB.update({"m5x": ("900069", "minute=5", "2", "1–5 minutes draw"), "m10x": ("105", "from=1|to=10", "2", "1–10 minutes draw")})
# Excluded Number of Goals: the pick wins when the count is NOT that number (top outcome "5+"/"3+": when it is below it)
for _n, _lab in ((0, "0"), (1, "1"), (2, "2"), (3, "3"), (4, "4"), (5, "5+")): RB[f"xg_{_n}"] = ("450004", "", str(_n), f"Excluded goals: {_lab}")
for _n, _lab in ((0, "0"), (1, "1"), (2, "2"), (3, "3+")):
    RB[f"xgh_{_n}"] = ("450005", "", str(_n), "{h} excluded goals: " + _lab); RB[f"xga_{_n}"] = ("450006", "", str(_n), "{a} excluded goals: " + _lab)
for _n, _lab in ((0, "0"), (2, "2"), (3, "3+")): RB[f"xg1_{_n}"] = ("810002", "", str(_n), f"1st half excluded goals: {_lab}")   # "1" left out: a tracker loser
RB_GROUPS = {"o15": ["o15"], "o25": ["o25"], "gg": ["gg"], "win": ["win_h", "win_a"], "up": ["up_h", "up_a"],
             "dc": ["dc_1x", "dc_x2", "dc_12"], "hgg": ["hgg"], "h1": ["h1o05"], "h2": ["h2o05"], "htx": ["htx"],
             "h1dc": ["h1dc_1x", "h1dc_x2"], "gb25": ["gb25"], "wu25": ["wu25_h", "wu25_a"], "h1u25": ["h1u25"],
             "u35": ["u35"], "u45": ["u45"], "u55": ["u55"], "h1ggy": ["h1gg_y"], "h1ggn": ["h1gg_n"],
             "m5x": ["m5x"], "m10x": ["m10x"],
             "eh": ["eh_h_01", "eh_a_10", "eh_h_20", "eh_a_02"],
             "xg": [f"xg_{n}" for n in range(6)], "xgt": [f"xg{s}_{n}" for s in ("h", "a") for n in range(4)], "xg1": ["xg1_0", "xg1_2", "xg1_3"]}

_EV = {}
def event_game(eid):
    hit = _EV.get(eid)
    if hit and time.time() - hit[0] < 600: return hit[1]          # full market lists kept 10 minutes
    res = _event_game(eid)
    if res[0]: _EV[eid] = (time.time(), res)
    return res

def _event_game(eid):
    d = curl(["-H", "Accept: application/json", "-H", "Referer: https://www.sportybet.com/ng/", f"https://www.sportybet.com/api/ng/factsCenter/event?eventId={eid}&productId=3"], 180)
    e = (d or {}).get("data") or {}
    if not e: return None, {}
    g = dict(eid=eid, ts=e["estimateStartTime"] / 1000, h=e["homeTeamName"], a=e["awayTeamName"], lg=e["sport"]["category"]["name"] + " - " + e["sport"]["category"]["tournament"]["name"],
             hid=e["homeTeamId"].split(":")[-1], aid=e["awayTeamId"].split(":")[-1], x={}, ou={})
    mk = {}
    for m in e.get("markets", []):
        for o in m.get("outcomes", []):
            if not o.get("isActive"): continue
            mk[(m["id"], m.get("specifier") or "", o["id"])] = float(o["odds"])
            if m["id"] == "1": g["x"][o["id"]] = float(o["odds"])
    return g, mk

def rebuild_rates(g, LX, hh):
    """rates() plus the extra markets a rebuild may use; win/double chance for either side, not only clear favourites"""
    R = dict(rates(g, LX, hh))
    H, A = LX.get(g["hid"], []), LX.get(g["aid"], [])
    if len(H) < 6 or len(A) < 6: return R
    f = lambda L, c: sum(1 for m in L if c(m)) / len(L)
    def both(ch, ca, hc=None):
        r = (f(H, ch) + f(A, ca)) / 2
        if hc and len(hh) >= 3: r = r * .7 + sum(1 for x in hh if hc(*x)) / len(hh) * .3
        return r
    H1, A1 = [m for m in H if m["h1f"] is not None], [m for m in A if m["h1f"] is not None]
    R["o25"] = both(lambda m: m["gf"] + m["ga"] >= 3, lambda m: m["gf"] + m["ga"] >= 3, lambda a, b: a + b >= 3)
    R["htx"] = (f(H1, lambda m: m["h1f"] == m["h1a"]) + f(A1, lambda m: m["h1f"] == m["h1a"])) / 2 if len(H1) >= 6 and len(A1) >= 6 else 0
    R["dc_1x"] = both(lambda m: m["gf"] >= m["ga"], lambda m: m["gf"] <= m["ga"], lambda a, b: a >= b)
    R["dc_12"] = both(lambda m: m["gf"] != m["ga"], lambda m: m["gf"] != m["ga"], lambda a, b: a != b)
    R.setdefault("dc_x2", both(lambda m: m["gf"] <= m["ga"], lambda m: m["gf"] >= m["ga"], lambda a, b: b >= a))
    R.setdefault("win_h", both(lambda m: m["gf"] > m["ga"], lambda m: m["gf"] < m["ga"], lambda a, b: a > b))
    R.setdefault("win_a", both(lambda m: m["gf"] < m["ga"], lambda m: m["gf"] > m["ga"], lambda a, b: b > a))
    # home team's home games / away team's away games, blended in (15 Nov: home-strong leagues such as Nigeria's NPFL were skipped –
    # Katsina 4/10 overall but unbeaten 6/6 at home). Tested on 3,370 finished home games: venue form alone was worse (too few games),
    # the blend was the best of the three and let ~20% more games reach 80% at nearly the same hit rate (84% vs 85%)
    Hh, Aa = [m for m in H if m.get("home")], [m for m in A if not m.get("home")]
    if len(Hh) >= 3 and len(Aa) >= 3:
        k = min(1, (min(len(Hh), len(Aa)) - 2) / 4) / 2
        v = {"dc_1x": (f(Hh, lambda m: m["gf"] >= m["ga"]) + f(Aa, lambda m: m["gf"] <= m["ga"])) / 2,
             "win_h": (f(Hh, lambda m: m["gf"] > m["ga"]) + f(Aa, lambda m: m["gf"] < m["ga"])) / 2,
             "dc_x2": (f(Aa, lambda m: m["gf"] >= m["ga"]) + f(Hh, lambda m: m["gf"] <= m["ga"])) / 2,
             "win_a": (f(Aa, lambda m: m["gf"] > m["ga"]) + f(Hh, lambda m: m["gf"] < m["ga"])) / 2}
        for key, val in v.items():
            if key in R: R[key] = (1 - k) * R[key] + k * val
    for k, a, b in (("up_h", "win_h", "dc_1x"), ("up_a", "win_a", "dc_x2")): R.setdefault(k, (R[a] * 2 + R[b]) / 3)
    xh, xa = g["x"].get("1", 99), g["x"].get("3", 99)
    if xh > xa: R["wu25_h"] = both(lambda m: m["gf"] <= 2, lambda m: m["ga"] <= 2, lambda a, b: a <= 2)     # home is the weaker team
    if xa > xh: R["wu25_a"] = both(lambda m: m["ga"] <= 2, lambda m: m["gf"] <= 2, lambda a, b: b <= 2)     # away is the weaker team
    # handicaps by goal margin (home's margin d = home goals - away goals)
    for k, ch, ca, hc in (("ah_h_m15", lambda m: m["gf"] - m["ga"] >= 2, lambda m: m["ga"] - m["gf"] >= 2, lambda a, b: a - b >= 2),
                          ("ah_a_m15", lambda m: m["ga"] - m["gf"] >= 2, lambda m: m["gf"] - m["ga"] >= 2, lambda a, b: b - a >= 2),
                          ("ah_h_p15", lambda m: m["gf"] - m["ga"] >= -1, lambda m: m["ga"] - m["gf"] >= -1, lambda a, b: a - b >= -1),
                          ("ah_a_p15", lambda m: m["ga"] - m["gf"] >= -1, lambda m: m["gf"] - m["ga"] >= -1, lambda a, b: b - a >= -1),
                          ("ah_h_p25", lambda m: m["gf"] - m["ga"] >= -2, lambda m: m["ga"] - m["gf"] >= -2, lambda a, b: a - b >= -2),
                          ("ah_a_p25", lambda m: m["ga"] - m["gf"] >= -2, lambda m: m["gf"] - m["ga"] >= -2, lambda a, b: b - a >= -2)):
        R[k] = both(ch, ca, hc)
    R["eh_h_01"], R["eh_a_10"], R["eh_h_20"], R["eh_a_02"] = R["ah_h_m15"], R["ah_a_m15"], R["ah_h_p15"], R["ah_a_p15"]   # same goal margins, 3-way market
    if len(H1) >= 6 and len(A1) >= 6:                  # opening-minutes draw: both sides' 1st-half goal rate, spread evenly over the half
        h1 = (sum(m["h1f"] + m["h1a"] for m in H1) / len(H1) + sum(m["h1f"] + m["h1a"] for m in A1) / len(A1)) / 2
        for k, mins in (("m5x", 5), ("m10x", 10)):
            lam = h1 * mins / 45 / 2                   # each side's share
            R[k] = sum((math.exp(-lam) * lam ** i / math.factorial(i)) ** 2 for i in range(4))   # level: 0-0, 1-1, ...
    for k, n in (("u35", 3), ("u45", 4), ("u55", 5)):
        R[k] = both(lambda m, n=n: m["gf"] + m["ga"] <= n, lambda m, n=n: m["gf"] + m["ga"] <= n, lambda a, b, n=n: a + b <= n)
    for n in range(6):      # match total is not n (5 = "5+": fewer than 5)
        c = (lambda t, n=n: t != n) if n < 5 else (lambda t: t < 5)
        R[f"xg_{n}"] = both(lambda m, c=c: c(m["gf"] + m["ga"]), lambda m, c=c: c(m["gf"] + m["ga"]), lambda a, b, c=c: c(a + b))
    for n in range(4):      # a team's goals are not n (3 = "3+": fewer than 3) – home: its scoring and the away side's conceding
        c = (lambda t, n=n: t != n) if n < 3 else (lambda t: t < 3)
        R[f"xgh_{n}"] = both(lambda m, c=c: c(m["gf"]), lambda m, c=c: c(m["ga"]), lambda a, b, c=c: c(a))
        R[f"xga_{n}"] = both(lambda m, c=c: c(m["ga"]), lambda m, c=c: c(m["gf"]), lambda a, b, c=c: c(b))
    # the user's rules: never "strong team won't score 3" (favourite excluded 3+), never "weak team will score" (weak side excluded 0)
    if xh < xa: R.pop("xgh_3", None)
    if xa < xh: R.pop("xga_3", None)
    if xh >= 3.1: R.pop("xgh_0", None)
    if xa >= 3.1: R.pop("xga_0", None)
    if len(H1) >= 6 and len(A1) >= 6:
        R["h1u25"] = (f(H1, lambda m: m["h1f"] + m["h1a"] <= 2) + f(A1, lambda m: m["h1f"] + m["h1a"] <= 2)) / 2
        R["h1gg_y"] = (f(H1, lambda m: m["h1f"] > 0 and m["h1a"] > 0) + f(A1, lambda m: m["h1f"] > 0 and m["h1a"] > 0)) / 2
        R["h1gg_n"] = 1 - R["h1gg_y"]
        for n in (0, 2, 3):
            c = (lambda t, n=n: t != n) if n < 3 else (lambda t: t < 3)
            R[f"xg1_{n}"] = (f(H1, lambda m, c=c: c(m["h1f"] + m["h1a"])) + f(A1, lambda m, c=c: c(m["h1f"] + m["h1a"]))) / 2
    for k, cond in (("h1dc_1x", lambda m: m["h1f"] >= m["h1a"]), ("h1dc_x2", lambda m: m["h1f"] <= m["h1a"])):
        if not R.get(k) and len(H1) >= 6 and len(A1) >= 6:
            R[k] = (f(H1, cond) + f(A1, (lambda m: m["h1f"] <= m["h1a"]) if k == "h1dc_1x" else (lambda m: m["h1f"] >= m["h1a"]))) / 2
    return R

def rebuild(req):
    code = re.sub(r"[^A-Za-z0-9]", "", str(req.get("code", ""))).upper()
    target = float(req.get("target") or 0)
    groups = [x for x in (req.get("markets") or list(RB_GROUPS)) if x in RB_GROUPS] or list(RB_GROUPS)
    keys = [k for gname in groups for k in RB_GROUPS[gname]]
    old = load(code) if code else []
    if not old: return dict(ok=False, msg=f"Could not load code {code or '(empty)'} from SportyBet.")
    now = time.time()
    live = [e for e in old if e["estimateStartTime"] / 1000 > now + 120]
    gone = len(old) - len(live)
    with ThreadPoolExecutor(8) as ex: EG = list(ex.map(lambda e: event_game(e["eventId"]), live))
    G = [(g, mk) for g, mk in EG if g]
    LX = fetch_teams([g for g, _ in G])
    with ThreadPoolExecutor(8) as ex: HH = list(ex.map(lambda gm: h2h(gm[0]["hid"], gm[0]["aid"]), G))
    games = []
    for (g, mk), hh in zip(G, HH):
        R = rebuild_rates(g, LX, hh); opts = []
        xh, xa = g["x"].get("1", 99), g["x"].get("3", 99)
        against = set()                       # never back the weaker side (user's rule)
        if xh < xa: against |= {"win_a", "up_a"}
        if xa < xh: against |= {"win_h", "up_h"}
        if xh * 1.5 <= xa: against |= {"dc_x2", "h1dc_x2"}
        if xa * 1.5 <= xh: against |= {"dc_1x", "h1dc_1x"}
        for k in keys:
            mid, spec, oid, lab = RB[k]; od = mk.get((mid, spec, oid))
            if k in against or not od or od < 1.04 or R.get(k, 0) < .55 - ST["L"] / 2 or not fits(k, profile(g, LX)) or not side_ok(k, g): continue
            # the stats and the bookmaker's own price must agree: last-10 form can't see a gap in level (a cup game between a
            # lower-league side and a top club), the price can – and no coin-flip prices (same cap as the other builders)
            pb = (R[k] + 1 / (od * 1.06)) / 2
            if od > SAFE.get(round(ST["L"], 1), SAFE[0.1])[0] + LONG.get(k, 0) or pb < .62: continue
            # the goal-market rules of the main builder (16 Nov tests) apply here too: price caps, and each leg counted at the rate
            # its tier really came true at, so the safest-combination search prefers the top tier
            if k == "o15":
                if od > O15_MAX: continue
                pb = max(pb, .88) if o15_top(LX[g["hid"]], LX[g["aid"]]) else min(pb, .82)
            elif k == "h1o05":
                if od > H1O05_MAX: continue
                H1_, A1_ = [m for m in LX[g["hid"]] if m["h1f"] is not None], [m for m in LX[g["aid"]] if m["h1f"] is not None]
                hit = lambda L: sum(m["h1f"] + m["h1a"] > 0 for m in L) / len(L)
                top = len(H1_) >= 6 and len(A1_) >= 6 and (min(hit(H1_), hit(A1_)) >= .9 or (min(hit(H1_), hit(A1_)) >= .8 and min(hit(H1_[-5:]), hit(A1_[-5:])) >= 1))
                pb = max(pb, .86) if top else min(pb, .78)
            elif k == "u35":
                if od > U35_MAX or exp_goals(LX[g["hid"]], LX[g["aid"]]) >= U35_EXP or min(xh, xa) < U35_FAV: continue
            elif k == "o25":
                if od > O25_MAX.get(round(ST["L"], 1), 1.60): continue
                pb = 1 / (od * 1.06)                                              # price-led: hot Over 2.5 runs did worse than the odds
            opts.append(dict(k=k, od=od, r=pb, rs=R[k], mid=mid, spec=spec, oid=oid, lab=lab.format(h=g["h"], a=g["a"])))
        if not opts: continue
        opts.sort(key=lambda o: (-value(o["k"], o["r"], o["od"]) if o["r"] >= .7 else 1 - o["r"], o["od"]))   # strong options by value first
        games.append(dict(g=g, opts=opts, i=0))
    if not games: return dict(ok=False, msg=f"None of the games on {code} offer the markets you ticked (or they have all started).")
    import math
    prod = lambda: math.prod(x["opts"][x["i"]]["od"] for x in games)
    dropped = 0; usable = len(games)
    if target > 1:
        # land on the user's odds: one market per game chosen together so the total is as close to the target as the prices
        # allow (search over every combination in 0.1% steps), and among the combinations that close, the safest legs.
        # A game is only left out when even its safest market would push the total past the target.
        S = 1000; T = round(math.log(target) * S); cap = T + 25
        risk = lambda o: -math.log(min(max(o["r"], .3), .99))
        layers = []; states = {0: 0.0}
        for x in games:
            nxt = {}
            for b, rk in states.items():
                for j, o in enumerate(x["opts"]):
                    nb = b + round(math.log(o["od"]) * S)
                    if nb > cap: continue
                    nr = rk + risk(o)
                    if nb not in nxt or nr < nxt[nb][0]: nxt[nb] = (nr, b, j)
                nr = rk + 50                                                      # leave this game out: only as a last resort
                if b not in nxt or nr < nxt[b][0]: nxt[b] = (nr, b, -1)
            layers.append(nxt); states = {b: v[0] for b, v in nxt.items()}
        near = [b for b in states if abs(b - T) <= 5]                            # within 0.5% of the target
        end = min(near, key=lambda b: states[b]) if near else min(states, key=lambda b: (abs(b - T), states[b]))
        for x, L in zip(reversed(games), reversed(layers)):
            _, prev, j = L[end]; x["i"] = j; end = prev
        dropped = sum(1 for x in games if x["i"] < 0)
        games = [x for x in games if x["i"] >= 0]
    legs = []
    for x in sorted(games, key=lambda x: x["g"]["ts"]):
        o = x["opts"][x["i"]]; g = x["g"]
        legs.append(dict(eid=g["eid"], mid=o["mid"], spec=o["spec"], oid=o["oid"], pick=o["lab"], odds=o["od"], ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"],
                         why=f"stats rate {o['rs']:.0%} from both teams' last 10, {o['r']:.0%} with the bookmaker's price" + (f" (chosen to land on your odds)" if x["i"] > 0 else " (safest of your markets for this game)")))
    note = []
    if gone: note.append(f"{gone} game(s) on {code} had already started and were left out.")
    if usable < len(live): note.append(f"{len(live) - usable} game(s) had none of your markets with strong enough stats (55%+) and were left out.")
    p = math.prod(l["odds"] for l in legs)
    if dropped: note.append(f"{dropped} game(s) were left out: even their safest market would have taken the total past {target:g}.")
    if target > 1:
        if abs(p / target - 1) <= .01: note.append(f"Total odds {p:,.2f} – on your target of {target:g}.")
        elif p < target: note.append(f"Highest odds possible with your markets on these games: {p:,.2f} (you asked for {target:g}).")
        else: note.append(f"Closest the prices allow: {p:,.2f} (you asked for {target:g}).")
    return finish([legs], "rebuild", {"rebuild": f"rebuild of {code}"}, note, req)

# ---------- combine several booking codes into one ----------
def combine(req):
    codes = []
    for c in re.split(r"[^A-Za-z0-9]+", " ".join(req.get("codes") or [])):
        c = c.upper()
        if len(c) >= 5 and c not in codes: codes.append(c)
    if len(codes) < 2: return dict(ok=False, msg="Enter at least two booking codes to combine.")
    size = 50 if req.get("split50") else 100
    with ThreadPoolExecutor(6) as ex: L = list(ex.map(load, codes))
    now = time.time(); legs, seen, note, started, clash, bad = [], {}, [], 0, 0, []
    for code, outs in zip(codes, L):
        if not outs: bad.append(code); continue
        for e in outs:
            m = e["markets"][0]; o = m["outcomes"][0]; ts = e["estimateStartTime"] / 1000
            if ts <= now + 60: started += 1; continue
            key = (m["id"], m.get("specifier") or "", o["id"])
            if e["eventId"] in seen:
                if seen[e["eventId"]] != key: clash += 1
                continue
            seen[e["eventId"]] = key
            desc = m.get("desc") or m.get("name") or ""
            legs.append(dict(eid=e["eventId"], mid=m["id"], spec=m.get("specifier") or "", oid=o["id"], pick=f"{desc} – {o['desc']}".strip(" –"), odds=float(o["odds"]),
                             ts=ts, game=f"{e['homeTeamName']} vs {e['awayTeamName']}", lg=e["sport"]["category"]["name"] + " - " + e["sport"]["category"]["tournament"]["name"], why=f"from {code}"))
    if bad: note.append("Could not load: " + ", ".join(bad) + ".")
    if started: note.append(f"{started} leg(s) had already started and were left out.")
    if clash: note.append(f"{clash} game(s) were on more than one code with different picks; the first pick was kept (one game can only be once on a slip).")
    if not legs: return dict(ok=False, msg=" ".join(note) or "No legs left to combine.")
    legs.sort(key=lambda p: p["ts"])
    try: L_ = max(0, min(int(req.get("legs") or 0), 100))
    except Exception: L_ = 0
    try: N_ = max(0, min(int(req.get("tickets") or 0), 20))
    except Exception: N_ = 0
    n = len(legs)
    if not L_ and not N_:                                                       # one code (split only past SportyBet's limit)
        T = [legs[i:i + size] for i in range(0, n, size)]
        if len(T) > 1: note.append(f"{n} legs is more than {size} per code, so they were split into {len(T)} codes.")
    else:
        if L_ and L_ > n: note.append(f"Only {n} legs were available, so each ticket has {n} legs instead of {L_}."); L_ = n
        both = bool(L_ and N_)
        if not L_: L_ = max(1, -(-n // N_))                                      # tickets only: share the legs evenly
        if not N_: N_ = -(-n // L_)                                              # legs only: as many tickets as needed
        if not both or N_ * L_ <= n:
            # enough legs for every ticket without repeats: deal them out in turn, so each ticket gets early and late games
            pool = legs
            T = [[] for _ in range(N_)]; k = 0
            for p in pool:
                while len(T[k % N_]) >= L_: k += 1
                T[k % N_].append(p); k += 1
                if all(len(t) >= L_ for t in T): break
            left = n - sum(len(t) for t in T)
            if left: note.append(f"{left} leg(s) were not needed for {N_} tickets of {L_} and were left out.")
        else:
            # more legs asked for than there are: tickets share games (each ticket still has every game only once)
            T = [[legs[(round(i * n / N_) + j) % n] for j in range(L_)] for i in range(N_)]     # staggered starts: no two tickets the same
            T = [list({p["eid"]: p for p in t}.values()) for t in T]
            note.append(f"{N_} tickets × {L_} legs needs {N_ * L_} legs but the codes have {n}, so some games are on more than one ticket.")
        T = [t for t in T if t]
    return finish(T, "combine", {"combine": "combined " + " + ".join(codes)}, note, req)

def pick_markets(G, LX, groups, want):
    """'Choose markets': every game, the best of the ticked markets (both teams' last 10 + meetings), never the weaker side"""
    keys = [k for gname in groups for k in RB_GROUPS.get(gname, [])]
    if not keys: return []
    floor = .8 - ST["L"]
    pre = []
    for g in G:
        R = rebuild_rates(g, LX, [])
        best = max((R.get(k, 0) - bar(k) for k in keys), default=-1)
        if best >= -.1: pre.append((best, g))
    pre.sort(key=lambda x: -x[0]); pre = [g for _, g in pre[:max(200, want * 6)]]
    def detail(g):
        _, mk = event_game(g["eid"]); return g, h2h(g["hid"], g["aid"]), mk
    with ThreadPoolExecutor(8) as ex: D = list(ex.map(detail, pre))
    out = []
    for g, hh, mk in D:
        R = rebuild_rates(g, LX, hh); xh, xa = g["x"].get("1", 99), g["x"].get("3", 99); prof = profile(g, LX)
        against = set()
        if xh < xa: against |= {"win_a", "up_a"}
        if xa < xh: against |= {"win_h", "up_h"}
        if xh * 1.5 <= xa: against |= {"dc_x2", "h1dc_x2"}
        if xa * 1.5 <= xh: against |= {"dc_1x", "h1dc_1x"}
        opts = []
        for k in keys:
            mid, spec, oid, lab = RB[k]; od = mk.get((mid, spec, oid))
            if k in against or not od or od < 1.06 or R.get(k, 0) < bar(k) or not fits(k, prof) or not side_ok(k, g) or not safe_enough(k, g, LX, R.get(k, 0), od): continue
            pb = (R[k] + 1 / (od * 1.06)) / 2                                     # stats and the bookmaker's price must agree (15 Nov review)
            if pb < bar(k) - .06: continue
            opts.append((value(k, R[k], od), R[k], k, od))
        if not opts: continue
        sc, r, k, od = max(opts); mid, spec, oid, lab = RB[k]
        out.append(dict(eid=g["eid"], mid=mid, spec=spec, oid=oid, pick=lab.format(h=g["h"], a=g["a"]), odds=od, ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"], s=sc, rate=r,
                        why=f"stats rate {r:.0%} from both teams' last 10" + (f" and {len(hh)} meetings" if len(hh) >= 3 else "")))
    return sorted(out, key=lambda r: -r["s"])

# ---------- season-data ticket types (lower divisions too): goal-fest, always-beaten, draw & low-scoring ----------
def pick_season(G, LX, kind, want):
    import season
    L = ST["L"]
    usable = [g for g in G if len(LX.get(g["hid"], [])) >= 6 and len(LX.get(g["aid"], [])) >= 6]
    # quick last-10 pre-filter so only promising games need their season tables
    def l10(g):
        H, A = LX[g["hid"]], LX[g["aid"]]
        tot = lambda L_: sum(m["gf"] + m["ga"] for m in L_) / len(L_)
        lose = lambda L_: sum(m["gf"] < m["ga"] for m in L_) / len(L_)
        if kind in ("goalfest", "gfbounce"): return min(tot(H), tot(A))
        if kind == "lowbounce": return -max(tot(H), tot(A))
        if kind == "beaten": return max(lose(H), lose(A))
        return -max(tot(H), tot(A))                                   # drawlow: the lower the better
    pre = sorted(usable, key=lambda g: -l10(g))[:max(80, want * 4)]
    season.prefetch(pre, len(pre))
    C = []
    for g in pre:
        Hs, As = season.teams(g)
        if not Hs or not As or min(Hs["n"], As["n"]) < 5: continue
        h, a = season.rates(Hs), season.rates(As)
        H, A = LX[g["hid"]], LX[g["aid"]]
        f = lambda L_, c: sum(1 for m in L_ if c(m)) / len(L_)
        if kind == "gfbounce":
            # user's own type (23 Nov): goal-fest teams whose LAST game ended under 2.5 -> Over 2.5 now. Tested first on 5,453 goal-fest
            # games: no bounce-back (after 1-3 quiet games Over 2.5 came true 66-67%, the same as any goal-fest game), and when BOTH
            # teams' last 2 games were quiet only ~55% – so those are skipped. Separate type: no other picker uses this.
            g2 = (h["gpg"] + a["gpg"]) / 2
            if min(h["gpg"], a["gpg"]) < 3.0 - L * 3 or g2 < 3.5 - L * 3: continue
            q = lambda L_, k: len(L_) >= k and all(m["gf"] + m["ga"] < 3 for m in L_[:k])      # LX lists are newest first
            if not (q(H, 1) or q(A, 1)) or (q(H, 2) and q(A, 2)): continue
            o25 = (f(H, lambda m: m["gf"] + m["ga"] >= 3) + f(A, lambda m: m["gf"] + m["ga"] >= 3)) / 2
            last = lambda L_: f"{L_[0]['gf']}-{L_[0]['ga']}"
            C.append((g, "18", "total=2.5", "12", "Over 2.5", o25, f"season: {h['gpg']:.1f} and {a['gpg']:.1f} goals a game; last games {g['h']} {last(H)}, {g['a']} {last(A)} (bounce-back)"))
        elif kind == "lowbounce":
            # user's own type (23 Nov), the mirror of gfbounce: low-scoring teams (same gate as drawlow) whose LAST game had 3+ goals ->
            # Under 2.5 now. Tested first on 6,030 such games: Under 2.5 came true 65% either way (no "back to normal" effect), worse when
            # BOTH teams' last game had 3+ goals – skipped, as are strong favourites (blowouts sink unders)
            if max(h["gpg"], a["gpg"]) > 2.4 + L * 3 or (h["gpg"] + a["gpg"]) / 2 > 2.0 + L * 3: continue
            if min(g["x"].get("1", 99), g["x"].get("3", 99)) < U35_FAV: continue
            hi = lambda L_: bool(L_) and L_[0]["gf"] + L_[0]["ga"] >= 3                       # LX lists are newest first
            if not (hi(H) or hi(A)) or (hi(H) and hi(A)): continue
            u25 = (f(H, lambda m: m["gf"] + m["ga"] <= 2) + f(A, lambda m: m["gf"] + m["ga"] <= 2)) / 2
            last = lambda L_: f"{L_[0]['gf']}-{L_[0]['ga']}"
            C.append((g, "18", "total=2.5", "13", "Under 2.5", u25, f"season: {h['gpg']:.1f} and {a['gpg']:.1f} goals a game; last games {g['h']} {last(H)}, {g['a']} {last(A)} (back to low)"))
        elif kind == "goalfest":
            g2 = (h["gpg"] + a["gpg"]) / 2
            if min(h["gpg"], a["gpg"]) < 3.0 - L * 3 or g2 < 3.5 - L * 3: continue
            o25 = (f(H, lambda m: m["gf"] + m["ga"] >= 3) + f(A, lambda m: m["gf"] + m["ga"] >= 3)) / 2
            o35 = (f(H, lambda m: m["gf"] + m["ga"] >= 4) + f(A, lambda m: m["gf"] + m["ga"] >= 4)) / 2
            for k, r_, line in (("o25", o25, "total=2.5"), ("o35", o35, "total=3.5")):
                C.append((g, "18", line, "12", f"Over {line[-3:]}", r_, f"season: {h['gpg']:.1f} and {a['gpg']:.1f} goals a game ({h['n']}/{a['n']} games)"))
        elif kind == "beaten":
            for weak, strong, side in ((h, a, "a"), (a, h, "h")):
                if weak["loss"] < .7 - L or strong["loss"] > .4 + L or strong["ppg"] < 1.3 - L * 2: continue
                venue_loss = weak["hloss"] if side == "a" else weak["aloss"]
                if venue_loss is not None and venue_loss < .6 - L: continue
                S, W = (A, H) if side == "a" else (H, A)
                r_win = (f(S, lambda m: m["gf"] > m["ga"]) + f(W, lambda m: m["gf"] < m["ga"])) / 2
                r_dc = (f(S, lambda m: m["gf"] >= m["ga"]) + f(W, lambda m: m["gf"] <= m["ga"])) / 2
                team = g["a"] if side == "a" else g["h"]; wk = g["h"] if side == "a" else g["a"]
                why = f"season: {wk} lost {weak['loss']:.0%} of {weak['n']}; {team} lost only {strong['loss']:.0%}"
                C.append((g, "1", "", "3" if side == "a" else "1", f"{team} to win", r_win, why))
                C.append((g, "10", "", "11" if side == "a" else "9", f"Double chance {team} or draw", r_dc, why))
        else:   # drawlow
            g2 = (h["gpg"] + a["gpg"]) / 2
            low = max(h["gpg"], a["gpg"]) <= 2.4 + L * 3
            if low:
                u35 = (f(H, lambda m: m["gf"] + m["ga"] <= 3) + f(A, lambda m: m["gf"] + m["ga"] <= 3)) / 2
                u25 = (f(H, lambda m: m["gf"] + m["ga"] <= 2) + f(A, lambda m: m["gf"] + m["ga"] <= 2)) / 2
                if exp_goals(H, A) < U35_EXP and min(g["x"].get("1", 99), g["x"].get("3", 99)) >= U35_FAV:     # same Under 3.5 rules as the other builders
                    C.append((g, "18", "total=3.5", "13", "Under 3.5", u35, f"season: {h['gpg']:.1f} and {a['gpg']:.1f} goals a game"))
                if g2 <= 2.0 + L * 3: C.append((g, "18", "total=2.5", "13", "Under 2.5", u25, f"season: {h['gpg']:.1f} and {a['gpg']:.1f} goals a game"))
            if min(h["draw"], a["draw"]) >= .40:
                dr = (f(H, lambda m: m["gf"] == m["ga"]) + f(A, lambda m: m["gf"] == m["ga"])) / 2
                C.append((g, "1", "", "2", "Draw", dr, f"season: draws {h['draw']:.0%} and {a['draw']:.0%}"))
    # prices for the shortlisted games, then the best-value safe option per game
    eids = list({c[0]["eid"]: c[0] for c in C}.values())
    with ThreadPoolExecutor(8) as ex: MK = dict(zip([g["eid"] for g in eids], ex.map(lambda g: event_game(g["eid"])[1], eids)))
    floor = .75 - L / 2
    best = {}
    for g, mid, spec, oid, pick, r_, why in C:
        od = MK.get(g["eid"], {}).get((mid, spec, oid))
        if not od or od < 1.06: continue
        if not (mid == "1" and oid == "2") and (r_ < floor or od > 1.70): continue    # no coin-flip prices (draws are judged on their draw rate)
        if spec == "total=3.5" and oid == "13" and od > U35_MAX: continue
        if mid == "18" and spec == "total=2.5" and oid == "13" and r_ < .80 and kind != "lowbounce": continue     # Under 2.5: the tracker's weakest market (the lowbounce type uses the normal bar)
        if mid == "1" and oid == "2" and r_ < .45 - L: continue
        v = edge(r_, od)
        if g["eid"] not in best or v > best[g["eid"]]["s"]:
            best[g["eid"]] = dict(eid=g["eid"], mid=mid, spec=spec, oid=oid, pick=pick, odds=od, ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"], s=v, rate=r_,
                                  why=f"{why}; last 10 rate {r_:.0%}")
    return sorted(best.values(), key=lambda x: -x["s"])

def finish(T, kind, names, note, req):
    """book every ticket, reload and check it, report"""
    if req.get("_legs_only"):                                                    # combined football + basketball ticket: hand the legs back, book later
        return dict(ok=bool(T and T[0]), legs=(T[0] if T else []), note=note, kind=names.get(kind, kind))
    if AVOID["hit"]: note.append(f"{len(AVOID['hit'])} game(s) already on your earlier tickets (not started yet) were skipped, so this ticket has fresh games.")
    res = []
    for t in T:
        t.sort(key=lambda p: p["ts"])
        code = post([dict(eventId=p["eid"], marketId=p["mid"], specifier=p["spec"], outcomeId=p["oid"]) for p in t])
        if not code: note.append("SportyBet refused one booking."); continue
        time.sleep(2); o = load(code)
        okc = len(o) == len(t) and len({e["eventId"] for e in o}) == len(o) and (kind in ("live", "bblive", "livemix") or all(e["estimateStartTime"]/1000 > time.time() for e in o))
        odds = 1.0
        for e in o: odds *= float(e["markets"][0]["outcomes"][0]["odds"])
        if not okc: note.append(f"{code} failed the reload check and was not used."); continue
        res.append(dict(code=code, odds=round(odds, 2), legs=[dict(t=hm(p["ts"]), game=p["game"], lg=p["lg"], pick=p["pick"], odds=p["odds"], why=p["why"],
                                                                        ts=p["ts"], eid=p["eid"], mid=p["mid"], spec=p["spec"], oid=p["oid"],
                                                                        k=f"{p['eid'].split(':')[-1]}|{p['mid']}|{p['spec']}|{p['oid']}") for p in t]))
    if kind == "u25": note.append("Under 2.5 has been the weakest market in your tracker.")
    for r_ in res:                                                              # the honest chance the whole ticket wins (15 Nov review: 11+ legs won 0 of 29)
        ch = 1.0
        for l in r_["legs"]: ch *= min(.97, 1 / (l["odds"] * 1.05))
        r_["chance"] = round(ch, 4)
    if res:
        c = res[0]["chance"]
        note.append(f"Chance all {len(res[0]['legs'])} legs win: about {c:.0%}" + (" – in your results, tickets of 11+ legs have won 0 of 29; 1–3 legs won 64%." if len(res[0]["legs"]) >= 11 else "."))
    return dict(ok=bool(res), kind=names[kind], req=req, tickets=res, note=note, first=min((p["ts"] for t in T for p in t), default=None))

# ---------- football + basketball on one ticket ----------
def fbmix(req):
    """runs the football builder and the basketball builder with the user's choices (each with all its own checks), then books
    every leg on one code"""
    import bball
    base = {k: req[k] for k in ("days", "strict", "from", "to") if k in req}
    fb = dict(base, type=req.get("fb_type") or "mix", legs=max(0, min(int(req.get("fb_legs") or 0), 40)), tickets=1, _legs_only=True)
    bb = dict(base, type="bball", kind=req.get("bb_kind") or "total", legs=max(0, min(int(req.get("bb_legs") or 0), 40)), _legs_only=True)
    if fb["type"].startswith("mk_"): fb["markets"] = [fb["type"][3:]]; fb["type"] = "markets"
    if not fb["legs"] and not bb["legs"]: return dict(ok=False, msg="Choose how many football and/or basketball legs you want.")
    F = main(fb) if fb["legs"] else dict(ok=True, legs=[], note=[])
    ST["L"] = {"strict": 0.0, "normal": 0.1, "relaxed": 0.2}.get(req.get("strict", "normal"), 0.1)
    B = bball.build(bb) if bb["legs"] else dict(ok=True, legs=[], note=[])
    note = []
    fl, bl = (F.get("legs") or [])[:fb["legs"]], (B.get("legs") or [])[:bb["legs"]]
    if fb["legs"] and len(fl) < fb["legs"]: note.append(f"Football: {len(fl)} of {fb['legs']} legs passed the checks" + (f" ({F.get('msg')})" if F.get("msg") else "") + ".")
    if bb["legs"] and len(bl) < bb["legs"]: note.append(f"Basketball: {len(bl)} of {bb['legs']} legs passed the checks" + (f" ({B.get('msg')})" if B.get("msg") else "") + ".")
    legs = fl + bl
    if not legs: return dict(ok=False, msg=" ".join(note) or "No legs passed the checks.")
    for l in legs: l.setdefault("ts", time.time() + 3600)
    return finish([legs], "fbmix", {"fbmix": f"football + basketball ({len(fl)} ⚽ + {len(bl)} 🏀)"}, note, req)

def livemix(req):
    """live football + live basketball on one code: each live picker runs with its own rules unchanged, then all legs are booked together"""
    import live, bblive
    fl_n = max(0, min(int(req.get("fb_legs") or 0), 40)); bl_n = max(0, min(int(req.get("bb_legs") or 0), 40))
    if not fl_n and not bl_n: return dict(ok=False, msg="Choose how many live football and/or basketball legs you want.")
    F = live.build(dict(strategy=req.get("strategy") or "mix", legs=fl_n, strict=req.get("strict", "normal"), _legs_only=True)) if fl_n else dict(legs=[])
    B = bblive.build(dict(kind=req.get("bb_kind") or "mix", legs=bl_n, strict=req.get("strict", "normal"), _legs_only=True)) if bl_n else dict(legs=[])
    fl, bl = (F.get("legs") or [])[:fl_n], (B.get("legs") or [])[:bl_n]
    note = []
    if fl_n and len(fl) < fl_n: note.append(f"Live football: {len(fl)} of {fl_n} legs passed" + (f" ({F.get('msg')})" if F.get("msg") else "") + ".")
    if bl_n and len(bl) < bl_n: note.append(f"Live basketball: {len(bl)} of {bl_n} legs passed" + (f" ({B.get('msg')})" if B.get("msg") else "") + ".")
    if not fl + bl: return dict(ok=False, msg=" ".join(note) or "No live legs passed the checks right now.")
    note.append("Live odds move fast – load the code on SportyBet straight away.")
    return finish([fl + bl], "livemix", {"livemix": f"live football + basketball ({len(fl)} ⚽ + {len(bl)} 🏀)"}, note, req)

# ---------- "Pattern: big-odds combo" (15 Nov, from the user's own results: patterns that beat the odds in BOTH periods) ----------
BANKERS = (("860", "", "74", "{h} or GG", .97), ("60021", "", "76", "{h} to score 3+ in a row – No", .95), ("58", "total=1.5", "76", "Both halves Over 1.5 – No", .95))
def pattern(req):
    """half-time draws at 1.80-2.50 (47.6% won vs 44.4% the odds said, 1,357 legs) + football bankers priced 1.15 or less (home or GG
    94-97%, home 3+ in a row No 92-95%, both halves Over 1.5 No 90-100%, vs ~86% the odds said) + basketball total points at
    1.15-1.25 (88% won vs 78%) + Over 1.5 top tier (o15_top, ~88%). One leg per game, strongest first, booked on one code."""
    import bball
    n_h = max(0, min(int(req.get("htd") or 0), 3)); n_b = max(0, min(int(req.get("bank") or 0), 4)); n_bb = max(0, min(int(req.get("bb") or 0), 6))
    n_fh = max(0, min(int(req.get("fh") or 0), 10)); n_o = max(0, min(int(req.get("o15") or 0), 20))
    if not n_h + n_b + n_bb + n_fh + n_o: return dict(ok=False, msg="Choose how many legs you want in each part.")
    ST["L"] = {"strict": 0.0, "normal": 0.1, "relaxed": 0.2}.get(req.get("strict", "normal"), 0.1)
    LAG = LAGOS; now = time.time()
    days = sorted({d for d in (req.get("days") or []) if re.fullmatch(r"\d{4}-\d\d-\d\d", str(d))}) or [dt.datetime.now(LAG).date().isoformat()]
    hi = dt.datetime.combine(dt.date.fromisoformat(days[-1]), dt.time(23, 59), LAG).timestamp()
    G = [g for g in games_cached(now + 20 * 60, hi) if dt.datetime.fromtimestamp(g["ts"], LAG).date().isoformat() in days]
    LX = fetch_teams(G); used, legs, note = set(), [], []
    dropped = [0]
    def chk(L):                                                                 # the normal builder's extra source checks, on the candidates before picking
        L = L[:40]
        try:
            import extra
            ok, d = extra.confirm(L, len(L)); dropped[0] += d; return ok[:len(L)]
        except Exception as e: print("pattern checks skipped:", e); return L
    if n_h:
        H = [p for p in pick_htdraw(G, LX) + pick_draw_weak(G, LX, True) if 1.80 <= p["odds"] <= 2.50]      # level-at-the-break teams, then weak sides
        H = chk(H)
        for p in H:
            if len([l for l in legs if l["mid"] == "60"]) >= n_h: break
            if p["eid"] in used: continue
            used.add(p["eid"]); p["why"] += " (pattern: half-time draw 1.80-2.50)"; legs.append(p)
        if len([l for l in legs if l["mid"] == "60"]) < n_h: note.append(f"Half-time draws: {len([l for l in legs if l['mid'] == '60'])} of {n_h} found at 1.80-2.50.")
    if n_b:
        cand = []
        for g in G:
            Hh, Aa = LX.get(g["hid"], []), LX.get(g["aid"], [])
            if len(Hh) < 6 or len(Aa) < 6 or g["eid"] in used: continue
            gpg = (sum(m["gf"] + m["ga"] for m in Hh) / len(Hh) + sum(m["gf"] + m["ga"] for m in Aa) / len(Aa)) / 2
            hgf = sum(m["gf"] for m in Hh) / len(Hh)
            cand.append((g, gpg, hgf))
        cand.sort(key=lambda c: c[1]); cand = cand[:90]                              # lower-scoring games first: all three bankers are about "not many goals"
        with ThreadPoolExecutor(8) as ex: MK = dict(zip([c[0]["eid"] for c in cand], ex.map(lambda c: event_game(c[0]["eid"])[1], cand)))
        opts = []
        for g, gpg, hgf in cand:
            mk = MK.get(g["eid"]) or {}
            Hh, Aa = LX[g["hid"]], LX[g["aid"]]
            fr = lambda L, c: sum(1 for m in L if c(m)) / len(L)
            for mid, spec, oid, lab, base in BANKERS:
                od = mk.get((mid, spec, oid))
                if not od or not 1.06 <= od <= 1.15: continue                       # 1.06+: a banker should still add something to the odds
                # each banker needs the teams' own stats behind it, not just a short price
                if mid == "860":                                                    # home or GG: home team rarely loses unless both teams score
                    if g["x"].get("1", 9) > 1.7: continue
                    r = (fr(Hh, lambda m: m["gf"] > m["ga"] or (m["gf"] > 0 and m["ga"] > 0)) + fr(Aa, lambda m: m["gf"] < m["ga"] or (m["gf"] > 0 and m["ga"] > 0))) / 2
                    if r < .85: continue
                    why = f"home won or both scored in {r:.0%} of the teams' last games"
                elif mid == "60021":                                                # home team 3+ in a row No: it rarely scores 3+ at all
                    r = 1 - fr(Hh, lambda m: m["gf"] >= 3)
                    if r < .8 or hgf > 2.0: continue
                    why = f"{g['h']} scored 3+ in only {1 - r:.0%} of its last {len(Hh)} games"
                else:                                                               # both halves Over 1.5 No: 2+ goals in each half is rare for these teams
                    H1, A1 = [m for m in Hh if m["h1f"] is not None], [m for m in Aa if m["h1f"] is not None]
                    if len(H1) < 6 or len(A1) < 6: continue
                    bh = lambda m: m["h1f"] + m["h1a"] >= 2 and m["h2f"] + m["h2a"] >= 2
                    r = 1 - (fr(H1, bh) + fr(A1, bh)) / 2
                    if r < .85 or gpg > 3.2: continue
                    why = f"2+ goals in BOTH halves in only {1 - r:.0%} of the teams' last games"
                opts.append(dict(eid=g["eid"], mid=mid, spec=spec, oid=oid, pick=lab.format(h=g["h"]), odds=od, ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"],
                                 rate=r, s=r, why=f"pattern banker: {why}; {gpg:.1f} goals a game"))
        opts.sort(key=lambda o: (-round(o["rate"], 2), -o["odds"]))                  # strongest stats first, then the ones that add most to the odds
        opts = chk(opts)
        nb = 0
        for o in opts:
            if nb >= n_b: break
            if o["eid"] in used: continue
            used.add(o["eid"]); legs.append(o); nb += 1
        if nb < n_b: note.append(f"Bankers: {nb} of {n_b} found at 1.15 or less.")
    if n_fh:
        # the user's own 1st-half pattern (verified 15 Nov on their results): 1st half double chance at 1.15-1.30 won 92.6% (217 legs,
        # 13 pts above the odds; at 1.30-1.60 only 48%), 1st half GG No at 1.30 or less ~85-92%, 1st half Under 2.5 ~84% (about what
        # its price says) – so prices are capped where the results stayed strong
        cap = {"h1dc_1x": (1.15, 1.30), "h1dc_x2": (1.15, 1.30), "h1gg_n": (1.04, 1.30), "h1u25": (1.04, 1.30)}
        F = [p for p in pick_markets(G, LX, ["h1dc", "h1ggn", "h1u25"], 200) if p["eid"] not in used]
        F = [p for p in F if any(RB[k][:3] == (p["mid"], p["spec"], p["oid"]) and cap[k][0] <= p["odds"] <= cap[k][1] for k in cap)]
        for p in F: p["chance"] = (min(p["rate"], .99) + 1 / (p["odds"] * 1.06)) / 2
        F.sort(key=lambda p: (-(p["mid"] == "63"), -p["chance"]))                      # 1st half double chance first: the strongest pocket
        F = chk(F)
        nf = 0
        for p in F:
            if nf >= n_fh: break
            if p["eid"] in used: continue
            used.add(p["eid"]); p["why"] += " (your 1st-half pattern)"; legs.append(p); nf += 1
        if nf < n_fh: note.append(f"1st-half pattern: {nf} of {n_fh} found in the price ranges that won.")
    if n_o:
        # Over 1.5 TOP TIER only (o15_top: both teams 2+ goals in 90%+ of their last 10, or 80%+ and all of their last 5; 87-89% on
        # 4,149 replayed games, 90% of tracked legs vs 84% implied), 1.25 or less, strongest first
        O = [p for p in pick_goals(G, LX, "o15") if p.get("top") and p["eid"] not in used]
        for p in O: p["chance"] = (min(p["rate"], .99) + 1 / (p["odds"] * 1.06)) / 2
        O.sort(key=lambda p: -p["chance"]); O = chk(O)
        no = 0
        for p in O:
            if no >= n_o: break
            if p["eid"] in used: continue
            used.add(p["eid"]); p["why"] += " (pattern: Over 1.5 top tier)"; legs.append(p); no += 1
        if no < n_o: note.append(f"Over 1.5 top tier: {no} of {n_o} found for the days chosen (tick more days for more).")
    if n_bb:
        B = bball.build(dict(kind="total", legs=40, strict=req.get("strict", "normal"), days=days, _legs_only=True))
        bb = [l for l in (B.get("legs") or []) if 1.15 <= l["odds"] <= 1.25 and l["eid"] not in used][:n_bb]
        for l in bb: used.add(l["eid"]); l["why"] += " (pattern: basketball total 1.15-1.25)"
        legs += bb
        if len(bb) < n_bb: note.append(f"Basketball totals: {len(bb)} of {n_bb} found at 1.15-1.25.")
    if dropped[0]: note.append(f"{dropped[0]} candidate(s) were left out because Flashscore, footybets, FotMob, DFP, Vitibet or the season table went against them.")
    if not legs: return dict(ok=False, msg=" ".join(note) or "No legs fit the pattern for the days chosen.")
    ch = math.prod({"60": .476, "225": .88, "63": .926, "75": .85, "68": .84, "18": .88}.get(l["mid"], .95) for l in legs)
    note.append(f"Pattern estimate from your own results: about {ch:.0%} chance this ticket wins" + (f" (about {round(ch * 10)} in 10)." if ch >= .3 else f" (about 1 in {round(1 / ch)})."))
    return finish([legs], "pattern", {"pattern": f"pattern combo ({sum(l['mid'] == '60' for l in legs)} HT draw + {sum(l['mid'] in ('860', '60021', '58') for l in legs)} bankers + {sum(l['mid'] in ('63', '75', '68') for l in legs)} 1st-half + {sum(l['mid'] == '18' for l in legs)} Over 1.5 + {sum(l['mid'] == '225' for l in legs)} basketball)"}, note, req)

# ---------- main ----------
def main(req):
    ST["L"] = {"strict": 0.0, "normal": 0.1, "relaxed": 0.2}.get(req.get("strict", "normal"), 0.1)
    if req.get("type") == "rebuild": return rebuild(req)
    if req.get("type") == "combine": return combine(req)
    if req.get("type") == "live":
        import live; return live.build(req)
    if req.get("type") == "bball":
        import bball; return bball.build(req)
    if req.get("type") == "bblive":
        import bblive; return bblive.build(req)
    if req.get("type") == "fbmix": return fbmix(req)
    if req.get("type") == "livemix": return livemix(req)
    if req.get("type") == "pattern": return pattern(req)
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
    # time window within each day (Lagos time): "from"/"to" as HH:MM; to < from wraps past midnight (e.g. 22:00-06:00)
    def _mins(x):
        m = re.fullmatch(r"(\d{1,2}):(\d\d)", str(x or "")); return int(m.group(1)) * 60 + int(m.group(2)) if m else None
    f_, t_ = _mins(req.get("from")), _mins(req.get("to"))
    if f_ is not None and t_ is not None and not (f_ == 0 and t_ >= 23 * 60 + 59):
        def _in(g):
            w = dt.datetime.fromtimestamp(g["ts"], LAGOS); m = w.hour * 60 + w.minute
            return f_ <= m <= t_ if f_ <= t_ else (m >= f_ or m <= t_)
        G = [g for g in G if _in(g)]; span += f", {req['from']}–{req['to']}"
    LX = fetch_teams(G)
    P = pick_fav(G, LX, kind == "smallfav") if kind in ("fav", "smallfav") else pick_fav1h(G, LX) if kind == "fav1h" else pick_fav1h(G, LX, 2) if kind == "fav2h" else pick_h1o05(G, LX) if kind == "h1o05" else pick_h1o05(G, LX, 2) if kind == "h2o05" else pick_yw_strong(G, LX) if kind == "o25yw" else pick_markets(G, LX, req.get("markets") or [], legs * nt) if kind == "markets" else pick_multi(G, LX, kind, legs * nt) if kind in SETS else pick_htdraw(G, LX) if kind == "htdraw" else pick_draw_weak(G, LX) if kind == "drawweak" else pick_draw_weak(G, LX, True) if kind == "htdrawweak" else pick_season(G, LX, kind, legs * nt) if kind in ("goalfest", "gfbounce", "lowbounce", "beaten", "drawlow") else pick_goals(G, LX, kind)
    # strongest first (user, 15 Nov: "make the top priority the best of the best"): every leg ranked by its chance of coming true –
    # its stats rate and the bookmaker's own price together – so weaker legs are only used once all stronger ones are on the ticket
    if P and all(p.get("rate") is not None for p in P):
        for p in P: p["chance"] = (min(p["rate"], .99) + 1 / (p["odds"] * 1.06)) / 2
        P.sort(key=lambda p: (-bool(p.get("top")), -p["chance"]))              # Over 1.5 top tier (o15_top) before the rest
    seen = set(); P = [p for p in P if not (p["eid"] in seen or seen.add(p["eid"]))]   # one leg per game
    dropped = 0
    if req.get("check", True):   # Flashscore home/away form + footybets must not go against a leg
        try:
            import extra
            P, dropped = extra.confirm(P, legs * nt)
        except Exception as e: print("extra checks skipped:", e)
    names = {"drawweak": "full-time draw – two weak teams", "htdrawweak": "half-time draw – weak teams", "goalfest": "goal-fest teams (season data)", "gfbounce": "goal-fest bounce-back (Over 2.5 after a quiet game)", "lowbounce": "low-scoring teams back to low (Under 2.5 after a 3+ goal game)", "beaten": "always-beaten teams (season data)", "drawlow": "draw & low-scoring teams (season data)", "markets": "your chosen markets", "htdraw": "half-time draw", "gg": "GG (both teams score)", "best": "your tracker's best markets", "mix": "safest market per game", "h2o05": "2nd half Over 0.5", "fav2h": "strong team to score in the 2nd half", "h1o05": "1st half Over 0.5", "fav1h": "strong team to score in the 1st half", "fav": "strong favourites to win", "smallfav": "smallest-odds favourites to win", "o15": "Over 1.5", "o25": "Over 2.5 (lowest prices first)",
             "o25yw": "Over 2.5 (strong teams in youth & women's leagues)", "u25": "Under 2.5 (low-scoring leagues)"}
    note = [f"{dropped} leg(s) dropped because Flashscore, footybets, FotMob, DFP or Vitibet went against them."] if dropped else []
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
    if len(P) < 2:
        if kind == "markets" and set(req.get("markets") or []) <= {"m5x", "m10x"}:
            return dict(ok=False, msg=f"Only {len(P)} game(s) have a strong 1–5 / 1–10 minutes draw right now ({span}). SportyBet opens these markets on only a few games, mostly on match day and mainly in the bigger leagues – try again closer to kick-off, or tick them together with other markets.")
        return dict(ok=False, msg=f"Only {len(P)} game(s) pass the checks for {names[kind]} ({span}) out of {len(G)} on SportyBet. Try 'Relaxed', more days, or another type.")
    if len(P) < legs:
        note.append(f"Only {len(P)} games passed the checks ({span}, {len(G)} games on SportyBet), so this ticket has {len(P)} legs instead of {legs}. Tick more days or use 'Relaxed' for more.")
        legs = len(P)
    can = len(P) // legs
    if can < nt: note.append(f"Only {len(P)} games qualified, enough for {can} ticket(s) of {legs} with no game repeated (you asked for {nt}).")
    nt = min(nt, can); use = P[:nt*legs]
    T = [use[i * legs:(i + 1) * legs] for i in range(nt)]   # ticket 1 = the strongest legs, ticket 2 = the next strongest, ...
    return finish(T, kind, names, note, req)

if __name__ == "__main__":
    req = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {"type": "fav", "legs": 10}
    out = main(req); out["built"] = time.time()
    print(json.dumps(out, indent=1))
    json.dump(out, open(os.path.join(HERE, "last.json"), "w"))
