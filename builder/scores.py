#!/usr/bin/env python3
"""Live and final scores for the legs of recent phone tickets (builds.json), from Flashscore (Sportradar as a backup),
with each leg marked won / lost / still open. Written to scores.json and pushed to the 'scores' branch, which
build.html reads through the GitHub API (no website rebuild needed, so it can refresh every minute or so)."""
import json, os, re, subprocess, time, difflib, unicodedata, base64, datetime as dt
from concurrent.futures import ThreadPoolExecutor
HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
FS = ["-H", "x-fsign: SW9D1eZo", "-H", "Referer: https://www.flashscore.com/"]
BUILDS = os.path.join(HERE, "builds.json"); OUT = os.path.join(HERE, "scores.json")
_list = {"at": 0, "rows": []}; _fid = {}; _final = {}; _codes = {}

def fs(name, tries=6, limit=60):
    """Flashscore feeds often get cut off mid-transfer; a complete one ends with an "A1÷" record"""
    best = ""
    for i in range(tries):
        out = subprocess.run(["curl", "-s", "--compressed", "-m", str(limit), "-A", UA, *FS, f"https://local-global.flashscore.ninja/2/x/feed/{name}"],
                             capture_output=True, text=True, errors="ignore").stdout
        if "A1÷" in out: return out
        if len(out) > len(best): best = out
        time.sleep(1)
    return best
def recs(s): return [dict(kv.split("÷", 1) for kv in r.split("¬") if "÷" in kv) for r in s.split("~")]
def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(fc|fk|sc|cf|cd|ac|afc|if|ik|jk|sk|club|de|the|w|women|wfc|lfc|u\d\d|reserves?|ii|b)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)
def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b: return 0
    return 1.0 if (a in b or b in a) and min(len(a), len(b)) >= 4 else difflib.SequenceMatcher(None, a, b).ratio()

def day_list():
    if time.time() - _list["at"] > 20 * 60:
        with ThreadPoolExecutor(3) as ex: feeds = list(ex.map(lambda off: fs(f"f_1_{off}_3_en_1", 8), (-1, 0, 1)))
        rows = [dict(id=r["AA"], h=r.get("AE", ""), a=r.get("AF", ""), t=int(r.get("AD", 0) or 0)) for f in feeds for r in recs(f) if "AA" in r]
        if rows: _list.update(at=time.time(), rows=rows)
    return _list["rows"]

def match_fid(leg):
    if leg["k"] in _fid: return _fid[leg["k"]]
    h, a = leg["game"].split(" vs ", 1); best = None
    for r in day_list():
        if abs(r["t"] - leg["ts"]) > 50 * 60: continue
        s = min(sim(h, r["h"]), sim(a, r["a"]))
        if s >= 0.72 and (best is None or s > best[0]): best = (s, r["id"])
    _fid[leg["k"]] = best[1] if best else None
    return _fid[leg["k"]]

def flash_score(fid):
    """status ns/live/ht/ft, full score, half-time score, minute"""
    d = next((r for r in recs(fs(f"dc_1_{fid}", 3, 20)) if "DA" in r), None)
    if not d: return None
    st = {"1": "ns", "2": "live", "3": "ft"}.get(d.get("DA"), "ns"); det = d.get("DB", "")
    h, a = int(d.get("DE") or 0), int(d.get("DF") or 0)
    ht = None
    if st != "ns" and det not in ("12",):   # 1st half over: get the half-time score
        su = recs(fs(f"df_sur_1_{fid}", 3, 20))
        for r in su:
            if "BA" in r and "BB" in r: ht = (int(r["BA"] or 0), int(r["BB"] or 0)); break
    minute = ""
    if st == "live":
        if det == "38": minute = "HT"
        elif det in ("12", "13") and d.get("DD"):
            m = int((time.time() - int(d["DD"])) / 60) + 1 + (45 if det == "13" else 0)
            minute = f"{min(m, 45 if det == '12' else 90)}'" + ("+" if (det == "12" and m > 45) or (det == "13" and m > 90) else "")
        else: minute = "live"
    return dict(st=st, h=h, a=a, ht=ht, min=minute, det=det, src="flashscore")

def radar_score(eid):
    d = subprocess.run(["curl", "-s", "-m", "30", "-A", UA, "-H", "Referer: https://www.sportybet.com/",
                        f"https://stats.fn.sportradar.com/sportybet/en/Europe:Berlin/gismo/match_info/{eid.split(':')[-1]}"], capture_output=True, text=True).stdout
    try: m = json.loads(d)["doc"][0]["data"]["match"]
    except Exception: return None
    r = m.get("result") or {}; p = (m.get("periods") or {}).get("p1")
    if r.get("home") is None: return dict(st="ns", h=0, a=0, ht=None, min="", src="sportradar")
    st = "ft" if (m.get("status") or {}).get("name") in ("Ended", "AET", "AP") else "live"
    return dict(st=st, h=r["home"], a=r["away"], ht=(p["home"], p["away"]) if p else None, min="" if st == "ft" else "live", src="sportradar")

def verdict(leg, s):
    """W / L when decided, '' while still open (or when the half-time score is needed but missing)"""
    mid, oid, spec = leg["mid"], leg["oid"], leg["spec"]; h, a, ht = s["h"], s["a"], s["ht"]; fin = s["st"] == "ft"
    line = float(spec.split("=")[1]) if spec.startswith("total=") else None
    if mid == "1":
        if not fin: return ""
        return "W" if (oid == "1" and h > a) or (oid == "2" and h == a) or (oid == "3" and a > h) else "L"
    if mid == "18":
        tot = h + a; over = oid == "12"
        if tot > line: return "W" if over else "L"
        return ("L" if over else "W") if fin else ""
    if mid in ("68", "69", "70"):           # 1st half totals: decided by the half-time score
        first_half_done = ht is not None or fin
        g = None
        if ht is not None: g = {"68": ht[0] + ht[1], "69": ht[0], "70": ht[1]}[mid]
        elif s["st"] == "live" and s.get("det") == "12":
            g = {"68": h + a, "69": h, "70": a}[mid]   # still in the 1st half: current score is the 1st-half score
        if g is not None and g > line: return "W" if oid == "12" else "L"
        if ht is not None: return ("L" if oid == "12" else "W")
        return ""
    if mid in ("90", "91", "92"):           # 2nd half totals: full time minus half time
        if ht is None: return ""
        g = {"90": (h + a) - (ht[0] + ht[1]), "91": h - ht[0], "92": a - ht[1]}[mid]
        if g > line: return "W" if oid == "12" else "L"
        return ("L" if oid == "12" else "W") if fin else ""
    return ""

def backfill(b):
    """older builds have no event ids: read them once from the booking code"""
    for t in b.get("tickets", []):
        if all("k" in l for l in t["legs"]): continue
        if t["code"] not in _codes:
            raw = subprocess.run(["curl", "-s", "--compressed", "-m", "60", "-A", UA, "-H", "Referer: https://www.sportybet.com/ng/",
                                  f"https://www.sportybet.com/api/ng/orders/share/{t['code']}"], capture_output=True, text=True).stdout
            try: _codes[t["code"]] = json.loads(raw)["data"]["outcomes"]
            except Exception: _codes[t["code"]] = []
        for l, e in zip(t["legs"], _codes[t["code"]]):
            m = e["markets"][0]; o = m["outcomes"][0]
            l.update(ts=e["estimateStartTime"] / 1000, eid=e["eventId"], mid=m["id"], spec=m.get("specifier") or "", oid=o["id"],
                     k=f"{e['eventId'].split(':')[-1]}|{m['id']}|{m.get('specifier') or ''}|{o['id']}")

def run_once():
    try: B = json.load(open(BUILDS))
    except Exception: return {}
    now = time.time(); legs = {}
    for b in B:
        if not b.get("ok"): continue
        backfill(b)
        for t in b["tickets"]:
            for l in t["legs"]:
                if "k" in l and now - 36 * 3600 < l["ts"] < now + 5 * 60: legs[l["k"]] = l
    games = {}
    for k, l in legs.items(): games.setdefault(l["eid"], l)
    day_list()                                   # once, before the parallel lookups
    def score(l):
        gk = l["eid"]
        if gk in _final: return gk, _final[gk]
        if now < l["ts"]: return gk, None
        fid = match_fid(l)
        s = (flash_score(fid) if fid else None) or radar_score(gk)
        if s and s["st"] == "ft" and (s["ht"] is not None or s["src"] == "sportradar"): _final[gk] = s
        return gk, s
    with ThreadPoolExecutor(10) as ex: S = dict(ex.map(score, games.values()))
    out = {}
    for k, l in legs.items():
        s = S.get(l["eid"])
        if not s or s["st"] == "ns": continue
        out[k] = dict(st=s["st"], sc=f"{s['h']}-{s['a']}", ht=f"{s['ht'][0]}-{s['ht'][1]}" if s["ht"] else "", m=s["min"], r=verdict(l, s), src=s["src"])
    by_code = {}   # also by "CODE#leg number", so the page can match legs of older builds too
    for b in B:
        for t in b.get("tickets", []) if b.get("ok") else []:
            for i, l in enumerate(t["legs"]):
                if l.get("k") in out: by_code[f"{t['code']}#{i}"] = out[l["k"]]
    doc = dict(updated=int(now), legs=by_code)
    json.dump(doc, open(OUT, "w"))
    return doc

def publish(doc, repo_dir="/tmp/scores-branch"):
    """one-commit 'scores' branch, force-pushed (no history build-up, no website rebuild)"""
    tok = os.environ.get("GH_TOKEN"); repo = os.environ.get("GITHUB_REPOSITORY", "awelebienichi/sportybet-results")
    if not tok: return
    os.makedirs(repo_dir, exist_ok=True)
    g = lambda *a: subprocess.run(["git", "-C", repo_dir, *a], capture_output=True, text=True)
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        g("init", "-q"); g("checkout", "-q", "-b", "scores")
        g("config", "user.name", "results-bot"); g("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    json.dump(doc, open(os.path.join(repo_dir, "scores.json"), "w"))
    g("add", "scores.json"); g("commit", "-q", "--amend", "-m", "scores") if g("rev-parse", "HEAD").returncode == 0 else g("commit", "-q", "-m", "scores")
    g("push", "-q", "-f", f"https://x-access-token:{tok}@github.com/{repo}.git", "scores")

if __name__ == "__main__":
    d = run_once(); print(json.dumps(d, indent=1)[:3000])
