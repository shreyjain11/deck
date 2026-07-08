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

last = {}
while True:
    for s in get():
        n = s["name"]; st = s["status"]
        if n == "control":
            continue
        prev = last.get(n)
        if st == "input" and prev != "input":
            base = "http://%s:7682/keyto?s=%s" % (TS, urllib.parse.quote(n))
            push("%s needs approval" % n,
                 "Claude is waiting for you. Approve to allow, or Deny to cancel.",
                 [{"action": "http", "label": "Approve", "url": base + "&k=Enter", "method": "POST", "clear": True},
                  {"action": "http", "label": "Deny", "url": base + "&k=Escape", "method": "POST", "clear": True}],
                 pri=5)
            extra_alert("%s needs your approval - open Deck to respond." % n)
        elif st == "idle" and prev == "working":
            push("%s finished" % n, "Session is done and back to idle.", pri=3)
        last[n] = st
    time.sleep(3)
