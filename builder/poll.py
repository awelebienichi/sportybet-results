#!/usr/bin/env python3
"""Ticket requests sent from build.html arrive on ntfy topic REQ_TOPIC. For each one: build it with phonebuild.py,
add the codes to the tracker, record them in builds.json and push the result to the user's phone (ntfy NTFY_TOPIC).
listen.py calls handle() the moment a request arrives; running this file directly handles whatever is waiting once."""
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

def load_state(): return json.load(open(STATE)) if os.path.exists(STATE) else {"since": "3h", "done": []}

def waiting(state):
    raw = subprocess.run(["curl", "-s", "-m", "30", f"https://ntfy.sh/{REQ}/json?poll=1&since={state['since']}"], capture_output=True, text=True).stdout
    msgs = []
    for l in raw.splitlines():
        try: m = json.loads(l)
        except Exception: continue
        if m.get("event") == "message" and m["id"] not in state["done"]: msgs.append(m)
    return msgs

def handle(msgs, state):
    builds = json.load(open(BUILDS)) if os.path.exists(BUILDS) else []
    for m in msgs:
        state["done"] = (state["done"] + [m["id"]])[-200:]; state["since"] = m["id"]
        try: req = json.loads(m.get("message", "{}"))
        except Exception: continue
        if time.time() - m.get("time", time.time()) > 3 * 3600: continue        # stale request
        print("building", req, flush=True); t0 = time.time()
        try: res = phonebuild.main(req)
        except Exception as e: res = dict(ok=False, msg=f"Builder error: {e}")
        when = dt.datetime.now(LAGOS).strftime("%a %d %b %H:%M")
        if not res.get("ok"):
            msg = res.get("msg") or "; ".join(res.get("note", [])) or "No ticket passed the checks."
            notify("Ticket request could not be built", msg, "default", "warning")
            builds.insert(0, dict(when=when, req=req, ok=False, msg=msg))
            continue
        codes = [t["code"] for t in res["tickets"]]
        name = f"{dt.datetime.now(LAGOS).strftime('%a %d %b')} - phone: {res['kind']} {len(res['tickets'][0]['legs'])} legs"
        subprocess.run([sys.executable, os.path.join(SITE, "tracker", "track.py"), "add", name, *codes], capture_output=True)
        for t in res["tickets"]:
            body = f"{len(t['legs'])} legs - {res['kind']} - total odds {t['odds']:,}\nFirst game {t['legs'][0]['t']}\n\n" + \
                   "\n".join(f"{l['t']} {l['game']}: {l['pick']} @ {l['odds']}" for l in t["legs"])
            if res.get("note"): body += "\n\n" + " ".join(res["note"])
            notify(f"Ticket ready: {t['code']}  ({t['odds']:,} odds)", body)
        print(f"done {codes} in {time.time()-t0:.0f}s", flush=True)
        builds.insert(0, dict(when=when, req=req, ok=True, kind=res["kind"], note=res.get("note", []), tickets=res["tickets"]))
    json.dump(builds[:40], open(BUILDS, "w"), indent=0)
    json.dump(state, open(STATE, "w"))

def save():
    """commit the new codes (tracker + builds list) so the website and the Mac see them"""
    g = lambda *a: subprocess.run(["git", "-C", SITE, *a], capture_output=True, text=True)
    g("add", "builder/state.json", "builder/builds.json", "tracker/tracker.json")
    if g("diff", "--cached", "--quiet").returncode == 0: return
    g("commit", "-qm", "Phone tickets " + dt.datetime.now(LAGOS).strftime("%a %d %b %H:%M"))
    for i in range(5):
        g("pull", "--rebase", "-q", "-X", "theirs")
        if g("push", "-q").returncode == 0: return
        time.sleep(5)

if __name__ == "__main__":
    st = load_state(); ms = waiting(st); print("new requests:", len(ms))
    if ms: handle(ms, st); save()
