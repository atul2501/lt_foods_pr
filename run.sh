#!/usr/bin/env bash
# Runs the whole service - API + email poller - in the background and keeps it running:
# any service that crashes or exits is restarted automatically (with backoff), so one bad
# moment (Gmail dropping the connection, OOM) doesn't leave the system half-down. Works in Git Bash / MSYS2 on Windows and on Linux/EC2.
#
#   ./run.sh            start everything in the background (returns immediately)
#   ./run.sh stop       stop everything
#   ./run.sh restart    stop, then start
#   ./run.sh status     show what's running
#   ./run.sh logs       follow logs/run.log + logs/app.log (Ctrl+C stops following only)
#   PORT=9000 ./run.sh  API on a different port (default 8001)
#
# Logs:
#   logs/app.log  - everything the app does today (JSON, one line per event); earlier days are
#                   moved to logs/app-<date>.log, deleted after LOG_RETENTION_DAYS (default 30) - LOG_FILE in .env
#   logs/run.log  - service starts/stops/crashes/restarts, plus any error output a service
#                   prints outside the app log (crash tracebacks, uvicorn startup errors)
set -uo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8002}"
RUN_DIR=.run
LOG_DIR=logs
RUN_LOG="$LOG_DIR/run.log"
MAIN_PID_FILE="$RUN_DIR/main.pid"
RUN_ID_FILE="$RUN_DIR/run.id"
STOP_FLAG="$RUN_DIR/stopping"
SERVICES=(api email)
# EMAIL_ENABLED=false (in .env, or as an env var which wins) starts only the API - the email
# poller is skipped. stop/status still cover every service, so a poller left running from an
# earlier start is still stopped.
EMAIL_ENABLED="${EMAIL_ENABLED:-$(grep -E '^[[:space:]]*EMAIL_ENABLED=' .env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d "\"' \r")}"
EMAIL_ENABLED="$(echo "${EMAIL_ENABLED:-true}" | tr -d "\"' 
" | tr '[:upper:]' '[:lower:]')"
START_SERVICES=(api)
case "$EMAIL_ENABLED" in
  false|0|no|off) ;;
  *) START_SERVICES+=(email) ;;
esac
# A service that stayed up at least this long is considered healthy again, so its restart
# delay resets to the minimum instead of staying at the backed-off value.
HEALTHY_AFTER_SECONDS=60
MIN_RESTART_DELAY=2
MAX_RESTART_DELAY=60
# Linux only: how long a service gets to exit on SIGTERM before it is force-killed. Safe
# either way - an email whose PDFs weren't all extracted stays unread and is retried.
STOP_GRACE_SECONDS=30

# IS_WINDOWS: the services are native Windows processes - either run from Git Bash/MSYS2,
# or from WSL using this project's Windows venv (.venv/Scripts/python.exe). Both need the
# Windows process handling below; plain `kill` can't reliably stop those processes.
IS_WINDOWS=0
IS_WSL=0
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;;
  Linux)
    if grep -qi microsoft /proc/version 2>/dev/null && [ -f .venv/Scripts/python.exe ] && [ ! -x .venv/bin/python ]; then
      IS_WINDOWS=1
      IS_WSL=1
    fi
    ;;
esac
if (( IS_WSL )); then ENV_KIND=wsl; elif (( IS_WINDOWS )); then ENV_KIND=msys; else ENV_KIND=linux; fi

# Always appended to run.log; also echoed when a person is watching (a terminal), but not
# in the background process, whose stdout already *is* run.log - that would double lines.
log() {
  local line="$(date '+%Y-%m-%d %H:%M:%S') [run.sh] $*"
  echo "$line" >> "$RUN_LOG"
  [ -t 1 ] && echo "$line"
  return 0
}

is_alive() {
  [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null
}

read_pid() {
  [ -f "$1" ] && cat "$1" 2>/dev/null
}

# Command-line pattern identifying each service's process (used on Windows, see below).
service_pattern() {
  case "$1" in
    api)    echo "uvicorn main:app" ;;
    email)  echo "email_ingest_main.py" ;;
  esac
}

# --- Windows process handling -------------------------------------------------------
# On Windows the services are native processes (.venv\Scripts\python.exe is a launcher
# that starts the real C:\PythonXY\python.exe as its child). MSYS/Git Bash/WSL `kill`
# does not reliably terminate those, and pids differ between Git Bash, MSYS2 and WSL - so
# instead of trusting pid files, services are found by what they actually are: this
# project's venv python running one of the service commands. That is also what
# catches copies left behind by an earlier crashed or killed run, whichever terminal
# started it.

win_path() {
  if (( IS_WSL )); then wslpath -w "$1"; else cygpath -w "$1"; fi
}

# Prints "<windows pid> <service>" for every running service launcher of this project.
win_service_pids() {
  # WSLENV passes VENV_PY through to powershell.exe when run from WSL; ignored elsewhere.
  VENV_PY="$(win_path "$PWD/.venv/Scripts/python.exe")" WSLENV="VENV_PY${WSLENV:+:$WSLENV}" \
    powershell.exe -NoProfile -NonInteractive -Command '
      Get-CimInstance Win32_Process |
        Where-Object { $_.ExecutablePath -eq $env:VENV_PY } |
        ForEach-Object {
          $c = $_.CommandLine
          if ($c -like "*uvicorn main:app*") { "$($_.ProcessId) api" }
          elseif ($c -like "*email_ingest_main.py*") { "$($_.ProcessId) email" }
        }' </dev/null 2>/dev/null | tr -d '\r'
}

# Kills a Windows process and all of its children (the launcher + the real python).
# </dev/null: taskkill reads stdin, and inside a `while read ... done < <(list)` loop it
# would otherwise swallow the rest of the list, leaving every later service running.
win_kill_tree() {
  if (( IS_WSL )); then
    taskkill.exe /F /T /PID "$1" </dev/null >/dev/null 2>&1
  else
    taskkill //F //T //PID "$1" </dev/null >/dev/null 2>&1   # // stops MSYS turning /F into a path
  fi
}

# Windows pid of an MSYS/Git Bash process (not available under WSL).
win_winpid() {
  (( IS_WSL )) || cat "/proc/$1/winpid" 2>/dev/null
}
# -------------------------------------------------------------------------------------

# Run in a background subshell; exec makes the service itself the process behind $!.
# stdout is dropped because it's the same JSON the app already writes to LOG_FILE
# (logs/app.log); stderr goes to run.log so a crash traceback or a uvicorn startup error
# is never lost.
run_service() {
  case "$1" in
    api)    exec "$PY" -m uvicorn main:app --host 0.0.0.0 --port "$PORT" --no-use-colors >/dev/null 2>>"$RUN_LOG" ;;
    email)  exec "$PY" email_ingest_main.py >/dev/null 2>>"$RUN_LOG" ;;
  esac
}

# Uses the venv's python directly rather than sourcing its activate script - a
# Windows-created activate has CRLF line endings that MSYS2/Git Bash can't parse, and
# calling the interpreter by path needs no activation anyway.
find_venv_python() {
  if [ -x .venv/Scripts/python.exe ]; then
    PY=.venv/Scripts/python.exe        # Windows venv layout
  elif [ -x .venv/bin/python ]; then
    PY=.venv/bin/python                # Linux/macOS venv layout
  else
    echo "No .venv found - create it first: python -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
  fi
  if ! "$PY" -c "import uvicorn" 2>/dev/null; then
    echo "Dependencies missing in .venv - run: $PY -m pip install -r requirements.txt" >&2
    exit 1
  fi
}

# Service stdout is discarded (see run_service), so the app log file is the only record
# of what the app does - make sure there is one even if .env doesn't set LOG_FILE. Only
# set when .env lacks it: a real env var would override the .env value.
ensure_app_log_file() {
  if ! grep -qE '^[[:space:]]*LOG_FILE=' .env 2>/dev/null; then
    export LOG_FILE="$LOG_DIR/app.log"
  fi
}

port_is_free() {
  "$PY" -c "import socket, sys; socket.socket().bind(('0.0.0.0', int(sys.argv[1])))" "$PORT" 2>/dev/null
}

# Runs one service forever: start it, wait for it to exit, restart it after a delay that
# doubles on each quick crash (2s, 4s, ... 60s) so a service that can't start (bad config)
# doesn't spin, but recovers on its own once the cause is fixed.
supervise() {
  local name=$1
  local delay=$MIN_RESTART_DELAY

  while is_current_run; do
    local started=$SECONDS
    run_service "$name" &
    local child=$!
    echo "$child" > "$RUN_DIR/$name.pid"
    log "$name started (pid $child)"

    local code=0
    wait "$child" || code=$?
    rm -f "$RUN_DIR/$name.pid"
    is_current_run || break

    if (( SECONDS - started >= HEALTHY_AFTER_SECONDS )); then
      delay=$MIN_RESTART_DELAY
    fi
    log "$name went down (exit code $code) - restarting in ${delay}s"
    sleep "$delay"
    delay=$(( delay * 2 > MAX_RESTART_DELAY ? MAX_RESTART_DELAY : delay * 2 ))
  done
}

# A supervisor keeps restarting its service only while its own run is the current one.
# Checked after every wait/sleep, so a supervisor that outlived a stop (e.g. one asleep in
# its backoff, or from a run started in a different terminal that `stop` couldn't reach)
# exits on its next wake-up instead of starting a duplicate service.
is_current_run() {
  [ ! -f "$STOP_FLAG" ] && [ "$(cat "$RUN_ID_FILE" 2>/dev/null)" = "$RUN_ID" ]
}

# Stops supervisors first (so nothing gets restarted), then the services themselves.
stop_services() {
  touch "$STOP_FLAG"

  if (( IS_WINDOWS )); then
    # Supervisors aren't killed by pid here (pid files may come from another shell's
    # numbering, see own_main_pid) - the stop flag / cleared run id makes each one exit
    # as soon as its service below is gone.
    local winpid _
    while read -r winpid _; do
      [ -n "$winpid" ] && win_kill_tree "$winpid"
    done < <(win_service_pids)
    return
  fi

  local name pid
  for name in "${SERVICES[@]}"; do
    pid=$(read_pid "$RUN_DIR/$name.supervisor.pid")
    is_alive "$pid" && kill "$pid" 2>/dev/null
  done

  local pids=()
  for name in "${SERVICES[@]}"; do
    pid=$(read_pid "$RUN_DIR/$name.pid")
    if is_alive "$pid"; then
      kill -TERM "$pid" 2>/dev/null
      pids+=("$pid")
    fi
  done

  local waited=0
  while (( waited < STOP_GRACE_SECONDS )); do
    local any_alive=0
    for pid in "${pids[@]}"; do
      is_alive "$pid" && any_alive=1
    done
    (( any_alive )) || break
    sleep 1
    waited=$(( waited + 1 ))
  done
  for pid in "${pids[@]}"; do
    if is_alive "$pid"; then
      log "pid $pid did not stop within ${STOP_GRACE_SECONDS}s - force killing"
      kill -9 "$pid" 2>/dev/null
    fi
  done
}

# Prints one "  <service>: running (...)" / "not running" line per service. Returns 0 if
# anything at all is running (including leftovers), 1 if nothing is.
status() {
  local name any=0
  if (( IS_WINDOWS )); then
    local found
    found=$(win_service_pids)
    for name in "${SERVICES[@]}"; do
      local pids
      pids=$(awk -v s="$name" '$2 == s { printf "%s%s", sep, $1; sep = ", " }' <<< "$found")
      if [ -n "$pids" ]; then
        local count
        count=$(awk -v s="$name" '$2 == s' <<< "$found" | wc -l)
        if (( count > 1 )); then
          echo "  $name: running $count COPIES (windows pids $pids) - run './run.sh stop'"
        else
          echo "  $name: running (windows pid $pids)"
        fi
        any=1
      else
        echo "  $name: not running"
      fi
    done
  else
    local pid
    for name in "${SERVICES[@]}"; do
      pid=$(read_pid "$RUN_DIR/$name.pid")
      if is_alive "$pid"; then
        echo "  $name: running (pid $pid)"
        any=1
      else
        echo "  $name: not running"
      fi
    done
  fi
  (( any ))
}

# The background process's pid, but only if it was started from this same kind of shell -
# pids from Git Bash, MSYS2 and WSL are separate numbering spaces, so a pid written by one
# could name an unrelated process in another.
own_main_pid() {
  local pid kind
  [ -f "$MAIN_PID_FILE" ] || return 0
  read -r pid kind < "$MAIN_PID_FILE"
  [ "$kind" = "$ENV_KIND" ] && echo "$pid"
}

is_running() {
  if (( IS_WINDOWS )); then
    # The real service processes are the only reliable signal on Windows (see above).
    [ -n "$(win_service_pids)" ]
    return
  fi
  is_alive "$(own_main_pid)" && return 0
  local name
  for name in "${SERVICES[@]}"; do
    is_alive "$(read_pid "$RUN_DIR/$name.pid")" && return 0
  done
  return 1
}

clear_run_state() {
  rm -f "$RUN_DIR"/*.pid "$RUN_DIR"/*.winpid "$RUN_ID_FILE" "$STOP_FLAG"
}

# The background process: supervises the services until told to stop.
daemon() {
  echo "$$ $ENV_KIND" > "$MAIN_PID_FILE"
  (( IS_WINDOWS )) && win_winpid $$ > "$RUN_DIR/main.winpid"
  RUN_ID="$$-$ENV_KIND-$(date +%s)"
  echo "$RUN_ID" > "$RUN_ID_FILE"
  find_venv_python
  ensure_app_log_file

  on_signal() {
    trap - INT TERM HUP
    log "shutdown requested - stopping all services"
    stop_services
    clear_run_state
    log "all services stopped"
    exit 0
  }
  trap on_signal INT TERM HUP

  local name
  [[ " ${START_SERVICES[*]} " == *" email "* ]] || log "email poller disabled (EMAIL_ENABLED=$EMAIL_ENABLED) - starting API only"
  for name in "${START_SERVICES[@]}"; do
    supervise "$name" &
    echo $! > "$RUN_DIR/$name.supervisor.pid"
  done
  log "all services running - API: http://localhost:$PORT/docs"

  # Keeps the daemon alive while its supervisors run; exits once this run is no longer
  # current (stopped, or replaced by a newer run) and its supervisors have exited.
  # Looping because `wait` returns early whenever a trap fires.
  while is_current_run; do
    wait
    sleep 1
  done
  wait
}

# Git Bash must not be launched as a child of WSL: every Windows process started through
# WSL interop is killed when the WSL command that launched it exits, so the services died
# seconds after start (with "child_copy ... Win32 error 299" fork errors in run.log).
# Win32_Process.Create starts it via the WMI service instead, outside this WSL session.
# Its output goes to a file, shown here once it finishes.
start_via_git_bash() {
  local gitbash='C:\Program Files\Git\bin\bash.exe'
  if [ ! -x "/mnt/c/Program Files/Git/bin/bash.exe" ]; then
    echo "Running under WSL, but the services must be started from Git Bash on Windows" >&2
    echo "(Git for Windows not found at C:\\Program Files\\Git). Start it from a Git Bash terminal." >&2
    return 1
  fi
  echo "(WSL detected - starting via Git Bash so the services don't depend on this WSL session)"

  local out="$RUN_DIR/start.out" done_file="$RUN_DIR/start.done"
  rm -f "$out" "$done_file"
  local project
  project=$(wslpath -m "$PWD")
  local cmd="\"$gitbash\" -c \"cd '$project' && PORT=$PORT ./run.sh start > $out 2>&1; echo \$? > $done_file\""
  if ! RUN_CMD="$cmd" WSLENV="RUN_CMD${WSLENV:+:$WSLENV}" powershell.exe -NoProfile -NonInteractive -Command '
      $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $env:RUN_CMD }
      if ($r.ReturnValue -ne 0) { Write-Error "Win32_Process.Create failed: $($r.ReturnValue)"; exit 1 }' </dev/null; then
    echo "Could not launch Git Bash - start it from a Git Bash terminal instead." >&2
    return 1
  fi

  local waited=0
  while [ ! -s "$done_file" ] && (( waited < 120 )); do
    sleep 1
    waited=$(( waited + 1 ))
  done
  cat "$out" 2>/dev/null
  if [ ! -s "$done_file" ]; then
    echo "Git Bash start did not finish within 120s - check './run.sh status' and logs/run.log" >&2
    return 1
  fi
  local code
  code=$(tr -dc '0-9' < "$done_file")
  rm -f "$out" "$done_file"
  return "${code:-1}"
}

start() {
  # From WSL, the services would be Windows processes owned through WSL's interop bridge,
  # which WSL tears down when the terminal/session that started them closes - leaving the
  # supervisors unable to track them (and restarting duplicates). Git Bash runs them as
  # plain Windows processes instead, so hand the start over to it. (This also covers an
  # MSYS2 terminal whose `bash` resolves to WSL's C:\Windows\System32\bash.exe.)
  if (( IS_WSL )); then
    start_via_git_bash
    return
  fi

  if is_running; then
    echo "Already running:"
    status
    echo "Use './run.sh restart' to restart or './run.sh stop' to stop."
    return 1
  fi
  clear_run_state

  # Done here in the foreground so a broken venv or busy port shows up
  # immediately in the terminal instead of only inside a log file.
  find_venv_python
  if ! port_is_free; then
    echo "Port $PORT is already in use by another program - stop it, or start on another port: PORT=9000 ./run.sh" >&2
    return 1
  fi

  log "starting in background"
  nohup "$0" __daemon >> "$RUN_LOG" 2>&1 < /dev/null &
  disown

  # Give the services a moment so status reflects reality (and an instant crash shows up).
  sleep 5
  echo
  status || true
  echo
  echo "Running in the background. API: http://localhost:$PORT/docs"
  echo "Logs:   logs/app.log (app, today)   logs/run.log (starts/stops/crashes)"
  echo "        ./run.sh logs     to follow both"
  echo "Stop:   ./run.sh stop"
}

stop() {
  if ! is_running; then
    echo "Not running."
    clear_run_state
    return 0
  fi
  echo "Stopping..."
  log "stop requested"

  local main_pid
  main_pid=$(own_main_pid)
  if (( IS_WINDOWS )); then
    # Invalidate the run first: every supervisor of it - including one this shell can't
    # see, e.g. started from WSL vs Git Bash - exits instead of restarting what's killed next.
    touch "$STOP_FLAG"
    rm -f "$RUN_ID_FILE"
    local main_winpid=""
    [ -n "$main_pid" ] && main_winpid=$(read_pid "$RUN_DIR/main.winpid")
    [ -n "$main_winpid" ] && win_kill_tree "$main_winpid"
  elif is_alive "$main_pid"; then
    kill -TERM "$main_pid" 2>/dev/null
    local waited=0
    while is_alive "$main_pid" && (( waited < STOP_GRACE_SECONDS + 10 )); do
      sleep 1
      waited=$(( waited + 1 ))
    done
  fi
  # Also catches services left behind by an earlier run that died without cleaning up.
  stop_services
  is_alive "$main_pid" && kill -9 "$main_pid" 2>/dev/null
  clear_run_state
  log "all services stopped"

  if (( IS_WINDOWS )) && [ -n "$(win_service_pids)" ]; then
    echo "Some services could not be stopped:" >&2
    status >&2
    return 1
  fi
  echo "Stopped."
}

mkdir -p "$RUN_DIR" "$LOG_DIR" data/pdfs
touch "$RUN_LOG"
# run.log only grows on starts/stops/crashes, but never let it grow without limit: once it
# passes RUN_LOG_MAX_BYTES, keep just its most recent lines.
RUN_LOG_MAX_BYTES=5000000
if [ "$(wc -c < "$RUN_LOG")" -gt "$RUN_LOG_MAX_BYTES" ]; then
  tail -n 5000 "$RUN_LOG" > "$RUN_LOG.tmp" && mv -f "$RUN_LOG.tmp" "$RUN_LOG"
fi

case "${1:-start}" in
  start)    start ;;
  stop)     stop ;;
  restart)  stop && start ;;
  status)
    if out=$(status); then echo "Running:"; else echo "Not running:"; fi
    echo "$out"
    ;;
  logs)     tail -n 50 -F "$RUN_LOG" "$LOG_DIR/app.log" ;;
  __daemon) daemon ;;
  *)
    echo "usage: $0 [start|stop|restart|status|logs]" >&2
    exit 2
    ;;
esac
