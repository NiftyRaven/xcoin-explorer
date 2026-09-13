#!/usr/bin/env bash
# One-click prep + start for XFER Explorer on Linux.
# No secrets: never writes rpcuser/rpcpassword, cookies, seeds, or keys.
set -euo pipefail
cd "$(dirname "$0")"

WALLET_VERSION="1.0.13"
WALLET_URL="https://github.com/NiftyRaven/x-coin/releases/download/v${WALLET_VERSION}/X-Coin-1.0.13-Linux-x86_64.tar.gz"
WALLET_HOME="${XCOIN_WALLET_HOME:-$HOME/.local/share/XCoin-Wallet/${WALLET_VERSION}}"

step() { printf '\n==> %s\n' "$1"; }

have_python() {
  command -v python3 >/dev/null 2>&1 && python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

install_python() {
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv python3-pip
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-pip python3-virtualenv
  else
    echo "Install Python 3.11+ (python3, python3-venv, python3-pip) and run ./setup.sh again."
    exit 1
  fi
}

ensure_conf() {
  local path="$1" key="$2" value="$3"
  mkdir -p "$(dirname "$path")"
  if [[ -f "$path" ]] && grep -Eq "^[[:space:]]*${key}[[:space:]]*=" "$path"; then
    return 0
  fi
  if [[ ! -f "$path" ]]; then
    cat >"$path" <<EOF
# Created by XFER Explorer setup.sh — no passwords.
server=1
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
EOF
    echo "Created $path (template, no secrets)"
    return 0
  fi
  printf '\n%s=%s\n' "$key" "$value" >>"$path"
  echo "Added $key=$value to $path"
}

find_wallet() {
  if [[ -n "${XCOIN_WALLET:-}" && -x "$XCOIN_WALLET" ]]; then
    printf '%s\n' "$XCOIN_WALLET"
    return 0
  fi
  local cand
  for cand in \
    "./X Coin Wallet" \
    "$WALLET_HOME/X Coin Wallet" \
    "$HOME/Downloads/xcoin-1.0.13-linux-x86_64/X Coin Wallet" \
    "$HOME/Downloads/X-Coin-1.0.13-Linux-x86_64/xcoin-1.0.13-linux-x86_64/X Coin Wallet"
  do
    if [[ -x "$cand" ]]; then
      printf '%s\n' "$cand"
      return 0
    fi
  done
  local found
  found="$(find "$WALLET_HOME" "$HOME/Downloads" "$PWD" -maxdepth 4 -type f -name 'X Coin Wallet' 2>/dev/null | head -n 1 || true)"
  if [[ -n "$found" && -x "$found" ]]; then
    printf '%s\n' "$found"
    return 0
  fi
  return 1
}

install_wallet() {
  echo "Downloading official X Coin ${WALLET_VERSION} (no keys in this repo)..."
  mkdir -p "$WALLET_HOME"
  local tar="$WALLET_HOME/X-Coin-${WALLET_VERSION}-Linux-x86_64.tar.gz"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$WALLET_URL" -o "$tar"
  else
    wget -q "$WALLET_URL" -O "$tar"
  fi
  tar -xzf "$tar" -C "$WALLET_HOME"
  rm -f "$tar"
  find "$WALLET_HOME" -maxdepth 4 -type f -name 'X Coin Wallet' | head -n 1
}

wallet_running() {
  pgrep -x xcoin-qt >/dev/null 2>&1 || pgrep -x xcoind >/dev/null 2>&1 || pgrep -f '[X] Coin Wallet' >/dev/null 2>&1
}

wait_rpc() {
  local cookie="$HOME/.xcoin/.cookie"
  echo "Waiting for wallet RPC (port 38442 or ~/.xcoin/.cookie)..."
  local i
  for i in $(seq 1 60); do
    if [[ -f "$cookie" ]]; then
      return 0
    fi
    if command -v ss >/dev/null 2>&1 && ss -lnt | grep -q ':38442'; then
      return 0
    fi
    sleep 2
  done
  return 1
}

echo "XFER Explorer one-click setup (Linux)"
echo "This does not spend coins and does not write RPC passwords."

step "Python"
if ! have_python; then
  install_python
fi
if ! have_python; then
  echo "Python 3.11+ is still missing."
  exit 1
fi
python3 --version

step "X Coin wallet 1.0.13+"
WALLET="$(find_wallet || true)"
if [[ -z "$WALLET" ]]; then
  WALLET="$(install_wallet)"
fi
if [[ -z "$WALLET" || ! -x "$WALLET" ]]; then
  echo "Could not find or install X Coin Wallet. Download 1.0.13 from"
  echo "https://github.com/NiftyRaven/x-coin/releases"
  exit 1
fi
chmod +x "$WALLET" || true
echo "Wallet: $WALLET"

step "Enable local RPC (server=1 only — no passwords)"
PACKAGED="$(dirname "$WALLET")/xcoin.conf"
DATA_CONF="$HOME/.xcoin/xcoin.conf"
ensure_conf "$PACKAGED" server 1
ensure_conf "$PACKAGED" rpcbind 127.0.0.1
ensure_conf "$PACKAGED" rpcallowip 127.0.0.1
ensure_conf "$DATA_CONF" server 1
ensure_conf "$DATA_CONF" rpcbind 127.0.0.1
ensure_conf "$DATA_CONF" rpcallowip 127.0.0.1

step "Start wallet if needed"
if wallet_running; then
  echo "Wallet already running. Leave it open."
else
  nohup "$WALLET" >/dev/null 2>&1 &
  echo "Started X Coin Wallet. Finish any first-run 12-word screen in that window."
fi

if ! wait_rpc; then
  echo "RPC is not up yet. When the wallet has finished loading, run: ./start.sh"
  echo "If it stays offline, see docs/TROUBLESHOOTING.md"
  exit 0
fi

chmod +x ./start.sh
exec ./start.sh
