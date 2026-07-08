#!/usr/bin/env bash
# Deck setup — installs the four launchd services from templates.
# Safe to re-run: it re-renders configs and reloads the services.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"          # the rcc/ directory
LA="$HOME/Library/LaunchAgents"
UID_N="$(id -u)"

echo "Deck setup  (repo: $ROOT)"
echo ""

# 1. dependencies -----------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || { echo "MISSING: $1  ->  brew install ${2:-$1}"; exit 1; }; }
need tmux; need ttyd
PYTHON="/usr/bin/python3"; [ -x "$PYTHON" ] || PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || { echo "MISSING: python3"; exit 1; }
TMUX_BIN="$(command -v tmux)"; TTYD_BIN="$(command -v ttyd)"

# 2. location check ----------------------------------------------------------
if [ "$ROOT" != "$HOME/rcc" ]; then
  echo "NOTE: the services expect this repo at ~/rcc (it's at $ROOT)."
  echo "      Clone/move it to ~/rcc, then re-run this script."
  exit 1
fi

# 3. bind address (Tailscale IP keeps the panel off the public internet) -----
TS_IP=""
command -v tailscale >/dev/null 2>&1 && TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
if [ -z "$TS_IP" ]; then
  read -rp "IP to bind the panel to (your Tailscale IP is strongly recommended): " TS_IP
fi
[ -n "$TS_IP" ] || { echo "no IP given"; exit 1; }

# 4. ntfy topic --------------------------------------------------------------
if [ -f "$ROOT/.ntfy_topic" ]; then
  TOPIC="$(cat "$ROOT/.ntfy_topic")"; echo "Using existing ntfy topic."
else
  read -rp "ntfy topic (blank = generate a random private one): " TOPIC || true
  [ -n "${TOPIC:-}" ] || TOPIC="rcc-$(openssl rand -hex 8)"
fi

# 5. write config (owner-only) ----------------------------------------------
printf '%s' "$TS_IP" > "$ROOT/.ts_ip";       chmod 600 "$ROOT/.ts_ip"
printf '%s' "$TOPIC" > "$ROOT/.ntfy_topic";   chmod 600 "$ROOT/.ntfy_topic"
mkdir -p "$ROOT/logs" "$ROOT/.state" "$ROOT/goals" "$ROOT/reviews"

# 6. render + install the plists --------------------------------------------
mkdir -p "$LA"
render() {
  sed -e "s#__HOME__#$HOME#g" -e "s#__TS_IP__#$TS_IP#g" -e "s#__PYTHON__#$PYTHON#g" \
      -e "s#__TMUX__#$TMUX_BIN#g" -e "s#__TTYD__#$TTYD_BIN#g" \
      "$HERE/com.rcc.$1.plist.template" > "$LA/com.rcc.$1.plist"
}
for s in ttyd panel notifier keepawake; do
  render "$s"
  launchctl bootout "gui/$UID_N/com.rcc.$s" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_N" "$LA/com.rcc.$s.plist"
  echo "  installed com.rcc.$s"
done

echo ""
echo "Done."
echo "  Panel:  http://$TS_IP:7682   (open on your phone over Tailscale)"
echo "  Push:   subscribe to topic  $TOPIC  in the ntfy app"
echo "  Check:  launchctl list | grep com.rcc"
echo ""
echo "keepawake needs passwordless sudo for pmset. Add via 'sudo visudo':"
echo "  $(whoami) ALL=(ALL) NOPASSWD: /usr/bin/pmset"
