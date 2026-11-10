"""Who a request is for. Every person who opens build.html picks a name once; the page then sends their id ("u") with
every request. Each person's tickets, rollover and notifications are kept apart under builder/users/<id>/.
The site owner's codes also go to the main results tracker and to the owner's private ntfy topic (NTFY_TOPIC)."""
import json, os, re, time
HERE = os.path.dirname(os.path.abspath(__file__))
USERS = os.path.join(HERE, "users"); os.makedirs(USERS, exist_ok=True)
OWNER = "owner-j7k2"          # the site owner's personal id (link: build.html?u=owner-j7k2)

def resolve(u):
    """a safe user id; requests without one (old pages) belong to the owner"""
    u = re.sub(r"[^a-z0-9-]", "", str(u or "").lower())[:40]
    return u if len(u) >= 3 else OWNER

def folder(uid):
    d = os.path.join(USERS, uid); os.makedirs(d, exist_ok=True); return d

def paths(uid):
    d = folder(uid)
    return dict(builds=os.path.join(d, "builds.json"), roll=os.path.join(d, "rollover.json"), profile=os.path.join(d, "profile.json"),
                topic=os.environ.get("NTFY_TOPIC", "") if uid == OWNER else "",      # only the owner gets ntfy; everyone else sees codes on the site
                track=uid == OWNER, page=f"https://sportybet.awelebienichi.workers.dev/build.html?u={uid}")

def remember(uid, name):
    p = paths(uid)["profile"]
    try: prof = json.load(open(p))
    except Exception: prof = {"created": int(time.time())}
    if name: prof["name"] = str(name)[:40]
    if uid == OWNER: prof["owner"] = True
    prof["last"] = int(time.time())
    json.dump(prof, open(p, "w"))

def all_users():
    return [d for d in sorted(os.listdir(USERS)) if os.path.isdir(os.path.join(USERS, d))]

def write_index():
    """users/index.json: who uses the builder (names, last visit, ticket count) for the owner's All users view"""
    out = []
    for uid in all_users():
        P = paths(uid)
        try: prof = json.load(open(P["profile"]))
        except Exception: prof = {}
        try: n = len(json.load(open(P["builds"])))
        except Exception: n = 0
        out.append(dict(u=uid, name=prof.get("name", uid), last=prof.get("last", 0), builds=n, owner=bool(prof.get("owner")), suspended=bool(prof.get("suspended"))))
    json.dump(sorted(out, key=lambda x: -x["last"]), open(os.path.join(USERS, "index.json"), "w"))

import hashlib
def is_admin(key):
    """the owner's admin key (only its salted fingerprint is stored in the public repo)"""
    try: want = json.load(open(os.path.join(HERE, "admin.json")))["sha256"]
    except Exception: return False
    return bool(key) and hashlib.sha256(("sportybet-admin:" + str(key)).encode()).hexdigest() == want

import hmac
_SEEN = set()
def signed_ok(req):
    """owner actions are signed on the owner's phone: sig = HMAC-SHA256(owner key, "ts|type|target|u"), valid 10 minutes, once.
    The key itself never travels (requests go through a public channel); the cloud knows it from the ADMIN_KEY secret."""
    key = os.environ.get("ADMIN_KEY", "")
    try: ts = int(req.get("ts", 0))
    except Exception: return False
    sig = str(req.get("sig", ""))
    if not key or not sig or abs(time.time() - ts / 1000) > 600 or sig in _SEEN: return False
    msg = f"{ts}|{req.get('type', '')}|{req.get('target', '')}|{req.get('u', '')}"
    ok = hmac.compare_digest(hmac.new(key.encode(), msg.encode(), "sha256").hexdigest(), sig)
    if ok: _SEEN.add(sig)
    return ok

def profile(uid):
    try: return json.load(open(paths(uid)["profile"]))
    except Exception: return {}

def suspended(uid): return bool(profile(uid).get("suspended"))

def set_suspended(uid, on):
    p = paths(uid)["profile"]; prof = profile(uid)
    prof["suspended"] = bool(on); prof["suspended_at"] = int(time.time()) if on else None
    json.dump(prof, open(p, "w"))
