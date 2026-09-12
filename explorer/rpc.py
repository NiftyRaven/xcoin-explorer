"""JSON-RPC client for xcoind / xcoin-qt."""

from __future__ import annotations

import itertools
import os
import socket
from pathlib import Path
from typing import Any

import httpx

from explorer.config import Settings, cookie_candidates, read_cookie

PROBE_PORTS = (38442, 28442, 48442)
PORT_LABEL = {
    38442: "mainnet",
    28442: "practice/regtest",
    48442: "testnet",
}


class RpcError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class XCoinRPC:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: httpx.Client | None = None
        self._url = settings.rpc_url()
        self._ids = itertools.count(1)
        self.connected = False
        self.last_error = "not connected"
        self.network = ""
        self.rpc_port = settings.rpc_port
        self.auth_needed = False

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _ensure_client(self, url: str, auth: tuple[str, str] | None) -> httpx.Client:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = httpx.Client(base_url=url, auth=auth, timeout=30.0)
        return self._client

    def _auth(self) -> tuple[str, str] | None:
        # Re-read cookie every attempt — wallet restart rewrites it.
        if self.settings.rpc_user:
            return self.settings.rpc_user, self.settings.rpc_password
        if self.settings.cookie_file:
            got = read_cookie(Path(os.path.expandvars(self.settings.cookie_file)))
            if got:
                return got
        for path in cookie_candidates():
            got = read_cookie(path)
            if got:
                self.settings.cookie_file = str(path)
                return got
        return None

    def connect(self) -> bool:
        auth = self._auth()
        ports: list[int] = []
        if self.settings.rpc_port:
            ports.append(self.settings.rpc_port)
        ports.extend([p for p in PROBE_PORTS if p not in ports])

        listening: list[int] = [p for p in ports if port_open(self.settings.rpc_host, p)]
        notes: list[str] = []

        for port in ports:
            url = self.settings.rpc_url(port)
            try:
                client = self._ensure_client(url, auth)
                payload = {"jsonrpc": "1.0", "id": "probe", "method": "getblockchaininfo", "params": []}
                r = client.post("/", json=payload)
                if r.status_code in (401, 403):
                    self.auth_needed = True
                    self.rpc_port = port
                    label = PORT_LABEL.get(port, str(port))
                    if auth is None:
                        self.last_error = (
                            f"The wallet is running on RPC port {port} ({label}) but this explorer "
                            f"could not log in (no cookie, no rpcuser in explorer.toml). "
                            f"Quit every X Coin window, start the wallet once, and confirm "
                            f"xcoin.conf contains server=1. Guide: docs/SETUP.md"
                        )
                    else:
                        self.last_error = (
                            f"RPC port {port} ({label}) rejected the username/password. "
                            f"They must match in xcoin.conf and explorer.toml. Guide: docs/SETUP.md"
                        )
                    self.connected = False
                    return False
                body = r.json()
                if body.get("error"):
                    notes.append(f"{port}: {body['error']}")
                    continue
                info = body.get("result") or {}
                self.connected = True
                self.auth_needed = False
                self.last_error = ""
                self.network = info.get("chain") or ""
                self.rpc_port = port
                self._url = url
                return True
            except Exception as e:
                notes.append(f"{url}: {e}")
                continue

        self.connected = False
        if listening:
            self.rpc_port = listening[0]
            self.last_error = (
                f"RPC port {listening[0]} is open but login failed. "
                + ("; ".join(notes) if notes else "Check .cookie or rpcuser.")
            )
        elif notes:
            self.last_error = notes[0]
        else:
            self.last_error = (
                "No X Coin wallet RPC on 38442 (main), 28442 (practice), or 48442 (testnet). "
                "Start the wallet with server=1. Guide: docs/SETUP.md"
            )
        return False

    def call(self, method: str, *params: Any) -> Any:
        if self._client is None or not self.connected:
            if not self.connect():
                raise RpcError(self.last_error)
        assert self._client is not None
        payload = {
            "jsonrpc": "1.0",
            "id": next(self._ids),
            "method": method,
            "params": list(params),
        }
        try:
            r = self._client.post("/", json=payload)
        except Exception as e:
            self.connected = False
            self.last_error = str(e)
            raise RpcError(str(e)) from e
        if r.status_code in (401, 403):
            self.connected = False
            self.last_error = "RPC authentication failed"
            raise RpcError(self.last_error, r.status_code)
        try:
            body = r.json()
        except Exception as e:
            raise RpcError(f"invalid RPC JSON: {e}") from e
        if body.get("error"):
            err = body["error"]
            if isinstance(err, dict):
                raise RpcError(str(err.get("message") or err), err.get("code"))
            raise RpcError(str(err))
        return body.get("result")

    def try_call(self, method: str, *params: Any, default: Any = None) -> Any:
        try:
            return self.call(method, *params)
        except RpcError:
            return default
