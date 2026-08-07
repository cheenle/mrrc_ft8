#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# ═══ rigctld 配置（SDD AD-008: rigctld 是 CAT 串口唯一 owner，本脚本负责拉起）═══
# FT-710 实测参数; 换电台/串口时覆盖环境变量即可:
#   MRRC_FT8_RIG_MODEL / MRRC_FT8_RIG_DEVICE / MRRC_FT8_RIG_BAUD / MRRC_FT8_RIGCTLD_PORT
# rigctld 监听端口必须与 server 的 MRRC_FT8_RIGCTLD（默认 127.0.0.1:4532）一致。
RIG_MODEL="${MRRC_FT8_RIG_MODEL:-1049}"
RIG_DEVICE="${MRRC_FT8_RIG_DEVICE:-/dev/cu.usbserial-0121DB3A0}"
RIG_BAUD="${MRRC_FT8_RIG_BAUD:-38400}"
RIGCTLD_PORT="${MRRC_FT8_RIGCTLD_PORT:-4532}"
RIGCTLD_LOG="/tmp/mrrc-rigctld.err.log"
RIG_START_ATTEMPTS=3

# Find every running server instance: the :8000 LISTEN socket owner plus any
# `python -m server.main` process. A survivor sharing the audio device leaves
# the next server's capture session permanently degraded, so none may remain.
old_server_pids() {
    { lsof -iTCP:8000 -sTCP:LISTEN -t 2>/dev/null || true;
      pgrep -f "server\.main" 2>/dev/null || true; } | sort -u
}

# Find every running rigctld: exact process-name match plus the loopback
# listener.  A stale rigctld holds a serial fd that goes dead when the radio
# power-cycles (macOS "Device not configured"), so it is always restarted.
old_rigctld_pids() {
    { pgrep -x rigctld 2>/dev/null || true;
      lsof -iTCP:"$RIGCTLD_PORT" -sTCP:LISTEN -t 2>/dev/null || true; } | sort -u
}

kill_pids() {
    local name="$1" qfn="$2" pids
    pids="$($qfn)"
    if [ -z "$pids" ]; then
        echo "$name 未在运行"
        return 0
    fi
    echo "Killing $name (PID: $(echo "$pids" | tr '\n' ' '))..."
    echo "$pids" | xargs kill 2>/dev/null || true
    for _ in $(seq 1 20); do
        pids="$($qfn)"
        [ -z "$pids" ] && break
        sleep 0.5
    done
    if [ -n "$pids" ]; then
        echo "Force killing $name..."
        echo "$pids" | xargs kill -9 2>/dev/null || true
        for _ in $(seq 1 10); do
            pids="$($qfn)"
            [ -z "$pids" ] && break
            sleep 0.5
        done
    fi
    if [ -n "$pids" ]; then
        echo "✗ 无法停止 $name，请手动检查" >&2
        return 1
    fi
    echo "✓ $name 已停止"
}

# ─── 1. 停止残留进程 ─────────────────────────────────────────────────
OLD_SERVER=$(old_server_pids)
[ -n "$OLD_SERVER" ] && kill_pids "existing server" old_server_pids

# rigctld 也要重启: radio 电源循环后其串口 fd 失效, 必须重新打开设备
OLD_RIGCTLD=$(old_rigctld_pids)
[ -n "$OLD_RIGCTLD" ] && kill_pids "existing rigctld" old_rigctld_pids

# Give CoreAudio time to fully release the USB audio device after the
# previous client died; measured on the FT-710 UAC device: reopening within
# ~2 s of a SIGKILLed holder yields a degraded stream (starved or
# undecodable audio that never recovers for the process lifetime).
sleep 8

# ─── 2. 启动 rigctld 并等待就绪 ──────────────────────────────────────
start_rigctld() {
    echo "Starting rigctld ($RIG_DEVICE @ $RIG_BAUD, model $RIG_MODEL, port $RIGCTLD_PORT)..."
    nohup rigctld -m "$RIG_MODEL" -r "$RIG_DEVICE" -s "$RIG_BAUD" \
        -T 127.0.0.1 -t "$RIGCTLD_PORT" -vvv >> "$RIGCTLD_LOG" 2>&1 &
    local rig_pid=$!
    for _ in $(seq 1 20); do
        if lsof -iTCP:"$RIGCTLD_PORT" -sTCP:LISTEN -t 2>/dev/null | grep -q .; then
            echo "rigctld ready (PID: $rig_pid)"
            return 0
        fi
        kill -0 "$rig_pid" 2>/dev/null || break   # 进程已退出 → 启动失败
        sleep 0.5
    done
    echo "✗ rigctld 启动失败" >&2
    tail -20 "$RIGCTLD_LOG" >&2 || true
    return 1
}

RIG_UP=false
for _ in $(seq 1 "$RIG_START_ATTEMPTS"); do
    if start_rigctld; then RIG_UP=true; break; fi
    echo "  重试（串口可能仍在枚举）..."
    sleep 2
done
if ! $RIG_UP; then
    echo "✗ rigctld 未能启动 — server 无法控制电台（CAT 将显示红）" >&2
    exit 1
fi

# ─── 3. 启动 server ──────────────────────────────────────────────────
echo "Starting server..."
MRRC_FT8_LOG_LEVEL="${MRRC_FT8_LOG_LEVEL:-DEBUG}" nohup venv/bin/python -m server.main > /tmp/mrrc-ft8.out.log 2> /tmp/mrrc-ft8.err.log &
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
