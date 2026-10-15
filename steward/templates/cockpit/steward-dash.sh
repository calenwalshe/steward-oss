#!/usr/bin/env bash
# steward-dash: live Steward cockpit panel (services + jobs). Ctrl-C to exit. Started by `steward dash`, which sets
# STEWARD_PYTHON (the interpreter), STEWARD_INSTALL_ROOT (the install root), STEWARD_PUBLIC_URL (probe URL, optional),
# STEWARD_DASH_UNITS (space-separated systemd units to show instead of the recorded ones, optional) and
# STEWARD_DASH_ONCE=1 (render one frame, exit). The services block comes from `python -m steward.dash_services`.
# Ticks every second so live jobs spin; data refreshes every 5 ticks, the public probe every 30.
# Click a job (or press its number) to jump to that job's Herdr tab; click a supervisor row
# (worker/astra/sol) under it to open a chat with that model in a new pane;
# click a job's yellow question line to expand its evidence panel, then its [ buttons ] (accept/stop/approve/deny
# act on one tap; [ send back ] and [ chat ] open the job chat; [ why? ] opens an evidence chat).
# click a supervisor's [switch] to turn it on/off (astra cycles off/shadow/active).
[ -n "${STEWARD_INSTALL_ROOT:-}" ] || { echo "steward-dash: start it with 'steward dash'" >&2; exit 1; }
cd "$STEWARD_INSTALL_ROOT" || exit 1
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"  # the jobs renderer imports steward from the install root
export GIT_OPTIONAL_LOCKS=0  # the panel only reads: never take (or strand) a repo index.lock
STEWARD_PYTHON="${STEWARD_PYTHON:-python3}"
ONCE="${STEWARD_DASH_ONCE:-}"
export STEWARD_DASH_MAP="${XDG_RUNTIME_DIR:-/tmp}/steward-dash.$$.map"
# Alternate screen: redraws never pile up in scrollback, so scrolling the pane shows nothing stale.
# No echo: mouse/wheel bytes that arrive while a frame renders are never printed as text.
if [ "$ONCE" != 1 ]; then
  _stty=$(stty -g 2>/dev/null); stty -echo 2>/dev/null
  printf '\033[?1049h\033[?1000h\033[?1006h\033[?25l'  # alt screen, mouse clicks (SGR encoding) on, cursor off
  while IFS= read -rsn1 -t 0.1 _; do :; done   # drain input queued before we started (no phantom clicks)
  trap 'printf "\033[?1000l\033[?1006l\033[?25h\033[?1049l"; [ -n "$_stty" ] && stty "$_stty" 2>/dev/null; rm -f "$STEWARD_DASH_MAP" "$STEWARD_DASH_MAP.note" "$STEWARD_DASH_MAP.expanded" "$STEWARD_DASH_MAP.expanded.tmp"; exit' INT TERM EXIT
else
  trap 'rm -f "$STEWARD_DASH_MAP" "$STEWARD_DASH_MAP.note" "$STEWARD_DASH_MAP.expanded" "$STEWARD_DASH_MAP.expanded.tmp"' EXIT
fi
spin=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
pub="-"; n=0; body=""; note=""; pending_confirm=""
declare -A row_act key_tab tog_act btn_act exp_act

# Background commands report their real outcome through this file; the loop shows it as the note.
NOTE_FILE="$STEWARD_DASH_MAP.note"
run_bg() {  # <pending note> <command...>: run in the background, then note what actually happened
  local pending=$1; shift
  note="$pending"
  { out=$("$@" 2>&1 | tail -1); printf '%s\n' "${out:-done}" > "$NOTE_FILE"; } </dev/null >/dev/null 2>&1 &
}

chat() { run_bg "… opening $2 chat for ${1:0:8}" "$STEWARD_PYTHON" -m steward.cli chat "$1" "$2"; }

refresh_now() { n=$(( (n / 5 + 1) * 5 )); }  # redraw the rows on the next pass

press_btn() {  # <action> <job> <approval id|->: only the bulk answers (accept all ★, accept ★ + accept) need a second click
  local out first
  out=$("$STEWARD_PYTHON" -m steward.dashact press "$1" "$2" "$3" 2>/dev/null)
  first=${out%%$'\n'*}
  case $first in
    armed) local review label
           review=$(sed -n 2p <<< "$out")  # the line after `armed`: what the bulk answer would take (G4, G9)
           case $1 in accept_rec) label="accept all ★" ;; accept_rec_accept) label="accept ★ + accept" ;; *) label=$1 ;; esac
           if [ -n "$review" ]; then note="→ $review — tap again (or [ confirm $label ]) within 20s"
           else note="→ tap here (or [ confirm $label ]) within 20s to $1"; fi
           pending_confirm="$1 $2 $3"; refresh_now ;;
    run) local -a argv; mapfile -t argv < <(tail -n +2 <<< "$out")
         run_bg "… $1 ${2:0:8}" "${argv[@]}" ;;
    *) note="couldn't $1 ${2:0:8}" ;;
  esac
}

expand_job() {  # toggle a job's evidence panel
  "$STEWARD_PYTHON" -m steward.dashact expand "$1" >/dev/null 2>&1
  refresh_now
}

toggle_sup() {  # <job> <role> <next>
  run_bg "… switching $2 to $3 for ${1:0:8}" "$STEWARD_PYTHON" -m steward.cli supervisor "$1" "$2" toggle
}

goto() {  # focus a job's tab; a one-line note says what happened
  if [ -n "$1" ] && herdr tab focus "$1" >/dev/null 2>&1; then note="→ opened $1"; else note="couldn't open ${1:-that job}"; fi
}

while true; do
  if (( n % 30 == 0 )) && [ -n "${STEWARD_PUBLIC_URL:-}" ]; then  # the user's own connector, only when configured
    pub=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$STEWARD_PUBLIC_URL")
  fi
  if (( n % 5 == 0 )); then
    export STEWARD_DASH_COLS=$(tput cols 2>/dev/null || echo 80)  # rows stay clickable only if nothing wraps
    export STEWARD_DASH_PUB="$pub"  # the connector probe result; dash_services shows it when a connector is configured
    svc=$("$STEWARD_PYTHON" -m steward.dash_services 2>&1)
    svc_rows=$(( $(printf '%s\n' "$svc" | wc -l) + 1 ))  # the service lines plus the blank line after them
    # ...and only if the frame never scrolls. The jobs renderer budgets for 5 service rows; give it the real count.
    lines_now=$(tput lines 2>/dev/null || echo 0)
    if (( lines_now > 0 )); then
      export STEWARD_DASH_ROWS=$(( lines_now - (svc_rows - 5) )); (( STEWARD_DASH_ROWS < 1 )) && STEWARD_DASH_ROWS=1
    else
      export STEWARD_DASH_ROWS=0
    fi
    body=$(
      printf '%s\n\n' "$svc"  # the services block, then a blank line
      "$STEWARD_PYTHON" -m steward.dash_jobs 2>&1
    )
    # Map screen rows to actions. The map has "key <n> <tab>", "expand <idx> <job>", "btn <idx> <c0> <c1> <action> <job> <id>" and "row <idx> <focus <tab>|chat <job> <role>>",
    # idx being the line within the jobs output, which starts after svc_rows (the service lines and the blank line
    # after them, counted from the real block) below the 2 header lines: screen row = 3 + svc_rows + idx.
    row_act=(); key_tab=(); tog_act=(); btn_act=(); exp_act=()
    while read -r kind a b c d e f; do
      case $kind in
        key) key_tab[$a]=$b ;;
        row) row_act[$((3 + svc_rows + a))]="$b $c $d" ;;
        expand) exp_act[$((3 + svc_rows + a))]=$b ;;
        btn) btn_act[$((3 + svc_rows + a))]+="$b $c $d $e $f"$'\n' ;;  # col_start col_end action job approval-id
        toggle) tog_act[$((3 + svc_rows + a))]="$b $c $d $e $f" ;;  # col_start col_end job role next
      esac
    done < "$STEWARD_DASH_MAP" 2>/dev/null
  fi
  if [ -s "$NOTE_FILE" ]; then
    note="$(cat "$NOTE_FILE")"; rm -f "$NOTE_FILE"
    # "<job id>  ACCEPTED" from accept/release → "✓ accepted 080502f5"
    if [[ $note =~ ^([0-9a-f]{8})[0-9a-f-]{28}[[:space:]]+([A-Z_]+)$ ]]; then
      note="✓ ${BASH_REMATCH[2],,} ${BASH_REMATCH[1]}"
    fi
    note="→ $note"
    n=$(( (n / 5 + 1) * 5 )); continue  # outcome is in: redraw the rows now so switches show their new state
  fi
  frame=${spin[n % ${#spin[@]}]}
  n=$((n+1))
  if [ "$ONCE" = 1 ]; then  # one frame for scripts and tests: no screen clear, no key loop
    printf '\033[1mSTEWARD\033[0m  %s %s\n\n%s\n' "$(date +%H:%M:%S)" "$frame" "${body//\{SPIN\}/$frame}"
    exit 0
  fi
  printf '\033[H\033[2J\033[1mSTEWARD\033[0m  %s %s  \033[2mclick or 1-9 to open\033[0m\n\n%s\n\033[2m%s\033[0m' \
    "$(date +%H:%M:%S)" "$frame" "${body//\{SPIN\}/$frame}" "$note"
  # Wait up to 1s for a key or click instead of sleeping.
  note_row=$(( 3 + $(printf '%s\n' "$body" | wc -l) ))  # header + blank + body lines, then the note
  if IFS= read -rsn1 -t 1 k; then
    if [[ $k == $'\e' ]]; then
      seq=""; while IFS= read -rsn1 -t 0.05 c; do seq+=$c; [[ $c == [A-Za-z] ]] && break; done
      # Only a well-formed SGR left-button PRESS acts: ESC [ < 0 ; col ; row M.
      # Everything else (focus events ESC[I / ESC[O, releases, other buttons) is ignored.
      if [[ $seq =~ ^\[\<0\;([0-9]+)\;([0-9]+)M$ ]]; then
        col=${BASH_REMATCH[1]}; srow=${BASH_REMATCH[2]}
        # The note under an armed button is itself the confirm target (people tap the words they read).
        if (( srow == note_row )) && [ -n "$pending_confirm" ]; then
          read -r pa pj pi <<< "$pending_confirm"; pending_confirm=""
          press_btn "$pa" "$pj" "$pi"
          continue
        fi
        # A click inside a row's [switch] toggles that supervisor; anywhere else falls through to the row action.
        read -r c0 c1 tj tr tn <<< "${tog_act[$srow]}"
        if [ -n "$tj" ] && (( col >= c0 && col <= c1 )); then
          toggle_sup "$tj" "$tr" "$tn"
          continue
        fi
        # A click on a decision [ button ] presses it; on a question line, toggles its evidence panel.
        if [ -n "${btn_act[$srow]}" ]; then
          hit=""
          while read -r b0 b1 bact bjob bid; do
            if [ -n "$bact" ] && (( col >= b0 && col <= b1 )); then press_btn "$bact" "$bjob" "$bid"; hit=1; break; fi
          done <<< "${btn_act[$srow]}"
          [ -n "$hit" ] && continue
        fi
        if [ -n "${exp_act[$srow]}" ]; then expand_job "${exp_act[$srow]}"; continue; fi
        act=${row_act[$srow]}
        read -r verb x y <<< "$act"
        case $verb in
          focus) [ -n "$x" ] && goto "$x" ;;
          chat) [ -n "$x" ] && [ -n "$y" ] && chat "$x" "$y" ;;
          jobchat) [ -n "$x" ] && goto "$x"; [ -n "$y" ] && chat "$y" job ;;  # tab focus + the job's chat
        esac
      fi
    elif [[ $k =~ ^[1-9]$ ]] && [ -n "${key_tab[$k]}" ]; then
      goto "${key_tab[$k]}"
    fi
  fi
done
