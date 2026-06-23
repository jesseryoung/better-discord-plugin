from streamcontroller_plugin_tools import BackendBase
import json
import math
import os
import queue
import socket
import struct
import threading
import urllib.error
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
        self._offset = 0
        self._running = False
        self._listener_thread: threading.Thread | None = None
        self._avatar_cache: dict[str, str] = {}
        self._muted: dict[str, bool] = {}
        self._current_user_id: str | None = None
        self._channel_id: str | None = None

    # ------------------------------------------------------------------ socket

    def _ipc_paths(self) -> list[str]:
        import tempfile
        paths = []
        xdg = os.environ.get("XDG_RUNTIME_DIR", "")
        # Inside a Flatpak XDG_RUNTIME_DIR is sandboxed; the host runtime dir is /run/user/<uid>
        host_xdg = f"/run/user/{os.getuid()}"
        for i in range(10):
            if xdg:
                paths.append(os.path.join(xdg, f"discord-ipc-{i}"))
                paths.append(os.path.join(xdg, "app", "com.discordapp.Discord", f"discord-ipc-{i}"))
            if host_xdg != xdg:
                paths.append(os.path.join(host_xdg, f"discord-ipc-{i}"))
                paths.append(os.path.join(host_xdg, "app", "com.discordapp.Discord", f"discord-ipc-{i}"))
            paths.append(os.path.join(tempfile.gettempdir(), f"discord-ipc-{i}"))
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
                elif evt == "VOICE_CHANNEL_SELECT":
                    threading.Thread(target=self._on_channel_select, daemon=True).start()

            except Exception as e:
                log.error(f"Discord IPC listener error: {e}")
                self._connected = False
                self._running = False
                try:
                    self.frontend.on_members_updated()
                except Exception:
                    pass
                break

    def _start_listener(self) -> None:
        self._running = True
        self._listener_thread = threading.Thread(target=self._listener_loop, daemon=True)
        self._listener_thread.start()

    # -------------------------------------------------------------------- auth

    def connect(self, client_id: str, access_token: str | None) -> bool:
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
        self._current_user_id = (resp.get("data") or {}).get("user", {}).get("id")

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
                self.disconnect()
                return False
        except Exception as e:
            log.error(f"AUTHENTICATE failed: {e}")
            self.disconnect()
            return False

        self._connected = True
        self._subscribe_events()
        self._refresh_members()
        self._subscribe_voice_state_events(self._channel_id)
        return True

    def refresh_access_token(self, client_id: str, refresh_token: str) -> tuple[str | None, str | None, str | None]:
        """
        Uses a refresh_token to obtain a new access_token from Discord.
        Returns (access_token, new_refresh_token, None) on success,
        (None, None, error_message) on failure.
        """
        try:
            params = urllib.parse.urlencode({
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }).encode()
            req = urllib.request.Request(
                "https://discord.com/api/oauth2/token",
                data=params,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "DiscordBot (https://github.com/jesseryoung/better-discord-plugin, 0.0.1)",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                token_data = json.loads(r.read().decode())
            token = token_data.get("access_token")
            refresh = token_data.get("refresh_token")
            if not token:
                msg = f"Token refresh gave no access_token: {token_data}"
                log.error(msg)
                return None, None, msg
            return token, refresh, None
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            msg = f"Token refresh HTTP {e.code}: {body}"
            log.error(msg)
            return None, None, msg
        except Exception as e:
            msg = f"Token refresh failed: {e}"
            log.error(msg)
            return None, None, msg

    def get_fresh_token(self, client_id: str, client_secret: str) -> tuple[str | None, str | None, str | None]:
        """
        Runs Steps 2–3 of the Discord OAuth flow.
        Returns (access_token, refresh_token, None) on success,
        (None, None, error_message) on failure.
        """
        if self._running:
            msg = "Listener still active — call disconnect() first"
            log.error(msg)
            return None, None, msg

        if not self._open_socket():
            msg = "Cannot connect to Discord IPC socket — is Discord running?"
            log.error(msg)
            return None, None, msg

        self._send_raw(OP_HANDSHAKE, {"v": 1, "client_id": client_id})
        _, resp = self._recv_raw()
        if resp.get("evt") != "READY":
            msg = f"IPC handshake failed: {resp.get('evt')}"
            log.error(msg)
            self._close_socket()
            return None, None, msg

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
            msg = "Timed out waiting for Discord approval (120s)"
            log.error(msg)
            self._close_socket()
            return None, None, msg
        finally:
            self._sock.settimeout(None)

        code = (resp.get("data") or {}).get("code")
        if not code:
            msg = f"AUTHORIZE returned no code: {resp.get('evt')} — {(resp.get('data') or {}).get('message', '')}"
            log.error(msg)
            self._close_socket()
            return None, None, msg

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
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "DiscordBot (https://github.com/jesseryoung/better-discord-plugin, 0.0.1)",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                token_data = json.loads(r.read().decode())
            token = token_data.get("access_token")
            refresh = token_data.get("refresh_token")
            if not token:
                msg = f"Token exchange gave no access_token: {token_data}"
                log.error(msg)
                return None, None, msg
            return token, refresh, None
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            msg = f"Token exchange HTTP {e.code}: {body}"
            log.error(msg)
            return None, None, msg
        except Exception as e:
            msg = f"Token exchange failed: {e}"
            log.error(msg)
            return None, None, msg

    def disconnect(self) -> None:
        self._running = False
        self._connected = False
        self._close_socket()
        if self._listener_thread and self._listener_thread.is_alive():
            self._listener_thread.join(timeout=2)

    def is_connected(self) -> bool:
        return self._connected

    # ---------------------------------------------------------- Discord events

    def _subscribe_events(self) -> None:
        try:
            self._send_frame({"cmd": "SUBSCRIBE", "evt": "VOICE_CHANNEL_SELECT", "args": {}})
        except Exception as e:
            log.error(f"Failed to subscribe to VOICE_CHANNEL_SELECT: {e}")

    def _subscribe_voice_state_events(self, channel_id: str | None) -> None:
        if not channel_id:
            return
        for evt in ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE", "VOICE_STATE_DELETE"):
            try:
                self._send_frame({"cmd": "SUBSCRIBE", "evt": evt, "args": {"channel_id": channel_id}})
            except Exception as e:
                log.error(f"Failed to subscribe to {evt} for channel {channel_id}: {e}")

    def _on_channel_select(self) -> None:
        self._refresh_members()
        self._subscribe_voice_state_events(self._channel_id)

    def _fetch_avatar(self, user_id: str, avatar_hash: str) -> str | None:
        """Download and cache a user's Discord avatar. Returns local file path or None."""
        if not avatar_hash:
            return None
        cache_key = f"{user_id}_{avatar_hash}"
        cached = self._avatar_cache.get(cache_key)
        if cached and os.path.exists(cached):
            return cached
        import tempfile
        cache_dir = os.path.join(tempfile.gettempdir(), "better_discord_avatars")
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, f"{cache_key}.png")
        url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=64"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "DiscordBot (https://github.com/jesseryoung/better-discord-plugin, 0.0.1)"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                with open(path, "wb") as f:
                    f.write(r.read())
            self._avatar_cache[cache_key] = path
            return path
        except Exception as e:
            log.warning(f"Failed to fetch avatar for {user_id}: {e}")
            return None

    def _refresh_members(self) -> None:
        try:
            resp = self._send_frame({"cmd": "GET_SELECTED_VOICE_CHANNEL", "args": {}})
            data = resp.get("data") or {}
            self._channel_id = data.get("id") or None
            voice_states = data.get("voice_states", [])
            members = []
            for vs in voice_states:
                user = vs.get("user", {})
                user_id = user.get("id", "")
                if user_id == self._current_user_id:
                    continue
                avatar_hash = user.get("avatar", "")
                avatar_path = self._fetch_avatar(user_id, avatar_hash)
                members.append({
                    "user_id": user_id,
                    "name": vs.get("nick") or user.get("username", "?"),
                    "avatar_path": avatar_path,
                })
            members.sort(key=lambda m: m["name"].casefold())
            with self._members_lock:
                self._members = members
                if self._offset >= max(1, len(members)):
                    self._offset = 0
        except Exception as e:
            log.error(f"Failed to refresh channel members: {e}")
            return
        try:
            self.frontend.on_members_updated()
        except Exception as e:
            log.error(f"Could not notify frontend of member update: {e}")

    # ---------------------------------------------------------- pager / state

    def get_channel_members(self) -> list[dict]:
        with self._members_lock:
            return list(self._members)

    def get_visible_members(self) -> list[dict | None]:
        """
        Returns 9 elements for the 9 person slots:
          slot 0-2 → display strip / dials, indices 1-3   — fills first
          slot 3-5 → bottom button row (row 1), cols 1-3  — fills second
          slot 6-8 → top button row    (row 0), cols 1-3  — fills last

        None means the slot is empty.
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

    def set_user_mute(self, user_id: str, muted: bool) -> None:
        self._muted[user_id] = muted
        try:
            self._send_frame({
                "cmd": "SET_USER_VOICE_SETTINGS",
                "args": {"user_id": user_id, "mute": muted},
            })
        except Exception as e:
            log.error(f"set_user_mute failed for {user_id}: {e}")

    def toggle_mute(self, user_id: str) -> None:
        self.set_user_mute(user_id, not self._muted.get(user_id, False))

    def is_muted(self, user_id: str) -> bool:
        return self._muted.get(user_id, False)


backend = Backend()
