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
                topic=os.environ.get("NTFY_TOPIC", "") if uid == OWNER else "sportyuser-" + uid,
                track=uid == OWNER, page=f"https://awelebienichi.github.io/sportybet-results/build.html?u={uid}")

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
        out.append(dict(u=uid, name=prof.get("name", uid), last=prof.get("last", 0), builds=n, owner=bool(prof.get("owner"))))
    json.dump(sorted(out, key=lambda x: -x["last"]), open(os.path.join(USERS, "index.json"), "w"))
