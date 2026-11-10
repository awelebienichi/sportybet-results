#!/usr/bin/env python3
"""Extra checks for the phone builder (same sources as the Mac builds): Flashscore home/away form and footybets.io tips.
A leg is dropped when either clearly goes against it; legs they back say so in their "why" line.
footybets pages are slow (~7s each), so warm_footybets() is run in the background by listen.py; Flashscore form is fetched
per shortlisted game (8 at a time, ~2s per 8) and kept for 12 hours."""
import json, os, re, subprocess, time
from concurrent.futures import ThreadPoolExecutor
import scores          # Flashscore helpers (complete-download retries, day lists, name matching)
HERE = os.path.dirname(os.path.abspath(__file__)); CACHE = os.path.join(HERE, "cache"); os.makedirs(CACHE, exist_ok=True)
UA = scores.UA

# ---------------- footybets.io ----------------
FB_MARKETS = {"over-1-5-goals": "Over 1.5 Goals", "over-2-5-goals": "Over 2.5 Goals", "under-2-5-goals": "Under 2.5 Goals",
              "over-3-5-goals": "Over 3.5 Goals", "btts": "Both teams to score"}
_MON = {"Okt": 10, "Nov": 11, "Dez": 12, "Jan": 1, "Feb": 2, "Mär": 3, "Apr": 4, "Mai": 5, "Jun": 6, "Jul": 7, "Aug": 8, "Sep": 9}
_FB = {"at": 0, "tips": []}

def warm_footybets():
    out = []
    def page(arg):
        slot, key = arg
        html = subprocess.run(["curl", "-s", "-L", "--compressed", "-m", "60", "-A", UA, f"https://footybets.io/predictions/{slot}/{key}/"], capture_output=True, text=True, errors="ignore").stdout
        t = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S); t = re.sub(r"<[^>]+>", "|", t)
        t = re.sub(r"\s+", " ", t); t = re.sub(r"(\| ?)+", "|", t).replace("&amp;", "&")
        res = []
        for m in re.finditer(r"\|(?:Mo|Di|Mi|Do|Fr|Sa|So)\., (\d+) (\w{3})\., (\d\d):(\d\d)\|([^|]+)\|([^|]+)\|Confidence\|(\d+)%\|" + re.escape(FB_MARKETS[key]) + r"\|", t):
            day, mon, hh, mm, h, a, conf = m.groups()
            if mon not in _MON: continue
            yr = time.gmtime().tm_year + (1 if _MON[mon] < time.gmtime().tm_mon - 6 else 0)
            # page times are Central European wall-clock; kept as a naive epoch and compared with _cet_naive(kickoff)
            ts_cet = time.mktime(time.strptime(f"{yr}-{_MON[mon]}-{day} {hh}:{mm}", "%Y-%m-%d %H:%M"))
            res.append(dict(h=h, a=a, t=ts_cet, mk=key, conf=int(conf)))
        return res
    jobs = [(s, k) for s in ("today", "tomorrow", "weekend") for k in FB_MARKETS]
    with ThreadPoolExecutor(5) as ex:
        for r in ex.map(page, jobs): out += r
    if out: _FB.update(at=time.time(), tips=out); json.dump(out, open(os.path.join(CACHE, "footybets.json"), "w"))
    return len(out)

def fb_tips():
    if not _FB["tips"]:
        f = os.path.join(CACHE, "footybets.json")
        if os.path.exists(f) and time.time() - os.path.getmtime(f) < 6 * 3600: _FB.update(at=os.path.getmtime(f), tips=json.load(open(f)))
    return _FB["tips"]

def fb_for(leg):
    h, a = leg["game"].split(" vs ", 1)
    return {x["mk"]: x["conf"] for x in fb_tips() if abs(x["t"] - _cet_naive(leg["ts"])) <= 90 * 60 and min(scores.sim(h, x["h"]), scores.sim(a, x["a"])) >= 0.6}

def _cet_naive(ts):
    """the leg's kickoff as a naive Central-European wall-clock epoch, comparable with the footybets times parsed above"""
    import datetime as dt
    cet = dt.timezone(dt.timedelta(hours=1))      # November-March: CET = UTC+1 (same as Nigeria)
    w = dt.datetime.fromtimestamp(ts, cet).replace(tzinfo=None)
    return time.mktime(w.timetuple())

def fb_verdict(leg, fb):
    """(supporting confidence or None, contradicted?)"""
    if not fb: return None, False
    mid, oid, spec = leg["mid"], leg["oid"], leg["spec"]
    ln = float(spec.split("=")[1]) if spec.startswith("total=") else None
    over = oid == "12"
    sup = None; bad = False
    if mid == "18" and ln is not None:
        sup = fb.get({(1.5, True): "over-1-5-goals", (2.5, True): "over-2-5-goals", (2.5, False): "under-2-5-goals", (3.5, True): "over-3-5-goals"}.get((ln, over), ""))
        if over and ln >= 2.5: bad = fb.get("under-2-5-goals", 0) >= 70
        if not over: bad = fb.get("over-2-5-goals", 0) >= 70 or (ln <= 1.5 and fb.get("over-1-5-goals", 0) >= 70)
    if mid == "450001" and oid == "25": sup = fb.get("over-1-5-goals")
    if mid in ("29", "860") and oid == "74": sup = fb.get("btts")
    if mid == "60" and oid == "2": bad = fb.get("over-3-5-goals", 0) >= 70     # a goal-fest tip is no half-time draw
    return sup, bad

# ---------------- Flashscore home/away form ----------------
def fs_form(fid):
    f = os.path.join(CACHE, f"fs_hh_{fid}.json")
    if os.path.exists(f) and time.time() - os.path.getmtime(f) < 12 * 3600:
        try: return json.load(open(f))
        except Exception: pass
    out = {"home_home": [], "away_away": [], "home_all": [], "away_all": []}
    sec = blk = None; names = []
    for r in scores.recs(scores.fs(f"df_hh_1_{fid}", 4, 30)):
        if "KA" in r: sec = "Home" if r["KA"].endswith("- Home") else "Away" if r["KA"].endswith("- Away") else "Overall"; continue
        if "KB" in r:
            if not r["KB"].startswith("Head") and r["KB"] not in names: names.append(r["KB"])
            blk = "h2h" if r["KB"].startswith("Head") else ("home" if names and r["KB"] == names[0] else "away")
            continue
        if "KU" not in r or "KT" not in r or "friendly" in r.get("KF", "").lower(): continue
        try: hg, ag = int(r["KU"]), int(r["KT"])
        except ValueError: continue
        if blk in ("home", "away"):
            mine = (hg, ag) if r.get("KS") == "home" else (ag, hg)
            key = {"Overall": f"{blk}_all", "Home": "home_home", "Away": "away_away"}.get(sec)
            if key and (sec == "Overall" or (sec == "Home" and blk == "home") or (sec == "Away" and blk == "away")) and len(out[key]) < 10:
                out[key].append(mine)
    tmp = f"{f}.{os.getpid()}.tmp"
    try: json.dump(out, open(tmp, "w")); os.replace(tmp, f)
    except OSError: pass
    return out

def fs_rate(leg, F):
    """share of the home side's home games and the away side's away games where this pick would have won (full-time markets only)"""
    H, A = F.get("home_home") or F.get("home_all"), F.get("away_away") or F.get("away_all")
    if not H or not A or len(H) < 4 or len(A) < 4: return None
    r = lambda L, fn: sum(1 for x in L if fn(*x)) / len(L)
    both = lambda fh, fa: (r(H, fh) + r(A, fa)) / 2
    mid, oid, spec = leg["mid"], leg["oid"], leg["spec"]
    ln = float(spec.split("=")[1]) if spec.startswith("total=") else None
    if mid in ("1", "60200"):
        return {"1": both(lambda g, a: g > a, lambda g, a: g < a), "3": both(lambda g, a: g < a, lambda g, a: g > a), "2": both(lambda g, a: g == a, lambda g, a: g == a)}.get(oid)
    if mid == "10": return {"9": both(lambda g, a: g >= a, lambda g, a: g <= a), "11": both(lambda g, a: g <= a, lambda g, a: g >= a)}.get(oid)
    if mid == "29": return both(lambda g, a: g > 0 and a > 0, lambda g, a: g > 0 and a > 0) if oid == "74" else None
    if mid == "860" and oid == "74": return both(lambda g, a: g > a or (g > 0 and a > 0), lambda g, a: g < a or (g > 0 and a > 0))
    if mid == "450001" and oid == "25": return both(lambda g, a: g + a >= 2, lambda g, a: g + a >= 2)
    if mid == "18" and ln is not None:
        ov = oid == "12"; return both(lambda g, a: (g + a > ln) == ov, lambda g, a: (g + a > ln) == ov)
    return None

def confirm(P, need):
    """check the best `need`*2 legs (at least 40) against Flashscore venue form and footybets; drop contradicted ones"""
    top, rest = P[:max(need * 2, 40)], P[max(need * 2, 40):]
    scores.day_list(False)           # Flashscore game list: the listener keeps it warm; never wait for it here (first minutes after a restart: no Flashscore check)
    import sources
    def one(p):
        fid = scores.match_fid(dict(k=p["eid"] + "|x", game=p["game"], ts=p["ts"]))
        F = fs_form(fid) if fid else None
        try: more = sources.check(p)
        except Exception: more = ([], [])
        try:
            import season
            ss, sb = season.check_leg(p); more = (more[0] + ss, more[1] + sb)        # whole-season record (lower divisions too)
        except Exception: pass
        return p, (fs_rate(p, F) if F else None), fb_verdict(p, fb_for(p)), more
    with ThreadPoolExecutor(8) as ex: R = list(ex.map(one, top))
    out = []; dropped = 0
    for p, fr, (sup, bad), (msup, mbad) in R:
        if bad or mbad or (fr is not None and fr < .5): dropped += 1; continue      # any source clearly against it: out
        extra = []
        if fr is not None: extra.append(f"Flashscore home/away form {fr:.0%}")
        if sup: extra.append(f"footybets {sup}%")
        extra += msup
        if extra: p["why"] += "; " + ", ".join(extra)
        p["src"] = 1 + (fr is not None) + (sup is not None) + len(msup)
        out.append(p)
    return out + rest, dropped
