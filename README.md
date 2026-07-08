# Deck — control Claude Code from your phone

Deck turns any Mac running [Claude Code](https://claude.com/claude-code) sessions
into something you can drive from your phone: live session cards, a clean chat
view of each conversation, a full terminal, and **push notifications with
Approve/Deny buttons** the moment Claude needs your permission — so you can
unblock a long-running task from the lock screen.

It's ~500 lines of Python 3 standard library (no pip installs) plus tmux and
ttyd, wired together with launchd. Reachable privately over Tailscale.

<!-- Add a screenshot here, e.g.  ![Deck](docs/deck.png)  -->

## What you get

- **Session cards** — every tmux session, its live status (working / needs-input /
  idle), and a one-line preview. Tap to focus.
- **Chat view** — reads Claude Code's own conversation logs and renders them as
  chat bubbles with markdown (headers, bold, lists, code blocks). Updates push
  instantly over Server-Sent Events; no polling, no flicker.
- **Approvals from anywhere** — when Claude asks to run a command, you get an
  ntfy push with **Approve** / **Deny** buttons that act on the session directly.
- **Start & resume from the phone** — the **New** sheet has a directory browser
  (create folders too); **History** lists past conversations and resumes them
  with `claude --continue`. No need to `ssh`/`cc` into the Mac.
- **Terminal** — a full ttyd terminal when you want the raw session.
- **Keep-awake** — holds the Mac awake while sessions run, with guardrails
  (time cap, battery floor, thermal release) and alerts on release.

## How it works

```
 phone ──Tailscale──> Mac
   │  http://<ts-ip>:7682   Deck panel      (phone/server.py, ThreadingHTTPServer + SSE)
   │  http://<ts-ip>:7681   ttyd terminal   (com.rcc.ttyd)
   └─ ntfy.sh push          notifier        (phone/notifier.py, polls /state)
                            keep-awake       (phone/keepawake.py)
   sessions live in tmux; the panel reads ~/.claude/projects/*/*.jsonl for chat
```

Four launchd agents (`com.rcc.ttyd`, `.panel`, `.notifier`, `.keepawake`) keep it
all running and restart on crash.

## Prerequisites

- macOS
- [`tmux`](https://github.com/tmux/tmux) and [`ttyd`](https://github.com/tsl0922/ttyd): `brew install tmux ttyd`
- Python 3 (the system `/usr/bin/python3` is fine)
- [Tailscale](https://tailscale.com) on the Mac and phone (keeps the panel off the public internet)
- The [ntfy](https://ntfy.sh) app on your phone (free) for push notifications

## Setup

```bash
git clone <your-fork> ~/rcc
cd ~/rcc
./deploy/setup.sh
```

`setup.sh` detects your Tailscale IP, creates a private ntfy topic, writes the
(git-ignored) config files, renders the launchd plists from `deploy/*.template`,
and loads the services. Then:

1. Open the ntfy app and subscribe to the topic it printed.
2. Open `http://<your-tailscale-ip>:7682` on your phone. Add to Home Screen for an app feel.

Start a session from the Mac with `claude` inside a named tmux session, or just
tap **＋ New** in the panel.

## Notifications

Push goes through **ntfy** by default (instant, free, and the only channel that
can carry the Approve/Deny buttons). Two optional plain-text channels can run
*alongside* it — both are off unless you create the config file:

- **iMessage** (free, local): put your number/Apple ID in `~/rcc/.imessage_to`.
  Requires the Mac signed into Messages and Automation permission for the
  notifier. Flaky from a background agent — see notes below.
- **Twilio SMS** (paid, rock-solid): put `{"sid","token","from","to"}` in
  `~/rcc/.twilio.json`. Works from any context.

These send a plain "needs approval" text; tap through to Deck to actually
approve. (SMS/iMessage can't render action buttons.)

## Security

- The panel binds to your **Tailscale IP only** — not `0.0.0.0`. It's reachable
  only from devices on your tailnet.
- Secrets (`.ntfy_topic`, `.ts_ip`, `.twilio.json`, `.imessage_to`) and personal
  content (logs, conversation cache, goals) are **git-ignored** — see `.gitignore`.
  Double-check `git status` before your first push.
- ttyd runs writable (`-W`): anyone who can reach it has a real shell. Keep it
  behind Tailscale.
- `keepawake` needs passwordless `sudo pmset` — `setup.sh` prints the `visudo` line.

## Managing the services

```bash
launchctl list | grep com.rcc                        # status
launchctl kickstart -k gui/$(id -u)/com.rcc.panel    # restart after editing server.py
tail -f ~/rcc/logs/com.rcc.panel.err                 # logs
```

## Tests

Pure-logic tests run with no network, tmux, or sockets:

```bash
/usr/bin/python3 phone/test_server.py   # server logic (status/chat/history/browser)
node phone/test_ui.js                   # client markdown + incremental render
```

## Bonus: the RCC goal-loop orchestrator

`scripts/rcc-*` is a separate, optional layer that drives a worker session toward
a goal autonomously (a cheap model evaluates each iteration; a stronger model
reviews milestones). It's independent of Deck — see [CLAUDE.md](CLAUDE.md).

## License

MIT — see LICENSE.
