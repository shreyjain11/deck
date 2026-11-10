#!/usr/bin/env python3
"""keepawake v3 — hold sleep off while (AC power) OR (Claude running, on battery),
with guardrails: time cap, battery floor, thermal release, ntfy alerts, logging.
Config: ~/rcc/keepawake.conf (created with defaults on first run)."""
import json, os, shutil, signal, subprocess, sys, time, urllib.request

HOME = os.path.expanduser("~")
LOG  = os.path.join(HOME, "rcc/logs/keepawake.log")
CONF = os.path.join(HOME, "rcc/keepawake.conf")
TOPIC_F = os.path.join(HOME, "rcc/.ntfy_topic")
# Apple silicon and Intel Homebrew prefixes (launchd's PATH has neither).
TMUX = next((p for p in (os.environ.get("DECK_TMUX"), "/opt/homebrew/bin/tmux",
                         "/usr/local/bin/tmux") if p and os.path.exists(p)),
            shutil.which("tmux") or "tmux")

DEFAULTS = {"batt_floor": 20, "max_hold_hours": 4.0, "thermal_limit": 70, "notify": 1}

def conf():
    c = dict(DEFAULTS)
    try:
        for line in open(CONF):
            if "=" in line:
                k, v = line.split("=", 1); k = k.strip()
                if k in c:
                    c[k] = float(v.strip()) if "." in v else int(float(v.strip()))
    except FileNotFoundError:
        with open(CONF, "w") as f:
            for k, v in DEFAULTS.items():
                f.write("%s=%s\n" % (k, v))
    return c

def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass

def notify(title, msg, pri=4):
    if not conf()["notify"]:
        return
    try:
        topic = open(TOPIC_F).read().strip()
        body = {"topic": topic, "title": title, "message": msg, "priority": pri}
        urllib.request.urlopen(urllib.request.Request("https://ntfy.sh",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}), timeout=6)
    except Exception as e:
        log("notify failed: %s" % e)

def run(*a):
    try:
        return subprocess.run(list(a), capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return ""

def on_ac():
    return "AC Power" in run("/usr/bin/pmset", "-g", "batt")

def batt_pct():
    for tok in run("/usr/bin/pmset", "-g", "batt").replace(";", " ").split():
        if tok.endswith("%"):
            try: return int(tok.rstrip("%"))
            except ValueError: pass
    return 100

def thermal_ok(limit):
    out = run("/usr/bin/pmset", "-g", "therm")
    for line in out.splitlines():
        if "CPU_Speed_Limit" in line or "CPU_Scheduler_Limit" in line:
            try:
                val = int(line.split("=")[1].strip())
                if val < limit:
                    return False, val
            except Exception:
                pass
    return True, 100

def claude_running():
    for pid in run(TMUX, "list-panes", "-a", "-F", "#{pane_pid}").split():
        for child in run("/usr/bin/pgrep", "-P", pid).split():
            cmd = run("/bin/ps", "-o", "comm=", "-p", child).strip()
            if "claude" in cmd or "node" in cmd:
                return True
    return False

holding = False; caf = None; hold_kind = None; hold_start = 0; blocked = False

def set_hold(on, kind=None, reason=""):
    global holding, caf, hold_kind, hold_start
    if on and not holding:
        caf = subprocess.Popen(["/usr/bin/caffeinate", "-dims"])
        subprocess.run(["sudo", "/usr/bin/pmset", "-a", "disablesleep", "1"])
        holding, hold_kind, hold_start = True, kind, time.time()
        log("HOLD start (%s)" % kind)
        if kind == "battery":
            notify("Deck: battery mode on",
                   "Mac staying awake on battery (capped at %sh, floor %s%%)."
                   % (conf()["max_hold_hours"], conf()["batt_floor"]), 3)
    elif not on and holding:
        if caf:
            try: caf.terminate()
            except Exception: pass
        subprocess.run(["sudo", "/usr/bin/pmset", "-a", "disablesleep", "0"])
        holding = False
        log("HOLD release (%s)" % reason)
        if hold_kind == "battery" and reason not in ("ac", "sessions-ended"):
            notify("Deck: Mac will sleep", "Reason: %s. It will be unreachable until woken." % reason)

def stop(signum, frame):
    set_hold(False, reason="keepawake stopped")
    sys.exit(0)

signal.signal(signal.SIGTERM, stop)        # launchctl bootout / unload / restart
signal.signal(signal.SIGINT, stop)

log("keepawake v3 started")
# disablesleep survives this process. An instance that died mid-hold (crash, restart,
# unload) left it at 1, and a fresh instance starts with holding=False, so nothing
# ever reset it: the Mac would never sleep again, even lid-closed on battery.
subprocess.run(["sudo", "/usr/bin/pmset", "-a", "disablesleep", "0"])
while True:
    try:
        c = conf()
        ac = on_ac()
        cl = claude_running()
        if ac:
            blocked = False
            set_hold(True, "ac")
        elif cl and not blocked:
            pct = batt_pct()
            t_ok, t_val = thermal_ok(c["thermal_limit"])
            elapsed_h = (time.time() - hold_start) / 3600 if (holding and hold_kind == "battery") else 0
            if not holding:
                if pct > c["batt_floor"] and t_ok:
                    set_hold(True, "battery")
            else:
                if hold_kind == "ac":
                    hold_kind, hold_start = "battery", time.time()
                    notify("Deck: switched to battery mode",
                           "Unplugged with sessions running. Capped at %sh." % c["max_hold_hours"], 3)
                elif pct <= c["batt_floor"]:
                    set_hold(False, reason="battery at %d%%" % pct); blocked = True
                elif not t_ok:
                    set_hold(False, reason="thermal throttling (%d%%)" % t_val); blocked = True
                elif elapsed_h >= c["max_hold_hours"]:
                    set_hold(False, reason="%sh time cap reached" % c["max_hold_hours"]); blocked = True
        else:
            if holding:
                set_hold(False, reason="sessions-ended" if not cl else "blocked")
            if not cl:
                blocked = False
    except Exception as e:
        log("loop error: %s" % e)
    time.sleep(30)
