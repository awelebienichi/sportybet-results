#!/usr/bin/env python3
"""Basketball tickets (separate tab on build.html). Every upcoming game on SportyBet is judged from both teams' last 10 games
(Sportradar): expected points for each side (its own scoring and the rival's conceding) plus a home edge, then the chance of each
line from a normal spread. The numbers below were measured on 3,799 finished games (teams' earlier games only): home edge 3.65
points, the final margin varies by 15.1 points around the prediction, the total by 17.9, each team's points by 11.7 – and with
those, the model's chances matched what really happened (handicaps it put at 80-85% came true 85%, totals at 90-95% came true 93%).
Markets: 219 winner (incl. OT), 223 handicap (incl. OT), 225 total points (incl. OT), 227 / 228 home / away team points."""
import datetime as dt, json, math, re, time
from concurrent.futures import ThreadPoolExecutor
import phonebuild as P
import bbsources

HCA, SDM, SDT, SDTEAM = 3.65, 15.1, 17.9, 11.7
LEVELS = {"strict": (.85, .80), "normal": (.80, .76), "relaxed": (.75, .72)}      # stats chance, chance after the bookmaker's price
Phi = lambda z: 0.5 * (1 + math.erf(z / math.sqrt(2)))
KINDS = {"mix": None, "win": ("219",), "hcp": ("223",), "total": ("225",), "team": ("227", "228")}
NAMES = {"mix": "basketball – best market per game", "win": "basketball – winners", "hcp": "basketball – handicap",
         "total": "basketball – total points", "team": "basketball – team points"}
_G = {"at": 0, "list": []}

def games():
    """every upcoming basketball game (cached 15 minutes)"""
    if time.time() - _G["at"] < 900 and _G["list"]: return _G["list"]
    out = {}
    for p in range(1, 6):
        d = P.curl(["-H", "Accept: application/json", "-H", "Referer: https://www.sportybet.com/ng/",
                    f"https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A2&marketId=219&pageSize=100&pageNum={p}&option=1"], 120) or {}
        T = (d.get("data") or {}).get("tournaments") or []
        for t in T:
            for e in t.get("events", []):
                cat, trn = e["sport"]["category"]["name"], e["sport"]["category"]["tournament"]["name"]
                if "Simulated" in cat or "SRL" in trn or "SRL" in e["homeTeamName"] + e["awayTeamName"]: continue     # simulated games: never (user's decision)
                out[e["eventId"]] = dict(eid=e["eventId"], ts=e["estimateStartTime"] / 1000, h=e["homeTeamName"], a=e["awayTeamName"],
                                         lg=e["sport"]["category"]["name"] + " - " + e["sport"]["category"]["tournament"]["name"],
                                         hid=e["homeTeamId"].split(":")[-1], aid=e["awayTeamId"].split(":")[-1], x={}, ou={})
        if not T or sum(len(t.get("events", [])) for t in T) < 100: break
    _G.update(at=time.time(), list=sorted(out.values(), key=lambda g: g["ts"]))
    return _G["list"]

def _within(keys, fn, secs, workers=12):
    """run fn on every key in parallel, but give up waiting after `secs`: a slow stats source must never block the builder"""
    from concurrent.futures import ThreadPoolExecutor, wait
    ex = ThreadPoolExecutor(workers); fut = {ex.submit(fn, k): k for k in keys}
    done, _ = wait(fut, timeout=secs); ex.shutdown(wait=False, cancel_futures=True)
    out = {}
    for f in done:
        try: out[fut[f]] = f.result()
        except Exception: pass
    return out

def teams(G, secs=60):
    T = sorted({t for g in G for t in (g["hid"], g["aid"])})
    return _within(T, P.lastx, secs)

def warm():
    """listener warm-up: the game list and every team's last 10, so a basketball build only needs prices"""
    G = games(); teams(G, 600); return len(G)

def expect(H, A):
    """expected points for home and away from both teams' last games (own scoring + the rival's conceding) and the home edge"""
    pf = lambda L: sum(m["gf"] for m in L) / len(L); pa = lambda L: sum(m["ga"] for m in L) / len(L)
    mh = (pf(H) + pa(A)) / 2 + HCA / 2; ma = (pf(A) + pa(H)) / 2 - HCA / 2
    return mh, ma

def chance(mid, spec, oid, mh, ma):
    d, t = mh - ma, mh + ma
    if mid == "219": p = Phi(d / SDM); return p if oid == "4" else 1 - p
    v = float(spec.split("=")[1]) if "=" in spec and "|" not in spec else None
    if v is None: return None
    if mid == "223":                       # home handicap v: home covers if margin + v > 0
        p = 1 - Phi((-v - d) / SDM); return p if oid == "1714" else 1 - p
    if mid == "225": p = 1 - Phi((v - t) / SDT); return p if oid == "12" else 1 - p
    if mid in ("227", "228"):
        m = mh if mid == "227" else ma; p = 1 - Phi((v - m) / SDTEAM); return p if oid == "12" else 1 - p
    return None

def label(g, mid, spec, oid):
    v = spec.split("=")[1] if "=" in spec else ""
    if mid == "219": return f"{g['h'] if oid == '4' else g['a']} to win (incl. OT)"
    if mid == "223":
        x = float(v) if oid == "1714" else -float(v); return f"{g['h'] if oid == '1714' else g['a']} {'+' if x > 0 else ''}{x:g} (handicap)"
    if mid == "225": return f"{'Over' if oid == '12' else 'Under'} {v} total points"
    if mid in ("227", "228"): return f"{g['h'] if mid == '227' else g['a']} {'Over' if oid == '12' else 'Under'} {v} points"
    return f"{mid} {spec} {oid}"

def build(req):
    kind = req.get("kind", "mix"); kind = kind if kind in KINDS else "mix"
    legs = max(1, min(int(req.get("legs", 10)), 50)); LV = LEVELS.get(req.get("strict", "normal"), LEVELS["normal"])
    LAG = dt.timezone(dt.timedelta(hours=1)); now = time.time()
    G = [g for g in games() if g["ts"] > now + 10 * 60]
    days = sorted({d for d in (req.get("days") or []) if re.fullmatch(r"\d{4}-\d\d-\d\d", str(d))})
    if days: G = [g for g in G if dt.datetime.fromtimestamp(g["ts"], LAG).date().isoformat() in days]
    span = ", ".join(dt.date.fromisoformat(d).strftime("%a %d") for d in days) or "next days"
    def _m(x):
        m = re.fullmatch(r"(\d{1,2}):(\d\d)", str(x or "")); return int(m.group(1)) * 60 + int(m.group(2)) if m else None
    f_, t_ = _m(req.get("from")), _m(req.get("to"))
    if f_ is not None and t_ is not None and not (f_ == 0 and t_ >= 23 * 60 + 59):
        def _in(g):
            w = dt.datetime.fromtimestamp(g["ts"], LAG); m = w.hour * 60 + w.minute
            return f_ <= m <= t_ if f_ <= t_ else (m >= f_ or m <= t_)
        G = [g for g in G if _in(g)]; span += f", {req['from']}–{req['to']}"
    if not G: return dict(ok=False, msg=f"No basketball games on SportyBet for {span}. SportyBet usually lists basketball 2–4 days ahead.")
    LX = teams(G, 60)
    G = [g for g in G if len(LX.get(g["hid"], [])) >= 8 and len(LX.get(g["aid"], [])) >= 8]      # strong data only
    if not G: return dict(ok=False, msg=f"None of the basketball games for {span} have enough recent results for both teams.")
    MK = _within([g["eid"] for g in G], lambda e: P.event_game(e)[1], 45)
    want = KINDS[kind]; best = []; cand = []
    for g in G:
        H, A = LX[g["hid"]], LX[g["aid"]]; mh, ma = expect(H, A)
        rest = []
        if bbsources.tired(g["hid"], g["ts"]): mh -= 1.0; rest.append(f"{g['h']} on 2nd game in 2 days")    # measured on 5,343 games: ~-1 point
        if bbsources.tired(g["aid"], g["ts"]): ma -= 1.0; rest.append(f"{g['a']} on 2nd game in 2 days")
        opts = []
        for (mid, spec, oid), od in MK.get(g["eid"], {}).items():
            if mid not in ("219", "223", "225", "227", "228") or (want and mid not in want): continue
            if not 1.08 <= od <= 1.75: continue
            p = chance(mid, spec, oid, mh, ma)
            if p is None or p < LV[0]: continue
            pb = (p + 1 / (od * 1.06)) / 2                                    # the stats and the bookmaker's own price must agree
            if pb < LV[1]: continue
            if mid == "219":                                                  # a winner: the team must also win most of its own games
                me = H if oid == "4" else A
                if sum(m["gf"] > m["ga"] for m in me) / len(me) < .6: continue
            opts.append(dict(eid=g["eid"], mid=mid, spec=spec, oid=oid, pick=label(g, mid, spec, oid), odds=od, ts=g["ts"], game=f"{g['h']} vs {g['a']}", lg=g["lg"],
                             rate=pb, s=P.edge(pb, od),
                             why=f"expected {g['h']} {mh:.0f} – {g['a']} {ma:.0f} (last {len(H)}/{len(A)} games, home edge included" + (", " + ", ".join(rest) if rest else "") + f"); {p:.0%} by the stats, {pb:.0%} with the price"))
        if opts:
            # one leg per game: best value with a high chance; total points (incl. overtime) lead, then team points, handicaps last
            opts.sort(key=lambda o: -(o["s"] + o["rate"] * .5 + {"225": .06, "227": .03, "228": .03, "223": -.02}.get(o["mid"], 0)))
            cand.append((g, opts))
    # every candidate goes through the extra checks (ESPN injuries, Flashscore home/away form, NBA ratings), best first per game
    strict = req.get("strict", "normal") == "strict"
    def judge(item):
        g, opts = item; nba = "NBA" in g["lg"] and "G League" not in g["lg"]; ok = []
        for o in opts[:4]:
            try: sup, bad = bbsources.check(g, o, nba)
            except Exception: sup, bad = [], []
            if bad or (strict and not sup): continue
            if sup: o["why"] += "; " + ", ".join(sup)
            ok.append(o)
            if len(ok) == 2: break
        return g["eid"], ok
    res = {}
    with ThreadPoolExecutor(8) as ex:
        futs = {ex.submit(judge, c): c for c in cand}
        for f in futs:
            try: eid, ok = f.result(timeout=90)
            except Exception: continue
            if ok:
                top = ok[0]
                alt = next((o for o in ok if o["mid"] != "223"), None)
                if alt and alt is not top: top["alt"] = alt
                best.append(top)
    best.sort(key=lambda o: (-o["rate"], -o["s"]))
    if kind == "mix":                                                         # handicaps: at most half the ticket, other markets fill the rest
        cap, n, keep = max(1, legs // 2), 0, []
        for o in best:
            if o["mid"] == "223":
                if n >= cap:
                    if o.get("alt"): keep.append(o["alt"])
                    continue
                n += 1
            o.pop("alt", None); keep.append(o)
        best = sorted(keep, key=lambda o: (-o["rate"], -o["s"]))
    for o in best: o.pop("alt", None)
    if not best: return dict(ok=False, msg=f"No basketball game passed the checks for {span} ({len(G)} games judged). Try Normal/Relaxed or more days.")
    target = float(req.get("target") or 0); note = []
    if target > 1:
        T, o = [], 1.0
        for leg in best:
            if len(T) >= legs or o >= target: break
            T.append(leg); o *= leg["odds"]
        if o < target:
            allo = math.prod(l["odds"] for l in best[:legs])
            return dict(ok=False, msg=f"{target:g} odds can't be reached: {len(best)} basketball legs pass the checks ({span}); with up to {legs} legs the most is {allo:.2f} odds. Lower the target or allow more legs.")
        B = T
    else:
        B = best[:legs]
        if len(B) < legs: note.append(f"Only {len(B)} basketball games passed the checks ({span}, {len(G)} judged), so the ticket has {len(B)} legs.")
    if req.get("strict", "normal") != "strict":
        note.append({"normal": "Normal: in testing about 85 in 100 such legs came true.", "relaxed": "Relaxed: in testing about 80 in 100 such legs came true."}.get(req.get("strict"), ""))
    return P.finish([B], "bb", {"bb": NAMES[kind]}, [n for n in note if n], req)
