#!/usr/bin/env python3
"""SportyBet error-odds scanner. Every ~90 s it reads every upcoming (next 24 h) and live football game with the
1X2, 1st-half 1X2 and goals markets, and flags prices that contradict the same game's other markets:
  R1 prematch: 1st-half Home/Away far above what the full-time price allows (the Romklao 31-odds case)
  R2 any:      a market whose prices add up to less than 100% (you'd win whatever happens) - a broken market
  R3 prematch: goal lines out of order (Over 3.5 cheaper than Over 2.5, etc.)
  R4 prematch: a price that jumped 3x or more since the last scan
  R5 live:     the team that is leading is still priced 3.0+ to win (feed stuck / not updated)
Each alert: Mac notification + sound, a ready booking code, a line in alerts.log, and the alerts page on the site.
usage: scan.py [--once]"""
import json, os, sys, time, subprocess, datetime as dt
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
HERE = os.environ.get("SCAN_HOME") or os.path.dirname(os.path.abspath(__file__)); SITE = os.environ.get("SITE") or os.path.expanduser("~/ticket_tracker/site")
WHO = os.environ.get("SCAN_WHO", "Mac")   # "Mac" here, "cloud" in the GitHub job
LOG = os.path.join(HERE, "alerts.log"); JS = os.path.join(HERE, "alerts.json"); PREV = {}
W = dt.timezone(dt.timedelta(hours=1)); B = "https://www.sportybet.com/api/ng"

def get(u):
    for i in range(3):
        try: return json.loads(subprocess.run(["curl", "-s", "-m", "30", "-A", UA, "-H", "Accept: application/json", "-H", "Referer: https://www.sportybet.com/ng/", u], capture_output=True, text=True).stdout)
        except Exception: time.sleep(2)
    return {}

def upcoming():
    E = []; p = 1
    while p < 15:
        d = (get(f"{B}/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A1&marketId=1,18,60&pageSize=100&pageNum={p}&timeline=24") or {}).get("data")
        if not d: break
        E += [e for t in d["tournaments"] for e in t["events"]]
        if p * 100 >= d["totalNum"]: break
        p += 1
    return E

def live():
    d = (get(f"{B}/factsCenter/liveOrPrematchEvents?sportId=sr%3Asport%3A1&marketId=1,18,60") or {}).get("data") or []
    return [e for t in d for e in t["events"]]

def mk(e):
    out = {}
    for m in e.get("markets") or []:
        if m.get("status", 0) != 0: continue
        o = {x["desc"]: (float(x["odds"]), x["id"]) for x in m["outcomes"] if x.get("isActive", 1) == 1 and float(x.get("odds") or 0) > 1}
        out[(m["id"], m.get("specifier") or "")] = o
    return out

def book(e, mid, spec, oid):
    s = {"eventId": e["eventId"], "marketId": mid, "outcomeId": oid, **({"specifier": spec} if spec else {})}
    r = subprocess.run(["curl", "-s", "-m", "30", "-X", "POST", "-H", "Content-Type: application/json", "-H", "Origin: https://www.sportybet.com",
                        "-H", "Referer: https://www.sportybet.com/ng/", "-A", UA, "--data", json.dumps({"selections": [s]}), f"{B}/orders/share"], capture_output=True, text=True).stdout
    try: return json.loads(r)["data"]["shareCode"]
    except Exception: return "-"

def check(e, is_live):
    M = mk(e); A = []
    ft = M.get(("1", "")) or {}; ht = M.get(("60", "")) or {}
    # R2 broken market: implied total < 97%
    for (mid, spec), o in M.items():
        if (mid in ("1", "60") and len(o) == 3) or (mid == "18" and len(o) == 2):
            tot = sum(1 / v[0] for v in o.values())
            if tot < 0.97:
                best = max(o.items(), key=lambda kv: kv[1][0])
                A.append(("R2", f"market adds up to {tot:.0%} - every outcome can be backed for profit", mid, spec, best[0], best[1]))
    if not is_live:
        # R1 1st half far above full time
        for side in ("Home", "Away"):
            if side in ft and side in ht and ft[side][0] <= 3.0 and ht[side][0] >= max(4 * ft[side][0], ft[side][0] + 6):
                A.append(("R1", f"1st Half {side} {ht[side][0]} but full-time {side} only {ft[side][0]}", "60", "", side, ht[side]))
        # R3 goal lines out of order
        ov = sorted(((float(spec.split("=")[1]), o["Over " + spec.split("=")[1]]) for (mid, spec), o in M.items() if mid == "18" and spec.startswith("total=") and ("Over " + spec.split("=")[1]) in o), key=lambda t: t[0])
        for (l1, a), (l2, b) in zip(ov, ov[1:]):
            if b[0] < a[0] / 1.15 and b[0] >= 1.05:
                A.append(("R3", f"Over {l2} at {b[0]} is cheaper than Over {l1} at {a[0]}", "18", f"total={l2:g}", f"Over {l2:g}", b))
        # R4 jump since last scan
        for (mid, spec), o in M.items():
            for k, v in o.items():
                pv = PREV.get((e["eventId"], mid, spec, k))
                if pv and v[0] >= 3 * pv and v[0] >= 4 and pv <= 3:
                    A.append(("R4", f"{k} jumped from {pv} to {v[0]} since the last scan", mid, spec, k, v))
                PREV[(e["eventId"], mid, spec, k)] = v[0]
    else:
        # R5 leader still priced as an outsider
        try:
            h, a = map(int, (e.get("setScore") or "0:0").split(":")); mins = int(str(e.get("playedSeconds") or "0:0").split(":")[0])
        except Exception: h = a = mins = 0
        if h != a and mins >= 20:
            lead = "Home" if h > a else "Away"
            if lead in ft and ft[lead][0] >= 3.0 and abs(h - a) >= 1:
                A.append(("R5", f"{lead} leads {h}-{a} at {mins}' but is still {ft[lead][0]} to win", "1", "", lead, ft[lead]))
    return A

CFG = json.load(open(os.path.join(HERE, "config.json"))) if os.path.exists(os.path.join(HERE, "config.json")) else {}
if os.environ.get("NTFY_TOPIC"): CFG["ntfy_topic"] = os.environ["NTFY_TOPIC"]
def notify(t, m, code=None):
    try: subprocess.run(["osascript", "-e", f'display notification {json.dumps(m)} with title {json.dumps(t)} sound name "Glass"'], timeout=10)
    except Exception: pass
    if CFG.get("ntfy_topic"):   # phone push via the free ntfy app (subscribe to this topic)
        hdr = ["-H", "Title: " + t.encode("ascii", "ignore").decode(), "-H", "Priority: urgent", "-H", "Tags: rotating_light"]
        if code and code != "-": hdr += ["-H", f"Click: https://www.sportybet.com/ng/?shareCode={code}"]
        try: subprocess.run(["curl", "-s", "-m", "20", *hdr, "-d", m, "https://ntfy.sh/" + CFG["ntfy_topic"]], capture_output=True, timeout=30)
        except Exception: pass

def page(alerts):
    rows = "".join(f"<tr><td>{a['when']}</td><td>{a['kick']}</td><td>{a['match']}<br><small>{a['league']}</small></td><td><b>{a['pick']}</b> @ {a['odds']}<br><small>{a['why']}</small></td><td class=c>{a['code']}</td><td>{a['status']}</td></tr>" for a in reversed(alerts[-200:]))
    html = f"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Odds Glitch Alerts</title>
<style>:root{{--bg:#fff;--fg:#111;--mut:#666;--line:#ddd;--acc:#0a7}}@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--mut:#999;--line:#333;--acc:#3c9}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.4 system-ui;margin:0;padding:16px}}h1{{font-size:20px;margin:0 0 4px}}p{{color:var(--mut);margin:0 0 12px}}
.t{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:640px}}td,th{{border-bottom:1px solid var(--line);padding:8px 6px;text-align:left;vertical-align:top}}
.c{{font:600 16px ui-monospace,monospace;color:var(--acc)}}small{{color:var(--mut)}}a{{color:var(--acc)}}</style>
<h1>Odds glitch alerts</h1><p>Prices on SportyBet that contradict the same game's other markets. Checked about every 90 seconds while the Mac is on. Updated {dt.datetime.now(W).strftime('%a %d %b %H:%M')}. <a href="index.html">Back to results</a></p>
<p>SportyBet can void bets on obvious pricing errors, and live errors last seconds - always check the price on the slip before you stake.</p>
<div class=t><table><tr><th>Spotted</th><th>Kick-off</th><th>Match</th><th>Price</th><th>Booking code</th><th>When</th></tr>{rows or '<tr><td colspan=6>No glitches spotted yet.</td></tr>'}</table></div></html>"""
    try:
        g = lambda *a: subprocess.run(["git", "-C", SITE, *a], capture_output=True, text=True)
        g("pull", "--rebase", "--autostash", "-q")
        os.makedirs(os.path.join(SITE, "glitch"), exist_ok=True); sh = os.path.join(SITE, "glitch", "alerts.json")
        old = json.load(open(sh)) if os.path.exists(sh) else []; keys = {a["key"] for a in alerts}
        alerts[:] = sorted([a for a in old if a["key"] not in keys] + alerts, key=lambda a: a["ts"])[-500:]
        json.dump(alerts, open(sh, "w"))
        rows = "".join(f"<tr><td>{a['when']}<br><small>{a.get('who','Mac')}</small></td><td>{a['kick']}</td><td>{a['match']}<br><small>{a['league']}</small></td><td><b>{a['pick']}</b> @ {a['odds']}<br><small>{a['why']}</small></td><td class=c>{a['code']}</td><td>{a['status']}</td></tr>" for a in reversed(alerts[-200:]))
        html = html.split("<tr><th>")[0] + "<tr><th>Spotted</th><th>Kick-off</th><th>Match</th><th>Price</th><th>Booking code</th><th>When</th></tr>" + (rows or "<tr><td colspan=6>No glitches spotted yet.</td></tr>") + "</table></div></html>"
        open(os.path.join(SITE, "alerts.html"), "w").write(html)
        g("add", "alerts.html", "glitch/alerts.json")
        if g("diff", "--cached", "--quiet").returncode:
            g("commit", "-m", f"glitch alerts ({WHO})")
            for i in range(3):
                if g("push", "-q", "origin", "HEAD:main").returncode == 0: break
                g("pull", "--rebase", "--autostash", "-q")
    except Exception as x: print("page error", x)

def main():
    alerts = json.load(open(JS)) if os.path.exists(JS) else []
    sh = os.path.join(SITE, "glitch", "alerts.json")
    if os.path.exists(sh): alerts = sorted({a["key"]: a for a in json.load(open(sh)) + alerts}.values(), key=lambda a: a["ts"])
    seen = {a["key"]: a["ts"] for a in alerts}
    if not os.path.exists(os.path.join(SITE, "alerts.html")): page(alerts)
    while True:
        t0 = time.time(); new = []
        try:
            for is_live, E in ((False, upcoming()), (True, live())):
                for e in E:
                    for rule, why, mid, spec, pick, (od, oid) in check(e, is_live):
                        key = f"{e['eventId']}|{rule}|{mid}|{spec}|{pick}"
                        if time.time() - seen.get(key, 0) < 3600: continue
                        seen[key] = time.time()
                        a = dict(key=key, ts=time.time(), who=WHO, when=dt.datetime.now(W).strftime("%a %H:%M"), rule=rule,
                                 kick=dt.datetime.fromtimestamp(e["estimateStartTime"] / 1000, W).strftime("%a %H:%M"),
                                 match=f"{e['homeTeamName']} vs {e['awayTeamName']}", league=(e.get("sport", {}).get("category", {}).get("tournament", {}) or {}).get("name", ""),
                                 pick=f"{ {'1':'1X2','60':'1st Half 1X2','18':'Over/Under'}.get(mid, mid)} {pick}", odds=od, why=why,
                                 status="LIVE " + str(e.get("setScore", "")) + " " + str(e.get("playedSeconds", "")) if is_live else "before kick-off",
                                 code=book(e, mid, spec, oid))
                        new.append(a)
                        line = f"{a['when']}  {a['match']}  {a['pick']} @ {od}  ({why})  code {a['code']}  [{a['status']}]"
                        print(line, flush=True); open(LOG, "a").write(line + "\n")
                        notify(f"Odds glitch: {a['pick']} @ {od}", f"{a['match']} ({a['kick']}, {a['status']}) - {why} - booking code {a['code']}", a['code'])
        except Exception as x:
            print("scan error", x, flush=True)
        if new:
            alerts += new; page(alerts); json.dump(alerts[-500:], open(JS, "w"))
        print(dt.datetime.now(W).strftime("%H:%M:%S"), "scan done", round(time.time() - t0), "s,", len(new), "new alerts", flush=True)
        if "--once" in sys.argv: break
        time.sleep(max(20, 90 - (time.time() - t0)))

main()
