#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
if [[ -f "/.dockerenv" ]]; then
    PYTHON_BIN="python"
    CELERY_BIN="celery"
else
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
    CELERY_BIN="$PROJECT_DIR/.venv/bin/celery"
fi

if [[ $# -gt 0 && ( "$1" == "help" || "$1" == "--help" || "$1" == "-h" ) ]]; then
    MODE="$1"
    shift
elif [[ $# -gt 0 && "$1" != -* ]]; then
    MODE="$1"
    shift
else
    MODE="cli"
fi

EXTRA_ARGS=("$@")
LOCAL_OLLAMA_URL="http://127.0.0.1:11434"
LOCAL_REDIS_URL="redis://127.0.0.1:6379/0"
TMUX_STARTED=0
TMUX_SESSION="latice"

# Match dotenv's local-file convention while allowing a shell override.
if [[ -z "${OLLAMA_URL+x}" ]]; then
    for env_file in "$PROJECT_DIR/.env.local" "$PROJECT_DIR/.env"; do
        if [[ -f "$env_file" ]]; then
            OLLAMA_URL="$(sed -n 's/^[[:space:]]*OLLAMA_URL[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)"
            OLLAMA_URL="${OLLAMA_URL//$'\r'/}"
            OLLAMA_URL="${OLLAMA_URL#\"}"
            OLLAMA_URL="${OLLAMA_URL%\"}"
            [[ -n "$OLLAMA_URL" ]] && break
        fi
    done
fi

OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"

if [[ -z "${OLLAMA_NAME+x}" ]]; then
    for env_file in "$PROJECT_DIR/.env.local" "$PROJECT_DIR/.env"; do
        if [[ -f "$env_file" ]]; then
            OLLAMA_NAME="$(sed -n 's/^[[:space:]]*OLLAMA_NAME[[:space:]]*=[[:space:]]*//p' "$env_file" | tail -n 1)"
            OLLAMA_NAME="${OLLAMA_NAME//$'\r'/}"
            OLLAMA_NAME="${OLLAMA_NAME#\"}"
            OLLAMA_NAME="${OLLAMA_NAME%\"}"
            [[ -n "$OLLAMA_NAME" ]] && break
        fi
    done
fi
OLLAMA_NAME="${OLLAMA_NAME:-phi3:latest}"

if [[ -f "/.dockerenv" ]]; then
    RUNTIME_REDIS_URL="${REDIS_URL:-redis://redis_server:6379/0}"
else
    RUNTIME_REDIS_URL="$LOCAL_REDIS_URL"
fi

quote_args() {
    local arg
    for arg in "$@"; do
        printf '%q ' "$arg"
    done
}

CLI_ARGS="$(quote_args "${EXTRA_ARGS[@]}")"
CMD_CLI="cd $(printf '%q' "$PROJECT_DIR") && OLLAMA_URL=$(printf '%q' "$OLLAMA_URL") REDIS_URL=$(printf '%q' "$RUNTIME_REDIS_URL") exec $(printf '%q' "$PYTHON_BIN") main.py ${CLI_ARGS}"
CMD_FULL_CLI="cd $(printf '%q' "$PROJECT_DIR") && OLLAMA_URL=$(printf '%q' "$LOCAL_OLLAMA_URL") REDIS_URL=$(printf '%q' "$RUNTIME_REDIS_URL") exec $(printf '%q' "$PYTHON_BIN") main.py ${CLI_ARGS}"
CMD_DEV_CLI="cd $(printf '%q' "$PROJECT_DIR") && OLLAMA_URL=$(printf '%q' "$LOCAL_OLLAMA_URL") REDIS_URL=$(printf '%q' "$RUNTIME_REDIS_URL") exec $(printf '%q' "$PYTHON_BIN") main.py ${CLI_ARGS}"
CMD_CELERY="cd $(printf '%q' "$PROJECT_DIR") && REDIS_URL=$(printf '%q' "$RUNTIME_REDIS_URL") exec $(printf '%q' "$CELERY_BIN") -A app.celery worker --loglevel=info"

print_usage() {
    cat <<'EOF'
Usage: ./start.sh [MODE] [main.py args...]

Modes:
  cli           Run main.py in the current terminal (default)
  full          Start Docker Redis/Ollama, then run Celery and main.py in panes
  dev           Start Docker Redis/Ollama, then run Celery and main.py in panes
  celery-only   Start Docker Redis/Ollama, then run the Celery worker
  ollama-only   Start Docker Ollama and ensure its configured model is present

Environment:
    OLLAMA_URL    Ollama URL used by main.py (read from .env or .env.local).
                  Note: dev/full modes always use http://127.0.0.1:11434 for
                  main.py, regardless of .env, since those modes assume
                  everything is running locally, not via docker-compose.
    REDIS_URL     Local modes use redis://127.0.0.1:6379/0. Docker retains
                  its configured Redis URL.
    OLLAMA_NAME   Model to check or pull in the local Ollama container.

Examples:
  ./start.sh full
  OLLAMA_URL=http://192.168.1.50:11434 ./start.sh dev
  ./start.sh cli --help
EOF
}

run_cli() {
    local args=("$PYTHON_BIN" main.py)
    if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
        args+=("${EXTRA_ARGS[@]}")
    fi
    exec env OLLAMA_URL="$OLLAMA_URL" REDIS_URL="$RUNTIME_REDIS_URL" "${args[@]}"
}

ensure_project_venv() {
    if ! command -v "$PYTHON_BIN" &>/dev/null || ! command -v "$CELERY_BIN" &>/dev/null; then
        echo "Project virtual environment is missing or incomplete. Run 'uv sync' first." >&2
        return 1
    fi
}

ensure_local_docker_services() {
    [[ -f "/.dockerenv" ]] && return 0

    if ! docker compose version &>/dev/null; then
        echo "Docker Compose is required for local Redis and Ollama services." >&2
        return 1
    fi

    docker compose up -d redis_server ollama

    local models=""
    local attempt
    for attempt in $(seq 1 30); do
        models="$(docker compose exec -T ollama ollama list 2>/dev/null || true)"
        [[ -n "$models" ]] && break
        sleep 1
    done

    if [[ -z "$models" ]]; then
        echo "Ollama container did not become ready within 30 seconds." >&2
        return 1
    fi

    if printf '%s\n' "$models" | awk 'NR > 1 {print $1}' | grep -Fxq -- "$OLLAMA_NAME"; then
        echo "Ollama model '$OLLAMA_NAME' is already available."
    else
        echo "Pulling Ollama model '$OLLAMA_NAME'..."
        docker compose exec -T ollama ollama pull "$OLLAMA_NAME"
    fi
}

cleanup_stale_tmux_session() {
    if command -v tmux &>/dev/null && tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
    fi
}

launch_terminal() {
    local title="$1"
    local command="$2"
    local login_cmd="bash -lc $(printf '%q' "$command")"

    # Keep local services visible together. Every process gets a titled pane
    # in one tiled tmux window instead of a separate tmux window.
    if command -v tmux &>/dev/null; then
        local pane_id
        if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
            pane_id="$(tmux new-session -d -s "$TMUX_SESSION" -n "Latice" -P -F '#{pane_id}' "$login_cmd")"
            tmux set-window-option -t "$TMUX_SESSION:0" remain-on-exit on
            tmux set-window-option -t "$TMUX_SESSION:0" pane-border-status top
            tmux set-window-option -t "$TMUX_SESSION:0" pane-border-format '#{pane_title}'
        else
            pane_id="$(tmux split-window -d -t "$TMUX_SESSION:0" -P -F '#{pane_id}' "$login_cmd")"
        fi
        tmux select-pane -t "$pane_id" -T "$title"
        tmux select-layout -t "$TMUX_SESSION:0" tiled
        TMUX_STARTED=1
        return 0
    fi

    if [[ -n "${DISPLAY:-}" ]]; then
        if command -v gnome-terminal &>/dev/null; then
            gnome-terminal --title="$title" -- bash -lc "$command" &
            return 0
        elif command -v konsole &>/dev/null; then
            konsole --new-tab -e bash -lc "$command" &
            return 0
        elif command -v alacritty &>/dev/null; then
            alacritty --title "$title" -e bash -lc "$command" &
            return 0
        elif command -v kitty &>/dev/null; then
            kitty --title "$title" bash -lc "$command" &
            return 0
        elif command -v xterm &>/dev/null; then
            xterm -T "$title" -e bash -lc "$command" &
            return 0
        fi
    fi

    echo "No supported terminal emulator found for '$title'." >&2
    return 1
}

attach_tmux_session() {
    [[ "$TMUX_STARTED" -eq 1 ]] || return 0
    tmux select-window -t "${TMUX_SESSION}:0"
    echo
    echo "All processes are visible in tiled panes. Use your mouse or Ctrl-b arrow"
    echo "keys only when you need to focus a pane; detach (leave running) with Ctrl-b d."
    echo
    if [[ -n "${TMUX:-}" ]]; then
        exec tmux switch-client -t "$TMUX_SESSION"
    fi
    exec tmux attach-session -t "$TMUX_SESSION"
}

case "$MODE" in
    cli)
        ensure_project_venv
        ensure_local_docker_services
        run_cli
        ;;
    full)
        ensure_project_venv
        cleanup_stale_tmux_session
        ensure_local_docker_services
        launch_terminal "Celery" "$CMD_CELERY"
        launch_terminal "Latice CLI" "$CMD_FULL_CLI"
        echo "Started Docker Redis/Ollama plus Celery and main.py in tiled panes."
        attach_tmux_session
        ;;
    dev)
        ensure_project_venv
        cleanup_stale_tmux_session
        ensure_local_docker_services
        launch_terminal "Celery" "$CMD_CELERY"
        launch_terminal "Latice CLI" "$CMD_DEV_CLI"
        echo "Started Docker Redis/Ollama plus Celery and main.py in tiled panes."
        echo "CLI Ollama URL: $LOCAL_OLLAMA_URL"
        attach_tmux_session
        ;;
    celery-only)
        ensure_project_venv
        ensure_local_docker_services
        exec env REDIS_URL="$RUNTIME_REDIS_URL" "$CELERY_BIN" -A app.celery worker --loglevel=info
        ;;
    ollama-only)
        ensure_local_docker_services
        echo "Ollama is available at $LOCAL_OLLAMA_URL with model '$OLLAMA_NAME'."
        ;;
    help|--help|-h)
        print_usage
        ;;
    *)
        echo "Unknown mode: $MODE" >&2
        print_usage >&2
        exit 1
        ;;
esac
