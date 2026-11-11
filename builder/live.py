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

def _final_dist(hs, as_, lh, la, cap=10):
    """{(home goals, away goals) at full time: probability}, each side's remaining goals an independent Poisson"""
    ph = [_pois(i, lh) for i in range(cap)]; pa = [_pois(j, la) for j in range(cap)]
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
    if mid in ("14", "16"):                                                     # the backed side: needs to outscore from here -> its win record, else its not-lose record
        back_h = oid in ("1711", "1714"); hs, as_ = g["score"]
        if mid == "16": v = float(spec.split("=")[1]); cushion = (hs - as_ + v) if back_h else (as_ - hs - v)
        else:
            x, y = [int(t) for t in spec.split("=")[1].split(":")]; cushion = (hs + x - as_ - y) if back_h else (as_ + y - hs - x)
        mid, oid = ("10", "9" if back_h else "11") if cushion > 0 else ("1", "1" if back_h else "3")
        spec = ""
    if mid == "10": proxy = dict(mid="10", oid=oid, spec="")                    # not-lose record (9 home or draw, 11 draw or away)
    elif mid == "1": proxy = dict(mid="1", oid=oid, spec="")
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
            else:
                pl = float(proxy["spec"].split("=")[1]); ov = sum(h + a > pl for h, a in hh) / len(hh); r = ov if oid == "12" else 1 - ov
                (bad if r < .4 else sup if r >= .6 else []).append(f"h2h {r:.0%} of {len(hh)} meetings ({proxy['spec'].split('=')[1]} line)")
    except Exception: pass
    return sup, bad

def build(req):
    strat = req.get("strategy", "handicap"); legs = max(1, min(int(req.get("legs", 2)), 40))
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
        if not have10 and not sH: continue                                      # nothing to judge the teams on
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
        g.update(gpg=gpg, lam=lam, sH=sH, sA=sA, lh=lh, la=la, src=src)
        # another goal coming: over (goals so far + 0.5)
        if strat in ("goals", "mix") and 10 <= g["minute"] <= 75:
            p = 1 - math.exp(-lam)
            C.append((g, "18", f"total={tot + 0.5}", "12", f"Over {tot + 0.5} (another goal)", p, f"{gpg:.1f} goals a game ({src}); {hs}-{as_} at {g['minute']}' – {p:.0%} chance of another"))
        # quiet game: at most one more goal (from 55'), or at most two more (from 45')
        if strat in ("dead", "mix") and g["minute"] >= 45:
            for more, frm in ((1, 55), (2, 45)):
                if g["minute"] < frm: continue
                p = _pois_le(more, lam)
                C.append((g, "18", f"total={tot + more + 0.5}", "13", f"Under {tot + more + 0.5}", p, f"{gpg:.1f} goals a game ({src}); {hs}-{as_} at {g['minute']}' – {p:.0%} chance of at most {more} more"))
        # a side holding a lead (from 55'): straight win, or win-or-draw when the lead is one goal
        if strat in ("hold", "mix") and g["minute"] >= 55 and hs != as_:
            lead_h = hs > as_; diff = abs(hs - as_)
            if have10:
                Lw, Tw = (H, A) if lead_h else (A, H)
                wr = sum(m["gf"] > m["ga"] for m in Lw) / len(Lw); trail_gf = sum(m["gf"] for m in Tw) / len(Tw)
            else:
                wr = (sH if lead_h else sA)["win"]; trail_gf = (sA if lead_h else sH)["gf"]
            ls = sH if lead_h else sA
            if wr >= .45 and (not ls or ls["loss"] <= .45):
                lam_t = trail_gf * left * 1.15                                   # a trailing side pushes harder
                team = g["h"] if lead_h else g["a"]
                p_win = _pois_le(diff - 1, lam_t)
                C.append((g, "1", "", "1" if lead_h else "3", f"{team} to win (leading {hs}-{as_})", p_win, f"{team} won {wr:.0%} of recent games, leads {hs}-{as_} at {g['minute']}'"))
                p_dc = _pois_le(diff, lam_t)
                C.append((g, "10", "", "9" if lead_h else "11", f"{team} or draw (leading {hs}-{as_})", p_dc, f"{team} won {wr:.0%} of recent games, leads {hs}-{as_} at {g['minute']}'"))
    # handicap & Asian handicap (settled on the full-time score; SportyBet moves the lines with the live score)
    if strat in ("handicap", "mix"):
        HG = [g for g in G if "gpg" in g and g["minute"] is not None and 10 <= g["minute"] <= 85]
        with ThreadPoolExecutor(8) as ex: HM = dict(zip([g["eid"] for g in HG], ex.map(lambda g: _live_odds(g["eid"]), HG)))
        for g in HG:
            g["mk"] = HM.get(g["eid"], {})
            D = _final_dist(*g["score"], g["lh"], g["la"])
            for (mid, spec, oid), od in g["mk"].items():
                if mid not in ("14", "16") or not spec.startswith("hcp="): continue
                if mid == "16" and float(spec.split("=")[1]) % 1 == 0: continue    # whole lines can end level (stake back) – half lines only
                if mid == "14" and oid == "1712": continue                         # handicap draws: too narrow
                if not 1.08 <= od <= 1.75: continue
                p = _hcp_p(D, mid, spec, oid)
                if p < .75: continue
                back_h = oid in ("1711", "1714"); hs, as_ = g["score"]
                C.append((g, mid, spec, oid, _hcp_label(g, mid, spec, oid), p,
                          f"{g['h']} expected {g['lh']:.1f} and {g['a']} {g['la']:.1f} more goals ({g['src']}); {hs}-{as_} at {g['minute']}' – {p:.0%} chance this handicap lands"))
    nodata = sum(1 for g in G if "gpg" not in g)
    if not C: return dict(ok=False, msg=f"{len(G)} games are in play, but none fits that strategy right now ({nodata} have no stats to judge them on). Try 'Best of all' or check back in a few minutes – the picture changes every few minutes.")
    # live prices for the candidates (the full in-play market list per game)
    eids = [e for e in {c[0]["eid"] for c in C} if not next((g for g in G if g["eid"] == e and "mk" in g), None)]
    with ThreadPoolExecutor(8) as ex: MK = dict(zip(eids, ex.map(_live_odds, eids)))
    MK.update({g["eid"]: g["mk"] for g in G if "mk" in g})
    best = {}
    for g, mid, spec, oid, pick, p, why in C:
        od = MK.get(g["eid"], {}).get((mid, spec, oid))
        if not od or od < 1.08 or od > 1.75: continue
        p = (p + 1 / (od * 1.06)) / 2                                           # the stats model and the bookmaker's own price must agree
        if p < .75: continue                                                    # high chance, no coin-flip prices
        leg = dict(eid=g["eid"], mid=mid, spec=spec, oid=oid, pick=pick, odds=od, ts=time.time() + 60, game=f"{g['h']} vs {g['a']}", lg=g["lg"],
                   rate=p, s=P.edge(p, od), why=f"LIVE {g['score'][0]}-{g['score'][1]} {g['minute']}': {why}")
        sup, bad = _prematch(g, mid, oid, spec)                               # every pre-match source the builder uses
        if bad: continue
        if sup: leg["why"] += "; " + ", ".join(sup)
        leg["pref"] = leg["s"] + (.05 if mid in ("14", "16") else 0)            # the live tab leans on handicaps
        if g["eid"] not in best or leg["pref"] > best[g["eid"]]["pref"]: best[g["eid"]] = leg
    B = sorted(best.values(), key=lambda x: (-(x["rate"] + (.03 if x["mid"] in ("14", "16") else 0)), -x["s"]))[:legs]
    if not B: return dict(ok=False, msg=f"{len(G)} games are in play and {len({c[0]['eid'] for c in C})} looked promising, but none reached a 75% chance at a fair price (1.08–1.75) without a stats source against it. Try again in a few minutes.")
    note = [] if len(B) >= legs else [f"Only {len(B)} live leg(s) were strong enough right now (you asked for {legs})."]
    note.append("Live odds move fast – load the code on SportyBet straight away.")
    return P.finish([B], "live", {"live": f"live – {dict(handicap='handicap & Asian handicap', goals='another goal coming', hold='favourite holding a lead', dead='quiet game', mix='best of all').get(strat, strat)}"}, note, req)
