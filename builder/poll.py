#!/usr/bin/env python3
"""Run by the cloud job every few minutes: pick up ticket requests sent from build.html (ntfy topic REQ_TOPIC),
build each one with phonebuild.py, add the codes to the tracker, record them in builds.json and push the result
to the user's phone (ntfy topic NTFY_TOPIC)."""
import json, os, subprocess, sys, time, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__)); SITE = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import phonebuild
REQ = os.environ.get("REQ_TOPIC", "sportyreq-8b4fc11513"); OUT = os.environ.get("NTFY_TOPIC", "")
STATE = os.path.join(HERE, "state.json"); BUILDS = os.path.join(HERE, "builds.json")
LAGOS = dt.timezone(dt.timedelta(hours=1))

def notify(title, body, prio="high", tags="soccer"):
    if not OUT: print("NOTIFY", title, body); return
    subprocess.run(["curl", "-s", "-m", "20", "-H", "Title: " + title, "-H", "Priority: " + prio, "-H", "Tags: " + tags,
                    "-H", "Click: https://awelebienichi.github.io/sportybet-results/build.html", "-d", body[:3900], "https://ntfy.sh/" + OUT], capture_output=True)

state = json.load(open(STATE)) if os.path.exists(STATE) else {"since": "3h", "done": []}
raw = subprocess.run(["curl", "-s", "-m", "30", f"https://ntfy.sh/{REQ}/json?poll=1&since={state['since']}"], capture_output=True, text=True).stdout
msgs = [json.loads(l) for l in raw.splitlines() if l.strip()]
msgs = [m for m in msgs if m.get("event") == "message" and m["id"] not in state["done"]]
print("new requests:", len(msgs))
builds = json.load(open(BUILDS)) if os.path.exists(BUILDS) else []
for m in msgs:
    state["done"] = (state["done"] + [m["id"]])[-200:]; state["since"] = m["id"]
    try: req = json.loads(m.get("message", "{}"))
    except Exception: continue
    if time.time() - m.get("time", time.time()) > 3 * 3600: continue        # stale request
    print("building", req)
    try: res = phonebuild.main(req)
    except Exception as e: res = dict(ok=False, msg=f"Builder error: {e}")
    when = dt.datetime.now(LAGOS).strftime("%a %d %b %H:%M")
    if not res.get("ok"):
        notify("Ticket request could not be built", res.get("msg") or "; ".join(res.get("note", [])) or "No ticket passed the checks.", "default", "warning")
        builds.insert(0, dict(when=when, req=req, ok=False, msg=res.get("msg") or "; ".join(res.get("note", []))))
        continue
    codes = [t["code"] for t in res["tickets"]]
    name = f"{dt.datetime.now(LAGOS).strftime('%a %d %b')} - phone: {res['kind']} {req.get('legs')} legs"
    subprocess.run([sys.executable, os.path.join(SITE, "tracker", "track.py"), "add", name, *codes], capture_output=True)
    for t in res["tickets"]:
        first = t["legs"][0]["t"]
        body = f"{len(t['legs'])} legs - {res['kind']} - total odds {t['odds']:,}\nFirst game {first}\n\n" + \
               "\n".join(f"{l['t']} {l['game']}: {l['pick']} @ {l['odds']}" for l in t["legs"])
        if res.get("note"): body += "\n\n" + " ".join(res["note"])
        notify(f"Ticket ready: {t['code']}  ({t['odds']:,} odds)", body)
    builds.insert(0, dict(when=when, req=req, ok=True, kind=res["kind"], note=res.get("note", []), tickets=res["tickets"]))
json.dump(builds[:40], open(BUILDS, "w"), indent=0)
json.dump(state, open(STATE, "w"))
