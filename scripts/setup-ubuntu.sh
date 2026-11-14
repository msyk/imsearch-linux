#!/usr/bin/env bash
# imsearch セットアップ（Ubuntu 22.04 / 24.04、メモリ 4GB・CPU のみを想定）
#
#   ./scripts/setup-ubuntu.sh [--swap] [--timer] [--no-ollama] [--no-models]
#
#   --swap       swap が無い（1GB 未満）場合に 2GB の /swapfile を作る
#   --timer      週 1 回の索引更新（systemd timer）を登録する
#   --no-ollama  Ollama のインストール・設定をしない（別サーバーの LLM を使う場合）
#   --no-models  モデルを pull しない
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WANT_SWAP=0; WANT_TIMER=0; WANT_OLLAMA=1; WANT_MODELS=1
for a in "$@"; do
  case "$a" in
    --swap) WANT_SWAP=1 ;;
    --timer) WANT_TIMER=1 ;;
    --no-ollama) WANT_OLLAMA=0 ;;
    --no-models) WANT_MODELS=0 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 1 ;;
  esac
done

CHAT_MODEL="${IMSEARCH_CHAT_MODEL:-qwen3:1.7b}"
EMBED_MODEL="${IMSEARCH_EMBED_MODEL:-qwen3-embedding:0.6b}"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

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
    $SUDO swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null
    echo 'vm.swappiness=10' | $SUDO tee /etc/sysctl.d/90-imsearch-swap.conf >/dev/null
    $SUDO sysctl -q -p /etc/sysctl.d/90-imsearch-swap.conf
    echo "2GB の /swapfile を作成しました"
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
    $SUDO cp "$APP_DIR/deploy/ollama-override.conf" /etc/systemd/system/ollama.service.d/override.conf
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
[ -f imsearch.env ] || cp imsearch.env.example imsearch.env

if [ "$WANT_TIMER" = 1 ]; then
  step "週 1 回の索引更新（systemd timer）"
  RUN_USER="${SUDO_USER:-$(id -un)}"
  sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__USER__#$RUN_USER#g" deploy/imsearch-ingest.service \
    | $SUDO tee /etc/systemd/system/imsearch-ingest.service >/dev/null
  $SUDO cp deploy/imsearch-ingest.timer /etc/systemd/system/
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now imsearch-ingest.timer
fi

step "診断"
.venv/bin/imsearch doctor || true
cat <<MSG

次の手順:
  .venv/bin/imsearch ingest            # 索引の作成（CPU のみだと数時間。README の「索引を別マシンで作る」も参照）
  .venv/bin/imsearch search "repeat-control"
  .venv/bin/imsearch ask "検索ボックスで部分一致検索をしたい"
MSG
