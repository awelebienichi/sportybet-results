#!/usr/bin/env python3
"""Personal ticket tracker.

  python3 track.py add "Set name" CODE [CODE ...]   store codes (legs read once from the booking code)
  python3 track.py check                            settle every stored ticket from final scores

Scores come from Sportradar's match feed (the data SportyBet uses). If Sportradar has
no half-time score, the half-by-half score from SportyBet's match page is used. Every
leg is marked here from the score itself; SportyBet's own settlement is never used.
Outputs report.html (codes highlighted with results beside them), summary.txt,
results.json (score cache) and a Mac notification.
"""
import json, os, re, sys, subprocess, time, datetime as dt, html, collections, concurrent.futures as cf

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "tracker.json")
CACHE = os.path.join(HERE, "results.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
WAT = dt.timezone(dt.timedelta(hours=1))
ENDED = {"Ended", "AET", "AP", "After extra time", "After penalties", "After Penalties"}
VOID = {"Postponed", "Cancelled", "Canceled", "Abandoned", "Walkover"}


def get(url, ref="https://www.sportybet.com/ng/"):
    try:
        return json.loads(subprocess.run(["curl", "-s", "-m", "40", "-A", UA, "-H", "Accept: application/json", "-H", f"Referer: {ref}", url],
                                         capture_output=True, text=True).stdout)
    except Exception:
        return None


def load_db():
    return json.load(open(DB)) if os.path.exists(DB) else {"sets": []}


# ---------------------------------------------------------------- add codes
def read_code(code):
    for _ in range(3):
        d = get(f"https://www.sportybet.com/api/ng/orders/share/{code}")
        try:
            O = d["data"]["outcomes"]
            break
        except Exception:
            time.sleep(1.5)
    else:
        return None
    legs = []
    for o in O:
        m = o["markets"][0]; x = m["outcomes"][0]
        legs.append(dict(eid=o["eventId"], match=f"{o['homeTeamName']} vs {o['awayTeamName']}", kickoff=o["estimateStartTime"] / 1000,
                         mid=str(m["id"]), spec=m.get("specifier") or "", mdesc=m["desc"], pick=x["desc"], odds=float(x["odds"])))
    return legs


def add(set_name, codes):
    db = load_db()
    s = next((s for s in db["sets"] if s["name"] == set_name), None)
    if not s:
        s = {"name": set_name, "added": time.time(), "tickets": []}
        db["sets"].append(s)
    have = {t["code"] for t in s["tickets"]}
    todo = [c.upper() for c in codes if c.upper() not in have]
    with cf.ThreadPoolExecutor(6) as ex:
        got = list(ex.map(read_code, todo))
    n = len(s["tickets"])
    for c, legs in zip(todo, got):
        if legs:
            n += 1
            s["tickets"].append({"ticket": n, "code": c, "legs": legs})
        else:
            print("could not read", c)
    json.dump(db, open(DB, "w"))
    print(f"{set_name}: {len(s['tickets'])} tickets stored")


# ---------------------------------------------------------------- scores
def sportradar(eid):
    d = get(f"https://stats.fn.sportradar.com/sportybet/en/Europe:Berlin/gismo/match_info/{eid.split(':')[-1]}", "https://www.sportybet.com/")
    try:
        m = d["doc"][0]["data"]["match"]
    except Exception:
        return None
    res = m.get("result") or {}; p1 = (m.get("periods") or {}).get("p1")
    return dict(status=(m.get("status") or {}).get("name", ""), ft=[res["home"], res["away"]] if res.get("home") is not None else None,
                ht=[p1["home"], p1["away"]] if p1 else None)


TL_MIDS = {"900313", "900069", "60010", "60011", "60012", "60020", "60021", "60022", "60210", "60100", "60200", "60110", "60180",
           "900300", "900301", "900302", "900303", "900304", "900305", "166", "177", "138", "139", "900393", "900394", "900342",
           "900396", "900552", "900553", "900546", "900547", "900544", "900545", "900568", "900569"} | {str(m) for m in range(60300, 60309)}


def extra(eid):
    """goal minutes, corners, cards and team stats from Sportradar's timeline and match details"""
    sid = eid.split(':')[-1]
    d = get(f"https://stats.fn.sportradar.com/sportybet/en/Europe:Berlin/gismo/match_timeline/{sid}", "https://www.sportybet.com/")
    try:
        ev = d["doc"][0]["data"]["events"]
    except Exception:
        return None
    x = dict(goals=[[e["time"], e["team"]] for e in ev if e["type"] == "goal" and e.get("team") in ("home", "away") and not e.get("disabled")],
             corners=[[e["time"], e["team"]] for e in ev if e["type"] == "corner" and e.get("team") in ("home", "away") and not e.get("disabled")],
             cards=[[e["time"], e["team"], e.get("card", "")] for e in ev if e["type"] == "card" and e.get("team") in ("home", "away") and not e.get("disabled")])
    d = get(f"https://stats.fn.sportradar.com/sportybet/en/Europe:Berlin/gismo/match_detailsextended/{sid}", "https://www.sportybet.com/")
    try:
        v = d["doc"][0]["data"]["values"]
        st = {}
        for k, name in (("124", "corners"), ("125", "sot"), ("126", "soff"), ("171", "sblk"), ("129", "fouls"), ("123", "offsides"),
                        ("40", "yellow"), ("45", "yellowred"), ("50", "red")):
            if k in v: st[name] = [int(v[k]["value"]["home"]), int(v[k]["value"]["away"])]
        x["stats"] = st
    except Exception:
        x["stats"] = {}
    return x


def sportybet_scores(eid):
    d = get(f"https://www.sportybet.com/api/ng/factsCenter/event?eventId={eid}&productId=3")
    try:
        e = d["data"]; gs = e.get("gameScore") or []
        return dict(status=e.get("matchStatus") or "", ft=[int(v) for v in e["setScore"].split(":")] if e.get("setScore") else None,
                    ht=[int(v) for v in gs[0].split(":")] if gs else None)
    except Exception:
        return None


def fetch(eid, ko, rec):
    now = time.time()
    if rec and rec.get("final") and (rec.get("void") or rec.get("ht")):
        return rec
    if ko > now - 2 * 3600:
        return rec
    a = sportradar(eid)
    new = dict(final=False, status=(a or {}).get("status", ""), ft=(a or {}).get("ft"), ht=(a or {}).get("ht"), src="sportradar")
    if a and a["status"] in VOID:
        new.update(final=True, void=True)
    elif a and a["status"] in ENDED and a["ft"]:
        new["final"] = True
        if not a["ht"]:
            b = sportybet_scores(eid)
            if b and b.get("ht"):
                new["ht"] = b["ht"]; new["src"] = "sportradar + half-time from sportybet scores"
    elif ko < now - 6 * 3600:
        b = sportybet_scores(eid)
        if b and b["status"] in ("Ended", "AET", "AP") and b.get("ft"):
            new.update(final=True, ft=b["ft"], ht=b.get("ht"), status=b["status"], src="sportybet scores")
            if not b.get("ht"):   # final score with no halves at all: usually an awarded result, not a played one
                new["doubtful"] = True
        elif b and b["status"] in ("Cancelled", "Abandoned", "Postponed"):
            new.update(final=True, void=True, status=b["status"], src="sportybet")
    return new


# ---------------------------------------------------------------- settlement from score
def num(s, key):
    m = re.search(key + r"=([-\d.]+)", s or "")
    return float(m.group(1)) if m else None


def ou(total, line, pick):
    if total == line: return "void"
    over = total > line
    return "won" if (over == pick.startswith("Over")) else "lost"


def res1x2(h, a):
    return "Home" if h > a else "Away" if a > h else "Draw"


def settle(l, r):
    """returns won / lost / void / pending / check"""
    if not r or not r.get("final"): return "pending"
    if r.get("void"): return "void"
    if r.get("doubtful"): return "check"
    ft = r.get("ft"); ht = r.get("ht")
    if not ft: return "pending"
    H, A = ft; T = H + A
    h1 = ht; h2 = [H - ht[0], A - ht[1]] if ht else None
    mid, spec, p = l["mid"], l["spec"], l["pick"]
    try:
        if mid == "1": return "won" if res1x2(H, A) == p else "lost"
        if mid in ("10", "63"):
            sc = ft if mid == "10" else h1
            if sc is None: return "pending"
            r1 = res1x2(*sc)
            ok = {"Home or Draw": r1 in ("Home", "Draw"), "Home or Away": r1 in ("Home", "Away"), "Draw or Away": r1 in ("Draw", "Away")}[p]
            return "won" if ok else "lost"
        if mid == "11":
            r1 = res1x2(H, A)
            return "void" if r1 == "Draw" else ("won" if r1 == p else "lost")
        if mid == "60":
            if not h1: return "pending"
            return "won" if res1x2(*h1) == p else "lost"
        if mid == "83":
            if not h2: return "pending"
            return "won" if res1x2(*h2) == p else "lost"
        if mid == "18": return ou(T, num(spec, "total"), p)
        if mid == "19": return ou(H, num(spec, "total"), p)
        if mid == "20": return ou(A, num(spec, "total"), p)
        if mid == "68":
            if not h1: return "pending"
            return ou(sum(h1), num(spec, "total"), p)
        if mid == "90":
            if not h2: return "pending"
            return ou(sum(h2), num(spec, "total"), p)
        if mid == "29": return "won" if ((H > 0 and A > 0) == (p == "Yes")) else "lost"
        if mid in ("450001", "450002", "450003"):
            v = T if mid == "450001" else H if mid == "450002" else A
            m = re.match(r"(\d+)(?:-(\d+))?(\+)?", p)
            lo = int(m.group(1)); hi = int(m.group(2)) if m.group(2) else lo
            plus = bool(m.group(3))
            ok = v >= lo and (plus or v <= hi)
            return "won" if ok else "lost"
        if mid == "450004":
            k = int(re.match(r"(\d+)", p).group(1)); plus = "+" in p
            hit = (T >= k) if plus else (T == k)
            return "lost" if hit else "won"
        if mid == "16":
            hc = float(re.search(r"\(([-+]?[\d.]+)\)", p).group(1))
            diff = (H - A) if p.startswith("Home") else (A - H)
            def one(x):
                v = diff + x
                return "won" if v > 0 else "lost" if v < 0 else "void"
            if abs(hc * 4) % 2 == 1:   # quarter line: split stake
                a1, a2 = one(hc - 0.25), one(hc + 0.25)
                if a1 == a2: return a1
                return "half-won" if "won" in (a1, a2) else "half-lost"
            return one(hc)
        if mid == "59":
            if not h1: return "pending"
            both = sum(h1) <= 1 and sum(h2) <= 1
            return "won" if both == (p == "Yes") else "lost"
        if mid in ("50", "51"):
            if not h1: return "pending"
            side = 0 if mid == "50" else 1
            won_half = (h1[side] > h1[1 - side]) or (h2[side] > h2[1 - side])
            return "won" if won_half == (p == "Yes") else "lost"
        if mid == "547":
            dc, _, uo = p.partition(" & ")
            r1 = res1x2(H, A)
            okdc = {"Home/Draw": r1 in ("Home", "Draw"), "Home/Away": r1 in ("Home", "Away"), "Draw/Away": r1 in ("Draw", "Away")}[dc]
            line = num(spec, "total"); okou = (T > line) if uo.startswith("Over") else (T < line)
            return "won" if (okdc and okou) else "lost"
        if mid in ("854", "856", "858"):
            line = num(spec, "total") or 2.5; r1 = res1x2(H, A)
            first = {"854": r1 == "Home", "856": r1 == "Draw", "858": r1 == "Away"}[mid]
            yes = first or T > line
            return "won" if yes == (p == "Yes") else "lost"
        if mid in ("60100", "60200", "60110"):
            r1 = res1x2(H, A)
            if mid == "60110":
                ok = {"Home or Draw": r1 in ("Home", "Draw"), "Home or Away": r1 in ("Home", "Away"), "Draw or Away": r1 in ("Draw", "Away")}[p]
            else:
                ok = r1 == p
            return "won" if ok else (settle_more(l, r, H, A, T, h1, h2) or "check")     # early payout could still have paid during the game
        if mid == "60020":   # any team 3+ goals in a row
            if p == "No" and H < 3 and A < 3: return "won"
            return settle_more(l, r, H, A, T, h1, h2) or "check"
        if mid == "60302":   # any team to lead by 3
            if p == "No" and abs(H - A) < 3 and max(H, A) < 3: return "won"
            if p == "Yes" and abs(H - A) >= 3: return "won"
            return settle_more(l, r, H, A, T, h1, h2) or "check"
        if mid == "60180":   # early goals (minutes unknown from final score)
            line = num(spec, "total")
            if p.startswith("Over") and T <= line: return "lost"
            if p.startswith("Under") and T < line: return "won"
            return settle_more(l, r, H, A, T, h1, h2) or "check"
        if mid == "85":
            if not h2: return "pending"
            r1 = res1x2(*h2)
            ok = {"Home or Draw": r1 in ("Home", "Draw"), "Home or Away": r1 in ("Home", "Away"), "Draw or Away": r1 in ("Draw", "Away")}[p]
            return "won" if ok else "lost"
        if mid == "810002":  # excluded number of goals - 1st half
            if not h1: return "pending"
            k = int(re.match(r"(\d+)", p).group(1)); t1 = sum(h1)
            hit = (t1 >= k) if "+" in p else (t1 == k)
            return "lost" if hit else "won"
        if mid == "900036":  # 1st half home team to win to nil
            if not h1: return "pending"
            yes = h1[0] > 0 and h1[1] == 0
            return "won" if yes == (p == "Yes") else "lost"
        r2 = settle_more(l, r, H, A, T, h1, h2)
        if r2: return r2
    except Exception:
        return "check"
    return "check"   # corners and other non-score markets


def settle_more(l, r, H, A, T, h1, h2):
    mid, spec, p = l["mid"], l["spec"], l["pick"]; yes = p == "Yes"
    yn = lambda cond: "won" if bool(cond) == yes else "lost"
    def eu(sc, hcp):      # European handicap a:b -> Home/Draw/Away of the adjusted score
        a, b = [float(v) for v in hcp.split(":")]
        return res1x2(sc[0] + a, sc[1] + b)
    def ah(sc, pick):
        hc = float(re.search(r"\(([-+]?[\d.]+)\)", pick).group(1))
        diff = (sc[0] - sc[1]) if pick.startswith("Home") else (sc[1] - sc[0])
        one = lambda x: "won" if diff + x > 0 else "lost" if diff + x < 0 else "void"
        if abs(hc * 4) % 2 == 1:
            a1, a2 = one(hc - 0.25), one(hc + 0.25)
            return a1 if a1 == a2 else ("half-won" if "won" in (a1, a2) else "half-lost")
        return one(hc)
    half = lambda: h1 is not None
    if mid == "31": return yn(A == 0)
    if mid == "32": return yn(H == 0)
    if mid == "33": return yn(H > A and A == 0)
    if mid == "34": return yn(A > H and H == 0)
    if mid in ("26", "27", "28"):
        v = T if mid == "26" else H if mid == "27" else A
        return "won" if (v % 2 == 0) == (p == "Even") else "lost"
    if mid == "14": return "won" if eu([H, A], spec.split("=")[1]) == p.split(" ")[0] else "lost"
    if mid == "60000": return yn(H >= 2 and A >= 2)
    if mid in ("860", "861", "862"):
        r1 = res1x2(H, A); first = {"860": "Home", "861": "Draw", "862": "Away"}[mid]
        return yn(r1 == first or (H > 0 and A > 0))
    if mid in ("863", "864", "865"):
        r1 = res1x2(H, A); first = {"863": "Home", "864": "Draw", "865": "Away"}[mid]
        return yn(r1 == first or H == 0 or A == 0)
    if mid in ("855", "857", "859"):
        line = num(spec, "total") or 2.5; r1 = res1x2(H, A); first = {"855": "Home", "857": "Draw", "859": "Away"}[mid]
        return yn(r1 == first or T < line)
    if mid == "900041": return yn(res1x2(H, A) != "Draw" and H > 0 and A > 0)
    if h1 is None: return None
    if mid in ("48", "49"):
        i = 0 if mid == "48" else 1
        return yn(h1[i] > h1[1 - i] and h2[i] > h2[1 - i])
    if mid in ("56", "57"):
        i = 0 if mid == "56" else 1
        return yn(h1[i] > 0 and h2[i] > 0)
    if mid == "58": return yn(sum(h1) > 1.5 and sum(h2) > 1.5)
    if mid == "75": return yn(h1[0] > 0 and h1[1] > 0)
    if mid == "95": return yn(h2[0] > 0 and h2[1] > 0)
    if mid == "900027": return yn(min(h1) > 0 and min(h2) > 0)
    if mid in ("64", "86"):
        sc = h1 if mid == "64" else h2; r1 = res1x2(*sc)
        return "void" if r1 == "Draw" else ("won" if r1 == p else "lost")
    if mid in ("65", "87"):
        sc = h1 if mid == "65" else h2
        return "won" if eu(sc, spec.split("=")[1]) == p.split(" ")[0] else "lost"
    if mid in ("66", "88"): return ah(h1 if mid == "66" else h2, p)
    if mid in ("69", "70"): return ou(h1[0] if mid == "69" else h1[1], num(spec, "total"), p)
    if mid in ("91", "92"): return ou(h2[0] if mid == "91" else h2[1], num(spec, "total"), p)
    if mid in ("74", "94"):
        v = sum(h1 if mid == "74" else h2)
        return "won" if (v % 2 == 0) == (p == "Even") else "lost"
    wtn = {"900036": (h1, 0), "900037": (h1, 1), "900038": (h2, 0), "900052": (h2, 1)}
    if mid in wtn:
        sc, i = wtn[mid]
        return yn(sc[i] > 0 and sc[1 - i] == 0)
    hoe = {"900032": (h1, 0), "900031": (h1, 1), "900029": (h2, 0), "900030": (h2, 1)}
    if mid in hoe:
        sc, i = hoe[mid]
        return "won" if (sc[i] % 2 == 0) == (p == "Even") else "lost"
    x = r.get("x")
    if not x: return None
    G = x.get("goals") or []
    if len(G) != T: return None            # timeline incomplete: leave for a manual check
    if mid == "900313":
        mins = num(spec, "minute"); v = sum(1 for g in G if g[0] <= mins)
        return ou(v, num(spec, "total"), p)
    if mid == "900069":
        mins = num(spec, "minute"); sc = [sum(1 for g in G if g[0] <= mins and g[1] == t) for t in ("home", "away")]
        return "won" if res1x2(*sc) == p else "lost"
    # running score, streaks and leads
    run = [0, 0]; maxlead = [0, 0]; streak = [0, 0]; best = [0, 0]; last = None; trail = [False, False]
    for g in G:
        i = 0 if g[1] == "home" else 1
        run[i] += 1
        streak[i] = streak[i] + 1 if last == i else 1; streak[1 - i] = 0; last = i
        best[i] = max(best[i], streak[i])
        maxlead[0] = max(maxlead[0], run[0] - run[1]); maxlead[1] = max(maxlead[1], run[1] - run[0])
        if run[0] < run[1]: trail[0] = True
        if run[1] < run[0]: trail[1] = True
    row = {"60010": (2, None), "60011": (2, 0), "60012": (2, 1), "60020": (3, None), "60021": (3, 0), "60022": (3, 1)}
    if mid in row:
        n, i = row[mid]
        return yn((max(best) if i is None else best[i]) >= n)
    if mid in {str(m) for m in range(60300, 60309)}:
        k = int(mid) - 60300; n = k % 3 + 1; who = k // 3      # 0 any, 1 home, 2 away
        hit = max(maxlead) >= n if who == 0 else maxlead[who - 1] >= n
        return yn(hit)
    if mid in ("60100", "60200"):
        n = 2 if mid == "60100" else 1; r1 = res1x2(H, A)
        if p == "Draw": return "won" if r1 == "Draw" else "lost"
        i = 0 if p == "Home" else 1
        return "won" if (r1 == p or maxlead[i] >= n) else "lost"
    if mid == "60210":
        i = 0 if p == "Home" else 1
        return "won" if (res1x2(H, A) == p) else ("lost" if trail[i] else "check")
    C = x.get("corners") or []; st = x.get("stats") or {}
    cor = {"900300": ("home", False), "900301": ("away", False), "900302": ("home", True), "900303": ("away", True), "166": (None, False), "177": (None, True)}
    if mid in cor:
        team, first = cor[mid]
        if not first and "corners" in st:
            v = st["corners"][0] if team == "home" else st["corners"][1] if team == "away" else sum(st["corners"])
        else:
            if not C: return None
            v = sum(1 for c in C if (team is None or c[1] == team) and (not first or c[0] <= 45))
        return ou(v, num(spec, "total"), p)
    statmk = {"900393": ("sot", None), "900546": ("sot", 0), "900547": ("sot", 1), "900342": ("fouls", None), "900544": ("fouls", 0),
              "900545": ("fouls", 1), "900396": ("offsides", None), "900568": ("offsides", 0), "900569": ("offsides", 1)}
    if mid in statmk:
        k, i = statmk[mid]
        if k not in st: return None
        return ou(sum(st[k]) if i is None else st[k][i], num(spec, "total"), p)
    if mid in ("139", "900304", "900305", "138"):
        if not {"yellow", "red", "yellowred"} <= set(st): return None
        y, yr, rd = st["yellow"], st["yellowred"], st["red"]
        if mid == "138":
            v = sum(y) * 10 + (sum(yr) + sum(rd)) * 25
        else:
            per = [y[i] + 2 * (yr[i] + rd[i]) for i in (0, 1)]    # SportyBet bookings: yellow 1, red 2
            v = sum(per) if mid == "139" else per[0] if mid == "900304" else per[1]
        line = num(spec, "total")
        if abs(v - line) <= (15 if mid == "138" else 1) and (sum(yr) + sum(rd)) > 0: return None   # red-card rules differ: check by hand
        return ou(v, line, p)
    return None


def fmt_odds(t):
    p = 1.0
    for l in t["legs"]:
        p *= l["odds"]
    return f"{p:,.2f}" if p < 1000 else f"{p:,.0f}"


# ---------------------------------------------------------------- check & report
def check():
    db = load_db()
    R = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    events = {}
    for s in db["sets"]:
        for t in s["tickets"]:
            for l in t["legs"]:
                events[l["eid"]] = l["kickoff"]
    todo = [(e, k) for e, k in events.items() if not (R.get(e, {}).get("final") and (R[e].get("void") or R[e].get("ht")))]
    with cf.ThreadPoolExecutor(8) as ex:
        for (e, k), rec in zip(todo, ex.map(lambda ek: fetch(ek[0], ek[1], R.get(ek[0])), todo)):
            if rec: R[e] = rec
    need = {l["eid"] for s in db["sets"] for t in s["tickets"] for l in t["legs"] if l["mid"] in TL_MIDS}
    todo = [e for e in need if R.get(e, {}).get("final") and not R[e].get("void") and "x" not in R[e]]
    with cf.ThreadPoolExecutor(8) as ex:
        for e, x in zip(todo, ex.map(extra, todo)):
            if x is not None: R[e]["x"] = x
    json.dump(R, open(CACHE, "w"))
    print(f"scores fetched for {sum(1 for e, k in events.items() if R.get(e, {}).get('final'))} of {len(events)} games ({len(todo)} timeline checks this run)")

    cls = {"won": "w", "half-won": "w", "lost": "l", "half-lost": "l", "void": "v", "pending": "p", "check": "c"}
    lab = {"won": "Won", "half-won": "Half won", "lost": "Lost", "half-lost": "Half lost", "void": "Void", "pending": "Not finished", "check": "Needs check"}
    totals = collections.Counter(); lines = [f"TICKET RESULTS - checked {dt.datetime.now(WAT).strftime('%a %d %b %H:%M')}", ""]
    body = []
    for s in sorted(db["sets"], key=lambda s: -s.get("added", 0)):
        cnt = collections.Counter(); rows = []
        for t in s["tickets"]:
            res = [settle(l, R.get(l["eid"])) for l in t["legs"]]
            c = collections.Counter(res)
            if c["lost"]: st = "LOST"
            elif c["pending"]: st = "PENDING"
            elif c["check"]: st = "CHECK"
            else: st = "WON"
            cnt[st] += 1; totals[st] += 1; rows.append((t, res, c, st))
        word = {"WON": "WON", "LOST": "LOST", "PENDING": "STILL RUNNING", "CHECK": "NEEDS CHECK"}
        lines.append(f"{s['name']}: {len(rows)} tickets | won {cnt['WON']} | lost {cnt['LOST']} | still running {cnt['PENDING']} | needs check {cnt['CHECK']}")
        lines.append(f"   {'#':>3}  {'CODE':<8}  {'TOTAL ODDS':>12}  {'RESULT':<13}  LEGS")
        for t, res, c, st in rows:
            lines.append(f"   {t['ticket']:>3}  [{t['code']}]  {fmt_odds(t):>12}  {word[st]:<13}  won {c['won']+c['half-won']}/{len(res)}"
                         + (f", lost {c['lost']+c['half-lost']}" if c['lost'] + c['half-lost'] else "") + (f", not finished {c['pending']}" if c['pending'] else "")
                         + (f", needs check {c['check']}" if c['check'] else ""))
        lines.append("")
        body.append((s, rows, cnt))
    lines.append(f"ALL: won {totals['WON']} | lost {totals['LOST']} | still running {totals['PENDING']} | needs check {totals['CHECK']}")
    open(os.path.join(HERE, "summary.txt"), "w").write("\n".join(lines) + "\n")

    h = ["<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>My Ticket Results</title>",
         "<style>:root{--bg:#f4f7f5;--card:#fff;--ink:#16201b;--muted:#5e6b65;--line:#dbe2de}"
         "@media (prefers-color-scheme:dark){:root{--bg:#0f1512;--card:#161e1a;--ink:#e3ebe7;--muted:#9aa8a2;--line:#2a3530}}"
         "body{font-family:-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--ink);margin:0;padding:16px}h1{margin:0 0 4px}p{color:var(--muted)}"
         ".s{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:12px 0;padding:10px}.s>summary{cursor:pointer;font-weight:600;font-size:15px}"
         ".t{border-left:5px solid #8a96a0;margin:6px 0;padding:4px 8px;background:var(--card)}.t.WON{border-left-color:#16804a}.t.LOST{border-left-color:#c0352b}.t.CHECK{border-left-color:#c9a227}"
         ".row{display:grid;grid-template-columns:42px 104px 110px 130px 1fr;gap:8px;align-items:center;cursor:pointer}"
         ".code{font-family:ui-monospace,Menlo,monospace;font-weight:700;font-size:15px;letter-spacing:.04em;padding:3px 8px;border-radius:6px;background:#fff3b0;color:#3d3300;border:1px solid #e6cf4f}"
         ".res{font-weight:700;padding:3px 8px;border-radius:6px;text-align:center}.WONr{background:#16804a;color:#fff}.LOSTr{background:#c0352b;color:#fff}.PENDINGr{background:#e3e8ec;color:#3b454d}.CHECKr{background:#c9a227;color:#221c00}"
         ".dots{display:flex;flex-wrap:wrap;gap:2px}.dots b{width:10px;height:10px;border-radius:2px;display:inline-block}.dw{background:#16804a}.dl{background:#c0352b}.dv{background:#7d8a93}.dp{background:#c4ccd2}.dc{background:#c9a227}"
         ".odds{font-weight:700;font-variant-numeric:tabular-nums;text-align:right}.odds small{font-weight:400;color:var(--muted);margin-left:4px}.legsum{font-size:12px;color:var(--muted)}table{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}td{padding:4px 6px;border-bottom:1px solid var(--line)}"
         "tr.w td{background:#e1f3e8}tr.l td{background:#fbe4e1}tr.v td{background:#eceff1}tr.p td{background:#f3f5f6}tr.c td{background:#f8efcc}"
         "@media (prefers-color-scheme:dark){.code{background:#4a3f00;color:#fff3b0;border-color:#8a7600}.PENDINGr{background:#2e373d;color:#cfd8de}"
         "tr.w td{background:#16301f}tr.l td{background:#3a1b18}tr.v td,tr.p td{background:#232b30}tr.c td{background:#3a3210}}"
         ".chip{padding:2px 9px;border-radius:9px;font-size:13px;font-weight:600;margin-right:4px}"
         "@media (max-width:520px){.row{grid-template-columns:34px 96px 1fr}.row>.res{grid-column:2/4}.row>span:last-child{grid-column:1/-1}}</style>",
         f"<h1>My Ticket Results</h1><p>Checked {html.escape(dt.datetime.now(WAT).strftime('%a %d %b %Y, %H:%M'))} WAT. Final scores from Sportradar (half-time from SportyBet's score data when Sportradar has none). Every leg is marked from the score, not from SportyBet's settlement.</p>",
         f"<p><span class='chip WONr'>Won {totals['WON']}</span><span class='chip LOSTr'>Lost {totals['LOST']}</span><span class='chip PENDINGr'>Still running {totals['PENDING']}</span>"
         + (f"<span class='chip CHECKr'>Needs check {totals['CHECK']}</span>" if totals['CHECK'] else "") + "</p>"]
    word = {"WON": "Won", "LOST": "Lost", "PENDING": "Still running", "CHECK": "Needs check"}
    for s, rows, cnt in body:
        h.append(f"<details class='s'><summary>{html.escape(s['name'])} — won {cnt['WON']}, lost {cnt['LOST']}, still running {cnt['PENDING']}" + (f", needs check {cnt['CHECK']}" if cnt['CHECK'] else "") + "</summary>")
        for t, res, c, st in rows:
            dots = "".join(f"<b class='d{cls[r]}'></b>" for r in res)
            legsum = f"won {c['won']+c['half-won']}/{len(res)}" + (f" · lost {c['lost']+c['half-lost']}" if c['lost'] + c['half-lost'] else "") + (f" · not finished {c['pending']}" if c['pending'] else "") + (f" · needs check {c['check']}" if c['check'] else "")
            h.append(f"<details class='t {st}'><summary><div class='row'><span>#{t['ticket']}</span><span class='code'>{t['code']}</span><span class='odds'>{fmt_odds(t)}<small>odds</small></span><span class='res {st}r'>{word[st]}</span>"
                     f"<span><span class='dots'>{dots}</span><span class='legsum'>{legsum}</span></span></div></summary><table>")
            for l, rr in sorted(zip(t["legs"], res), key=lambda z: z[0]["kickoff"]):
                r = R.get(l["eid"], {}); ft = r.get("ft"); ht = r.get("ht")
                score = (f"FT {ft[0]}:{ft[1]}" if ft else "") + (f" (HT {ht[0]}:{ht[1]})" if ht else "") + (" – no half scores, possibly awarded" if r.get("doubtful") else "")
                ko = dt.datetime.fromtimestamp(l["kickoff"], WAT).strftime("%a %d %H:%M")
                h.append(f"<tr class='{cls[rr]}'><td>{ko}</td><td>{html.escape(l['match'])}</td><td>{html.escape(l['mdesc'])}{(' '+html.escape(l['spec'].replace('total=','').replace('hcp=',''))) if l['spec'] and 'Asian' not in l['mdesc'] else ''} – {html.escape(l['pick'])} @{l['odds']}</td><td>{score}</td><td><b>{lab[rr]}</b></td></tr>")
            h.append("</table></details>")
        h.append("</details>")
    open(os.path.join(HERE, "report.html"), "w").write("\n".join(h))
    msg = f"Tickets: won {totals['WON']}, lost {totals['LOST']}, running {totals['PENDING']}"
    try:   # Mac notification; skipped where there is no osascript (the GitHub cloud runner)
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "My ticket results"'], capture_output=True)
    except OSError:
        pass
    print(lines[-1])


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "add":
        add(sys.argv[2], sys.argv[3:])
    elif len(sys.argv) >= 2 and sys.argv[1] == "check":
        check()
    else:
        print(__doc__)
