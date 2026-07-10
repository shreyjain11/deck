#!/usr/bin/env python3
import json, os, time, urllib.request, urllib.parse, subprocess, base64

HOME = os.path.expanduser("~")
TOPIC = open(os.path.join(HOME, "rcc/.ntfy_topic")).read().strip()
TS = open(os.path.join(HOME, "rcc/.ts_ip")).read().strip()

def get():
    try:
        return json.load(urllib.request.urlopen("http://%s:7682/state" % TS, timeout=4))["sessions"]
    except Exception:
        return []

def push(title, msg, actions=None, pri=4):
    body = {"topic": TOPIC, "title": title, "message": msg, "priority": pri}
    if actions:
        body["actions"] = actions
    try:
        req = urllib.request.Request("https://ntfy.sh",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=6)
    except Exception:
        pass

# ── Optional extra alert channels (both OFF unless you add the config file) ──
# These are plain text (no Approve/Deny buttons — tap through to Deck for that).
def _imessage(text):
    f = os.path.join(HOME, "rcc", ".imessage_to")     # file contains phone/Apple ID
    if not os.path.exists(f):
        return False
    to = open(f).read().strip()
    if not to:
        return False
    script = ('on run {t, m}\n tell application "Messages"\n'
              '  set svc to 1st account whose service type = iMessage\n'
              '  send m to participant t of svc\n end tell\nend run')
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script, to, text],
                       capture_output=True, timeout=15)
        return True
    except Exception:
        return False

def _twilio(text):
    f = os.path.join(HOME, "rcc", ".twilio.json")      # {sid, token, from, to}
    if not os.path.exists(f):
        return False
    try:
        c = json.load(open(f))
        data = urllib.parse.urlencode({"From": c["from"], "To": c["to"], "Body": text}).encode()
        req = urllib.request.Request(
            "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % c["sid"], data=data)
        req.add_header("Authorization",
                       "Basic " + base64.b64encode(("%s:%s" % (c["sid"], c["token"])).encode()).decode())
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False

def extra_alert(text):
    if not _imessage(text):     # prefer free/local iMessage; fall back to Twilio SMS
        _twilio(text)

REMIND_SEC = 300   # re-notify every 5 min while a prompt is still unanswered

def notify_approval(n, first):
    q = urllib.parse.quote(n)
    # 'view' actions open the URL in Safari (foreground -> Tailscale routes it,
    # cleartext allowed). Background 'http' POSTs from the ntfy app do NOT reach
    # a cleartext tailnet endpoint on iOS, so tapping those did nothing.
    ap = "http://%s:7682/approve?s=%s" % (TS, q)
    dn = "http://%s:7682/deny?s=%s" % (TS, q)
    msg = ("Claude is waiting for you. Tap Approve to allow, or Deny to cancel."
           if first else "Still waiting - tap Approve to allow, or Deny to cancel.")
    push(("%s needs approval" if first else "%s still needs approval") % n, msg,
         [{"action": "view", "label": "Approve", "url": ap, "clear": True},
          {"action": "view", "label": "Deny", "url": dn, "clear": True}],
         pri=5)
    extra_alert("%s needs your approval - open Deck to respond." % n)

last = {}
pending = {}       # session -> time we last pushed an approval (drives reminders)
while True:
    now = time.time()
    names = set()
    for s in get():
        n = s["name"]; st = s["status"]
        if n == "control":
            continue
        names.add(n)
        prev = last.get(n)
        if st == "input":
            if prev != "input" or now - pending.get(n, 0) >= REMIND_SEC:
                notify_approval(n, prev != "input")
                pending[n] = now
        else:
            pending.pop(n, None)                     # answered/moved on -> stop reminding
            if st == "idle" and prev == "working":
                push("%s finished" % n, "Session is done and back to idle.", pri=3)
        last[n] = st
    for d in (last, pending):                        # forget sessions that vanished
        for k in [k for k in d if k not in names]:
            d.pop(k, None)
    time.sleep(3)
