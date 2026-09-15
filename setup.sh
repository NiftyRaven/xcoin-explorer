#!/usr/bin/env bash
# One-click prep + start for XFER Explorer on Linux.
# No secrets: never writes rpcuser/rpcpassword, cookies, seeds, or keys.
# If X Coin Wallet is already on this PC, use it. If not, download the
# latest official tarball from NiftyRaven/x-coin Releases.
set -euo pipefail
cd "$(dirname "$0")"

FALLBACK_VERSION="1.0.14"
FALLBACK_URL="https://github.com/NiftyRaven/x-coin/releases/download/v${FALLBACK_VERSION}/X-Coin-${FALLBACK_VERSION}-Linux-x86_64.tar.gz"
WALLET_HOME="${XCOIN_WALLET_HOME:-$HOME/.local/share/XCoin-Wallet}"
WALLET_VERSION="$FALLBACK_VERSION"
WALLET_URL="$FALLBACK_URL"

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

resolve_latest_wallet() {
  local json pair ver url
  json="$(curl -fsSL -H 'User-Agent: XFER-Explorer-setup' \
    https://api.github.com/repos/NiftyRaven/x-coin/releases/latest 2>/dev/null || true)"
  if [[ -z "$json" ]] && command -v wget >/dev/null 2>&1; then
    json="$(wget -qO- --header='User-Agent: XFER-Explorer-setup' \
      https://api.github.com/repos/NiftyRaven/x-coin/releases/latest 2>/dev/null || true)"
  fi
  pair="$(printf '%s' "$json" | python3 -c '
import json, sys
try:
    rel = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
for a in rel.get("assets") or []:
    name = a.get("name") or ""
    if name.startswith("X-Coin-") and name.endswith("-Linux-x86_64.tar.gz"):
        tag = (rel.get("tag_name") or "").lstrip("v")
        print(tag + " " + (a.get("browser_download_url") or ""))
        break
' 2>/dev/null || true)"
  ver="${pair%% *}"
  url="${pair#* }"
  if [[ -n "$ver" && -n "$url" && "$url" == https://* ]]; then
    WALLET_VERSION="$ver"
    WALLET_URL="$url"
    echo "Latest official wallet on GitHub: ${WALLET_VERSION}"
    return 0
  fi
  WALLET_VERSION="$FALLBACK_VERSION"
  WALLET_URL="$FALLBACK_URL"
  echo "Could not query GitHub Releases; using ${WALLET_VERSION}."
}

find_wallet() {
  if [[ -n "${XCOIN_WALLET:-}" && -x "$XCOIN_WALLET" ]]; then
    printf '%s\n' "$XCOIN_WALLET"
    return 0
  fi
  local pid exe
  pid="$(pgrep -n -x xcoin-qt 2>/dev/null || true)"
  if [[ -n "$pid" && -r "/proc/$pid/exe" ]]; then
    exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
    if [[ -n "$exe" && -x "$exe" ]]; then
      printf '%s\n' "$exe"
      return 0
    fi
  fi
  local found
  found="$(find "$WALLET_HOME" "$HOME/Downloads" "$HOME/Desktop" "$PWD" \
    -maxdepth 5 -type f -name 'X Coin Wallet' 2>/dev/null \
    | grep -v Practice | head -n 1 || true)"
  if [[ -n "$found" && -x "$found" ]]; then
    printf '%s\n' "$found"
    return 0
  fi
  return 1
}

install_wallet() {
  resolve_latest_wallet
  echo "No wallet found. Downloading official X Coin ${WALLET_VERSION} (no keys in this repo)..."
  local dest="$WALLET_HOME/${WALLET_VERSION}"
  mkdir -p "$dest"
  local tar="$dest/X-Coin-${WALLET_VERSION}-Linux-x86_64.tar.gz"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$WALLET_URL" -o "$tar"
  else
    wget -q "$WALLET_URL" -O "$tar"
  fi
  tar -xzf "$tar" -C "$dest"
  rm -f "$tar"
  find "$dest" -maxdepth 5 -type f -name 'X Coin Wallet' | grep -v Practice | head -n 1
}

wallet_running() {
  pgrep -x xcoin-qt >/dev/null 2>&1 || pgrep -x xcoind >/dev/null 2>&1 || pgrep -f '[X] Coin Wallet' >/dev/null 2>&1
}

rpc_ready() {
  local cookie="$HOME/.xcoin/.cookie"
  [[ -f "$cookie" ]] && return 0
  if command -v ss >/dev/null 2>&1 && ss -lnt | grep -q ':38442'; then
    return 0
  fi
  return 1
}

wait_rpc() {
  echo "Waiting for wallet RPC (port 38442 or ~/.xcoin/.cookie)..."
  local i
  for i in $(seq 1 60); do
    if rpc_ready; then
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

step "X Coin wallet"
WALLET="$(find_wallet || true)"
if [[ -n "$WALLET" ]]; then
  echo "Found installed wallet."
else
  WALLET="$(install_wallet)"
fi
if [[ -z "$WALLET" || ! -x "$WALLET" ]]; then
  echo "Could not find or install X Coin Wallet. Download the latest from"
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
  if rpc_ready; then
    echo "Wallet already running with RPC. Leave it open."
  else
    echo "Wallet is open but RPC is not on 38442."
    echo "Fully quit it, then run ./setup.sh again so server=1 is read."
    exit 0
  fi
else
  nohup "$WALLET" >/dev/null 2>&1 &
  echo "Started X Coin Wallet. Finish any first-run 12-word screen in that window."
  if ! wait_rpc; then
    echo "RPC is not up yet. When the wallet has finished loading, run: ./start.sh"
    echo "If it stays offline, see docs/TROUBLESHOOTING.md"
    exit 0
  fi
fi

chmod +x ./start.sh
exec ./start.sh
