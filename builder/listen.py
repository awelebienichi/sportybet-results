#!/usr/bin/env python3
"""Always-on listener (one GitHub Actions run at a time, each up to ~5h45m, the next one queued behind it).
- checks for build.html requests every few seconds and builds them straight away
- a background thread keeps the 7-day game list and every team's last-10 form warm, so a build only has to do the
  final checks (head-to-head) and the booking"""
import os, sys, time, threading, traceback
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import poll, phonebuild
END = time.time() + int(os.environ.get("RUN_SECONDS", "20700"))
LOCK = threading.Lock()

def warm():
    while time.time() < END - 600:
        try:
            t0 = time.time()
            with LOCK: G = phonebuild.games_cached(time.time(), time.time() + 7 * 86400, fresh=0)   # fresh list
            phonebuild.fetch_teams(G)                                                           # cached files < 6h old are reused
            import extra, scores
            print("warm: footybets tips", extra.warm_footybets(), "| flashscore games", len(scores.day_list(True)), flush=True)
            print(f"warm: {len(G)} games, team form ready in {time.time()-t0:.0f}s", flush=True)
        except Exception: traceback.print_exc()
        time.sleep(90 * 60)

def live_scores():
    import scores
    while time.time() < END:
        try: scores.publish(scores.run_once())
        except Exception: traceback.print_exc()
        time.sleep(75)

threading.Thread(target=warm, daemon=True).start()
threading.Thread(target=live_scores, daemon=True).start()
state = poll.load_state()
while time.time() < END:
    try:
        ms = poll.waiting(state)
        if ms:
            with LOCK: poll.handle(ms, state)
            poll.save()
    except Exception: traceback.print_exc()
    time.sleep(6)
print("listener run finished; the next queued run takes over", flush=True)
