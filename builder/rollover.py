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
        s.update(active=True, start_stake=stake, stake=stake, odds=odds, hour=hour, day=1, current=None, started=today())
        save(s); notify("Daily rollover started", f"Stake {naira(stake)}, about {odds:g} odds a day, ticket every day at {hour:02d}:00.\nToday's ticket is being built now.")
        build_today(s, notify, force=True)
    elif t == "rollover_stop":
        s["active"] = False; save(s); notify("Daily rollover stopped", f"Stopped on day {s.get('day', 1)} with stake {naira(s.get('stake', 0))}.")
    elif t == "rollover_now":
        if not s.get("active"): notify("No rollover running", "Start one on the builder page first.", "default", "warning"); return
        build_today(s, notify, force=True)

def pick_safe(target):
    """fewest, strongest legs to reach `target`: candidates from the safest-market picker plus strong favourites (strict),
    chosen by odds gained per unit of risk, at most 6 legs"""
    now = time.time(); end = dt.datetime.combine(dt.datetime.now(LAGOS).date(), dt.time(23, 59), LAGOS).timestamp()
    phonebuild.ST["L"] = 0.0
    G = phonebuild.games_cached(now + 45 * 60, end)
    LX = phonebuild.fetch_teams(G)
    C = phonebuild.pick_multi(G, LX, "mix", 30) + phonebuild.pick_fav(G, LX)
    try:
        import extra
        C, _ = extra.confirm(C, 30)
    except Exception: pass
    best = {}
    for c in C:
        r = c.get("rate", .7)
        r = min(r, .97); c["r"] = r
        if c["odds"] < 1.12 or r < .75: continue          # tiny prices need too many legs; weak stats are out
        if c["eid"] not in best or (c["r"], -c["odds"]) > (best[c["eid"]]["r"], -best[c["eid"]]["odds"]): best[c["eid"]] = c
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
    if not legs or prod < s["odds"] * .85:
        notify("Rollover: no safe ticket today", f"Not enough strong games left today to reach about {s['odds']:g} odds safely. The rollover waits for tomorrow (stake stays {naira(s['stake'])}).", "default", "warning")
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
    body = f"Stake {naira(s['stake'])} → {naira(s['stake'] * t['odds'])} if it wins\n{len(t['legs'])} legs, {t['odds']} odds\n\n" + "\n".join(f"{l['t']} {l['game']}: {l['pick']} @ {l['odds']}" for l in t["legs"])
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
