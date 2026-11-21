#!/usr/bin/env bash
# imsearch セットアップ（Ubuntu 22.04 / 24.04、メモリ 2GB 以上・CPU のみを想定）
#
#   ./scripts/setup-ubuntu.sh [--swap|--no-swap] [--timer] [--web] [--profile 2g|4g|api] [--no-ollama] [--no-models]
#
#   --swap       swap が無い（1GB 未満）場合に 2GB の /swapfile を作る（メモリ 3GB 未満では既定で作る）
#   --no-swap    メモリが少なくても swap を作らない
#   --profile    メモリ設定。既定は搭載メモリから自動（3.5GB 未満 → 2g、以上 → 4g）
#                api: LLM・埋め込みをさくらの AI Engine で行う（Ollama を入れない。メモリ 1GB 可）
#   --timer      週 1 回の索引更新（systemd timer）を登録する
#   --web        Web アプリ（imsearch serve、127.0.0.1:8000）を systemd サービスとして常駐させる
#   --no-ollama  Ollama のインストール・設定をしない（別サーバーの LLM を使う場合）
#   --no-models  モデルを pull しない
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WANT_SWAP=auto; WANT_TIMER=0; WANT_WEB=0; WANT_OLLAMA=1; WANT_MODELS=1; PROFILE=auto
while [ $# -gt 0 ]; do
  a="$1"; shift
  case "$a" in
    --swap) WANT_SWAP=1 ;;
    --no-swap) WANT_SWAP=0 ;;
    --profile) PROFILE="${1:-}"; shift || true ;;
    --profile=*) PROFILE="${a#--profile=}" ;;
    --timer) WANT_TIMER=1 ;;
    --web) WANT_WEB=1 ;;
    --no-ollama) WANT_OLLAMA=0 ;;
    --no-models) WANT_MODELS=0 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 1 ;;
  esac
done

CHAT_MODEL="${IMSEARCH_CHAT_MODEL:-qwen3:1.7b}"
EMBED_MODEL="${IMSEARCH_EMBED_MODEL:-qwen3-embedding:0.6b}"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
# in a container / LXC-type VPS the cgroup limit may be lower than what /proc/meminfo shows
CG_MAX=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo max)
if [ "$CG_MAX" != max ] && [ "$((CG_MAX / 1048576))" -lt "$MEM_MB" ]; then MEM_MB=$((CG_MAX / 1048576)); fi
if [ "$PROFILE" = auto ]; then
  if [ "$MEM_MB" -ge 3500 ]; then PROFILE=4g; else PROFILE=2g; fi
  if [ "$MEM_MB" -lt 1800 ]; then
    echo "メモリ ${MEM_MB} MiB ではローカルの LLM は動きません。--profile api（さくらの AI Engine）を指定してください。" >&2
    exit 1
  fi
fi
case "$PROFILE" in 2g|4g|api) ;; *) echo "--profile は 2g / 4g / api です: $PROFILE" >&2; exit 1 ;; esac
if [ "$PROFILE" = api ]; then WANT_OLLAMA=0; WANT_MODELS=0; fi
if [ "$WANT_SWAP" = auto ]; then
  if [ "$PROFILE" != api ] && [ "$MEM_MB" -lt 3000 ]; then WANT_SWAP=1; else WANT_SWAP=0; fi
fi
echo "メモリ ${MEM_MB} MiB → 設定 ${PROFILE}$( [ "$WANT_SWAP" = 1 ] && echo '、swap を確認' )"

step "パッケージ（python3-venv, git, curl）"
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq python3 python3-venv python3-pip git curl ca-certificates >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3, 10), sys.version' \
  || { echo "Python 3.10 以上が必要です" >&2; exit 1; }

if [ "$WANT_SWAP" = 1 ]; then
  step "swap"
  SWAP_MB=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
  if [ "$SWAP_MB" -lt 1024 ] && [ ! -e /swapfile ]; then
    $SUDO fallocate -l 2G /swapfile || $SUDO dd if=/dev/zero of=/swapfile bs=1M count=2048
    $SUDO chmod 600 /swapfile
    $SUDO mkswap /swapfile >/dev/null
    if $SUDO swapon /swapfile 2>/dev/null; then
      grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null
      echo 'vm.swappiness=10' | $SUDO tee /etc/sysctl.d/90-imsearch-swap.conf >/dev/null
      $SUDO sysctl -q -p /etc/sysctl.d/90-imsearch-swap.conf || true
      echo "2GB の /swapfile を作成しました"
    else
      # containers / LXC-type VPS cannot enable swap themselves
      $SUDO rm -f /swapfile
      echo "WARN: swap を有効にできません（コンテナ型の環境など）。swap なしで続けます。" >&2
    fi
  else
    echo "swap は設定済みです（${SWAP_MB} MiB）"
  fi
fi

if [ "$WANT_OLLAMA" = 1 ]; then
  step "Ollama"
  if ! command -v ollama >/dev/null; then
    echo "Ollama を公式インストーラーで導入します（https://ollama.com/install.sh）"
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  if [ -d /etc/systemd/system ] && command -v systemctl >/dev/null; then
    $SUDO mkdir -p /etc/systemd/system/ollama.service.d
    if [ "$PROFILE" = 4g ]; then
      # 4GB: keep the chat and embedding models loaded together, higher memory cap
      sed -e 's/OLLAMA_MAX_LOADED_MODELS=1/OLLAMA_MAX_LOADED_MODELS=2/' \
          -e 's/^MemoryHigh=.*/MemoryHigh=3000M/' -e 's/^MemoryMax=.*/MemoryMax=3500M/' \
          "$APP_DIR/deploy/ollama-override.conf" \
        | $SUDO tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null
    else
      $SUDO cp "$APP_DIR/deploy/ollama-override.conf" /etc/systemd/system/ollama.service.d/override.conf
    fi
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now ollama >/dev/null 2>&1 || true
    $SUDO systemctl restart ollama
  fi
  for i in $(seq 1 30); do curl -fs http://127.0.0.1:11434/api/version >/dev/null && break; sleep 1; done
  if [ "$WANT_MODELS" = 1 ]; then
    ollama pull "$EMBED_MODEL"
    ollama pull "$CHAT_MODEL"
  fi
fi

step "Python 仮想環境"
cd "$APP_DIR"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e .
if [ ! -f imsearch.env ]; then
  if [ "$PROFILE" = 4g ]; then
    # enable the "メモリ 4GB 以上のとき" block of the example
    awk '/^# --- / {on=0} /^# --- メモリ 4GB 以上のとき/ {on=1} on && /^#IMSEARCH_/ {sub(/^#/, "")} {print}' \
      imsearch.env.example > imsearch.env
  elif [ "$PROFILE" = api ]; then
    # enable the "さくらの AI Engine" block; the API key is filled in by the user
    awk '/^# --- / {on=0} /^# --- LLM をさくらのクラウド/ {on=1} on && /^#IMSEARCH_/ {sub(/^#/, "")} {print}' \
      imsearch.env.example > imsearch.env
    chmod 600 imsearch.env
    echo "imsearch.env の IMSEARCH_LLM_API_KEY= に AI Engine のアカウントトークン（<UUID>:<シークレット>）を書いてください"
  else
    cp imsearch.env.example imsearch.env
  fi
  echo "imsearch.env を作成しました（設定 ${PROFILE}）"
fi

if [ "$WANT_TIMER" = 1 ]; then
  step "週 1 回の索引更新（systemd timer）"
  RUN_USER="${SUDO_USER:-$(id -un)}"
  sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__USER__#$RUN_USER#g" deploy/imsearch-ingest.service \
    | $SUDO tee /etc/systemd/system/imsearch-ingest.service >/dev/null
  $SUDO cp deploy/imsearch-ingest.timer /etc/systemd/system/
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now imsearch-ingest.timer
fi

if [ "$WANT_WEB" = 1 ]; then
  step "Web アプリ（systemd service）"
  RUN_USER="${SUDO_USER:-$(id -un)}"
  sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__USER__#$RUN_USER#g" deploy/imsearch-web.service \
    | $SUDO tee /etc/systemd/system/imsearch-web.service >/dev/null
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now imsearch-web.service
  $SUDO systemctl restart imsearch-web.service
  echo "http://127.0.0.1:8000/ で起動しました（外部公開は deploy/nginx-imsearch.conf か deploy/apache-imsearch.conf を参照）"
fi

step "診断"
if [ "$PROFILE" = api ]; then INGEST_NOTE="埋め込みも API で行う。数分〜十数分"; else INGEST_NOTE="CPU のみだと数時間。README の「索引を別マシンで作る」も参照"; fi
.venv/bin/imsearch doctor || true
cat <<MSG

次の手順:
  .venv/bin/imsearch ingest            # 索引の作成（$INGEST_NOTE）
  .venv/bin/imsearch search "repeat-control"
  .venv/bin/imsearch ask "検索ボックスで部分一致検索をしたい"
  .venv/bin/imsearch serve             # Web アプリ（--web を付けて実行した場合は常駐済み）
MSG
