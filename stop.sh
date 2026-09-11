#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

stop_processes() {
    local label="$1"
    local pattern="$2"
    local stopped=0
    local pid

    while read -r pid; do
        [[ -z "$pid" || "$pid" == "$$" ]] && continue
        if kill -TERM "$pid" 2>/dev/null; then
            echo "Stopped $label process (PID $pid)."
            stopped=1
        fi
    done < <(pgrep -f "$pattern" || true)

    return "$stopped"
}

if command -v tmux &>/dev/null && tmux has-session -t latice 2>/dev/null; then
    tmux kill-session -t latice
    echo "Stopped tmux session: latice."
fi

stopped_any=0
stop_processes "CLI" "${PROJECT_DIR}/main\.py" || stopped_any=1
stop_processes "Celery" "celery -A app\.celery worker" || stopped_any=1
stop_processes "Ollama" "ollama serve" || stopped_any=1

if [[ "$stopped_any" -eq 0 ]]; then
    echo "No Latice application processes were running."
else
    echo "Latice application processes stopped."
fi
