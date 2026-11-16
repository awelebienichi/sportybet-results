#!/usr/bin/env python3
"""CompactStats (compactstats.com) – its own % for Over 1.5 / 2.5 / 3.5 goals and both teams to score, next to the bookmaker's
(margin removed). Tested 16 Nov 2026 on 293 finished games: overall it was no sharper than the odds (Over 2.5: said 58%, came true
53%; bookmaker said 59%), and on 66 of the builder's own goal legs it pointed both ways. So it only ever BACKS a goal leg (shown in
the "why" text), never blocks one, and every view is logged to cache/cstats_log.json so a week of results can decide if it earns more.
Only the pre-match builder uses it (extra.confirm); football live never calls it."""
import datetime as dt, json, os, re, subprocess, time
import scores
HERE = os.path.dirname(os.path.abspath(__file__)); CACHE = os.path.join(HERE, "cache", "cstats"); os.makedirs(CACHE, exist_ok=True)
LOG = os.path.join(HERE, "cache", "cstats_log.json")
UA = scores.UA; LAGOS = dt.timezone(dt.timedelta(hours=1))
KEYS = (("o15", "Over 1.5 goals"), ("o25", "Over 2.5 goals"), ("o35", "Over 3.5 goals"), ("btts", "Both teams to score: Yes"))

def _get(url, t=40):
    return subprocess.run(["curl", "-sL", "--compressed", "-m", str(t), "-A", UA, url], capture_output=True, text=True, errors="ignore").stdout

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

def pages():
    """{date: [(home, away, url)]} from the sitemap (every listed game, a week ahead)"""
    def get():
        out = {}
        for u in re.findall(r"<loc>(https://compactstats\.com/match/(\d{4}-\d\d-\d\d)/([^<]+?)/)</loc>", _get("https://compactstats.com/sitemap.xml")):
            url, day, slug = u
            if "-v-" not in slug: continue
            h, a = [x.replace("-", " ") for x in slug.split("-v-", 1)]
            out.setdefault(day, []).append((h, a, url))
        return out
    return _cached("sitemap.json", 3, get) or {}

def parse(s):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s, flags=re.S); t = re.sub(r"<[^>]+>", "|", t)
    t = re.sub(r"\|[\s|]*", "|", t); t = re.sub(r"\s+", " ", t)
    out = {}
    for k, lab in KEYS:
        m = re.search(r"\|(\d+)%\|" + re.escape(lab) + r"\|(?:[^|]*\|){1,3}?([\d.]+)\|price\|(\d+)%\|bookmaker", t)
        if m: out[k] = dict(p=int(m.group(1)) / 100, bk=int(m.group(3)) / 100)
    return out

def match(leg):
    """CompactStats' numbers for a leg's game, or None (game not listed)"""
    if " vs " not in leg["game"]: return None
    h, a = leg["game"].split(" vs ", 1)
    d = dt.datetime.fromtimestamp(leg["ts"], LAGOS).date()
    P = pages(); best = None
    for day in {d.isoformat(), (d - dt.timedelta(days=1)).isoformat()}:          # the site dates games by UK time
        for ph, pa, url in P.get(day, []):
            s = min(scores.sim(h, ph), scores.sim(a, pa))
            if s >= .72 and (best is None or s > best[0]): best = (s, url)
    if not best: return None
    return _cached("m_" + re.sub(r"[^a-z0-9]+", "_", best[1].split("/match/")[1]) + ".json", 3, lambda: parse(_get(best[1]))) or None

def _kind(leg):
    mid, oid, spec = leg["mid"], leg["oid"], leg.get("spec") or ""
    if mid == "18" and oid == "12": return {"total=1.5": "o15", "total=2.5": "o25", "total=3.5": "o35"}.get(spec)
    if mid == "29" and oid == "74": return "btts"
    if mid == "450001" and oid == "25": return "o15"
    return None

def _log(leg, k, C):
    try: L = json.load(open(LOG))
    except Exception: L = {}
    L[f"{leg['eid']}|{k}"] = dict(game=leg["game"], ts=leg["ts"], odds=leg.get("odds"), **C)
    tmp = f"{LOG}.{os.getpid()}.tmp"
    try: json.dump(L, open(tmp, "w")); os.replace(tmp, LOG)
    except OSError: pass

def check(leg):
    """(support notes, []) – backing only, goal legs only"""
    k = _kind(leg)
    if not k: return [], []
    try: M = match(leg)
    except Exception: M = None
    if not M or k not in M: return [], []
    C = M[k]; _log(leg, k, C)
    if C["p"] >= .6 and C["p"] >= C["bk"]: return [f"CompactStats {C['p']:.0%} (bookmaker {C['bk']:.0%})"], []
    return [], []

def warm():
    return sum(len(v) for v in pages().values())
