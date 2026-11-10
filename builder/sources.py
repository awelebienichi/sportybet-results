#!/usr/bin/env python3
"""More stats for the phone builder: FotMob (league table, goals for/against, form), dailyfootballpredictions (a % per
market) and Vitibet (win/draw/loss % and a predicted score). check(leg) says which of them back a leg and whether any
clearly goes against it. Day lists are warmed in the background by listen.py; FotMob match details and tables are
fetched per shortlisted game and kept a few hours."""
import json, os, re, subprocess, time, html, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import scores
HERE = os.path.dirname(os.path.abspath(__file__)); CACHE = os.path.join(HERE, "cache", "src"); os.makedirs(CACHE, exist_ok=True)
UA = scores.UA; LAGOS = dt.timezone(dt.timedelta(hours=1))

def _curl(url, hdr=(), t=40):
    for i in range(3):
        r = subprocess.run(["curl", "-s", "-L", "--compressed", "-m", str(t), "-A", UA, *sum([["-H", h] for h in hdr], []), url], capture_output=True, text=True, errors="ignore")
        if r.stdout: return r.stdout
        time.sleep(2)
    return ""

def _cached(name, hours, fn):
    f = os.path.join(CACHE, name)
    if os.path.exists(f) and time.time() - os.path.getmtime(f) < hours * 3600:
        try: return json.load(open(f))
        except Exception: pass
    v = fn()
    if v:
        tmp = f"{f}.{os.getpid()}.tmp"
        try: json.dump(v, open(tmp, "w")); os.replace(tmp, f)
        except OSError: pass
    return v

def _days(ts_list):
    return sorted({dt.datetime.fromtimestamp(t, LAGOS).date().isoformat() for t in ts_list})

# ---------------- FotMob ----------------
def fm_day(day):
    def get():
        d = _curl(f"https://www.fotmob.com/api/data/matches?date={day.replace('-', '')}&timezone=Africa%2FLagos&ccode3=NGA")
        try: d = json.loads(d)
        except Exception: return []
        out = []
        for lg in d.get("leagues", []):
            for m in lg.get("matches", []):
                try: t = dt.datetime.fromisoformat(m["status"]["utcTime"].replace("Z", "+00:00")).timestamp()
                except Exception: continue
                out.append(dict(id=m["id"], h=m["home"]["name"], a=m["away"]["name"], hid=str(m["home"]["id"]), aid=str(m["away"]["id"]), t=t))
        return out
    return _cached(f"fm_day_{day}.json", 3, get) or []

def fm_table(url):
    def get():
        x = _curl(url)
        rows = []
        for i, m in enumerate(re.finditer(r"<t ([^>]*)/>", x)):
            a = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            try: rows.append(dict(pos=i + 1, id=a["id"], name=a.get("name", ""), pts=int(a.get("p", 0)), w=int(a.get("w", 0)), d=int(a.get("d", 0)), l=int(a.get("l", 0)),
                                  gf=int(a.get("g", 0)), ga=int(a.get("c", 0))))
            except Exception: pass
        return rows
    return _cached("fm_tab_" + re.sub(r"[^0-9a-z]", "_", url.split("/")[-1]) + ".json", 6, get) or []

def fm_match(mid):
    def get():
        try: d = json.loads(_curl(f"https://www.fotmob.com/api/data/matchDetails?matchId={mid}"))
        except Exception: return {}
        c = d.get("content", {}); out = {}
        tb = (c.get("table") or {}).get("url")
        if tb: out["table"] = tb
        h2 = (c.get("h2h") or {}).get("summary")
        if h2: out["h2h"] = h2                                     # [home wins, draws, away wins]
        return out
    return _cached(f"fm_m_{mid}.json", 6, get) or {}

def fm_info(leg):
    """table positions and goals for this game, from today's home side's point of view"""
    h, a = leg["game"].split(" vs ", 1); best = None
    for r in fm_day(dt.datetime.fromtimestamp(leg["ts"], LAGOS).date().isoformat()):
        if abs(r["t"] - leg["ts"]) > 50 * 60: continue
        s1, s2 = scores.sim(h, r["h"]), scores.sim(a, r["a"]); s = min(s1, s2)
        if s < .72 and max(s1, s2) >= .9 and abs(r["t"] - leg["ts"]) <= 600: s = .73
        if s >= .72 and (best is None or s > best[0]): best = (s, r)
    if not best: return None
    r = best[1]; det = fm_match(r["id"])
    out = {"h2h": det.get("h2h")}
    if det.get("table"):
        T = {x["id"]: x for x in fm_table(det["table"])}
        th, ta = T.get(r["hid"]), T.get(r["aid"])
        if th and ta and min(th["w"] + th["d"] + th["l"], ta["w"] + ta["d"] + ta["l"]) >= 4:
            ph = th["w"] + th["d"] + th["l"]; pa = ta["w"] + ta["d"] + ta["l"]
            out.update(n=len(T), pos_h=th["pos"], pos_a=ta["pos"], ppg_h=th["pts"] / ph, ppg_a=ta["pts"] / pa,
                       gpg_h=(th["gf"] + th["ga"]) / ph, gpg_a=(ta["gf"] + ta["ga"]) / pa, gf_h=th["gf"] / ph, gf_a=ta["gf"] / pa)
    return out

# ---------------- dailyfootballpredictions ----------------
def dfp_day(day):
    def get():
        n = dt.datetime.now(dt.timezone(dt.timedelta(hours=3)))            # the site's key is Istanbul time to the quarter hour
        api = f"{n.hour:02d}{n.minute // 15 * 15:02d}"
        try: d = json.loads(_curl(f"https://apiv1.dailyfootballpredictions.com/v1/match/list?d={day}&api={api}&gmt=1&lang=en",
                                  ("Origin: https://www.dailyfootballpredictions.com", "Referer: https://www.dailyfootballpredictions.com/")))
        except Exception: return []
        rows = []
        for m in d.get("data") or []:
            pc = {o["prefix"]: o.get("percentage") for g in m.get("odds") or [] for o in g["odds"] if o.get("percentage") is not None}
            if pc: rows.append(dict(h=m["teamHome"]["name"], a=m["teamAway"]["name"], t=m["unixTime"], pc=pc))
        return rows
    return _cached(f"dfp_{day}.json", 3, get) or []

def _match_rows(leg, rows, tkey="t"):
    h, a = leg["game"].split(" vs ", 1); best = None
    for r in rows:
        if abs(r[tkey] - leg["ts"]) > 75 * 60: continue
        s1, s2 = scores.sim(h, r["h"]), scores.sim(a, r["a"]); s = min(s1, s2)
        if s < .72 and max(s1, s2) >= .9 and abs(r[tkey] - leg["ts"]) <= 900: s = .73
        if s >= .72 and (best is None or s > best[0]): best = (s, r)
    return best[1] if best else None

# ---------------- Vitibet ----------------
def vb_day(day):
    def get():
        x = _curl(f"https://www.vitibet.com/index.php?clanek=quicktips&sekce=fotbal&lang=en&date={day}")
        x = re.sub(r"<script.*?</script>|<style.*?</style>", "", x, flags=re.S)
        x = re.sub(r"\s+", " ", html.unescape(re.sub("<[^>]+>", "|", x))); x = re.sub(r"(\|\s*)+", "|", x)
        out = []
        for m in re.finditer(r"\|(\d\d):(\d\d)\|([^|]{2,60})\|([^|]{2,60})\|INDEX\|([+-]?[\d.]+)\|1\|(\d+)%\|X\|(\d+)%\|2\|(\d+)%\|Tip\|(\d+)-(\d+)\|", x):
            hh, mm, h, a, idx, p1, px, p2, sh, sa = m.groups()
            d0 = dt.datetime.fromisoformat(day).replace(tzinfo=LAGOS)          # Vitibet times are Central European = Lagos time in winter
            t = d0.replace(hour=int(hh), minute=int(mm)).timestamp()
            out.append(dict(h=h.strip(), a=a.strip(), t=t, p1=int(p1), px=int(px), p2=int(p2), sh=int(sh), sa=int(sa), idx=float(idx)))
        return out
    return _cached(f"vb_{day}.json", 3, get) or []

def warm(ts_list):
    days = _days(ts_list)[:4]
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(fm_day, days)); list(ex.map(dfp_day, days)); list(ex.map(vb_day, days))
    return {d: (len(fm_day(d)), len(dfp_day(d)), len(vb_day(d))) for d in days}

# ---------------- verdict for one leg ----------------
def check(leg):
    """(list of supporting notes, list of notes that go against it). Only full-time result and goal markets are judged."""
    mid, oid, spec = leg["mid"], leg["oid"], leg.get("spec") or ""
    ln = float(spec.split("=")[1]) if spec.startswith("total=") else None
    day = dt.datetime.fromtimestamp(leg["ts"], LAGOS).date().isoformat()
    sup, bad = [], []
    side = "h" if (mid in ("1", "60200") and oid == "1") else "a" if (mid in ("1", "60200") and oid == "3") else None
    if mid == "10": side = {"9": "h", "11": "a"}.get(oid)                 # double chance leaning to a side
    over = None
    if mid == "18" and ln is not None: over = oid == "12"
    if mid == "450001" and oid == "25": over, ln = True, 1.5
    if mid == "29": over, ln = (oid == "74"), None
    try: F = fm_info(leg)
    except Exception: F = None
    if F and "pos_h" in F:
        n = F["n"]; ph, pa = F["pos_h"], F["pos_a"]
        if side:
            mine, theirs = (ph, pa) if side == "h" else (pa, ph); my_ppg, their_ppg = (F["ppg_h"], F["ppg_a"]) if side == "h" else (F["ppg_a"], F["ppg_h"])
            if mid == "10":                                               # double chance: only "clearly the weaker side" is against it
                if mine - theirs >= max(6, n // 3) and their_ppg - my_ppg >= .7: bad.append(f"FotMob table: {mine}{_th(mine)} vs {theirs}{_th(theirs)}")
            elif theirs - mine >= 3 and my_ppg >= their_ppg: sup.append(f"FotMob: {mine}{_th(mine)} vs {theirs}{_th(theirs)} of {n}")
            elif mine - theirs >= 3 and their_ppg > my_ppg: bad.append(f"FotMob table: {mine}{_th(mine)} vs {theirs}{_th(theirs)}")
        if over is not None and mid != "29":
            g = (F["gpg_h"] + F["gpg_a"]) / 2
            if over and ln is not None:
                if g >= ln + 0.9: sup.append(f"FotMob: {g:.1f} goals a game")
                elif g <= ln - 0.2: bad.append(f"FotMob: only {g:.1f} goals a game")
            elif not over and ln is not None:
                if g <= ln - 0.6: sup.append(f"FotMob: {g:.1f} goals a game")
                elif g >= ln + 0.4: bad.append(f"FotMob: {g:.1f} goals a game")
        if mid == "29" and min(F.get("gf_h", 0), F.get("gf_a", 0)) >= 1.3 and over: sup.append("FotMob: both score 1.3+ a game")
    try: D = _match_rows(leg, dfp_day(day))
    except Exception: D = None
    if D:
        pc = D["pc"]; key = None
        if side and mid in ("1", "60200"): key = "ms1" if side == "h" else "ms2"
        if mid == "10": key = {"9": "ct_1x", "11": "ct_x2", "10": "ct_12"}.get(oid)
        if mid == "29": key = "kg_var" if oid == "74" else "kg_yok"
        if mid == "18" and ln is not None:
            suf = {0.5: "_05", 1.5: "_15", 2.5: "", 3.5: "_35", 4.5: "_45", 5.5: "_55"}.get(ln)
            if suf is not None: key = ("ust" if oid == "12" else "alt") + suf
        if mid == "450001" and oid == "25": key = "ust_15"
        v = pc.get(key) if key else None
        if v is not None:
            if v >= 65: sup.append(f"DFP {v:.0f}%")
            elif v <= 35: bad.append(f"DFP only {v:.0f}%")
    try: V = _match_rows(leg, vb_day(day))
    except Exception: V = None
    if V:
        if side and mid in ("1", "60200"):
            mine, theirs = (V["p1"], V["p2"]) if side == "h" else (V["p2"], V["p1"])
            if mine >= 55: sup.append(f"Vitibet {mine}%")
            elif theirs > mine + 10: bad.append(f"Vitibet favours the other side ({theirs}%)")
        if mid == "10":
            other = V["p2"] if oid == "9" else V["p1"] if oid == "11" else None
            if other is not None and other >= 60: bad.append(f"Vitibet: other side {other}%")
        if over is not None and mid in ("18", "450001") and ln is not None:
            tot = V["sh"] + V["sa"]
            if over and tot > ln + 0.5: sup.append(f"Vitibet tip {V['sh']}-{V['sa']}")
            if over and ln >= 2.5 and tot <= 1: bad.append(f"Vitibet tip {V['sh']}-{V['sa']}")
            if not over and tot < ln - 0.5: sup.append(f"Vitibet tip {V['sh']}-{V['sa']}")
            if not over and tot >= ln + 1.5: bad.append(f"Vitibet tip {V['sh']}-{V['sa']}")
    return sup, bad

def _th(n): return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
