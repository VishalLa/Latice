#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

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

quote_args() {
    local arg
    for arg in "$@"; do
        printf '%q ' "$arg"
    done
}

CLI_ARGS="$(quote_args "${EXTRA_ARGS[@]}")"
CMD_CLI="cd $(printf '%q' "$PROJECT_DIR") && OLLAMA_URL=$(printf '%q' "$OLLAMA_URL") exec python3 main.py ${CLI_ARGS}"
CMD_FULL_CLI="cd $(printf '%q' "$PROJECT_DIR") && OLLAMA_URL=$(printf '%q' "$LOCAL_OLLAMA_URL") exec python3 main.py ${CLI_ARGS}"
CMD_CELERY="cd $(printf '%q' "$PROJECT_DIR") && exec celery -A app.celery worker --loglevel=info"
CMD_OLLAMA="cd $(printf '%q' "$PROJECT_DIR") && exec ollama serve"

print_usage() {
    cat <<'EOF'
Usage: ./start.sh [MODE] [main.py args...]

Modes:
  cli           Run main.py in the current terminal (default)
  full          Run Ollama, Celery, and main.py in separate terminals
  dev           Run Celery and main.py in separate terminals
  celery-only   Run only the Celery worker in the current terminal
  ollama-only   Run only the Ollama server in the current terminal

Environment:
    OLLAMA_URL    Ollama URL used by main.py (read from .env or .env.local)

Examples:
  ./start.sh full
  OLLAMA_URL=http://192.168.1.50:11434 ./start.sh dev
  ./start.sh cli --help
EOF
}

run_cli() {
    local args=(python3 main.py)
    if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
        args+=("${EXTRA_ARGS[@]}")
    fi
    exec env OLLAMA_URL="$OLLAMA_URL" "${args[@]}"
}

launch_terminal() {
    local title="$1"
    local command="$2"

    if command -v gnome-terminal &>/dev/null; then
        gnome-terminal --title="$title" -- bash -lc "$command" &
    elif command -v konsole &>/dev/null; then
        konsole --new-tab -e bash -lc "$command" &
    elif command -v xterm &>/dev/null; then
        xterm -T "$title" -e bash -lc "$command" &
    elif command -v tmux &>/dev/null; then
        if ! tmux has-session -t latice 2>/dev/null; then
            tmux new-session -d -s latice "$command"
        else
            tmux split-window -t latice "$command"
        fi
    else
        echo "No supported terminal emulator found for '$title'." >&2
        return 1
    fi
}

case "$MODE" in
    cli)
        run_cli
        ;;
    full)
        launch_terminal "Ollama" "$CMD_OLLAMA"
        launch_terminal "Celery" "$CMD_CELERY"
        launch_terminal "Latice CLI" "$CMD_FULL_CLI"
        echo "Started Ollama, Celery, and main.py in separate terminals."
        ;;
    dev)
        launch_terminal "Celery" "$CMD_CELERY"
        launch_terminal "Latice CLI" "$CMD_CLI"
        echo "Started Celery and main.py in separate terminals."
        echo "CLI Ollama URL: $OLLAMA_URL"
        ;;
    celery-only)
        exec celery -A app.celery worker --loglevel=info
        ;;
    ollama-only)
        exec ollama serve
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
