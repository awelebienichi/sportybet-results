#!/usr/bin/env python3
"""Live (in-play) tickets: every game in play is scored with the builder's pre-match knowledge of both teams (Sportradar last 10,
season record, the extra sources) combined with the live score, minute and odds. Probabilities come from the teams' expected
goals for the time that is left (Poisson), so a leg is only taken when the chance is high (75%+) and the price is not a coin flip.
Strategies: goals (another goal coming), hold (a strong favourite protecting a lead late on), dead (a quiet low-scoring game),
mix (the best of all three)."""
import json, math, re, time
from concurrent.futures import ThreadPoolExecutor
import phonebuild as P

def _live_list():
    out = {}
    for m in ("1", "18"):
        d = P.curl(["-H", "Accept: application/json", "-H", "Referer: https://www.sportybet.com/ng/",
                    f"https://www.sportybet.com/api/ng/factsCenter/liveOrPrematchEvents?sportId=sr%3Asport%3A1&marketId={m}"], 60) or {}
        for t in d.get("data") or []:
            for e in t.get("events", []):
                out.setdefault(e["eventId"], e)
    return list(out.values())

def _minute(e):
    s = str(e.get("playedSeconds") or "")
    m = re.match(r"(\d+):", s)
    if m: return int(m.group(1))
    st = str(e.get("matchStatus") or "").lower()
    return 45 if ("half" in st and "time" in st) or st == "ht" else None

def _in_play(e):
    st = str(e.get("matchStatus") or "").lower()
    if "not start" in st or "ended" in st or "end" == st: return False
    if "simulated" in e["sport"]["category"]["name"].lower() or "srl" in e["sport"]["category"]["tournament"]["name"].lower(): return False
    return _minute(e) is not None

def _score(e):
    try: h, a = [int(x) for x in str(e.get("setScore") or "0:0").split(":")]; return h, a
    except Exception: return 0, 0

def _pois_le(k, lam):
    """P(X <= k) for a Poisson(lam)"""
    return sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(k + 1))

def _pois(k, lam): return math.exp(-lam) * lam ** k / math.factorial(k)

DISP = 1.3       # tested on 696 finished matches replayed at 15'-75': with this extra spread the chances match what really happened
def _final_dist(hs, as_, lh, la, cap=12):
    """{(home goals, away goals) at full time: probability}. Each side's remaining goals: negative binomial with the expected
    count as its mean – real games end lopsided (5-1, 6-0) more often than a plain Poisson says, which had made the model overconfident"""
    def pm(l):
        if l <= 0: return [1.0] + [0.0] * (cap - 1)
        r = l / (DISP - 1); q = r / (r + l)
        return [math.exp(math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1) + r * math.log(q) + k * math.log(1 - q)) for k in range(cap)]
    ph, pa = pm(lh), pm(la)
    return {(hs + i, as_ + j): ph[i] * pa[j] for i in range(cap) for j in range(cap)}

def _hcp_p(D, mid, spec, oid):
    """chance a handicap outcome wins (Asian whole-line pushes count as not winning)"""
    if mid == "14":
        x, y = [int(v) for v in spec.split("=")[1].split(":")]
        f = {"1711": lambda h, a: h + x > a + y, "1712": lambda h, a: h + x == a + y, "1713": lambda h, a: a + y > h + x}[oid]
    else:
        v = float(spec.split("=")[1])
        f = (lambda h, a: h - a + v > 0) if oid == "1714" else (lambda h, a: a - h - v > 0)
    return sum(p for (h, a), p in D.items() if f(h, a))

KIND = {"14": "handicap", "16": "handicap", "1": "hold", "10": "hold", "18": "goals", "19": "goals", "20": "goals", "68": "goals", "29": "goals"}

def _p(D, D1, mid, spec, oid, lead):
    """chance an outcome wins from the score distributions; None for markets this does not judge"""
    if "|" in spec: return None                                                 # "rest of match" style lines (settled from the current score)
    ln = float(spec.split("=")[1]) if spec.startswith("total=") else None
    ev = lambda Dx, f: sum(p for (h, a), p in Dx.items() if f(h, a))
    if mid == "16": return None                                                 # Asian handicap: not used (user's choice, 11 Nov)
    if mid in ("14", "16") and spec.startswith("hcp="):
        if mid == "14" and oid == "1712": return None                          # handicap draws: too narrow
        return _hcp_p(D, mid, spec, oid)
    if mid == "1":
        if oid == "2" or (oid == "1" and lead < 1) or (oid == "3" and lead > -1): return None   # a win only for the side in front; no live draws
        return ev(D, (lambda h, a: h > a) if oid == "1" else (lambda h, a: a > h))
    if mid == "10":
        if (oid == "9" and lead < 0) or (oid == "11" and lead > 0): return None   # never the side that is behind
        return ev(D, {"9": lambda h, a: h >= a, "11": lambda h, a: a >= h, "10": lambda h, a: h != a}[oid])
    if mid == "18" and ln is not None and oid in ("12", "13"):
        return ev(D, (lambda h, a: h + a > ln) if oid == "12" else (lambda h, a: h + a < ln))
    if mid in ("19", "20") and ln is not None and oid in ("12", "13"):
        t = (lambda h, a: h) if mid == "19" else (lambda h, a: a)
        return ev(D, (lambda h, a: t(h, a) > ln) if oid == "12" else (lambda h, a: t(h, a) < ln))
    if mid == "68" and ln is not None and D1 and oid in ("12", "13"):         # 1st half total, only while the 1st half is being played
        return ev(D1, (lambda h, a: h + a > ln) if oid == "12" else (lambda h, a: h + a < ln))
    if mid == "29" and oid in ("74", "76"):
        return ev(D, (lambda h, a: h > 0 and a > 0) if oid == "74" else (lambda h, a: not (h > 0 and a > 0)))
    return None

def _label(g, mid, spec, oid):
    if mid in ("14", "16"): return _hcp_label(g, mid, spec, oid)
    ln = spec.split("=")[1] if spec.startswith("total=") else ""
    if mid == "1": return f"{g['h'] if oid == '1' else g['a']} to win"
    if mid == "10": return {"9": f"{g['h']} or draw", "11": f"{g['a']} or draw", "10": f"{g['h']} or {g['a']}"}[oid]
    if mid == "18": return f"{'Over' if oid == '12' else 'Under'} {ln}"
    if mid in ("19", "20"): return f"{g['h'] if mid == '19' else g['a']} {'Over' if oid == '12' else 'Under'} {ln}"
    if mid == "68": return f"1st half {'Over' if oid == '12' else 'Under'} {ln}"
    if mid == "29": return "GG – Yes" if oid == "74" else "GG – No"
    return f"{mid}/{spec}/{oid}"

def _hcp_label(g, mid, spec, oid):
    if mid == "14":
        x, y = spec.split("=")[1].split(":")
        side = {"1711": g["h"], "1712": "Draw", "1713": g["a"]}[oid]
        return f"Handicap {x}:{y} – {side}"
    v = float(spec.split("=")[1]); v = v if oid == "1714" else -v
    return f"Asian handicap {g['h'] if oid == '1714' else g['a']} {'+' if v > 0 else ''}{v:g}"

def _live_odds(eid):
    d = P.curl(["-H", "Accept: application/json", "-H", "Referer: https://www.sportybet.com/ng/",
                f"https://www.sportybet.com/api/ng/factsCenter/event?eventId={eid}&productId=1"], 60) or {}
    mk = {}
    for m in (d.get("data") or {}).get("markets", []):
        if m.get("status", 0) != 0: continue                                  # suspended right now
        for o in m.get("outcomes", []):
            if o.get("isActive"): mk[(m["id"], m.get("specifier") or "", o["id"])] = float(o["odds"])
    return mk

def _prematch(g, mid, oid, spec):
    """the builder's pre-match sources on the same idea as the live leg: a lead held -> that side to win; another goal -> Over 2.5;
    a quiet game -> Under 2.5. Any source clearly against it (Flashscore venue form under 50%, footybets, FotMob/DFP/Vitibet,
    season table, head-to-head) blocks the leg; the ones for it are listed in the reason."""
    import extra, scores, sources, season
    ln = float(spec.split("=")[1]) if spec.startswith("total=") else None
    if mid in ("14", "16", "19", "20"): return _own_record(g, mid, oid, spec, ln)
    if mid in ("14", "16"):                                                     # the backed side: needs to outscore from here -> its win record, else its not-lose record
        back_h = oid in ("1711", "1714"); hs, as_ = g["score"]
        if mid == "16": v = float(spec.split("=")[1]); cushion = (hs - as_ + v) if back_h else (as_ - hs - v)
        else:
            x, y = [int(t) for t in spec.split("=")[1].split(":")]; cushion = (hs + x - as_ - y) if back_h else (as_ + y - hs - x)
        mid, oid = ("10", "9" if back_h else "11") if cushion > 0 else ("1", "1" if back_h else "3")
        spec = ""
    if mid == "10": proxy = dict(mid="10", oid=oid, spec="")                    # not-lose record (9 home or draw, 11 draw or away)
    elif mid == "1": proxy = dict(mid="1", oid=oid, spec="")
    elif mid == "29": proxy = dict(mid="29", oid=oid, spec="")
    elif oid == "12": proxy = dict(mid="18", oid="12", spec=f"total={min(max(ln, 1.5), 2.5)}")     # Over 0.5/1.5 legs -> the teams' Over 1.5 record
    else: proxy = dict(mid="18", oid="13", spec=f"total={max(ln, 2.5) if ln <= 3.5 else 3.5}")
    p = dict(proxy, eid=g["eid"], ts=g["ts"], game=f"{g['h']} vs {g['a']}", pick="", lg=g["lg"])
    sup, bad = [], []
    try:
        fid = scores.match_fid(dict(k=g["eid"] + "|x", game=p["game"], ts=g["ts"]))
        F = extra.fs_form(fid) if fid else None
        fr = extra.fs_rate(p, F) if F else None
        if fr is not None: (bad if fr < .5 else sup).append(f"Flashscore home/away form {fr:.0%}")
    except Exception: pass
    try:
        fs, fb = extra.fb_verdict(p, extra.fb_for(p))
        if fb: bad.append("footybets against")
        elif fs: sup.append(f"footybets {fs}%")
    except Exception: pass
    for fn in (lambda: sources.check(p), lambda: season.check(p, g)):
        try: a, b = fn(); sup += a; bad += b
        except Exception: pass
    try:                                                                         # head-to-head (last 8 meetings)
        hh = P.h2h(g["hid"], g["aid"])
        if len(hh) >= 3:
            if mid == "10":
                lost = sum((a > h) if proxy["oid"] == "9" else (h > a) for h, a in hh) / len(hh)
                if lost >= .6: bad.append(f"h2h: lost {lost:.0%} of {len(hh)} meetings")
            elif mid == "1":
                lost = sum((a > h) if proxy["oid"] == "1" else (h > a) for h, a in hh) / len(hh)
                if lost >= .5: bad.append(f"h2h: lost {lost:.0%} of {len(hh)} meetings")
            elif proxy["mid"] == "18":
                pl = float(proxy["spec"].split("=")[1]); ov = sum(h + a > pl for h, a in hh) / len(hh); r = ov if oid == "12" else 1 - ov
                (bad if r < .4 else sup if r >= .6 else []).append(f"h2h {r:.0%} of {len(hh)} meetings ({proxy['spec'].split('=')[1]} line)")
    except Exception: pass
    return sup, bad

def _own_record(g, mid, oid, spec, ln):
    """handicaps and team goals have their own test – a 1X2 or match-total proxy would judge the wrong thing
    (e.g. 'Romania +2.5' only needs Romania not to lose by 3+, not to avoid defeat).
    Handicap: how often each side's recent games (and the meetings) ended with a margin that clears the line.
    Team goals: that team's own scoring and the rival's conceding against the line."""
    LX = P.fetch_teams([g]); H, A = LX.get(g["hid"], []), LX.get(g["aid"], [])
    if len(H) < 6 or len(A) < 6: return [], ["not enough recent games"]
    f = lambda L, c: sum(1 for m in L if c(m)) / len(L)
    hh = P.h2h(g["hid"], g["aid"])
    if mid in ("14", "16"):
        if mid == "16": v = float(spec.split("=")[1]); need = -v                          # home margin must be > need (home side) / < need (away side)
        else:
            x, y = [int(t) for t in spec.split("=")[1].split(":")]; need = y - x
        home_side = oid in ("1711", "1714")
        ok_h = (lambda m: m["gf"] - m["ga"] > need) if home_side else (lambda m: m["gf"] - m["ga"] < need)      # home team's games, its own side
        ok_a = (lambda m: m["ga"] - m["gf"] > need) if home_side else (lambda m: m["ga"] - m["gf"] < need)      # away team's games, seen from the home side
        rh, ra = f(H, ok_h), f(A, ok_a)
        hr = (sum(1 for a_, b_ in hh if ((a_ - b_ > need) if home_side else (a_ - b_ < need))) / len(hh)) if len(hh) >= 3 else None
        label = "margin record"
    else:
        over = oid == "12"; home_team = mid == "19"
        own, opp = (H, A) if home_team else (A, H)
        c_own = (lambda m: m["gf"] > ln) if over else (lambda m: m["gf"] < ln)
        c_opp = (lambda m: m["ga"] > ln) if over else (lambda m: m["ga"] < ln)
        rh, ra = f(own, c_own), f(opp, c_opp)
        hr = (sum(1 for a_, b_ in hh if (((a_ if home_team else b_) > ln) == over)) / len(hh)) if len(hh) >= 3 else None
        label = "goals record"
    sup, bad = [], []
    lo = min(rh, ra)
    if lo < .6: bad.append(f"{label} only {lo:.0%} in one side's last games")
    elif lo >= .8: sup.append(f"{label} {rh:.0%} / {ra:.0%} in both sides' last games")
    if hr is not None:
        if hr < .5: bad.append(f"h2h only {hr:.0%} of {len(hh)} meetings")
        elif hr >= .75: sup.append(f"h2h {hr:.0%} of {len(hh)} meetings")
    return sup, bad

# safety levels: (stats chance, chance after the bookmaker's price, a backing source required). From the replay of 696 finished
# matches, legs the model put at 88%+ came true ~93-95% of the time, 85-88% about 88%, 80-85% about 82%.
LEVELS = {"strict": (.88, .82, True), "normal": (.85, .78, False), "relaxed": (.80, .74, False)}

def build(req):
    LV = LEVELS.get(req.get("strict", "strict"), LEVELS["strict"])
    strat = req.get("strategy", "mix"); legs = max(1, min(int(req.get("legs", 2)), 40))
    E = [e for e in _live_list() if _in_play(e)]
    if not E: return dict(ok=False, msg="No games are in play right now. Try again when matches are on (usually from late morning).")
    G = [dict(eid=e["eventId"], ts=e["estimateStartTime"] / 1000, h=e["homeTeamName"], a=e["awayTeamName"],
              lg=e["sport"]["category"]["name"] + " - " + e["sport"]["category"]["tournament"]["name"],
              hid=e["homeTeamId"].split(":")[-1], aid=e["awayTeamId"].split(":")[-1], x={}, ou={}, minute=_minute(e), score=_score(e)) for e in E]
    LX = P.fetch_teams(G)
    try:
        import season; season.prefetch(G, len(G))
    except Exception: season = None
    C = []
    for g in G:
        H, A = LX.get(g["hid"], []), LX.get(g["aid"], [])
        if g["minute"] is None: continue
        sH = sA = None
        if season:
            Hs, As = season.teams(g)
            if Hs and As and min(Hs["n"], As["n"]) >= 5: sH, sA = season.rates(Hs), season.rates(As)
        have10 = len(H) >= 6 and len(A) >= 6
        if not ((len(H) >= 8 and len(A) >= 8) or (have10 and sH)): continue      # strong data only: 8+ recent games each, or 6+ plus the season table
        g10 = (sum(m["gf"] + m["ga"] for m in H) / len(H) + sum(m["gf"] + m["ga"] for m in A) / len(A)) / 2 if have10 else None
        gs = (sH["gpg"] + sA["gpg"]) / 2 if sH else None
        gpg = (g10 + gs) / 2 if g10 is not None and gs is not None else (g10 if g10 is not None else gs)
        src = "last 10 + season" if have10 and sH else "last 10" if have10 else "season table"
        left = max(1, 93 - g["minute"]) / 90                                    # share of the game still to play (with stoppage time)
        lam = gpg * left
        hs, as_ = g["score"]; tot = hs + as_
        # each side's scoring rate: its own scoring and the other side's conceding (last 10 and/or season)
        def rate(L_, key):
            return sum(m[key] for m in L_) / len(L_)
        parts_h, parts_a = [], []
        if have10: parts_h.append((rate(H, "gf") + rate(A, "ga")) / 2); parts_a.append((rate(A, "gf") + rate(H, "ga")) / 2)
        if sH: parts_h.append((sH["gf"] + sA["ga"]) / 2); parts_a.append((sA["gf"] + sH["ga"]) / 2)
        lh, la = sum(parts_h) / len(parts_h) * left, sum(parts_a) / len(parts_a) * left
        g.update(gpg=gpg, lam=lam, sH=sH, sA=sA, lh=lh, la=la, src=src, left=left)
    # every market SportyBet has open on each game, judged on the same footing: the chance from each side's expected remaining goals
    # (final-score and, in the 1st half, half-time-score distributions). Each game gets the market that suits it best – or nothing.
    HG = [g for g in G if "gpg" in g and 5 <= g["minute"] <= 85]
    with ThreadPoolExecutor(8) as ex: HM = dict(zip([g["eid"] for g in HG], ex.map(lambda g: _live_odds(g["eid"]), HG)))
    for g in HG:
        g["mk"] = HM.get(g["eid"], {}); hs, as_ = g["score"]; lead = (hs > as_) - (hs < as_)
        D = _final_dist(hs, as_, g["lh"], g["la"])
        D1 = None
        if g["minute"] < 42 and g.get("left"):
            f1 = max(0.0, (47 - g["minute"]) / 90) / g["left"]                  # share of the remaining goals that fall before half-time
            D1 = _final_dist(hs, as_, g["lh"] * f1, g["la"] * f1)
        for (mid, spec, oid), od in g["mk"].items():
            if not 1.08 <= od <= 1.75: continue
            p = _p(D, D1, mid, spec, oid, lead)
            if p is None: continue
            kind = KIND.get(mid, "")
            if kind == "goals" and oid not in ("12", "74"): kind = "dead"
            # replay test (696 matches): "another goal" legs came true ~6 points less often than the model said; Unders before
            # half-time were weaker (85-89%) than from 45' on (94-95%); handicaps and results held at 93-96% at every minute
            o15 = mid == "18" and spec == "total=1.5" and oid == "12"
            if kind == "goals" and not o15: p -= .06                          # Over 1.5 tested the other way: the model under-rated it (85-90% said, 92% real)
            if kind == "dead" and g["minute"] < 45: continue
            if p < (LV[0] - .05 if o15 else LV[0]): continue                    # Over 1.5 comes true ~5 points more often than the model says
            if strat != "mix" and kind != strat: continue
            C.append((g, mid, spec, oid, _label(g, mid, spec, oid), p,
                      f"{g['h']} expected {g['lh']:.1f} and {g['a']} {g['la']:.1f} more goals ({g['src']}); {hs}-{as_} at {g['minute']}' – {p:.0%} chance"))
    nodata = sum(1 for g in G if "gpg" not in g)
    if not C: return dict(ok=False, msg=f"{len(G)} games are in play, but none fits that strategy right now ({nodata} have no stats to judge them on). Try 'Best of all' or check back in a few minutes – the picture changes every few minutes.")
    # live prices for the candidates (the full in-play market list per game)
    MK = {g["eid"]: g["mk"] for g in G if "mk" in g}
    per = {}
    for g, mid, spec, oid, pick, p, why in C:
        od = MK.get(g["eid"], {}).get((mid, spec, oid))
        if not od: continue
        p = (p + 1 / (od * 1.06)) / 2                                           # the stats model and the bookmaker's own price must agree
        if p < LV[1]: continue                                                  # high chance, no coin-flip prices
        per.setdefault(g["eid"], []).append(dict(g=g, eid=g["eid"], mid=mid, spec=spec, oid=oid, pick=pick, odds=od, ts=g["ts"], game=f"{g['h']} vs {g['a']}",
                                                 lg=g["lg"], rate=p, s=P.edge(p, od), why=f"LIVE {g['score'][0]}-{g['score'][1]} {g['minute']}': {why}"))
    HC = lambda x: x["mid"] in ("14", "16")
    O15 = lambda x: x["mid"] == "18" and x["spec"] == "total=1.5" and x["oid"] == "12"
    def choose(L):
        """the legs of one game that pass every check, best first: Over 1.5 first when it is strong, handicaps behind other markets"""
        out = []
        for leg in sorted(L, key=lambda x: -(x["s"] + x["rate"] * .5 + (.05 if O15(x) else 0) - (.02 if HC(x) else 0)))[:4]:
            sup, bad = _prematch(leg["g"], leg["mid"], leg["oid"], leg["spec"])    # every pre-match source the builder uses
            if bad or (LV[2] and not sup): continue                             # none against it (strict: at least one source for it too)
            if sup: leg["why"] += "; " + ", ".join(sup)
            leg.pop("g", None); out.append(leg)
        return out
    with ThreadPoolExecutor(6) as ex: OK = [v for v in ex.map(choose, per.values()) if v]
    OK.sort(key=lambda L: (-L[0]["rate"], -L[0]["s"]))
    cap = max(1, legs // 3)                                                     # handicaps: at most a third of the ticket
    B, nh = [], 0
    for L in OK:                                                                # one leg per game
        leg = next((x for x in L if not HC(x) or nh < cap), None)
        if not leg: continue
        nh += HC(leg); B.append(leg)
    B.sort(key=lambda x: (-x["rate"], -x["s"]))
    if not B: return dict(ok=False, msg=f"{len(G)} games are in play and {len(per)} looked promising, but none reached the bar (88% from the stats, 82% after the bookmaker's price, a stats source backing it and none against it). Try again in a few minutes.")
    target = float(req.get("target") or 0)
    if target > 1:                                                              # target odds: strongest legs first until the target is reached
        T, o = [], 1.0
        for leg in B:
            if len(T) >= legs or o >= target: break
            T.append(leg); o *= leg["odds"]
        if o < target:
            allo = 1.0
            for leg in B[:legs]: allo *= leg["odds"]
            return dict(ok=False, msg=f"{target:g} odds can't be reached right now: {len(B)} live leg(s) are strong enough, and the most they make with up to {legs} legs is {allo:.2f} odds. "
                                      f"Lower the target, allow more legs, or try again in a few minutes.")
        B = T
    else: B = B[:legs]
    if target > 1: note = [f"Target {target:g} odds reached with {len(B)} leg(s), strongest first."]
    else: note = [] if len(B) >= legs else [f"Only {len(B)} live leg(s) were strong enough right now (you asked for {legs})."]
    if req.get("strict", "strict") != "strict": note.append({"normal": "Normal safety: in testing about 88 in 100 such legs came true.", "relaxed": "Relaxed safety: in testing only about 82 in 100 such legs came true."}[req["strict"]])
    note.append("Live odds move fast – load the code on SportyBet straight away.")
    return P.finish([B], "live", {"live": f"live – {dict(handicap='handicap & Asian handicap', goals='another goal coming', hold='favourite holding a lead', dead='quiet game', mix='best of all').get(strat, strat)}"}, note, req)
