#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Find every running server instance: the :8000 LISTEN socket owner plus any
# `python -m server.main` process. A survivor sharing the audio device leaves
# the next server's capture session permanently degraded, so none may remain.
old_pids() {
    { lsof -iTCP:8000 -sTCP:LISTEN -t 2>/dev/null || true;
      pgrep -f "server\.main" 2>/dev/null || true; } | sort -u
}

OLD_PIDS=$(old_pids)
if [ -n "${OLD_PIDS:-}" ]; then
    echo "Killing existing server (PID: $(echo $OLD_PIDS | tr '\n' ' '))..."
    echo "$OLD_PIDS" | xargs kill 2>/dev/null || true
    for _ in $(seq 1 20); do
        OLD_PIDS=$(old_pids)
        [ -z "${OLD_PIDS:-}" ] && break
        sleep 0.5
    done
    if [ -n "${OLD_PIDS:-}" ]; then
        echo "Force killing..."
        echo "$OLD_PIDS" | xargs kill -9 2>/dev/null || true
        for _ in $(seq 1 10); do
            OLD_PIDS=$(old_pids)
            [ -z "${OLD_PIDS:-}" ] && break
            sleep 0.5
        done
    fi
fi

# Give CoreAudio time to fully release the USB audio device after the
# previous client died; measured on the FT-710 UAC device: reopening within
# ~2 s of a SIGKILLed holder yields a degraded stream (starved or
# undecodable audio that never recovers for the process lifetime).
sleep 8

echo "Starting server..."
nohup venv/bin/python -m server.main > /tmp/mrrc-ft8.out.log 2> /tmp/mrrc-ft8.err.log &
NEW_PID=$!
echo "Started (PID: $NEW_PID)"

for _ in $(seq 1 20); do
    if lsof -iTCP:8000 -sTCP:LISTEN -t 2>/dev/null | grep -q "^$NEW_PID$"; then
        echo "Server running on http://127.0.0.1:8000"
        exit 0
    fi
    kill -0 $NEW_PID 2>/dev/null || break
    sleep 0.5
done
echo "FAILED - check logs:"
tail -20 /tmp/mrrc-ft8.err.log
exit 1
