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
    if mid == "1": proxy = dict(mid="1", oid=oid, spec="")
    elif oid == "12": proxy = dict(mid="18", oid="12", spec="total=2.5")
    else: proxy = dict(mid="18", oid="13", spec="total=2.5")
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
            if mid == "1":
                lost = sum((a > h) if oid == "1" else (h > a) for h, a in hh) / len(hh)
                if lost >= .5: bad.append(f"h2h: lost {lost:.0%} of {len(hh)} meetings")
            else:
                ov = sum(h + a > 2.5 for h, a in hh) / len(hh); r = ov if oid == "12" else 1 - ov
                (bad if r < .4 else sup if r >= .6 else []).append(f"h2h {r:.0%} of {len(hh)} meetings")
    except Exception: pass
    return sup, bad

def build(req):
    strat = req.get("strategy", "mix"); legs = max(1, min(int(req.get("legs", 2)), 10))
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
        if len(H) < 6 or len(A) < 6 or g["minute"] is None: continue
        gpg = (sum(m["gf"] + m["ga"] for m in H) / len(H) + sum(m["gf"] + m["ga"] for m in A) / len(A)) / 2
        sH = sA = None
        if season:
            Hs, As = season.teams(g)
            if Hs and As and min(Hs["n"], As["n"]) >= 5: sH, sA = season.rates(Hs), season.rates(As); gpg = (gpg + (sH["gpg"] + sA["gpg"]) / 2) / 2
        left = max(1, 93 - g["minute"]) / 90                                    # share of the game still to play (with stoppage time)
        lam = gpg * left
        hs, as_ = g["score"]; tot = hs + as_
        g.update(gpg=gpg, lam=lam, sH=sH, sA=sA)
        # another goal coming: over (goals so far + 0.5)
        if strat in ("goals", "mix") and 15 <= g["minute"] <= 70 and gpg >= 2.8:
            p = 1 - math.exp(-lam)
            C.append((g, "18", f"total={tot + 0.5}", "12", f"Over {tot + 0.5} (another goal)", p, f"{gpg:.1f} goals a game; {hs}-{as_} at {g['minute']}' – {p:.0%} chance of another"))
        # quiet game: under (goals so far + 1.5)
        if strat in ("dead", "mix") and g["minute"] >= 55 and tot <= 1 and gpg <= 2.6:
            p = _pois_le(1, lam)
            C.append((g, "18", f"total={tot + 1.5}", "13", f"Under {tot + 1.5}", p, f"{gpg:.1f} goals a game; {hs}-{as_} at {g['minute']}' – {p:.0%} chance of at most one more"))
        # strong favourite holding a lead late on
        if strat in ("hold", "mix") and g["minute"] >= 65 and hs != as_:
            lead_h = hs > as_; diff = abs(hs - as_)
            Lw, Tw = (H, A) if lead_h else (A, H)
            wr = sum(m["gf"] > m["ga"] for m in Lw) / len(Lw); tl = sum(m["gf"] < m["ga"] for m in Tw) / len(Tw)
            leader_strong = wr >= .55 and (not sH or (sH if lead_h else sA)["loss"] <= .35)
            # chance the trailing side scores `diff` or more in the time left (they score at their own rate)
            trail_gf = sum(m["gf"] for m in Tw) / len(Tw); lam_t = trail_gf * left
            p = _pois_le(diff - 1, lam_t)
            if leader_strong:
                team = g["h"] if lead_h else g["a"]
                C.append((g, "1", "", "1" if lead_h else "3", f"{team} to win (leading {hs}-{as_})", p * (0.97 if tl >= .4 else 0.93),
                          f"{team} won {wr:.0%} of last 10, leads {hs}-{as_} at {g['minute']}'"))
    if not C: return dict(ok=False, msg="No live game fits that strategy with strong enough stats right now. Try 'Best of all' or check back in a few minutes.")
    # live prices for the candidates (the full in-play market list per game)
    eids = list({c[0]["eid"] for c in C})
    with ThreadPoolExecutor(8) as ex: MK = dict(zip(eids, ex.map(_live_odds, eids)))
    best = {}
    for g, mid, spec, oid, pick, p, why in C:
        od = MK.get(g["eid"], {}).get((mid, spec, oid))
        if not od or od < 1.08 or od > 1.75 or p < .75: continue                # high chance, no coin-flip prices
        leg = dict(eid=g["eid"], mid=mid, spec=spec, oid=oid, pick=pick, odds=od, ts=time.time() + 60, game=f"{g['h']} vs {g['a']}", lg=g["lg"],
                   rate=p, s=P.edge(p, od), why=f"LIVE {g['score'][0]}-{g['score'][1]} {g['minute']}': {why}")
        sup, bad = _prematch(g, mid, oid, spec)                               # every pre-match source the builder uses
        if bad: continue
        if sup: leg["why"] += "; " + ", ".join(sup)
        if g["eid"] not in best or leg["s"] > best[g["eid"]]["s"]: best[g["eid"]] = leg
    B = sorted(best.values(), key=lambda x: (-x["rate"], -x["s"]))[:legs]
    if not B: return dict(ok=False, msg="Live prices for the strong picks are either too low, too high or suspended right now. Try again in a minute.")
    note = [] if len(B) >= legs else [f"Only {len(B)} live leg(s) were strong enough right now (you asked for {legs})."]
    note.append("Live odds move fast – load the code on SportyBet straight away.")
    return P.finish([B], "live", {"live": f"live – {dict(goals='another goal coming', hold='favourite holding a lead', dead='quiet game', mix='best of all').get(strat, strat)}"}, note, req)
