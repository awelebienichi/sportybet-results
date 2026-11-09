#!/usr/bin/env python3
"""Daily rollover: one safe ticket of about the target odds each day (few legs, strongest stats), settled from the live
scores; a win rolls the whole return into the next day's stake, a loss starts again from the starting stake.
State lives in rollover.json (committed with the other builder files, shown on build.html)."""
import json, math, os, sys, time, subprocess, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__)); SITE = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import phonebuild
STATE = os.path.join(HERE, "rollover.json"); LAGOS = dt.timezone(dt.timedelta(hours=1))
def today(): return dt.datetime.now(LAGOS).date().isoformat()
def load():
    try: return json.load(open(STATE))
    except Exception: return {"active": False, "history": []}
def save(s): json.dump(s, open(STATE, "w"), indent=0)
def naira(x): return f"₦{x:,.0f}"

def handle(req, notify):
    """requests from build.html: rollover_start {stake, odds, hour}, rollover_stop, rollover_now"""
    s = load(); t = req.get("type")
    if t == "rollover_start":
        stake = max(100.0, float(req.get("stake") or 1000)); odds = min(20.0, max(1.5, float(req.get("odds") or 4)))
        hour = int(req.get("hour") or 10) % 24
        s.update(active=True, start_stake=stake, stake=stake, odds=odds, hour=hour, day=1, current=None, started=today(), short="skip" if req.get("short") == "skip" else "lower")
        save(s); notify("Daily rollover started", f"Stake {naira(stake)}, about {odds:g} odds a day, ticket every day at {hour:02d}:00.\nToday's ticket is being built now.")
        build_today(s, notify, force=True)
    elif t == "rollover_stop":
        s["active"] = False; save(s); notify("Daily rollover stopped", f"Stopped on day {s.get('day', 1)} with stake {naira(s.get('stake', 0))}.")
    elif t == "rollover_now":
        if not s.get("active"): notify("No rollover running", "Start one on the builder page first.", "default", "warning"); return
        build_today(s, notify, force=True)

def team_rates(leg, g, LX):
    """each team's own rate for this pick from its last 10 (the rollover needs BOTH teams strong, not just the average)"""
    H, A = LX.get(g["hid"], []), LX.get(g["aid"], [])
    if len(H) < 7 or len(A) < 7: return None
    f = lambda L, c: sum(1 for m in L if c(m)) / len(L)
    H1, A1 = [m for m in H if m["h1f"] is not None], [m for m in A if m["h1f"] is not None]
    f1 = lambda L, c: f(L, c) if len(L) >= 7 else None
    mid, oid, spec = leg["mid"], leg["oid"], leg["spec"]
    if mid == "18" and spec == "total=1.5" or (mid == "450001" and oid == "25"): c = lambda m: m["gf"] + m["ga"] >= 2; return f(H, c), f(A, c)
    if mid == "18" and spec == "total=2.5" and oid == "12": c = lambda m: m["gf"] + m["ga"] >= 3; return f(H, c), f(A, c)
    if mid == "29": c = lambda m: m["gf"] > 0 and m["ga"] > 0; return f(H, c), f(A, c)
    if mid == "860": return f(H, lambda m: m["gf"] > m["ga"] or (m["gf"] > 0 and m["ga"] > 0)), f(A, lambda m: m["gf"] < m["ga"] or (m["gf"] > 0 and m["ga"] > 0))
    if mid == "68": c = lambda m: m["h1f"] + m["h1a"] > 0; return f1(H1, c), f1(A1, c)
    if mid == "90": c = lambda m: m["h2f"] + m["h2a"] > 0; return f1(H1, c), f1(A1, c)
    if mid in ("69", "70", "91", "92"):          # a team to score in a half: that team scores, and the rival concedes, in that half
        k = "h1" if mid in ("69", "70") else "h2"
        home_side = mid in ("69", "91")
        T, O = (H1, A1) if home_side else (A1, H1)
        return f1(T, lambda m: m[k + "f"] > 0), f1(O, lambda m: m[k + "a"] > 0)
    if mid == "63": return (f1(H1, lambda m: m["h1f"] >= m["h1a"]), f1(A1, lambda m: m["h1f"] <= m["h1a"])) if oid == "9" else (f1(H1, lambda m: m["h1f"] <= m["h1a"]), f1(A1, lambda m: m["h1f"] >= m["h1a"]))
    if mid in ("1", "60200"):
        return (f(H, lambda m: m["gf"] > m["ga"]), f(A, lambda m: m["gf"] < m["ga"])) if oid == "1" else (f(A, lambda m: m["gf"] > m["ga"]), f(H, lambda m: m["gf"] < m["ga"]))
    if mid == "10" and oid == "11": return f(H, lambda m: m["gf"] <= m["ga"]), f(A, lambda m: m["gf"] >= m["ga"])
    return None

def h2h_halves(h, a):
    """meetings with half-time and full-time scores, from today's home side: (h1 home, h1 away, ft home, ft away)"""
    d = phonebuild.get(f"https://stats.fn.sportradar.com/sportybet/en/Europe:Berlin/gismo/stats_team_versusrecent/{h}/{a}", "https://www.sportybet.com/")
    out = []
    try:
        for m in d["doc"][0]["data"]["matches"][:10]:
            p = (m.get("periods") or {}).get("p1"); r = m.get("result") or {}
            if not p or r.get("home") is None: continue
            hh = m["teams"]["home"]["uid"] == int(h)
            out.append((p["home"], p["away"], r["home"], r["away"]) if hh else (p["away"], p["home"], r["away"], r["home"]))
    except Exception: pass
    return out

def h2h_half_rate(leg, HH):
    if len(HH) < 3: return None
    mid, oid = leg["mid"], leg["oid"]
    c = {"68": lambda a, b, x, y: a + b > 0, "90": lambda a, b, x, y: (x - a) + (y - b) > 0,
         "69": lambda a, b, x, y: a > 0, "70": lambda a, b, x, y: b > 0, "91": lambda a, b, x, y: x - a > 0, "92": lambda a, b, x, y: y - b > 0,
         "63": (lambda a, b, x, y: a >= b) if oid == "9" else (lambda a, b, x, y: b >= a)}.get(mid)
    return sum(1 for z in HH if c(*z)) / len(HH) if c else None

def h2h_rate(leg, hh):
    if len(hh) < 3: return None
    mid, oid, spec = leg["mid"], leg["oid"], leg["spec"]
    c = {("18", "total=1.5"): lambda a, b: a + b >= 2, ("18", "total=2.5"): lambda a, b: a + b >= 3}.get((mid, spec))
    if mid == "450001": c = lambda a, b: a + b >= 2
    if mid == "29": c = lambda a, b: a > 0 and b > 0
    if mid == "860": c = lambda a, b: a > b or (a > 0 and b > 0)
    if mid in ("1", "60200"): c = (lambda a, b: a > b) if oid == "1" else (lambda a, b: b > a)
    if mid == "10" and oid == "11": c = lambda a, b: b >= a
    return sum(1 for x in hh if c(*x)) / len(hh) if c else None

def pick_safe(target, lo=None, hi=None):  # noqa: the strictest picker
    """the strictest picker on the builder: each leg needs BOTH teams at 85%+ for the pick in their own last 10, at least one
    more source agreeing (head-to-head 75%+, Flashscore home/away form 80%+, or a footybets tip) and nothing against it"""
    now = time.time(); end = dt.datetime.combine(dt.datetime.now(LAGOS).date(), dt.time(23, 59), LAGOS).timestamp()
    phonebuild.ST["L"] = -0.05                      # underlying pickers stricter than "strict"
    G = phonebuild.games_cached(lo or now + 45 * 60, hi or end)
    LX = phonebuild.fetch_teams(G)
    C = (phonebuild.pick_multi(G, LX, "mix", 30) + phonebuild.pick_fav(G, LX) + phonebuild.pick_goals(G, LX, "o25") + phonebuild.pick_goals(G, LX, "o15")
         + phonebuild.pick_fav1h(G, LX) + phonebuild.pick_fav1h(G, LX, 2) + phonebuild.pick_h1o05(G, LX) + phonebuild.pick_h1o05(G, LX, 2))
    phonebuild.ST["L"] = 0.1
    GM = {g["eid"]: g for g in G}
    try:
        import extra, scores
        scores.day_list(False)
    except Exception: extra = None
    pre = []
    for c in C:
        if c["odds"] < 1.12 or c["eid"] not in GM: continue
        tr = team_rates(c, GM[c["eid"]], LX)
        if not tr or None in tr or min(tr) < .85: continue
        c["tr"] = tr; pre.append(c)
    def sources(c):
        g = GM[c["eid"]]
        if c["mid"] in ("68", "90", "69", "70", "91", "92", "63"): hr = h2h_half_rate(c, h2h_halves(g["hid"], g["aid"]))
        else: hr = h2h_rate(c, phonebuild.h2h(g["hid"], g["aid"]))
        fr = sup = None; bad = False
        if extra:
            try:
                fid = scores.match_fid(dict(k=c["eid"] + "|x", game=c["game"], ts=c["ts"]))
                F = extra.fs_form(fid) if fid else None
                fr = extra.fs_rate(c, F) if F else None
                sup, bad = extra.fb_verdict(c, extra.fb_for(c))
            except Exception: pass
        return c, hr, fr, sup, bad
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex: S = list(ex.map(sources, pre))
    best = {}
    for c, hr, fr, sup, bad in S:
        if bad or (hr is not None and hr < .75) or (fr is not None and fr < .8): continue      # anything against it: out
        agree = [x for x in (("head-to-head", hr) if hr is not None and hr >= .75 else None, ("Flashscore home/away", fr) if fr is not None and fr >= .8 else None,
                             ("footybets", sup / 100) if sup and sup >= 70 else None) if x]
        if not agree: continue                                                                    # at least two sources
        c["r"] = min(.97, min(c["tr"]) * .6 + sum(v for _, v in agree) / len(agree) * .4)
        c["why"] = f"both teams {min(c['tr']):.0%}+ in their own last 10 (home {c['tr'][0]:.0%}, away {c['tr'][1]:.0%}); " + ", ".join(f"{n} {v:.0%}" for n, v in agree)
        if c["eid"] not in best or c["r"] > best[c["eid"]]["r"]: best[c["eid"]] = c
    pool = list(best.values())
    plan = None                                   # for 3..7 legs: the strongest legs priced high enough to reach the target together
    for k in range(2, 8):
        floor = target ** (1 / k) * .9
        cand = sorted((c for c in pool if c["odds"] >= floor), key=lambda c: -c["r"])[:k + 6]
        cand.sort(key=lambda c: -c["r"])
        legs = cand[:k]
        while len(legs) == k and math.prod(c["odds"] for c in legs) < target:    # swap the cheapest leg for the next-best pricier one
            rest = [c for c in cand if c not in legs and c["odds"] > min(l["odds"] for l in legs)]
            if not rest: break
            cheap = min(legs, key=lambda c: c["odds"]); legs[legs.index(cheap)] = rest[0]
        if len(legs) == k and math.prod(c["odds"] for c in legs) >= target * .97:
            chance = math.prod(c["r"] for c in legs)
            if plan is None or chance > plan[0]: plan = (chance, legs)
    if not plan:
        legs = sorted(pool, key=lambda c: -math.log(c["odds"]) / -math.log(c["r"]))[:7]
        return legs, math.prod(c["odds"] for c in legs) if legs else 0
    return plan[1], math.prod(c["odds"] for c in plan[1])

def build_today(s, notify, force=False):
    if not s.get("active"): return
    if s.get("current") and s["current"].get("date") == today() and not force: return
    if s.get("current") and s["current"].get("result") is None and s["current"].get("date") == today():
        notify("Rollover ticket already out", f"Today's code is {s['current']['code']}.", "default"); return
    legs, prod = pick_safe(s["odds"])
    short = not legs or prod < s["odds"] * .85
    lower_note = ""
    if short and s.get("short", "lower") == "lower" and legs and prod >= 2.5:
        short = False; lower_note = f"Only {prod:.2f} odds today: not enough games passed the strict rollover checks to reach {s['odds']:g}.\n"
    if short:
        notify("Rollover: no safe ticket today", f"Not enough games passed the strict rollover checks today ({prod:.2f} odds possible, minimum 2.5" + (f", your target {s['odds']:g}" if s.get("short") == "skip" else "") + f"). The rollover waits for tomorrow (stake stays {naira(s['stake'])}).", "default", "warning")
        s["current"] = {"date": today(), "code": None, "result": "skip"}; save(s); return
    res = phonebuild.finish([legs], "rollover", {"rollover": f"daily rollover day {s['day']}"}, [], {"type": "rollover"})
    if not res.get("ok"):
        notify("Rollover: booking failed", "SportyBet refused the booking; it will try again in a few minutes.", "default", "warning"); return
    t = res["tickets"][0]
    s["current"] = {"date": today(), "code": t["code"], "odds": t["odds"], "stake": s["stake"], "n": len(t["legs"]), "result": None}
    save(s)
    builds = json.load(open(os.path.join(HERE, "builds.json"))) if os.path.exists(os.path.join(HERE, "builds.json")) else []
    builds.insert(0, dict(when=dt.datetime.now(LAGOS).strftime("%a %d %b %H:%M"), req={"type": "rollover", "legs": len(t["legs"])}, ok=True,
                          kind=f"daily rollover – day {s['day']}", note=[f"Stake {naira(s['stake'])} → returns {naira(s['stake'] * t['odds'])} if it wins"], tickets=[t]))
    json.dump(builds[:40], open(os.path.join(HERE, "builds.json"), "w"), indent=0)
    subprocess.run([sys.executable, os.path.join(SITE, "tracker", "track.py"), "add", f"{dt.datetime.now(LAGOS).strftime('%a %d %b')} - daily rollover day {s['day']}", t["code"]], capture_output=True)
    body = lower_note + f"Stake {naira(s['stake'])} → {naira(s['stake'] * t['odds'])} if it wins\n{len(t['legs'])} legs, {t['odds']} odds\n\n" + "\n".join(f"{l['t']} {l['game']}: {l['pick']} @ {l['odds']}" for l in t["legs"])
    notify(f"Rollover day {s['day']}: {t['code']}", body)

def tick(notify, lock=None):
    """called every few seconds by the listener: build today's ticket at the set hour; settle it from scores.json"""
    s = load()
    if not s.get("active"): return False
    changed = False
    cur = s.get("current") or {}
    now = dt.datetime.now(LAGOS)
    if cur.get("date") != today() and now.hour >= s.get("hour", 10) and (cur.get("result") is not None or not cur.get("code")):
        if lock:
            with lock: build_today(s, notify)
        else: build_today(s, notify)
        return True
    if cur.get("code") and cur.get("result") is None:
        try: sc = json.load(open(os.path.join(HERE, "scores.json"))).get("legs", {})
        except Exception: sc = {}
        rs = [sc.get(f"{cur['code']}#{i}", {}).get("r", "") for i in range(cur.get("n", 0))]
        if "L" in rs:
            cur["result"] = "lost"; s["history"].insert(0, dict(date=cur["date"], day=s["day"], code=cur["code"], stake=cur["stake"], odds=cur["odds"], result="lost", returned=0))
            notify(f"Rollover lost on day {s['day']}", f"{cur['code']} lost a leg. Starting again tomorrow from {naira(s['start_stake'])}.", "high", "x")
            s["stake"] = s["start_stake"]; s["day"] = 1; changed = True
        elif rs and all(r == "W" for r in rs):
            ret = cur["stake"] * cur["odds"]; cur["result"] = "won"
            s["history"].insert(0, dict(date=cur["date"], day=s["day"], code=cur["code"], stake=cur["stake"], odds=cur["odds"], result="won", returned=round(ret)))
            notify(f"Rollover day {s['day']} WON ✅", f"{cur['code']} won: {naira(cur['stake'])} → {naira(ret)}. Tomorrow's stake: {naira(ret)} (day {s['day'] + 1}).", "high", "white_check_mark")
            s["stake"] = round(ret); s["day"] += 1; changed = True
        if changed: s["history"] = s["history"][:60]; save(s)
    return changed
