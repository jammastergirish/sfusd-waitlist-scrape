#!/usr/bin/env bash
#
# Poll the SFUSD waitlist and send a desktop notification when — and only when —
# our top choice moves. The last seen position lives in a git-ignored state file,
# so a restart does not re-announce a position you have already been told about.
#
#   ./watch_waitlist.sh                  # check every 5 minutes
#   ./watch_waitlist.sh --once           # one check, for cron/launchd
#   ./watch_waitlist.sh --pref 2         # watch the second choice instead
#   ./watch_waitlist.sh --timeout 120    # the portal is having a slow day
#   ./watch_waitlist.sh --retries 5      # ride out a longer wobble
#
# Needs credentials without a prompt: a .env next to sfusd_waitlist.py, or
# PARENTVUE_USERNAME / PARENTVUE_PASSWORD exported.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRAPER="${SCRAPER:-$SCRIPT_DIR/sfusd_waitlist.py}"   # overridable so the loop is testable
STATE_FILE="${STATE_FILE:-$SCRIPT_DIR/.waitlist_position}"
INTERVAL="${INTERVAL:-300}"
PREF="${PREF:-1}"
TIMEOUT="${TIMEOUT:-90}"   # generous: the portal is often slow, and we are in no hurry
RETRIES="${RETRIES:-3}"           # attempts per check, not counting nothing
RETRY_DELAY="${RETRY_DELAY:-5}"   # seconds before the second attempt
RETRY_FACTOR="${RETRY_FACTOR:-3}" # …then 15s, 45s, and so on
ONCE=0

usage() { sed -n '2,${/^#/!q;p;}' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

die() { printf '%s\n' "$*" >&2; exit 1; }

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --once) ONCE=1; shift ;;
    --interval) INTERVAL="${2:?--interval needs seconds}"; shift 2 ;;
    --pref) PREF="${2:?--pref needs a preference order number}"; shift 2 ;;
    --timeout) TIMEOUT="${2:?--timeout needs seconds}"; shift 2 ;;
    --retries) RETRIES="${2:?--retries needs a count}"; shift 2 ;;
    --state) STATE_FILE="${2:?--state needs a path}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

[[ -f "$SCRAPER" ]] || die "scraper not found: $SCRAPER"
if [[ -z "${PARENTVUE_PASSWORD:-}" && ! -f "$SCRIPT_DIR/.env" ]]; then
  die "no credentials: create $SCRIPT_DIR/.env (see .env.example) or export PARENTVUE_USERNAME/PARENTVUE_PASSWORD"
fi

# --------------------------------------------------------------------------- #

notify() {
  local title="$1" message="$2" escaped
  log "NOTIFY $title — $message"
  if command -v terminal-notifier >/dev/null 2>&1; then
    terminal-notifier -title "$title" -message "$message" -sound Glass || true
  elif command -v osascript >/dev/null 2>&1; then
    escaped=$(printf '%s' "$message" | sed 's/[\\"]/\\&/g')
    osascript -e "display notification \"$escaped\" with title \"$title\" sound name \"Glass\"" || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send "$title" "$message" || true
  else
    log "(no notifier available on this machine)"
  fi
}

# Preference order is the first column and position the last, so a comma inside a
# quoted school name only shifts the columns in between: rebuild the name from them.
extract_row() {
  awk -F, -v pref="$1" '
    $1 == pref {
      school = $2
      for (i = 3; i <= NF - 3; i++) school = school "," $i
      gsub(/^"|"$/, "", school)
      print school "\t" $NF
      exit
    }'
}

# Retry a slow or unreachable portal with a widening gap. Rejected credentials are
# not a wobble, so those stop immediately rather than walking into a lockout.
scrape() {
  local csv_file="$1" errors="$2" attempt=1 delay="$RETRY_DELAY" message
  while :; do
    if uv run "$SCRAPER" -q --format csv --timeout "$TIMEOUT" >"$csv_file" 2>"$errors"; then
      ((attempt > 1)) && log "recovered on attempt $attempt"
      return 0
    fi
    message=$(tail -n1 "$errors")
    if [[ "$message" == *"Login failed"* ]]; then
      log "credentials rejected, not retrying: $message"
      return 1
    fi
    if ((attempt >= RETRIES)); then
      log "check failed after $attempt attempt(s), leaving the saved position alone: $message"
      return 1
    fi
    log "attempt $attempt failed, retrying in ${delay}s: $message"
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * RETRY_FACTOR))
  done
}

check() {
  local out errors csv row school position previous prev_school prev_position move
  out=$(mktemp) errors=$(mktemp)
  if ! scrape "$out" "$errors"; then
    rm -f "$out" "$errors"
    return 0
  fi
  csv=$(cat "$out")
  rm -f "$out" "$errors"

  row=$(printf '%s\n' "$csv" | extract_row "$PREF")
  if [[ -n "$row" ]]; then
    IFS=$'\t' read -r school position <<<"$row"
  else                      # an offer accepted, or the row withdrawn
    school="(not listed)"
    position="—"
  fi

  previous=""
  [[ -f "$STATE_FILE" ]] && previous=$(cat "$STATE_FILE")
  prev_school="${previous%%|*}"
  prev_position="${previous##*|}"

  if [[ -z "$previous" ]]; then
    log "baseline: choice $PREF is $school at position $position"
  elif [[ "$position" == "$prev_position" && "$school" == "$prev_school" ]]; then
    log "no change: choice $PREF is $school at position $position"
    return 0
  elif [[ "$school" != "$prev_school" ]]; then
    notify "SFUSD waitlist" "Choice $PREF changed: $prev_school ($prev_position) → $school ($position)"
  else
    move=""
    if [[ "$position" =~ ^[0-9]+$ && "$prev_position" =~ ^[0-9]+$ ]]; then
      if ((position < prev_position)); then
        move=" — up $((prev_position - position))"
      else
        move=" — down $((position - prev_position))"
      fi
    fi
    notify "SFUSD waitlist" "$school: $prev_position → $position$move"
  fi

  printf '%s|%s\n' "$school" "$position" > "$STATE_FILE"
}

trap 'log "stopped"; exit 0' INT TERM

if ((ONCE)); then
  check
  exit 0
fi

log "watching choice $PREF every ${INTERVAL}s (state: $STATE_FILE) — ctrl-c to stop"
while true; do
  check
  sleep "$INTERVAL"
done
