#!/data/data/com.termux/files/usr/bin/bash

TUNNEL_NAME="yuzu"
INTERVAL_SECONDS="${1:-3600}"
CHECK_INTERVAL_SECONDS=10
METRICS_PORT=20241

# お問い合わせに対応できる「オーナー(管理者)」のメールアドレス。
# 複数人にしたい場合はカンマ区切りで並べる(例: "a@example.com,b@example.com")。
# ここで指定した値は、この後 start_server() が python3 server.py を起動するたびに
# 自動的に引き継がれる。
export YTDLP_API_OWNER_EMAILS="yuzu3daze@gmail.com"

LOG_DIR="$HOME/.cloudflared_logs"
mkdir -p "$LOG_DIR"
CLOUDFLARED_LOG="$LOG_DIR/current.log"
SERVER_LOG="$LOG_DIR/server.log"
RESTART_FLAG="$LOG_DIR/restart_flag"
TAIL_PID_FILE="$LOG_DIR/tail.pid"

CLOUDFLARED_PID=""
SERVER_PID=""
LOG_WATCHER_PID=""
TAIL_PID=""

log() {
  echo "[$(date '+%H:%M:%S')] $1"
}

# cloudflaredが完全に諦めて終了する時に必ず出てくる文言。
# 個々の接続の瞬断("network is unreachable"や"Connection terminated"単体)は
# cloudflared自身が自動で再接続してくれるので、ここでは反応しない
# (反応してしまうと、自己修復できる瞬断のたびに毎回全体を再起動することになり、
# かえって不安定になる)。あくまで「もう二度と繋がらない」ことが確定した時だけ拾う。
FATAL_LOG_PATTERNS='Initiating shutdown|Tunnel server stopped|panic:|Failed to create new metrics|Fatal error'

# tail -F | while ... done & という書き方だと、$! で取れるPIDはwhileループ側だけで、
# tail本体のプロセスは掴めないまま残り続けてしまう(再起動するたびに増え続けてしまう
# 原因になっていた)。tailとwhileループのPIDを両方きちんと記録しておく。
start_log_watcher() {
  rm -f "$RESTART_FLAG" "$TAIL_PID_FILE"
  {
    tail -F -n0 "$CLOUDFLARED_LOG" 2>/dev/null &
    echo $! > "$TAIL_PID_FILE"
    wait
  } | while IFS= read -r line; do
    if echo "$line" | grep -qE "$FATAL_LOG_PATTERNS"; then
      echo "$(date '+%H:%M:%S') $line" >> "$LOG_DIR/fatal_events.log"
      touch "$RESTART_FLAG"
    fi
  done &
  LOG_WATCHER_PID=$!
  sleep 0.2
  TAIL_PID=$(cat "$TAIL_PID_FILE" 2>/dev/null)
}

stop_log_watcher() {
  kill "$LOG_WATCHER_PID" 2>/dev/null
  kill "$TAIL_PID" 2>/dev/null
  LOG_WATCHER_PID=""
  TAIL_PID=""
}

start_cloudflared() {
  log "起動: cloudflared tunnel run $TUNNEL_NAME (IPv4固定)"
  : > "$CLOUDFLARED_LOG"
  cloudflared tunnel --edge-ip-version 4 run "$TUNNEL_NAME" >> "$CLOUDFLARED_LOG" 2>&1 &
  CLOUDFLARED_PID=$!
  start_log_watcher
}

restart_cloudflared() {
  kill "$CLOUDFLARED_PID" 2>/dev/null
  wait "$CLOUDFLARED_PID" 2>/dev/null
  stop_log_watcher
  start_cloudflared
}

start_server() {
  log "起動: python3 server.py"
  SERVER_PID_FILE="$LOG_DIR/server.pid"
  rm -f "$SERVER_PID_FILE"
  {
    python3 -u server.py 2>&1 &
    echo $! > "$SERVER_PID_FILE"
    wait
  } | tee -a "$SERVER_LOG" &
  SERVER_TEE_PID=$!
  sleep 0.3
  SERVER_PID=$(cat "$SERVER_PID_FILE" 2>/dev/null)
}

start_services() {
  start_cloudflared
  start_server
}

stop_services() {
  log "停止中..."
  stop_log_watcher
  kill "$CLOUDFLARED_PID" 2>/dev/null
  kill "$SERVER_PID" 2>/dev/null
  kill "$SERVER_TEE_PID" 2>/dev/null
  wait "$CLOUDFLARED_PID" 2>/dev/null
  wait "$SERVER_PID" 2>/dev/null
}

cleanup_and_exit() {
  stop_services
  log "終了します"
  exit 0
}

trap cleanup_and_exit INT TERM

is_tunnel_actually_ready() {
  local response
  response=$(curl -s --max-time 5 "http://127.0.0.1:${METRICS_PORT}/ready" 2>/dev/null)
  if [ -z "$response" ]; then
    return 1
  fi
  local ready_connections
  ready_connections=$(echo "$response" | grep -o '"readyConnections":[0-9]*' | grep -o '[0-9]*$')
  if [ -z "$ready_connections" ] || [ "$ready_connections" -eq 0 ]; then
    return 1
  fi
  return 0
}

# Wi-Fi自体が切れている間はcloudflaredを何回再起動しても繋がらない。
# それを知らずに10秒おきに再起動を連発すると、無駄にCPU・メモリを使い続けて
# 端末が重くなる一方だったため、失敗が連続するたびに待ち時間を伸ばす
# (10秒->20秒->40秒->最大120秒)。1回でも接続に成功したらリセットされる。
BACKOFF_SECONDS=10
MAX_BACKOFF_SECONDS=120

apply_backoff() {
  log "接続に失敗し続けているため、次の再起動まで${BACKOFF_SECONDS}秒待ちます"
  sleep "$BACKOFF_SECONDS"
  BACKOFF_SECONDS=$((BACKOFF_SECONDS * 2))
  if [ "$BACKOFF_SECONDS" -gt "$MAX_BACKOFF_SECONDS" ]; then
    BACKOFF_SECONDS=$MAX_BACKOFF_SECONDS
  fi
}

reset_backoff() {
  BACKOFF_SECONDS=10
}

# 3層で監視する:
#   1. プロセス自体が死んでいないか(kill -0)
#   2. ログに致命的なエラー文言が出ていないか(tail -Fでリアルタイム監視、即座に反応)
#   3. 実際にCloudflareへ繋がっているか(/ready、死んだふり状態を検知)
# 定期的な全体再起動(INTERVAL_SECONDS間隔)はこれとは別に維持する。
watch_until_interval() {
  local elapsed=0
  local unready_count=0
  while [ "$elapsed" -lt "$INTERVAL_SECONDS" ]; do
    sleep "$CHECK_INTERVAL_SECONDS"
    elapsed=$((elapsed + CHECK_INTERVAL_SECONDS))

    if ! kill -0 "$CLOUDFLARED_PID" 2>/dev/null; then
      log "cloudflaredが予期せず終了していました"
      stop_log_watcher
      apply_backoff
      start_cloudflared
      unready_count=0
      continue
    fi

    if [ -f "$RESTART_FLAG" ]; then
      log "ログに致命的なエラーを検知しました"
      apply_backoff
      restart_cloudflared
      unready_count=0
      continue
    fi

    if is_tunnel_actually_ready; then
      unready_count=0
      reset_backoff
    else
      unready_count=$((unready_count + 1))
      log "cloudflaredの接続状態が確認できません(${unready_count}回目)"
      # 起動直後の一時的な未接続と区別するため、3回連続(既定で約30秒)
      # 未接続が続いた場合だけ「死んだふり」と判断して再起動する。
      if [ "$unready_count" -ge 3 ]; then
        log "cloudflaredが死んだふり状態と判断しました"
        apply_backoff
        restart_cloudflared
        unready_count=0
      fi
    fi

    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      log "server.pyが予期せず終了していました。即座に再起動します"
      start_server
    fi
  done
}

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock
  log "wake-lockを取得しました(端末がスリープしにくくなります)"
fi

log "自動再起動スクリプト開始(定期再起動: ${INTERVAL_SECONDS}秒ごと、生死/ログ/接続確認: ${CHECK_INTERVAL_SECONDS}秒ごと)"
log "cloudflaredのログは ${CLOUDFLARED_LOG} に出力されます"

while true; do
  start_services
  watch_until_interval
  log "定期再起動のタイミングです"
  stop_services
  sleep 2
done
