# rcc-common.sh — shared config + helpers for all RCC scripts.
# Sourced, never executed directly. Targets bash 3.2 + BSD/macOS userland.
# shellcheck shell=bash

# --- resolve RCC_HOME (the ~/rcc folder = parent of this script's dir) -------
_rcc_self="${BASH_SOURCE[0]}"
_rcc_dir="$(cd "$(dirname "$_rcc_self")" && pwd)"
RCC_HOME="$(cd "$_rcc_dir/.." && pwd)"
export RCC_HOME
RCC_SCRIPTS="$RCC_HOME/scripts"
RCC_GOALS="$RCC_HOME/goals"
RCC_REVIEWS="$RCC_HOME/reviews"
RCC_LOGS="$RCC_HOME/logs"
RCC_STATE="$RCC_HOME/.state"
mkdir -p "$RCC_GOALS" "$RCC_REVIEWS" "$RCC_LOGS" "$RCC_STATE" 2>/dev/null || true

# --- model routing (override via env or rcc.conf) ----------------------------
# Worker sessions write code (cheap/fast). Evaluator judges every iteration
# (cheapest). Reviews only fire at milestones + final (most capable).
# NOTE: 'fable' is not a verified CLI alias on this box, so the review model
# defaults to the full id 'claude-fable-5' which is guaranteed to resolve.
# Run `rcc-verify-models` to confirm all three before trusting a loop.
RCC_MODEL_WORKER="${RCC_MODEL_WORKER:-sonnet}"
RCC_MODEL_EVAL="${RCC_MODEL_EVAL:-haiku}"
RCC_MODEL_REVIEW="${RCC_MODEL_REVIEW:-claude-fable-5}"

# --- loop guard defaults (all override via env) ------------------------------
RCC_MAX_ITERS="${RCC_MAX_ITERS:-50}"       # hard cap on loop iterations
RCC_MAX_FAILS="${RCC_MAX_FAILS:-5}"        # consecutive evaluator failures -> failed
RCC_SLEEP="${RCC_SLEEP:-45}"               # seconds between iterations (30-60)
RCC_READ_LINES="${RCC_READ_LINES:-80}"     # pane lines the evaluator sees
RCC_STALL_READS="${RCC_STALL_READS:-3}"    # identical reads before "stalled"
RCC_SEND_WAIT="${RCC_SEND_WAIT:-20}"       # seconds to wait for an idle target
RCC_CONF_HIGH="${RCC_CONF_HIGH:-0.8}"      # evaluator confidence gate for "done"

# optional user overrides file
[ -f "$RCC_HOME/rcc.conf" ] && . "$RCC_HOME/rcc.conf"

# --- the multiplexer abstraction (tmux today; single seam if that changes) ---
RCC_MUX="${RCC_MUX:-tmux}"

rcc_have() { command -v "$1" >/dev/null 2>&1; }

require_mux() {
  if ! rcc_have "$RCC_MUX"; then
    cat >&2 <<EOF
ERROR: '$RCC_MUX' is not installed — RCC controls Claude sessions through it.
Install it, then re-run:
    brew install tmux        # macOS
    sudo apt install tmux    # Debian/Ubuntu
(If homebrew complains about /opt/homebrew ownership, first run:
    sudo chown -R "\$(whoami)" /opt/homebrew)
EOF
    return 127
  fi
}

# List sessions, one name per line.
mux_list_sessions() { tmux list-sessions -F '#{session_name}' 2>/dev/null; }

# True if a session exists.
mux_has_session() { tmux has-session -t "$1" 2>/dev/null; }

# Print the last N lines of a session's active pane to stdout.
mux_capture() {
  local s="$1" n="${2:-$RCC_READ_LINES}"
  tmux capture-pane -p -t "$s" -S "-$n" 2>/dev/null
}

# Type a literal string into a session, then submit with Enter.
mux_send() {
  local s="$1" text="$2"
  tmux send-keys -t "$s" -l -- "$text" 2>/dev/null || return 1
  tmux send-keys -t "$s" Enter 2>/dev/null
}

# Working directory of a session's active pane (for git diffs).
mux_pane_path() { tmux display-message -p -t "$1" '#{pane_current_path}' 2>/dev/null; }

# Emit "session<TAB>tty<TAB>current_cmd<TAB>pid" for every pane.
mux_list_panes() {
  tmux list-panes -a -F '#{session_name}	#{pane_tty}	#{pane_current_command}	#{pane_pid}' 2>/dev/null
}

# Does this session run a Claude Code process? Checks the pane tty's process
# list for a 'claude' command (robust to claude launching under node).
mux_session_is_claude() {
  local s="$1"
  mux_list_panes | while IFS='	' read -r sess tty cmd pid; do
    [ "$sess" = "$s" ] || continue
    local ttyname="${tty#/dev/}"
    if ps -t "$ttyname" -o command= 2>/dev/null | grep -qi 'claude'; then
      echo yes; return 0
    fi
    case "$cmd" in *claude*) echo yes; return 0;; esac
  done | grep -q yes
}

# --- small utilities ---------------------------------------------------------
rcc_ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

die() { echo "ERROR: $*" >&2; exit 1; }

warn() { echo "WARN: $*" >&2; }

# Append one terse line to a session's log. Never dumps transcripts.
rcc_log() {
  local s="$1"; shift
  printf '%s  %s\n' "$(rcc_ts)" "$*" >> "$RCC_LOGS/$s.log"
}

# Content hash of stdin (for stall detection). cksum is universal.
rcc_hash() { cksum | awk '{print $1"-"$2}'; }

# Extract a scalar field from JSON. Prefers jq; falls back to a flat-key grep.
json_field() {
  local key="$1" json="$2"
  if rcc_have jq; then
    # NOTE: do NOT use '.[$k] // empty' — jq treats a `false` value as empty,
    # so boolean false fields would vanish. has() preserves false/0.
    printf '%s' "$json" | jq -r --arg k "$key" 'if has($k) then .[$k] else empty end' 2>/dev/null
  else
    printf '%s' "$json" \
      | tr -d '\n' \
      | grep -oE "\"$key\"[[:space:]]*:[[:space:]]*(\"[^\"]*\"|true|false|[0-9.]+)" \
      | head -1 \
      | sed -E "s/.*:[[:space:]]*//; s/^\"//; s/\"$//"
  fi
}

# Strip ``` fences / prose and return the first {...} JSON object found.
json_clean() {
  printf '%s' "$1" \
    | sed -e 's/```json//g' -e 's/```//g' \
    | tr -d '\r' \
    | awk 'BEGIN{d=0} { for(i=1;i<=length($0);i++){c=substr($0,i,1); if(c=="{"){d++} if(d>0){printf "%s",c} if(c=="}"){d--; if(d==0){print ""; exit}}} }'
}

# Rewrite a single line in a file safely (BSD sed -i is a footgun -> temp+mv).
# Usage: file_replace_line <file> <match-ERE> <new-line>
file_replace_line() {
  local file="$1" match="$2" newline="$3" tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/rcc.XXXXXX")" || return 1
  awk -v m="$match" -v repl="$newline" '
    $0 ~ m && !done { print repl; done=1; next } { print }
  ' "$file" > "$tmp" && mv "$tmp" "$file"
}

# Check off the first unchecked "- [ ] <needle>" line in a file.
file_check_box() {
  local file="$1" needle="$2" tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/rcc.XXXXXX")" || return 1
  awk -v n="$needle" '
    !done && /- \[ \]/ && index($0,n)>0 { sub(/- \[ \]/,"- [x]"); done=1 }
    { print }
  ' "$file" > "$tmp" && mv "$tmp" "$file"
}

# First unchecked milestone text (the "- [ ] ..." lines under ## Milestones).
goal_next_milestone() {
  local file="$1"
  awk '
    /^## Milestones/     { inms=1; next }
    /^## /               { inms=0 }
    inms && /- \[ \]/    { sub(/^[[:space:]]*- \[ \][[:space:]]*/,""); print; exit }
  ' "$file"
}

# Count remaining unchecked milestones.
goal_open_milestones() {
  awk '
    /^## Milestones/  { inms=1; next }
    /^## /            { inms=0 }
    inms && /- \[ \]/ { c++ }
    END{ print c+0 }
  ' "$1"
}

# Read the Status: value from a goal file.
goal_status() { grep -E '^Status:' "$1" 2>/dev/null | head -1 | sed -E 's/^Status:[[:space:]]*//'; }

# Read the "# Goal:" one-liner from a goal file.
goal_title() { grep -E '^# Goal:' "$1" 2>/dev/null | head -1 | sed -E 's/^# Goal:[[:space:]]*//'; }

# Set the Status: line in a goal file.
goal_set_status() { file_replace_line "$1" '^Status:' "Status: $2"; }

# --- headless Claude calls ---------------------------------------------------
# rcc_claude <model> <prompt>  -> prints the model's answer text on stdout.
# When jq is present, uses --output-format json to also capture per-call cost;
# set RCC_COST_SESSION=<session> to accumulate cost into .state/<session>.cost.
rcc_claude() {
  local model="$1" prompt="$2" out result cost
  rcc_have claude || { echo "ERROR: claude CLI not found" >&2; return 127; }
  if rcc_have jq; then
    out="$(claude -p --model "$model" --output-format json "$prompt" 2>>"$RCC_STATE/claude.err")" || return 1
    result="$(printf '%s' "$out" | jq -r '.result // .text // empty' 2>/dev/null)"
    cost="$(printf '%s' "$out" | jq -r '(.total_cost_usd // .cost_usd // empty)|tostring' 2>/dev/null)"
    if [ -n "${RCC_COST_SESSION:-}" ] && [ -n "$cost" ] && [ "$cost" != "null" ]; then
      printf '%s\n' "$cost" >> "$RCC_STATE/$RCC_COST_SESSION.cost"
    fi
    if [ -n "$result" ]; then printf '%s' "$result"; else printf '%s' "$out"; fi
  else
    claude -p --model "$model" "$prompt" 2>>"$RCC_STATE/claude.err"
  fi
}

# Sum of a session's recorded per-call costs, or "n/a".
rcc_cost_total() {
  local f="$RCC_STATE/$1.cost"
  [ -f "$f" ] || { echo "n/a"; return; }
  awk '{s+=$1} END{ if(NR==0){print "n/a"} else {printf "$%.4f", s} }' "$f"
}
