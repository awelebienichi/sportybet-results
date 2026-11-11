#!/usr/bin/env python3
"""Per-person results: builder/users/<id>/results.json keeps every ticket that person built (builder, rollover, live) from their
last reset onwards, with each leg's final score and result. The listener updates it from the live-scores pass, so the list
never loses a ticket when the recent-tickets list is cleared or rolls over."""
import glob, json, os, time
import users
HERE = os.path.dirname(os.path.abspath(__file__))
KEEP = 500

def path(uid): return os.path.join(users.folder(uid), "results.json")

def load(uid):
    try: return json.load(open(path(uid)))
    except Exception: return {"from": int(time.time()), "tickets": []}        # a new list starts now (older tickets are not brought in)

def reset(uid):
    json.dump({"from": int(time.time()), "tickets": []}, open(path(uid), "w"), indent=0)

def status(legs):
    rs = [l.get("r", "") for l in legs]
    return "lost" if "L" in rs else "won" if rs and all(r == "W" for r in rs) else "open"

def update(doc):
    """add new tickets and settle legs from the scores pass (doc = scores.run_once()); True when any file changed"""
    S = (doc or {}).get("legs", {})
    changed = False
    for f in glob.glob(os.path.join(HERE, "users", "*", "builds.json")):
        uid = os.path.basename(os.path.dirname(f))
        try: B = json.load(open(f))
        except Exception: continue
        R = load(uid); fresh = not os.path.exists(path(uid)); by = {t["code"]: t for t in R["tickets"]}; ch = fresh
        for b in reversed(B):
            if not b.get("ok") or (b.get("at") or 0) < R["from"]: continue
            for t in b["tickets"]:
                if t["code"] in by: continue
                e = dict(code=t["code"], at=b["at"], when=b.get("when", ""), kind=b.get("kind", ""), odds=t["odds"], status="open",
                         legs=[dict(t=l.get("t", ""), game=l["game"], pick=l["pick"], odds=l["odds"], ts=l.get("ts")) for l in t["legs"]])
                R["tickets"].insert(0, e); by[t["code"]] = e; ch = True
        for e in R["tickets"]:
            if e["status"] != "open" and all(l.get("r") for l in e["legs"]): continue
            for i, l in enumerate(e["legs"]):
                s = S.get(f"{e['code']}#{i}")
                if not s or not s.get("r"): continue                              # W / L only once decided (Over lines can win before the end)
                if l.get("r") != s["r"] or l.get("sc") != s["sc"]:
                    l.update(r=s["r"], sc=s["sc"], ht=s.get("ht", "")); ch = True
            st = status(e["legs"])
            if st != e["status"]: e["status"] = st; ch = True
        if ch:
            R["tickets"] = R["tickets"][:KEEP]
            json.dump(R, open(path(uid), "w"), indent=0); changed = True
    return changed
