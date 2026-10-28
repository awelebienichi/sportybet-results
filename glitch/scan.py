#!/usr/bin/env python3
"""SportyBet error-odds scanner. Every ~90 s it reads every upcoming (next 24 h) and live football game with the
1X2, 1st-half 1X2 and goals markets, and flags prices that contradict the same game's other markets:
  R1 prematch: 1st-half Home/Away far above what the full-time price allows (the Romklao 31-odds case)
  R2 any:      a market whose prices add up to less than 100% (you'd win whatever happens) - a broken market
  R3 prematch: goal lines out of order (Over 3.5 cheaper than Over 2.5, etc.)
  R4 prematch: a price that jumped 3x or more since the last scan
  R5 live:     the team that is leading is still priced 3.0+ to win (feed stuck / not updated)
  R6-R11 prematch: double chance, draw no bet, Asian handicap, GG/NG, 1st-half goals and team goals checked
               against the markets they must agree with; flagged when one pays 25%+ more than it can be worth
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

GROUPS = ["1,18,60", "10,11,29", "68", "19,20", "16"]   # 1X2, goals, 1st-half 1X2 | double chance, DNB, GG/NG | 1st-half goals | team goals | Asian handicap
from concurrent.futures import ThreadPoolExecutor
def _merge(lists):
    E = {}
    for L in lists:
        for e in L:
            if e["eventId"] in E: E[e["eventId"]]["markets"] = (E[e["eventId"]].get("markets") or []) + (e.get("markets") or [])
            else: E[e["eventId"]] = dict(e, markets=list(e.get("markets") or []))
    return list(E.values())
def _page(a):
    g, p = a
    d = (get(f"{B}/factsCenter/pcUpcomingEvents?sportId=sr%3Asport%3A1&marketId={g}&pageSize=100&pageNum={p}&timeline=24") or {}).get("data")
    return ([e for t in d["tournaments"] for e in t["events"]], d["totalNum"]) if d else ([], 0)
def upcoming():
    first = _page((GROUPS[0], 1)); n = max(1, -(-first[1] // 100))
    jobs = [(g, p) for g in GROUPS for p in range(1, n + 1) if (g, p) != (GROUPS[0], 1)]
    with ThreadPoolExecutor(10) as ex: rest = [r[0] for r in ex.map(_page, jobs)]
    return _merge([first[0]] + rest)

def live():
    def one(g):
        d = (get(f"{B}/factsCenter/liveOrPrematchEvents?sportId=sr%3Asport%3A1&marketId={g}") or {}).get("data") or []
        return [e for t in d for e in t["events"]]
    with ThreadPoolExecutor(5) as ex: return _merge(list(ex.map(one, GROUPS)))

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

def _fair(ft):   # de-margined full-time win/draw/win chances
    if not all(k in ft for k in ("Home", "Draw", "Away")): return None
    q = {k: 1 / ft[k][0] for k in ("Home", "Draw", "Away")}; t = sum(q.values())
    return {k: v / t for k, v in q.items()}

def cross(M, R=1.25):
    """prices that must agree with another market on the same game; flag when one pays 25%+ more than it can be worth"""
    A = []; ft = M.get(("1", "")) or {}; p = _fair(ft)
    def flag(rule, why, mid, spec, k, o, lim):
        if k in o and o[k][0] >= 1.5 and o[k][0] > lim * R: A.append((rule, why + f" (should be about {lim:.2f})", mid, spec, k, o[k]))
    if p:
        dc = M.get(("10", "")) or {}
        for k, a, b in (("Home or Draw", "Home", "Draw"), ("Draw or Away", "Draw", "Away"), ("Home or Away", "Home", "Away")):
            if k in dc: flag("R6", f"Double Chance {k} at {dc[k][0]} but 1X2 says it's a {p[a]+p[b]:.0%} chance", "10", "", k, dc, 1 / (p[a] + p[b]))
        dnb = M.get(("11", "")) or {}
        for k in ("Home", "Away"):
            if k in dnb: flag("R7", f"Draw No Bet {k} at {dnb[k][0]} doesn't fit the 1X2", "11", "", k, dnb, (p["Home"] + p["Away"]) / p[k])
        ah = {spec: o for (mid, spec), o in M.items() if mid == "16"}
        if "hcp=-0.5" in ah: flag("R11", f"Asian Handicap Home -0.5 at {ah['hcp=-0.5'].get('Home (-0.5)', (0,))[0]} is the same bet as 1X2 Home {ft['Home'][0]}", "16", "hcp=-0.5", "Home (-0.5)", ah["hcp=-0.5"], ft["Home"][0])
        if "hcp=0.5" in ah: flag("R11", f"Asian Handicap Away -0.5 is the same bet as 1X2 Away {ft['Away'][0]}", "16", "hcp=0.5", "Away (-0.5)", ah["hcp=0.5"], ft["Away"][0])
        if "hcp=0" in ah and dnb:
            for k, h in (("Home", "Home (0)"), ("Away", "Away (0)")):
                if k in dnb: flag("R11", f"Asian Handicap {h} is the same bet as Draw No Bet {k} {dnb[k][0]}", "16", "hcp=0", h, ah["hcp=0"], dnb[k][0])
    gg = M.get(("29", "")) or {}; u15 = (M.get(("18", "total=1.5")) or {}).get("Under 1.5")
    if u15 and "No" in gg: flag("R8", f"GG/NG No at {gg['No'][0]} - but 'no goal for one side' is at least as likely as Under 1.5 ({u15[0]})", "29", "", "No", gg, u15[0])
    for (mid, spec), o in list(M.items()):
        if mid == "68" and spec.startswith("total="):          # 1st-half goals can never be more than full-match goals
            ln = spec.split("=")[1]; full = M.get(("18", spec)) or {}
            if f"Under {ln}" in o and f"Under {ln}" in full: flag("R9", f"1st Half Under {ln} at {o[f'Under {ln}'][0]} pays more than full-match Under {ln}", "68", spec, f"Under {ln}", o, full[f"Under {ln}"][0])
            if f"Over {ln}" in full and f"Over {ln}" in o: flag("R9", f"full-match Over {ln} at {full[f'Over {ln}'][0]} pays more than 1st Half Over {ln}", "18", spec, f"Over {ln}", full, o[f"Over {ln}"][0])
        if mid in ("19", "20") and spec.startswith("total="):   # one team's goals can never be more than the match total
            ln = spec.split("=")[1]; full = M.get(("18", spec)) or {}; side = "Home" if mid == "19" else "Away"
            if f"Under {ln}" in o and f"Under {ln}" in full: flag("R10", f"{side} team Under {ln} at {o[f'Under {ln}'][0]} pays more than match Under {ln}", mid, spec, f"Under {ln}", o, full[f"Under {ln}"][0])
            if f"Over {ln}" in full and f"Over {ln}" in o: flag("R10", f"match Over {ln} at {full[f'Over {ln}'][0]} pays more than {side} team Over {ln}", "18", spec, f"Over {ln}", full, o[f"Over {ln}"][0])
    return A

def check(e, is_live):
    M = mk(e); A = []
    ft = M.get(("1", "")) or {}; ht = M.get(("60", "")) or {}
    # R2 broken market: implied total < 97%
    for (mid, spec), o in M.items():
        if (mid in ("1", "60") and len(o) == 3) or (mid in ("18", "68", "19", "20", "29", "11", "16") and len(o) == 2):
            tot = sum(1 / v[0] for v in o.values())
            if tot < 0.97:
                best = max(o.items(), key=lambda kv: kv[1][0])
                A.append(("R2", f"market adds up to {tot:.0%} - every outcome can be backed for profit", mid, spec, best[0], best[1]))
    if not is_live:
        A += cross(M)
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
                                 pick=f"{ {'1':'1X2','60':'1st Half 1X2','18':'Over/Under','10':'Double Chance','11':'Draw No Bet','29':'GG/NG','68':'1st Half Over/Under','19':'Home team goals','20':'Away team goals','16':'Asian Handicap'}.get(mid, mid)} {pick}", odds=od, why=why,
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
