#!/usr/bin/env python3
"""Build web/index.html: the tracker's results as a page that can be published to claude.ai and opened on any device.
Uses the same settlement as track.py check (run that first). Data is embedded as compact JSON and drawn by the page."""
import json, os, sys, collections, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import track

WAT = dt.timezone(dt.timedelta(hours=1))
db = track.load_db()
R = json.load(open(track.CACHE)) if os.path.exists(track.CACHE) else {}
code = {"won": "w", "half-won": "w", "lost": "l", "half-lost": "l", "void": "v", "pending": "p", "check": "c"}
S = []; idx = {}
def si(s):                       # string table keeps the page small
    if s not in idx: idx[s] = len(S); S.append(s)
    return idx[s]
sets = []; tot = collections.Counter()
for s in sorted(db["sets"], key=lambda s: -s.get("added", 0)):
    tk = []; cnt = collections.Counter()
    for t in s["tickets"]:
        res = [track.settle(l, R.get(l["eid"])) for l in t["legs"]]
        c = collections.Counter(res)
        st = "L" if c["lost"] else ("R" if c["pending"] else ("C" if c["check"] else "W"))
        cnt[st] += 1; tot[st] += 1
        legs = []
        for l, rr in sorted(zip(t["legs"], res), key=lambda z: z[0]["kickoff"]):
            r = R.get(l["eid"], {}); ft = r.get("ft"); ht = r.get("ht")
            sc = (f"{ft[0]}:{ft[1]}" if ft else "") + (f" ({ht[0]}:{ht[1]})" if ht else "")
            mk = l["mdesc"] + ((" " + l["spec"].replace("total=", "").replace("hcp=", "")) if l["spec"] and "Asian" not in l["mdesc"] and l["spec"].split("=")[-1] not in l["mdesc"] else "")
            leg = [int(l["kickoff"]), si(l["match"]), si(mk), si(l["pick"]), l["odds"], sc, code[rr]]
            if rr in ("pending", "check") and not r.get("final"):   # what the page needs to fetch and settle this leg live
                leg.append([l["eid"].split(":")[-1], l["mid"], l["spec"], l["pick"]])
            legs.append(leg)
        odds = 1.0
        for l in t["legs"]: odds *= l["odds"]
        tk.append([t["ticket"], t["code"], round(odds, 2), st, legs])
    sets.append(dict(n=s["name"], a=int(s.get("added", 0)), c=dict(cnt), t=tk))
data = dict(tl=sorted(track.TL_MIDS), ended=sorted(track.ENDED), void=sorted(track.VOID), when=dt.datetime.now(WAT).strftime("%a %d %b %Y, %H:%M"), tot=dict(tot), s=S, sets=sets)
tpl = open(os.path.join(HERE, "web_template.html")).read()
os.makedirs(os.path.join(HERE, "web"), exist_ok=True)
out = tpl.replace("/*DATA*/null", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
open(os.path.join(HERE, "web", "index.html"), "w").write(out)
print("web/index.html", round(len(out) / 1e6, 2), "MB", dict(tot))
