#!/usr/bin/env python3
# Offline unit tests for server.py logic (no sockets, no real tmux).
# Imports server.py as a module (its __main__ guard prevents binding) and
# monkeypatches the tmux layer `tx()` to simulate sessions.
import importlib.util, os, sys, time, tempfile, json, glob

SP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
spec = importlib.util.spec_from_file_location("deck", SP)
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

P = [0]; F = [0]
def ok(c, m):
    if c: P[0] += 1; print("  PASS:", m)
    else: F[0] += 1; print("  FAIL:", m)

print("[status heuristics + preview]")
ok(srv.status_of(["Do you want to proceed?", "❯ 1. Yes"]) == "input", "input: do-you-want")
# the real bug: prompt above, Claude's mode line pinned as the LAST line
ok(srv.status_of(["This command requires approval", "Do you want to proceed?",
                  "❯ 1. Yes", "  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents"]) == "input",
   "input detected even with mode line pinned below (the reported bug)")
ok(srv.status_of(["Esc to cancel · Tab to amend · ctrl+e to explain",
                  "  ⏵⏵ accept edits on (shift+tab to cycle)"]) == "input", "input: prompt footer")
ok(srv.status_of(["some output", "  Working… (esc to interrupt)"]) == "working", "live working indicator -> working")
ok(srv.status_of(["some output line", "  ⏵⏵ accept edits on (shift+tab to cycle)"]) == "idle", "idle detected")
# regression: stale indicators left high in the buffer must NOT be counted (they were)
ok(srv.status_of(["build step (esc to interrupt)"] + ["out %d" % i for i in range(20)]
                 + ["  ⏵⏵ accept edits on (shift+tab to cycle)"]) == "idle",
   "stale 'esc to interrupt' high in buffer -> idle (no false WORKING)")
ok(srv.status_of(["This command requires approval", "Do you want to proceed?"]
                 + ["opt %d" % i for i in range(20)] + ["  ⏵⏵ accept edits on"]) == "idle",
   "stale prompt text high in buffer -> idle (no false INPUT / spurious reminders)")
ok(srv.pick_preview(["Do you want to proceed?",
                     "  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents"]) == "Do you want to proceed?",
   "preview skips the mode line and shows the real prompt")

print("[sanitize / uniq_name]")
ok(srv.sanitize("My Project!") == "My-Project", "sanitize strips specials")
ok(srv.sanitize("...") == "session", "sanitize empty -> session")
srv.tx = lambda *a: "alpha\nbeta\n" if a[:1] == ("list-sessions",) else ""
ok(srv.uniq_name("alpha") == "alpha-2", "uniq_name resolves collision")
ok(srv.uniq_name("gamma") == "gamma", "uniq_name passes free name")

print("[find_jsonl against real logs + mtime-skip (2a)]")
real = None
for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
    cwd, title = srv.read_meta(f)
    if cwd:
        real = (f, cwd); break
ok(real is not None, "read_meta extracted a real cwd")
if real:
    f, cwd = real
    def fake_tx(*a):
        if a[:1] == ("list-sessions",): return "sess1\n"
        if a[:1] == ("display-message",): return cwd + "\n"
        return ""
    srv.tx = fake_tx
    srv._SESS_CWD.clear(); srv._last_glob = 0
    got = srv.find_jsonl("sess1")
    ok(got is not None and srv.JSONL_INDEX.get(got, {}).get("cwd") == cwd,
       "find_jsonl maps session cwd -> correct jsonl")
    # mtime-skip proof: clear index, force scan (reads all), force again (reads none)
    calls = [0]; orig = srv.read_meta
    srv.read_meta = lambda fp: (calls.__setitem__(0, calls[0] + 1), orig(fp))[1]
    srv.JSONL_INDEX.clear()
    calls[0] = 0; srv._last_glob = 0; srv.refresh_index(force=True); first = calls[0]
    calls[0] = 0; srv._last_glob = 0; srv.refresh_index(force=True); second = calls[0]
    ok(first > 0 and second == 0, "refresh_index re-reads all first pass, ZERO second (mtime skip)")
    srv.read_meta = orig

print("[chat offset tracking / incremental parse (2b)]")
d = tempfile.mkdtemp(); jf = os.path.join(d, "c.jsonl")
def wl(fh, role, text):
    content = text if role == "user" else [{"type": "text", "text": text}]
    fh.write(json.dumps({"cwd": "/x", "message": {"role": role, "content": content}}) + "\n")
with open(jf, "w") as fh:
    wl(fh, "user", "hello"); wl(fh, "assistant", "hi there")
srv.find_jsonl = lambda n: jf
srv.CHAT_CACHE.clear()
pc = [0]; op = srv.parse_into
srv.parse_into = lambda line, msgs: (pc.__setitem__(0, pc[0] + 1), op(line, msgs))[1]
m1 = srv.chat("s")
ok(len(m1) == 2 and m1[0]["t"] == "hello" and m1[1]["t"] == "hi there", "chat parses initial 2 msgs")
base = pc[0]
m1b = srv.chat("s")
ok(len(m1b) == 2 and pc[0] == base, "chat is a no-op when file unchanged (0 new parses)")
with open(jf, "a") as fh: wl(fh, "user", "again")
m2 = srv.chat("s")
ok(len(m2) == 3 and m2[2]["t"] == "again", "chat picks up appended message")
ok(pc[0] == base + 1, "chat parsed ONLY the 1 appended line (true incremental)")
with open(jf, "w") as fh: wl(fh, "user", "fresh")
m3 = srv.chat("s")
ok(len(m3) == 1 and m3[0]["t"] == "fresh", "chat handles truncation/rotation (offset reset)")
srv.parse_into = op

print("[state shape + one capture per session]")
cap = [0]
def stx(*a):
    if a[:1] == ("list-sessions",): return "control\nwork\n"
    if a[:1] == ("capture-pane",): cap[0] += 1; return "out line\nx… (esc to interrupt)\n"
    return ""
srv.tx = stx
st = srv.state()
ok("sessions" in st and "current" in st, "state has sessions+current")
ok(len(st["sessions"]) == 1 and st["sessions"][0]["name"] == "work", "state excludes control")
ok(st["sessions"][0]["status"] == "working" and st["sessions"][0]["preview"], "state derives status+preview")
ok(cap[0] == 1, "state issues exactly ONE capture-pane per session (was two)")

print("[history / dirs / ago]")
srv.tx = lambda *a: ""
h = srv.history()
ok(isinstance(h, list), "history returns a list")
if h:
    ok(all(all(kk in x for kk in ("cwd", "title", "ago", "dir")) for x in h), "history items well-formed")
    ok(all(h[i]["mtime"] >= h[i + 1]["mtime"] for i in range(len(h) - 1)), "history sorted recent-first")
dd = srv.dirs()
ok(isinstance(dd, list) and all("cwd" in x and "dir" in x for x in dd), "dirs well-formed")
ok(srv.ago(time.time() - 30) == "just now" and srv.ago(time.time() - 7200).endswith("h"), "ago formats")

print("[directory browser: list_subdirs / make_dir]")
bd = tempfile.mkdtemp()
os.makedirs(os.path.join(bd, "sub1")); os.makedirs(os.path.join(bd, "sub2"))
os.makedirs(os.path.join(bd, ".hidden"))
open(os.path.join(bd, "afile.txt"), "w").close()
ls = srv.list_subdirs(bd)
ok(ls["path"] == os.path.abspath(bd), "list_subdirs reports resolved path")
ok(ls["dirs"] == ["sub1", "sub2"], "list_subdirs returns only visible subdirs (no dotfiles, no files)")
ok(ls["parent"] == os.path.dirname(os.path.abspath(bd)), "list_subdirs gives parent for Up")
ok(srv.list_subdirs("/does/not/exist")["path"] == srv.HOME, "list_subdirs falls back to HOME on bad path")
new = srv.make_dir(bd, "created")
ok(new == os.path.join(bd, "created") and os.path.isdir(new), "make_dir creates the folder")
ok(srv.make_dir(bd, "../escape") is None and srv.make_dir(bd, "a/b") is None, "make_dir rejects path traversal")
ok("created" in srv.list_subdirs(bd)["dirs"], "new folder shows up in the browser listing")

print("[request handling: exact targets, no guessing, stale approvals, cross-site]")
import io
ok(srv.pane("decktest") == "=decktest:" and srv.sess_t("decktest") == "=decktest",
   "targets are exact-match (bare names prefix-match a different session)")
SCREEN = {"work": "Do you want to proceed?\n❯ 1. Yes\nEsc to cancel · Tab to amend\n"}
def htx(*a):
    if a[:1] == ("list-sessions",): return "control\n" + "".join(s + "\n" for s in SCREEN)
    if a[:1] == ("capture-pane",): return SCREEN.get(a[-1].strip("=:"), "")
    return ""
srv.tx = htx
RAN = []
srv.subprocess.run = lambda argv, *x, **kw: RAN.append(argv)
srv.action_log = lambda m: None
def req(method, path, headers=None):
    h = srv.H.__new__(srv.H)
    h.path = path; h.command = method; h.request_version = "HTTP/1.1"
    h.requestline = "%s %s HTTP/1.1" % (method, path); h.client_address = ("t", 0)
    hd = {"Host": "100.1.2.3:7682"}; hd.update(headers or {})
    h.headers = hd; h.wfile = io.BytesIO(); h.close_connection = False
    del RAN[:]
    (h.do_POST if method == "POST" else h.do_GET)()
    out = h.wfile.getvalue().decode("utf-8", "replace")
    return int(out.split(" ", 2)[1]), out, list(RAN)
keys = lambda ran: [a for a in ran if "send-keys" in a]
code, out, ran = req("POST", "/type?s=&t=rm%20-rf%20build")
ok(code == 404 and not keys(ran), "/type with no session is refused, not sent to the control shell")
code, out, ran = req("POST", "/key?s=decktest&k=Enter")
ok(code == 404 and not keys(ran), "/key for a closed session is refused (no prefix-match landing)")
code, out, ran = req("POST", "/type?s=work&t=hi", {"Origin": "http://100.1.2.3:7682",
                                                   "Sec-Fetch-Site": "same-origin"})
ok(code == 200 and ["-t", "=work:", "-l", "--", "hi"] == keys(ran)[0][2:],
   "/type from the panel's own page reaches the exact session")
code, out, ran = req("POST", "/type?s=work&t=hi", {"Origin": "https://evil.example"})
ok(code == 403 and not keys(ran), "/type from another site's page is refused")
code, out, ran = req("POST", "/key?s=work&k=Enter", {"Sec-Fetch-Site": "cross-site"})
ok(code == 403 and not keys(ran), "cross-site Sec-Fetch-Site is refused")
code, out, ran = req("GET", "/approve?s=work", {"Sec-Fetch-Site": "none"})
ok(code == 303 and "Location: /ack?r=approved&s=work" in out and keys(ran)[0][-2:] == ["=work:", "Enter"],
   "approve from a notification tap presses Enter, then redirects (reload can't re-press)")
code, out, ran = req("GET", "/deny?s=decktest")
ok(code == 303 and "r=gone" in out and not keys(ran), "deny for a closed session sends nothing")
SCREEN["work"] = "some output\n  Working… (esc to interrupt)\n"
code, out, ran = req("GET", "/deny?s=work")
ok(code == 303 and "r=stale" in out and not keys(ran),
   "late Deny while Claude is working sends nothing (Escape would interrupt it)")
code, out, ran = req("GET", "/approve?s=work", {"Referer": "https://evil.example/page"})
ok(code == 403 and not keys(ran), "approve linked from another site is refused")
code, out, ran = req("GET", "/ack?r=stale&s=work")
ok(code == 200 and "Nothing to answer" in out, "ack page explains a stale tap")
code, out, ran = req("GET", "/state")
ok(code == 200 and "Access-Control-Allow-Origin" not in out, "no wildcard CORS (other sites can't read chats)")

print("\n=== python: %d passed, %d failed ===" % (P[0], F[0]))
sys.exit(1 if F[0] else 0)
