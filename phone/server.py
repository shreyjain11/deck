#!/usr/bin/env python3
import json, glob, os, subprocess, sys, time, urllib.parse, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

TMUX = "/opt/homebrew/bin/tmux"
HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
INDEX_FILE = os.path.join(HOME, "rcc", ".state", "deck_index.json")
SELECTED = "control"

LOCK = threading.Lock()
JSONL_INDEX = {}   # path -> {"mtime": float, "cwd": str|None, "title": str|None}
CHAT_CACHE = {}    # path -> {"offset": int, "buf": bytes, "msgs": [ {r,t} ]}
_SESS_CWD = {}     # session name -> (ts, cwd)   short cache to spare tmux calls
_last_glob = 0.0


# ------------------------------------------------------------------ tmux ----
def tx(*a):
    try:
        return subprocess.run([TMUX] + list(a), capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""

def sessions():
    return [s for s in tx("list-sessions", "-F", "#{session_name}").split("\n") if s]

def sel():
    global SELECTED
    if SELECTED not in sessions():
        ss = sessions(); SELECTED = ss[0] if ss else "control"
    return SELECTED

def tail(n, c=40):
    return tx("capture-pane", "-pt", n).rstrip("\n").split("\n")[-c:]

def status_of(lines):
    # Scan the WHOLE visible pane, not just the tail: Claude pins its mode line
    # ("accept edits on …") at the very bottom, so an approval prompt sits above it.
    t = "\n".join(lines).lower()
    if ("do you want to proceed" in t or "requires approval" in t
            or "esc to cancel" in t or "tab to amend" in t
            or "yes, allow" in t or "no, and tell claude" in t
            or "yes, and don't ask" in t):
        return "input"
    if "esc to interrupt" in t:
        return "working"
    return "idle"

def status(n):
    return status_of(tail(n, 200))

# Claude's persistent UI chrome + prompt scaffolding — not useful as a preview.
_CHROME = ("accept edits on", "manual mode on", "? for shortcuts", "for agents",
           "tab to cycle", "shift+tab", "esc to cancel", "tab to amend",
           "ctrl+e to explain", "esc to interrupt")

def pick_preview(lines):
    """Last meaningful pane line — skip Claude's mode line, prompt footer,
    menu options and separators so cards show real content (e.g. the question)."""
    for l in reversed(lines):
        s = l.strip()
        if not s:
            continue
        low = s.lower()
        if any(c in low for c in _CHROME):
            continue
        opt = s.lstrip("❯>▸* ").strip()            # skip menu options ("1. Yes", "3. No")
        if len(opt) >= 2 and opt[0].isdigit() and opt[1] in ".)":
            continue
        if sum(ch.isalnum() for ch in s) < 3:       # skip box-drawing/separators
            continue
        return s[:60]
    return ""

def session_cwd(n):
    now = time.time()
    c = _SESS_CWD.get(n)
    if c and now - c[0] < 2:
        return c[1]
    cwd = tx("display-message", "-p", "-t", n, "#{pane_current_path}").strip()
    _SESS_CWD[n] = (now, cwd)
    return cwd

def sanitize(name):
    out = "".join(ch if (ch.isalnum() or ch in "_-") else "-" for ch in name).strip("-")
    return out or "session"

def uniq_name(base):
    base = sanitize(base); existing = set(sessions())
    if base not in existing:
        return base
    i = 2
    while "%s-%d" % (base, i) in existing:
        i += 1
    return "%s-%d" % (base, i)


# --------------------------------------------------- jsonl index (2a) -------
def read_meta(f):
    """First cwd and first real user message (title) — one pass over the file."""
    cwd = None; title = None
    try:
        with open(f, errors="replace") as fh:
            for line in fh:
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if cwd is None and j.get("cwd"):
                    cwd = j["cwd"]
                if title is None:
                    m = j.get("message")
                    if isinstance(m, dict) and m.get("role") == "user":
                        c = m.get("content")
                        if isinstance(c, str):
                            t = c
                        elif isinstance(c, list):
                            t = "\n".join(b.get("text", "") for b in c
                                          if isinstance(b, dict) and b.get("type") == "text")
                        else:
                            t = ""
                        t = (t or "").strip()
                        if t and not t.startswith("<") and "Caveat:" not in t:
                            title = t.split("\n")[0][:80]
                if cwd is not None and title is not None:
                    break
    except Exception:
        pass
    return cwd, title

def save_index():
    try:
        os.makedirs(os.path.dirname(INDEX_FILE), exist_ok=True)
        with open(INDEX_FILE, "w") as fh:
            json.dump(JSONL_INDEX, fh)
    except Exception:
        pass

def load_index():
    global JSONL_INDEX
    try:
        with open(INDEX_FILE) as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            JSONL_INDEX = d
    except Exception:
        pass

def refresh_index(force=False):
    """Re-scan projects, but only re-read files whose mtime changed (2a)."""
    global _last_glob
    now = time.time()
    with LOCK:
        if not force and now - _last_glob < 5:
            return
        _last_glob = now
        seen = set()
        for f in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
            seen.add(f)
            try:
                m = os.path.getmtime(f)
            except OSError:
                continue
            e = JSONL_INDEX.get(f)
            if e and e.get("mtime") == m:
                continue                       # unchanged -> skip the read
            cwd, title = read_meta(f)
            JSONL_INDEX[f] = {"mtime": m, "cwd": cwd, "title": title}
        for f in list(JSONL_INDEX):
            if f not in seen:
                del JSONL_INDEX[f]             # prune deleted logs
        save_index()

def index_snapshot():
    with LOCK:                                     # avoid "dict changed size" races
        return list(JSONL_INDEX.items())

def path_for_cwd(cwd):
    best = None; bm = -1.0
    for f, e in index_snapshot():
        if e.get("cwd") == cwd and e.get("mtime", -1) > bm:
            best = f; bm = e["mtime"]
    return best

def find_jsonl(n):
    cwd = session_cwd(n)
    if not cwd:
        return None
    refresh_index()
    p = path_for_cwd(cwd)
    if p is None:
        refresh_index(force=True)             # cache miss -> full rescan once
        p = path_for_cwd(cwd)
    return p


# --------------------------------------------- chat (offset tracking, 2b) ---
def parse_into(line, msgs):
    try:
        j = json.loads(line)
    except Exception:
        return
    m = j.get("message")
    if not isinstance(m, dict):
        return
    role = m.get("role"); content = m.get("content")
    if role == "user":
        if isinstance(content, str):
            txt = content
        elif isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            if not parts:
                return
            txt = "\n".join(parts)
        else:
            return
        txt = txt.strip()
        if txt and not txt.startswith("<") and "Caveat:" not in txt:
            msgs.append({"r": "user", "t": txt})
    elif role == "assistant" and isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                msgs.append({"r": "asst", "t": b["text"].strip()})
            elif b.get("type") == "tool_use":
                msgs.append({"r": "tool", "t": "used " + b.get("name", "tool")})

def chat(n):
    f = find_jsonl(n)
    if not f:
        return []
    with LOCK:
        try:
            size = os.path.getsize(f)
        except OSError:
            return []
        c = CHAT_CACHE.get(f)
        if not c or size < c["offset"]:          # new file or truncated/rotated
            c = {"offset": 0, "buf": b"", "msgs": []}
            CHAT_CACHE[f] = c
        if size > c["offset"]:
            try:
                with open(f, "rb") as fh:
                    fh.seek(c["offset"])
                    data = fh.read()
            except OSError:
                return list(c["msgs"][-60:])
            c["offset"] = size
            raw = c["buf"] + data
            parts = raw.split(b"\n")
            c["buf"] = parts.pop()                # keep trailing partial line
            for pb in parts:
                if not pb.strip():
                    continue
                parse_into(pb.decode("utf-8", "replace"), c["msgs"])
            if len(c["msgs"]) > 1000:             # bound memory
                c["msgs"] = c["msgs"][-500:]
        return list(c["msgs"][-60:])


# ----------------------------------------------------- state / history -----
def state():
    out = []
    ss = sessions()
    for s in ss:
        if s == "control":
            continue
        lines = tail(s, 200)                       # full visible pane, one capture
        out.append({"name": s, "status": status_of(lines),
                    "preview": pick_preview(lines)})
    global SELECTED
    if SELECTED not in ss:
        SELECTED = ss[0] if ss else "control"
    return {"sessions": out, "current": SELECTED}

def cwd_to_session():
    d = {}
    for s in sessions():
        if s == "control":
            continue
        cw = session_cwd(s)
        if cw:
            d[cw] = s
    return d

def ago(m):
    d = time.time() - m
    if d < 60:   return "just now"
    if d < 3600: return "%dm" % (d // 60)
    if d < 86400:return "%dh" % (d // 3600)
    return "%dd" % (d // 86400)

def history():
    refresh_index()
    scwd = cwd_to_session()
    by_cwd = {}
    for f, e in index_snapshot():
        cwd = e.get("cwd")
        if not cwd:
            continue
        cur = by_cwd.get(cwd)
        if not cur or e.get("mtime", -1) > cur["mtime"]:
            by_cwd[cwd] = {"cwd": cwd, "mtime": e.get("mtime", 0),
                           "title": e.get("title") or os.path.basename(cwd.rstrip("/"))}
    out = []
    for cwd, d in by_cwd.items():
        d["dir"] = os.path.basename(cwd.rstrip("/")) or cwd
        d["ago"] = ago(d["mtime"])
        d["session"] = scwd.get(cwd)
        out.append(d)
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:60]

def dirs():
    out = []; seen = set()
    for d in history():
        if d["cwd"] in seen:
            continue
        seen.add(d["cwd"])
        out.append({"cwd": d["cwd"], "dir": d["dir"]})
    return out[:12]

def list_subdirs(path):
    """Immediate subdirectories of `path` (default HOME) for the New-session browser."""
    path = os.path.abspath(os.path.expanduser(path or HOME))
    if not os.path.isdir(path):
        path = HOME
    names = []
    try:
        for name in sorted(os.listdir(path), key=str.lower):
            if name.startswith("."):
                continue
            if os.path.isdir(os.path.join(path, name)):
                names.append(name)
    except OSError:
        pass
    return {"path": path,
            "parent": (os.path.dirname(path) if path != "/" else None),
            "home": HOME, "dirs": names}

def make_dir(path, name):
    name = (name or "").strip().strip("/")
    if not name or "/" in name or name in (".", ".."):
        return None
    base = os.path.abspath(os.path.expanduser(path or HOME))
    full = os.path.join(base, name)
    try:
        os.makedirs(full, exist_ok=True)
        return full
    except OSError:
        return None


# ----------------------------------------------------- actions -------------
def switch(n):
    global SELECTED
    SELECTED = n
    for c in [x for x in tx("list-clients", "-F", "#{client_name}").split("\n") if x]:
        subprocess.run([TMUX, "switch-client", "-c", c, "-t", n])

def make_session(name, cwd, resume=False):
    name = uniq_name(name)
    if not os.path.isdir(cwd):
        cwd = HOME
    subprocess.run([TMUX, "new-session", "-d", "-s", name, "-c", cwd])
    if resume:
        # login shell already set PATH; send the command literally then Enter
        subprocess.run([TMUX, "send-keys", "-t", name, "-l", "--", "claude --continue"])
        subprocess.run([TMUX, "send-keys", "-t", name, "Enter"])
    else:
        subprocess.run([TMUX, "send-keys", "-t", name, "claude", "Enter"])
    switch(name)
    return name

def resume_cwd(cwd):
    for s in sessions():
        if s == "control":
            continue
        if session_cwd(s) == cwd:
            switch(s)
            return s
    return make_session(os.path.basename(cwd.rstrip("/")) or "session", cwd, resume=True)


HTML = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name=apple-mobile-web-app-capable content=yes>
<title>Deck</title><style>
:root{--bg:#0d0f12;--pan:#16191f;--pan2:#1d2129;--ink:#e8eaed;--mut:#8a919c;--live:#39d353;--wait:#f5b83d;--idle:#4a5160;--warn:#e5534b;--go:#2ea043;--blu:#3d5afe;--asst:#22262e}
*{box-sizing:border-box;margin:0;-webkit-tap-highlight-color:transparent;font-family:-apple-system,sans-serif}
body{background:var(--bg);color:var(--ink);display:flex;flex-direction:column;height:100dvh;overflow:hidden}
header{display:flex;align-items:center;gap:10px;padding:10px 12px 4px}
h1{font-size:15px;font-weight:800;letter-spacing:.16em}.sub{font-size:11px;color:var(--mut)}
.hbtn{background:var(--pan2);color:var(--ink);border:1px solid #2a3040;border-radius:10px;padding:8px 13px;font-size:13px;font-weight:600}
#new{margin-left:auto}
#cards{display:flex;gap:8px;padding:6px 12px 8px;overflow-x:auto}#cards::-webkit-scrollbar{display:none}
.card{background:var(--pan);border:1px solid #232936;border-radius:14px;padding:10px 12px;min-width:150px;max-width:210px;flex-shrink:0;text-align:left;color:var(--ink)}
.card.on{border-color:var(--blu);background:var(--pan2)}
.tp{display:flex;align-items:center;gap:6px}
.dot{width:9px;height:9px;border-radius:50%;background:var(--idle);flex-shrink:0}
.dot.working{background:var(--live);animation:p 1.2s infinite}.dot.input{background:var(--wait);animation:p .7s infinite}
@keyframes p{50%{opacity:.3}}@media(prefers-reduced-motion){.dot{animation:none}}
.nm{font-size:14px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pv{font-size:11px;color:var(--mut);font-family:ui-monospace,Menlo,monospace;margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.st{font-size:10px;margin-top:4px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut)}
.st.input{color:var(--wait);font-weight:700}.st.working{color:var(--live)}
#view{flex:1;position:relative;overflow:hidden;background:var(--bg)}
#chat{position:absolute;inset:0;overflow-y:auto;padding:14px 12px;display:flex;flex-direction:column;gap:8px}
.msg{max-width:82%;padding:9px 13px;border-radius:16px;font-size:14.5px;line-height:1.4;word-break:break-word;-webkit-user-select:text;user-select:text}
.msg.user{align-self:flex-end;background:var(--blu);color:#fff;border-bottom-right-radius:5px;white-space:pre-wrap}
.msg.asst{align-self:flex-start;background:var(--asst);color:var(--ink);border-bottom-left-radius:5px}
.msg.tool{align-self:center;background:transparent;color:var(--mut);font-size:11px;font-family:ui-monospace,Menlo,monospace;padding:2px 8px;white-space:pre-wrap}
.msg.asst .mdh{font-weight:800;font-size:15.5px;margin:3px 0}
.msg.asst b{font-weight:800}
.msg.asst code{font-family:ui-monospace,Menlo,monospace;background:#0d0f12;border:1px solid #2a3040;border-radius:5px;padding:1px 5px;font-size:13px}
.msg.asst pre.cb{background:#0b0d10;border:1px solid #2a3040;border-radius:10px;padding:10px;margin:6px 0;overflow-x:auto}
.msg.asst pre.cb code{border:0;background:none;padding:0;font-size:12.5px;line-height:1.45;white-space:pre}
.msg.asst ul.mdl{margin:4px 0 4px 2px;padding-left:18px}.msg.asst ul.mdl li{margin:2px 0}
.msg.asst p{margin:4px 0}.msg.asst p:first-child{margin-top:0}.msg.asst p:last-child{margin-bottom:0}
#empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;padding:30px;color:var(--mut);font-size:14px;line-height:1.5}
#term{position:absolute;inset:0;width:100%;height:100%;border:0;display:none}
body.termmode #chat,body.termmode #empty{display:none}body.termmode #term{display:block}
#keys{display:flex;gap:6px;padding:6px 10px;overflow-x:auto}#keys::-webkit-scrollbar{display:none}
#keys button{background:var(--pan2);color:#fff;border:0;border-radius:10px;padding:12px 15px;font-size:15px;font-weight:600;min-width:52px;flex-shrink:0}
.yes{background:var(--go)!important}.esc,.ctlc{background:#5c2b29!important}
.kill{background:transparent!important;border:1px solid var(--warn)!important;color:var(--warn)!important}
#chips{display:flex;gap:6px;padding:2px 10px 4px;overflow-x:auto}#chips::-webkit-scrollbar{display:none}
#chips button{background:transparent;border:1px solid #2a3040;color:var(--mut);border-radius:20px;padding:7px 13px;font-size:12px;font-family:ui-monospace,Menlo,monospace;flex-shrink:0}
#bar{display:flex;gap:6px;padding:6px 10px calc(8px + env(safe-area-inset-bottom))}
#txt{flex:1;background:var(--pan2);border:1px solid #2a3040;border-radius:18px;color:#fff;padding:12px 15px;font-size:16px;resize:none;max-height:120px}
#send{background:var(--blu);border:0;border-radius:18px;color:#fff;padding:0 20px;font-size:15px;font-weight:700}
#ov{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;z-index:10;align-items:flex-end}
#ov.on{display:flex}
#sheet{background:var(--pan);width:100%;max-height:82dvh;border-radius:18px 18px 0 0;padding:14px 14px calc(18px + env(safe-area-inset-bottom));overflow-y:auto}
#sheet h2{font-size:15px;font-weight:800;margin-bottom:10px;display:flex;align-items:center}
#sheet h2 .x{margin-left:auto;background:var(--pan2);border:1px solid #2a3040;color:var(--ink);border-radius:10px;padding:6px 12px;font-size:13px}
.row{display:block;width:100%;text-align:left;background:var(--pan2);border:1px solid #232936;border-radius:12px;padding:12px 14px;margin-bottom:8px;color:var(--ink)}
.row .t{font-size:14px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .m{font-size:11px;color:var(--mut);font-family:ui-monospace,Menlo,monospace;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .badge{color:var(--live);font-weight:700}
#nname{width:100%;background:var(--pan2);border:1px solid #2a3040;border-radius:12px;color:#fff;padding:12px 14px;font-size:16px;margin-bottom:10px}
.dlabel{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.08em;margin:6px 0}
.mkbtn{width:100%;background:var(--blu);border:0;border-radius:12px;color:#fff;padding:13px;font-size:15px;font-weight:700;margin-top:6px}
.dchip{display:inline-block;background:var(--pan2);border:1px solid #2a3040;color:var(--ink);border-radius:10px;padding:9px 12px;font-size:13px;margin:0 6px 6px 0}
.dchip.on{border-color:var(--blu);background:#20305e}
.dlabel2{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.08em;margin:12px 0 6px}
.brow{display:flex;align-items:center;gap:6px;margin:2px 0 6px}
.brow .bp{flex:1;min-width:0;font-size:12px;color:var(--mut);font-family:ui-monospace,Menlo,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.brow button{background:var(--pan2);border:1px solid #2a3040;color:var(--ink);border-radius:10px;padding:8px 11px;font-size:13px;font-weight:600;flex-shrink:0}
#blist{max-height:32vh;overflow-y:auto;margin-bottom:4px}
.drow{display:block;width:100%;text-align:left;background:var(--pan2);border:1px solid #232936;border-radius:10px;padding:11px 13px;margin-bottom:6px;color:var(--ink);font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.drow .fi{color:var(--mut);margin-right:4px}
.hsearch{width:100%;background:var(--pan2);border:1px solid #2a3040;border-radius:12px;color:#fff;padding:11px 14px;font-size:16px;margin-bottom:10px}
.row .badge{color:var(--live);font-weight:700;margin-right:6px}
</style></head><body>
<header><div><h1>DECK</h1><div class=sub id=count></div></div>
<button class=hbtn id=hist onclick="openHist()">History</button>
<button class=hbtn id=mode onclick="toggle()">Terminal</button>
<button class="hbtn" id=new onclick="openNew()">&#65291; New</button></header>
<div id=cards></div>
<div id=view><div id=chat></div><div id=empty style=display:none></div><iframe id=term></iframe></div>
<div id=keys>
<button class=yes onclick="k('Enter')">&#10003; Yes</button>
<button onclick="k('1')">1</button><button onclick="k('2')">2</button><button onclick="k('3')">3</button>
<button class=esc onclick="k('Escape')">Esc</button>
<button onclick="k('Up')">&#8593;</button><button onclick="k('Down')">&#8595;</button>
<button onclick="k('Tab')">Tab</button><button onclick="k('BTab')">&#8679;Tab</button>
<button onclick="k('C-a')">^A</button><button class=ctlc onclick="k('C-c')">^C</button>
<button class=kill onclick="kill()">Kill</button></div>
<div id=chips>
<button onclick="cmd('rcc-status')">rcc-status</button>
<button onclick="cmd('/compact')">/compact</button>
<button onclick="cmd('/model')">/model</button>
<button onclick="cmd('/cost')">/cost</button>
<button onclick="cmd('clear')">clear</button></div>
<div id=bar><textarea id=txt rows=1 placeholder="Message Claude&#8230;"></textarea>
<button id=send onclick="send()">Send</button></div>
<div id=ov onclick="if(event.target.id=='ov')closeOv()"><div id=sheet></div></div>
<script>
let CUR="",TERM=false,tset=false,hasSess=false,es=null;
let msgKeys=[],cardEls={},browsePath="",browseParent="",HIST=[];
const P=u=>fetch(u,{method:'POST'}).catch(()=>{});
function k(x){P('/key?k='+encodeURIComponent(x))}
function cmd(c){P('/type?t='+encodeURIComponent(c))}
function send(){let t=document.getElementById('txt');if(!t.value.trim())return;cmd(t.value);t.value='';t.style.height='auto'}
function kill(){if(CUR&&confirm('Kill "'+CUR+'"?'))P('/kill?s='+encodeURIComponent(CUR)).then(()=>setTimeout(()=>location.reload(),600))}
function toggle(){TERM=!TERM;document.body.classList.toggle('termmode',TERM);
 document.getElementById('mode').textContent=TERM?'Clean':'Terminal';
 if(TERM&&!tset){document.getElementById('term').src='http://'+location.hostname+':7681';tset=true}}

/* ---- safe markdown renderer (bug 3): escape first, then transform ---- */
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function inl(s){return s
 .replace(/`([^`]+)`/g,'<code>$1</code>')
 .replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')
 .replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<i>$2</i>')}
function md(t){
 let src=esc(t),blocks=[];
 src=src.replace(/```([\w-]*)\n?([\s\S]*?)```/g,function(_,lang,code){
  blocks.push('<pre class=cb><code>'+code.replace(/\n$/,'')+'</code></pre>');
  return '\uE000'+(blocks.length-1)+'\uE000'});
 let lines=src.split('\n'),out=[],i=0,para=[];
 function flush(){if(para.length){out.push('<p>'+para.join('<br>')+'</p>');para=[]}}
 while(i<lines.length){
  let ln=lines[i],ph=ln.match(/^\uE000(\d+)\uE000$/);
  if(ph){flush();out.push(blocks[+ph[1]]);i++;continue}
  let hm=ln.match(/^(#{1,6})\s+(.*)$/);
  if(hm){flush();out.push('<div class=mdh>'+inl(hm[2])+'</div>');i++;continue}
  if(/^\s*[-*]\s+/.test(ln)){flush();let items=[];
   while(i<lines.length&&/^\s*[-*]\s+/.test(lines[i])){items.push('<li>'+inl(lines[i].replace(/^\s*[-*]\s+/,''))+'</li>');i++}
   out.push('<ul class=mdl>'+items.join('')+'</ul>');continue}
  if(ln.trim()===''){flush();i++;continue}
  para.push(inl(ln));i++}
 flush();return out.join('')}

/* ---- incremental chat render (bug 1): fingerprint + append-only ---- */
function bubble(m){let d=document.createElement('div');
 d.className='msg '+(m.r==='user'?'user':m.r==='tool'?'tool':'asst');
 if(m.r==='asst')d.innerHTML=md(m.t);else d.textContent=m.t;return d}
function applyChat(ms){
 let chat=document.getElementById('chat'),empty=document.getElementById('empty');
 if(!ms.length){chat.style.display='none';empty.style.display='flex';
  empty.textContent='No messages yet in "'+CUR+'". Type below to talk to Claude, or tap Terminal for the raw session.';
  msgKeys=[];chat.innerHTML='';return}
 empty.style.display='none';chat.style.display='flex';
 let keys=ms.map(m=>m.r+''+m.t);
 if(keys.join('')===msgKeys.join(''))return;               /* nothing changed */
 let pref=keys.length>=msgKeys.length&&msgKeys.every((x,idx)=>x===keys[idx]);
 let near=chat.scrollTop+chat.clientHeight>chat.scrollHeight-80;
 if(pref){for(let idx=msgKeys.length;idx<keys.length;idx++)chat.appendChild(bubble(ms[idx]))}
 else{chat.innerHTML='';ms.forEach(m=>chat.appendChild(bubble(m)))}               /* window slid/diverged */
 msgKeys=keys;
 if(near||pref)chat.scrollTop=chat.scrollHeight}

/* ---- in-place card diff (bug 2d): no full rebuild, no flicker ---- */
function applyState(d){
 CUR=d.current;
 document.getElementById('count').textContent=d.sessions.length+' sessions · '+(CUR||'—');
 let el=document.getElementById('cards'),names=d.sessions.map(x=>x.name);
 hasSess=names.length>0;
 for(let nm in cardEls){if(names.indexOf(nm)<0){el.removeChild(cardEls[nm].root);delete cardEls[nm]}}
 d.sessions.forEach(s=>{
  let c=cardEls[s.name];
  if(!c){let b=document.createElement('button');b.className='card';
   b.innerHTML='<div class=tp><span class=dot></span><span class=nm></span></div><div class=pv></div><div class=st></div>';
   b.onclick=()=>selectSession(s.name);el.appendChild(b);
   c=cardEls[s.name]={root:b,dot:b.querySelector('.dot'),nm:b.querySelector('.nm'),pv:b.querySelector('.pv'),st:b.querySelector('.st')};
   c.nm.textContent=s.name}
  if(c.dot.className!=='dot '+s.status)c.dot.className='dot '+s.status;
  let stt=s.status==='input'?'needs input':s.status;
  if(c.st.textContent!==stt)c.st.textContent=stt;
  if(c.st.className!=='st '+s.status)c.st.className='st '+s.status;
  let pv=s.preview||'—';if(c.pv.textContent!==pv)c.pv.textContent=pv;
  let on=s.name===CUR;if(c.root.classList.contains('on')!==on)c.root.classList.toggle('on',on)});
 if(hasSess&&names.indexOf(CUR)<0){selectSession(names[0])}
 if(!hasSess){let chat=document.getElementById('chat'),empty=document.getElementById('empty');
  chat.style.display='none';empty.style.display='flex';
  empty.textContent='No sessions yet. Tap ＋ New (top right) to start one, or run  cc <name>  on your Mac.'}}

function selectSession(n){if(n===CUR&&es)return;CUR=n;P('/switch?s='+encodeURIComponent(n));
 msgKeys=[];document.getElementById('chat').innerHTML='';subscribe()}

/* ---- single SSE subscription (bug 2c): push updates, no polling ---- */
function subscribe(){if(es)es.close();
 es=new EventSource('/events?s='+encodeURIComponent(CUR));
 es.addEventListener('state',e=>{try{applyState(JSON.parse(e.data))}catch(x){}});
 es.addEventListener('chat',e=>{if(!TERM){try{applyChat(JSON.parse(e.data))}catch(x){}}})}

/* ---- History (bug 4) — filterable, tidy rows ---- */
function ovClose(){document.getElementById('ov').classList.remove('on')}
function closeOv(){ovClose()}
async function openHist(){let sh=document.getElementById('sheet');
 sh.innerHTML='<h2>History<button class=x onclick="ovClose()">Close</button></h2>'
  +'<div style="color:var(--mut);padding:8px">Loading…</div>';
 document.getElementById('ov').classList.add('on');
 try{HIST=await(await fetch('/history')).json()}catch(e){HIST=[]}
 sh.innerHTML='<h2>History<button class=x onclick="ovClose()">Close</button></h2>'
  +'<input class=hsearch id=hq placeholder="Filter conversations…" autocapitalize=off autocorrect=off>'
  +'<div id=hlist></div>';
 let q=document.getElementById('hq');q.addEventListener('input',()=>renderHist(q.value));
 renderHist('')}
function renderHist(f){f=(f||'').toLowerCase();let list=document.getElementById('hlist');if(!list)return;
 let items=HIST.filter(it=>!f||it.title.toLowerCase().includes(f)||(it.dir||'').toLowerCase().includes(f)||(it.cwd||'').toLowerCase().includes(f));
 if(!items.length){list.innerHTML='<div style="color:var(--mut);padding:10px">No matches.</div>';return}
 list.innerHTML='';
 items.forEach(it=>{let b=document.createElement('button');b.className='row';
  b.onclick=()=>resume(encodeURIComponent(it.cwd));
  let open=it.session?'<span class=badge>● open</span>':'';
  b.innerHTML='<div class=t>'+esc(it.title)+'</div><div class=m>'+open+esc(it.dir)+' · '+it.ago+'</div>';
  list.appendChild(b)})}
function resume(cwdEnc){ovClose();
 fetch('/resume?cwd='+cwdEnc,{method:'POST'}).then(r=>r.text()).then(n=>{
  n=n.trim();if(n){msgKeys=[];document.getElementById('chat').innerHTML='';CUR=n;subscribe()}
 }).catch(()=>{})}

/* ---- New session (bug 5) — full directory browser + make folder ---- */
async function openNew(){let sh=document.getElementById('sheet');
 sh.innerHTML='<h2>New session<button class=x onclick="ovClose()">Close</button></h2>'
  +'<input id=nname placeholder="Session name (optional)" autocapitalize=off autocorrect=off>'
  +'<div class=dlabel2>Recent</div><div id=recent></div>'
  +'<div class=dlabel2>Folder</div>'
  +'<div class=brow><button onclick="goUp()">↑ Up</button><div class=bp id=bpath></div>'
  +'<button onclick="mkFolder()">＋ Folder</button></div><div id=blist></div>'
  +'<button class=mkbtn id=startbtn onclick="doNew()">Start Claude</button>';
 document.getElementById('ov').classList.add('on');
 setTimeout(()=>{let e=document.getElementById('nname');if(e)e.focus()},150);
 let ds=[];try{ds=await(await fetch('/dirs')).json()}catch(e){}
 let w=document.getElementById('recent');w.innerHTML='';
 let home=document.createElement('span');home.className='dchip';home.textContent='Home';home.onclick=()=>gotoDir('');w.appendChild(home);
 ds.forEach(d=>{let c=document.createElement('span');c.className='dchip';c.textContent=d.dir;c.title=d.cwd;c.onclick=()=>gotoDir(d.cwd);w.appendChild(c)});
 gotoDir('')}
async function gotoDir(path){let d;try{d=await(await fetch('/ls?dir='+encodeURIComponent(path))).json()}catch(e){return}
 browsePath=d.path;browseParent=d.parent||'';
 let disp=d.path;if(d.home&&disp.indexOf(d.home)===0)disp='~'+disp.slice(d.home.length);
 let bp=document.getElementById('bpath');if(bp)bp.textContent=disp||'/';
 let sb=document.getElementById('startbtn');if(sb)sb.textContent='Start Claude in '+((d.path.split('/').filter(Boolean).pop())||'/');
 let list=document.getElementById('blist');if(!list)return;list.innerHTML='';
 if(!d.dirs.length){list.innerHTML='<div style="color:var(--mut);padding:8px;font-size:13px">No sub-folders here.</div>';return}
 d.dirs.forEach(name=>{let b=document.createElement('button');b.className='drow';
  b.innerHTML='<span class=fi>▸</span>'+esc(name);
  b.onclick=()=>gotoDir(d.path.replace(/\/$/,'')+'/'+name);list.appendChild(b)})}
function goUp(){if(browseParent)gotoDir(browseParent)}
function mkFolder(){let nm=prompt('New folder name');if(!nm||!nm.trim())return;
 fetch('/mkdir?dir='+encodeURIComponent(browsePath)+'&name='+encodeURIComponent(nm.trim()),{method:'POST'})
  .then(r=>r.text()).then(()=>gotoDir(browsePath)).catch(()=>{})}
function doNew(){let nm=document.getElementById('nname').value.trim();
 if(!nm)nm=browsePath.split('/').filter(Boolean).pop()||'session';
 ovClose();P('/new?s='+encodeURIComponent(nm)+'&dir='+encodeURIComponent(browsePath)).then(()=>setTimeout(()=>location.reload(),1000))}

let T=document.getElementById('txt');
T.addEventListener('input',()=>{T.style.height='auto';T.style.height=Math.min(T.scrollHeight,120)+'px'});
T.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}});
subscribe();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def _s(self, b, t="text/html; charset=utf-8", code=200):
        body = b.encode() if isinstance(b, str) else b
        self.send_response(code)
        self.send_header("Content-Type", t)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _sse_send(self, event, data):
        self.wfile.write(("event: %s\ndata: %s\n\n" % (event, data)).encode())
        self.wfile.flush()

    def do_events(self, s):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            laststate = None; lastchat = None; beat = 0
            while True:
                st = json.dumps(state())
                if st != laststate:
                    self._sse_send("state", st); laststate = st
                ch = json.dumps(chat(s))
                if ch != lastchat:
                    self._sse_send("chat", ch); lastchat = ch
                beat += 1
                if beat % 8 == 0:                       # ~10s keepalive comment
                    self.wfile.write(b": ping\n\n"); self.wfile.flush()
                time.sleep(1.2)
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            return

    def do_GET(self):
        try:
            q = urllib.parse.urlparse(self.path); p = urllib.parse.parse_qs(q.query)
            g = lambda k: p.get(k, [""])[0]
            if q.path == "/state":
                self._s(json.dumps(state()), "application/json; charset=utf-8")
            elif q.path == "/chat":
                self._s(json.dumps(chat(g("s") or sel())), "application/json; charset=utf-8")
            elif q.path == "/output":
                self._s("\n".join(tail(g("s") or sel(), 120)), "text/plain; charset=utf-8")
            elif q.path == "/history":
                self._s(json.dumps(history()), "application/json; charset=utf-8")
            elif q.path == "/dirs":
                self._s(json.dumps(dirs()), "application/json; charset=utf-8")
            elif q.path == "/ls":
                self._s(json.dumps(list_subdirs(g("dir"))), "application/json; charset=utf-8")
            elif q.path == "/events":
                self.do_events(g("s") or sel())
            else:
                self._s(HTML)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        try:
            q = urllib.parse.urlparse(self.path); p = urllib.parse.parse_qs(q.query)
            g = lambda k: p.get(k, [""])[0]
            body = "ok"
            if q.path == "/switch" and g("s") in sessions():
                switch(g("s"))
            elif q.path == "/key":
                subprocess.run([TMUX, "send-keys", "-t", sel(), g("k")])
            elif q.path == "/keyto" and g("s"):
                subprocess.run([TMUX, "send-keys", "-t", g("s"), g("k")])
            elif q.path == "/type":
                subprocess.run([TMUX, "send-keys", "-t", sel(), "-l", "--", g("t")])
                subprocess.run([TMUX, "send-keys", "-t", sel(), "Enter"])
            elif q.path == "/new":
                dd = g("dir") or HOME
                nm = g("s") or os.path.basename(dd.rstrip("/")) or "session"
                body = make_session(nm.replace(" ", "-"), dd, resume=False)
            elif q.path == "/mkdir" and g("name"):
                r = make_dir(g("dir"), g("name"))
                body = r if r else ""
            elif q.path == "/resume" and g("cwd"):
                body = resume_cwd(g("cwd"))
            elif q.path == "/kill" and g("s") and g("s") != "control":
                subprocess.run([TMUX, "kill-session", "-t", g("s")])
            self._s(body, "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    load_index()
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(os.environ.get("DECK_PORT", "7682"))
    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    srv.serve_forever()
