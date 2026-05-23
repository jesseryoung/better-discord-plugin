from streamcontroller_plugin_tools import BackendBase
import json
import math
import os
import queue
import socket
import struct
import threading
import urllib.parse
import urllib.request
import uuid
from loguru import logger as log

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2

SCOPES = ["rpc", "rpc.voice.read", "identify"]


class Backend(BackendBase):
    def __init__(self):
        super().__init__()
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._pending: dict[str, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._connected = False
        self._members: list[dict] = []
        self._members_lock = threading.Lock()
        self._volumes: dict[str, int] = {}
        self._restore_volumes: dict[str, int] = {}
        self._offset = 0
        self._running = False
        self._listener_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ socket

    def _ipc_paths(self) -> list[str]:
        paths = []
        for i in range(10):
            xdg = os.environ.get("XDG_RUNTIME_DIR", "")
            if xdg:
                paths.append(os.path.join(xdg, f"discord-ipc-{i}"))
                paths.append(os.path.join(xdg, "app", "com.discordapp.Discord", f"discord-ipc-{i}"))
            paths.append(f"/tmp/discord-ipc-{i}")
        return paths

    def _open_socket(self) -> bool:
        for path in self._ipc_paths():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(path)
                self._sock = s
                return True
            except OSError:
                continue
        return False

    def _close_socket(self) -> None:
        if self._sock:
            try:
                self._send_raw(OP_CLOSE, {})
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _send_raw(self, op: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        header = struct.pack("<II", op, len(data))
        with self._send_lock:
            self._sock.sendall(header + data)

    def _recv_raw(self) -> tuple[int, dict]:
        buf = b""
        while len(buf) < 8:
            chunk = self._sock.recv(8 - len(buf))
            if not chunk:
                raise ConnectionError("Discord IPC socket closed")
            buf += chunk
        op, length = struct.unpack("<II", buf)
        data = b""
        while len(data) < length:
            chunk = self._sock.recv(length - len(data))
            if not chunk:
                raise ConnectionError("Discord IPC socket closed")
            data += chunk
        return op, json.loads(data.decode("utf-8"))

    def _send_frame(self, payload: dict, timeout: float = 10.0) -> dict:
        """Send a FRAME and wait for the matching response via nonce routing."""
        nonce = str(uuid.uuid4())
        payload["nonce"] = nonce
        q: queue.Queue = queue.Queue()
        with self._pending_lock:
            self._pending[nonce] = q
        try:
            self._send_raw(OP_FRAME, payload)
            return q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"No response for {payload.get('cmd')}")
        finally:
            with self._pending_lock:
                self._pending.pop(nonce, None)

    # ---------------------------------------------------------------- listener

    def _listener_loop(self) -> None:
        while self._running:
            try:
                _, msg = self._recv_raw()
                nonce = msg.get("nonce")
                evt = msg.get("evt")

                if nonce:
                    with self._pending_lock:
                        q = self._pending.get(nonce)
                    if q is not None:
                        q.put(msg)
                        continue

                if evt in ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE", "VOICE_STATE_DELETE"):
                    threading.Thread(target=self._refresh_members, daemon=True).start()

            except Exception as e:
                log.error(f"Discord IPC listener error: {e}")
                self._connected = False
                self._running = False
                break

    def _start_listener(self) -> None:
        self._running = True
        self._listener_thread = threading.Thread(target=self._listener_loop, daemon=True)
        self._listener_thread.start()

    # -------------------------------------------------------------------- auth

    def connect(self, client_id: str, client_secret: str, access_token: str | None) -> bool:
        if not client_id:
            log.warning("Discord client_id not configured")
            return False

        if not self._open_socket():
            log.error("Could not connect to Discord IPC socket — is Discord running?")
            return False

        # Step 1: Handshake (pre-listener, synchronous)
        self._send_raw(OP_HANDSHAKE, {"v": 1, "client_id": client_id})
        _, resp = self._recv_raw()
        if resp.get("evt") != "READY":
            log.error(f"Discord handshake failed: {resp}")
            self._close_socket()
            return False

        if not access_token:
            self._close_socket()
            return False

        # Start listener before AUTHENTICATE so the response gets routed through nonce queues
        self._start_listener()

        # Step 4: Authenticate with cached token
        try:
            resp = self._send_frame({
                "cmd": "AUTHENTICATE",
                "args": {"access_token": access_token},
            })
            if resp.get("evt") == "ERROR":
                log.warning(f"AUTHENTICATE rejected (token expired?): {resp}")
                self._running = False
                self._close_socket()
                return False
        except Exception as e:
            log.error(f"AUTHENTICATE failed: {e}")
            self._running = False
            self._close_socket()
            return False

        self._connected = True
        self._subscribe_events()
        threading.Thread(target=self._refresh_members, daemon=True).start()
        return True

    def get_fresh_token(self, client_id: str, client_secret: str) -> str | None:
        """
        Runs Steps 2–3 of the Discord OAuth flow:
          - Sends AUTHORIZE (triggers an approval modal inside Discord)
          - Waits up to 120s for the user to approve
          - POSTs the returned code to discord.com to exchange for an access_token
        Returns the access_token string, or None on failure.
        The caller is responsible for saving the token to plugin settings.
        """
        if self._running:
            log.error("Cannot get_fresh_token while listener is active")
            return None

        if not self._open_socket():
            log.error("Could not connect to Discord IPC socket for token flow")
            return None

        self._send_raw(OP_HANDSHAKE, {"v": 1, "client_id": client_id})
        _, resp = self._recv_raw()
        if resp.get("evt") != "READY":
            log.error(f"Handshake failed during token flow: {resp}")
            self._close_socket()
            return None

        # Step 2: Authorize — Discord shows an in-app approval modal
        self._send_raw(OP_FRAME, {
            "nonce": str(uuid.uuid4()),
            "cmd": "AUTHORIZE",
            "args": {"client_id": client_id, "scopes": SCOPES},
        })
        self._sock.settimeout(120)
        try:
            _, resp = self._recv_raw()
        except socket.timeout:
            log.error("AUTHORIZE timed out — user did not approve the Discord modal within 120s")
            self._close_socket()
            return None
        finally:
            self._sock.settimeout(None)

        code = (resp.get("data") or {}).get("code")
        if not code:
            log.error(f"AUTHORIZE did not return a code: {resp}")
            self._close_socket()
            return None

        self._close_socket()

        # Step 3: Exchange the authorization code for an access_token
        try:
            params = urllib.parse.urlencode({
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": code,
            }).encode()
            req = urllib.request.Request(
                "https://discord.com/api/oauth2/token",
                data=params,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                token_data = json.loads(r.read().decode())
            token = token_data.get("access_token")
            if not token:
                log.error(f"Token exchange response had no access_token: {token_data}")
            return token
        except Exception as e:
            log.error(f"Token exchange HTTP request failed: {e}")
            return None

    def is_connected(self) -> bool:
        return self._connected

    # ---------------------------------------------------------- Discord events

    def _subscribe_events(self) -> None:
        for evt in ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE", "VOICE_STATE_DELETE"):
            try:
                self._send_frame({"cmd": "SUBSCRIBE", "evt": evt, "args": {}})
            except Exception as e:
                log.error(f"Failed to subscribe to {evt}: {e}")

    def _refresh_members(self) -> None:
        try:
            resp = self._send_frame({"cmd": "GET_SELECTED_VOICE_CHANNEL", "args": {}})
            data = resp.get("data") or {}
            voice_states = data.get("voice_states", [])
            members = []
            for vs in voice_states:
                user = vs.get("user", {})
                members.append({
                    "user_id": user.get("id", ""),
                    "name": vs.get("nick") or user.get("username", "?"),
                })
            members.sort(key=lambda m: m["name"].casefold())
            with self._members_lock:
                self._members = members
                if self._offset >= max(1, len(members)):
                    self._offset = 0
        except Exception as e:
            log.error(f"Failed to refresh channel members: {e}")

    # ---------------------------------------------------------- pager / state

    def get_channel_members(self) -> list[dict]:
        with self._members_lock:
            return list(self._members)

    def get_visible_members(self) -> list[dict | None]:
        """
        Returns 9 elements for the 9 person slots:
          slot 0-2 → display strip row (row 2), cols 1-3  (knob-controlled)
          slot 3-5 → bottom button row (row 1), cols 1-3
          slot 6-8 → top button row (row 0), cols 1-3

        None means the slot is empty. Fills bottom-up so the first
        members (alphabetically) are always on the knob row.
        """
        with self._members_lock:
            members = list(self._members)

        n = len(members)
        result: list[dict | None] = [None] * 9
        if n == 0:
            return result

        page_count = math.ceil(n / 3)
        rows_used = min(page_count, 3)
        current_page = (self._offset // 3) % page_count

        for row_offset in range(rows_used):
            page = (current_page + row_offset) % page_count
            for col in range(3):
                member_idx = page * 3 + col
                slot_idx = row_offset * 3 + col
                if member_idx < n:
                    result[slot_idx] = members[member_idx]

        return result

    def page_down(self) -> None:
        with self._members_lock:
            n = len(self._members)
        if n <= 3:
            return
        page_count = math.ceil(n / 3)
        current_page = (self._offset // 3) % page_count
        self._offset = ((current_page + 1) % page_count) * 3

    def get_pager_offset(self) -> int:
        return self._offset

    # --------------------------------------------------------- volume / mute

    def set_user_volume(self, user_id: str, volume: int) -> None:
        volume = max(0, min(200, volume))
        self._volumes[user_id] = volume
        try:
            self._send_frame({
                "cmd": "SET_USER_VOICE_SETTINGS",
                "args": {"user_id": user_id, "volume": volume},
            })
        except Exception as e:
            log.error(f"set_user_volume failed for {user_id}: {e}")

    def get_user_volume(self, user_id: str) -> int:
        return self._volumes.get(user_id, 100)

    def toggle_mute(self, user_id: str) -> None:
        current = self.get_user_volume(user_id)
        if current == 0:
            restore = self._restore_volumes.pop(user_id, 100)
            self.set_user_volume(user_id, restore)
        else:
            self._restore_volumes[user_id] = current
            self.set_user_volume(user_id, 0)

    def is_muted(self, user_id: str) -> bool:
        return self._volumes.get(user_id, 100) == 0


backend = Backend()
