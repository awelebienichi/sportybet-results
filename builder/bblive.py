#!/usr/bin/env python3
"""Live (in-play) basketball tickets – completely separate from football live (live.py is never touched by this).
Each game in play: the points still to come = each side's pre-game expected points (bball.expect: last 10 games, home edge) times
the share of the game left; the spread of what is left = the full-game spread x sqrt(share left).
Replayed on 2,819 finished games at the end of each quarter: using this game's own scoring pace made it worse (as in football), and
with the settings above the chances matched reality (said 90-95% -> totals 94%, handicaps 93%, winners 91% came true)."""
import math, re, time
from concurrent.futures import ThreadPoolExecutor
import phonebuild as P
import bball

LEVELS = {"strict": (.90, .84), "normal": (.86, .80), "relaxed": (.82, .76)}       # stats chance, chance after the bookmaker's price
Phi = lambda z: 0.5 * (1 + math.erf(z / math.sqrt(2)))
MARKETS = ("219", "223", "225", "227", "228", "236", "756", "757")

def _feed():
    out = {}
    d = P.curl(["-H", "Accept: application/json", "-H", "Referer: https://www.sportybet.com/ng/",
                "https://www.sportybet.com/api/ng/factsCenter/liveOrPrematchEvents?sportId=sr%3Asport%3A2&marketId=219"], 60) or {}
    for t in d.get("data") or []:
        for e in t.get("events", []):
            cat, trn = e["sport"]["category"]["name"], e["sport"]["category"]["tournament"]["name"]
            if "Simulated" in cat or "SRL" in trn or "SRL" in e["homeTeamName"] + e["awayTeamName"]: continue      # never simulated games
            out[e["eventId"]] = e
    return list(out.values())

def _state(e):
    """(home, away, minutes played, game length) or None when the game isn't in a state we judge"""
    m = re.match(r"(\d+):(\d+)", str(e.get("playedSeconds") or ""))
    try: h, a = [int(x) for x in str(e.get("setScore") or "").split(":")]
    except Exception: return None
    if not m: return None
    played = int(m.group(1)) + int(m.group(2)) / 60
    length = 48 if "NBA" in e["sport"]["category"]["tournament"]["name"] and "G League" not in e["sport"]["category"]["tournament"]["name"] else 40
    if played <= 0 or played >= length or "OT" in str(e.get("matchStatus", "")).upper(): return None   # not started / overtime: skip
    return h, a, played, length

def _odds(eid):
    d = P.curl(["-H", "Accept: application/json", "-H", "Referer: https://www.sportybet.com/ng/",
                f"https://www.sportybet.com/api/ng/factsCenter/event?eventId={eid}&productId=1"], 40) or {}
    mk = {}
    for m in (d.get("data") or {}).get("markets", []):
        if m["id"] not in MARKETS or m.get("status", 0) != 0: continue
        for o in m.get("outcomes", []):
            if o.get("isActive"): mk[(m["id"], m.get("specifier") or "", o["id"])] = float(o["odds"])
    return mk

def _quarter(mid, spec, oid, e, played, length, mh, ma):
    """a quarter's total (236) or one team's quarter points (756/757) in play: points already scored in that quarter plus each side's
    expected points for the part of it still to come; quarters already finished are skipped"""
    S = bball._parts(spec)
    try: v, n = float(S["total"]), int(S["quarternr"])
    except Exception: return None
    ql = length / 4; gs = e.get("gameScore") or []
    into = played - (n - 1) * ql
    if into >= ql or n < 1 or n > 4: return None                                # that quarter is over
    rq = 1.0 if into <= 0 else 1 - into / ql
    if into > 0:
        try: qh, qa = [int(x) for x in gs[n - 1].split(":")]
        except Exception: return None
    else: qh = qa = 0
    sh = bball.QSH[n - 1]
    if mid == "236": m, sd = qh + qa + (mh + ma) * sh * rq, bball.QSD[n - 1] * math.sqrt(rq)
    else: m, sd = ((qh + mh * sh * rq) if mid == "756" else (qa + ma * sh * rq)), bball.QSDT * math.sqrt(rq)
    if rq < .15: return None                                                    # the last moments of a quarter: prices are frozen/suspended anyway
    p = 1 - Phi((v - m) / sd); return p if oid == "12" else 1 - p

def _chance(mid, spec, oid, fh, fa, r):
    d, t = fh - fa, fh + fa; sm, st, stm = bball.SDM * math.sqrt(r), bball.SDT * math.sqrt(r), bball.SDTEAM * math.sqrt(r)
    if mid == "219": p = Phi(d / sm); return p if oid == "4" else 1 - p
    if "=" not in spec or "|" in spec: return None
    v = float(spec.split("=")[1])
    if mid == "223": p = 1 - Phi((-v - d) / sm); return p if oid == "1714" else 1 - p
    if mid == "225": p = 1 - Phi((v - t) / st); return p if oid == "12" else 1 - p
    m = fh if mid == "227" else fa; p = 1 - Phi((v - m) / stm); return p if oid == "12" else 1 - p

def build(req):
    LV = LEVELS.get(req.get("strict", "normal"), LEVELS["normal"]); legs = max(1, min(int(req.get("legs", 4)), 40))
    E = [(e, s) for e in _feed() for s in [_state(e)] if s]
    if not E: return dict(ok=False, msg="No basketball games are in play right now (or only in overtime). Most live basketball is in the evening and at night (Nigeria time).")
    G = [dict(eid=e["eventId"], ts=e["estimateStartTime"] / 1000, h=e["homeTeamName"], a=e["awayTeamName"], hid=e["homeTeamId"].split(":")[-1],
              aid=e["awayTeamId"].split(":")[-1], lg=e["sport"]["category"]["name"] + " - " + e["sport"]["category"]["tournament"]["name"], st=s, e=e) for e, s in E]
    G = [g for g in G if .1 <= g["st"][2] / g["st"][3] <= .92]                    # from 10% of the game played to the last few minutes
    LX = bball.teams(G, 45)
    G = [g for g in G if len(LX.get(g["hid"], [])) >= 8 and len(LX.get(g["aid"], [])) >= 8]
    if not G: return dict(ok=False, msg=f"{len(E)} basketball games are in play, but none has enough recent results for both teams to judge.")
    MK = bball._within([g["eid"] for g in G], _odds, 40)
    per = []
    for g in G:
        h, a, played, length = g["st"]; r = 1 - played / length
        mh, ma = bball.expect(LX[g["hid"]], LX[g["aid"]]); fh, fa = h + mh * r, a + ma * r
        opts = []
        for (mid, spec, oid), od in (MK.get(g["eid"]) or {}).items():
            if not 1.03 <= od <= 1.75: continue
            p = _quarter(mid, spec, oid, g["e"], played, length, mh, ma) if mid in ("236", "756", "757") else _chance(mid, spec, oid, fh, fa, r)
            if p is None or p < LV[0]: continue
            pb = (p + 1 / (od * 1.06)) / 2                                        # the stats and the bookmaker's own price must agree
            if pb < LV[1]: continue
            q = int(played // (length / 4)) + 1
            opts.append(dict(eid=g["eid"], mid=mid, spec=spec, oid=oid, pick=bball.label(g, mid, spec, oid), odds=od, ts=g["ts"], game=f"{g['h']} vs {g['a']}",
                             lg=g["lg"], rate=pb, s=P.edge(pb, od),
                             why=f"LIVE {h}-{a}, {played:.0f} of {length} min: expected final {fh:.0f}-{fa:.0f}; {p:.0%} by the stats, {pb:.0%} with the price"))
        if opts:   # one leg per game; total points lead (strongest market in testing), handicaps last
            opts.sort(key=lambda o: -(o["s"] + o["rate"] * .5 + {"225": .06, "236": .04, "227": .03, "228": .03, "756": .02, "757": .02, "223": -.02}.get(o["mid"], 0)))
            per.append(opts)
    if not per: return dict(ok=False, msg=f"{len(G)} basketball games were judged in play, but none reached the bar right now. Try Normal/Relaxed or again in a few minutes.")
    per.sort(key=lambda L: (-L[0]["rate"], -L[0]["s"]))
    cap, nh, B = max(1, legs // 2), 0, []
    for L in per:                                                               # handicaps at most half the ticket
        o = next((x for x in L if x["mid"] != "223" or nh < cap), None)
        if not o: continue
        nh += o["mid"] == "223"; B.append(o)
    target = float(req.get("target") or 0); note = []
    if target > 1:
        T, prod = [], 1.0
        for o in B:
            if len(T) >= legs or prod >= target: break
            T.append(o); prod *= o["odds"]
        if prod < target:
            return dict(ok=False, msg=f"{target:g} odds can't be reached right now: {len(B)} live basketball legs pass, the most they make with up to {legs} legs is {math.prod(o['odds'] for o in B[:legs]):.2f}.")
        B = T
    else:
        B = B[:legs]
        if len(B) < legs: note.append(f"Only {len(B)} live basketball leg(s) were strong enough right now (you asked for {legs}).")
    note.append("Live odds move fast – load the code on SportyBet straight away.")
    return P.finish([B], "bblive", {"bblive": "live basketball"}, note, req)
