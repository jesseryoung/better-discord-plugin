import os
import threading

from loguru import logger as log

import globals as gl
from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.Signals import Signals

from .actions.ChannelPager.ChannelPager import ChannelPager
from .actions.PagerDown.PagerDown import PagerDown
from .actions.PagerExit.PagerExit import PagerExit


class BetterDiscord(PluginBase):
    def __init__(self):
        super().__init__()

        self.prev_page_path: str | None = None
        self._pager_page_path = os.path.join(self.PATH, "pages", "Discord Channel Pager.json")
        gl.signal_manager.connect_signal(signal=Signals.ChangePage, callback=self._on_page_changed_global)

        self.launch_backend(
            backend_path=os.path.join(self.PATH, "backend", "backend.py"),
            open_in_terminal=False,
        )

        self._connect_lock = threading.Lock()
        self._user_connecting = threading.Event()
        self._connected = False

        # On startup only try the cached token — never auto-prompt for OAuth.
        threading.Thread(target=self._try_cached_connect, daemon=True).start()
        threading.Thread(target=self._reconnect_watcher, daemon=True).start()

        self.channel_pager_holder = ActionHolder(
            plugin_base=self,
            action_base=ChannelPager,
            action_id="net_jesseyoung_betterdiscord::ChannelPager",
            action_name="Channel Pager Slot",
        )
        self.add_action_holder(self.channel_pager_holder)

        self.pager_down_holder = ActionHolder(
            plugin_base=self,
            action_base=PagerDown,
            action_id="net_jesseyoung_betterdiscord::PagerDown",
            action_name="Pager Down",
        )
        self.add_action_holder(self.pager_down_holder)

        self.pager_exit_holder = ActionHolder(
            plugin_base=self,
            action_base=PagerExit,
            action_id="net_jesseyoung_betterdiscord::PagerExit",
            action_name="Pager Exit",
        )
        self.add_action_holder(self.pager_exit_holder)

        self.register(
            plugin_name="Better Discord",
            github_repo="",
            plugin_version="0.0.1",
            app_version="1.5.0",
        )

        self.register_page(os.path.join(self.PATH, "pages", "Discord Channel Pager.json"))

    # ----------------------------------------------------------------- auth

    def _try_cached_connect(self) -> None:
        """Startup path: only attempt connection if a cached token exists."""
        settings = self.get_settings()
        client_id = settings.get("client_id", "")
        access_token = settings.get("access_token")
        if not client_id or not access_token:
            return
        if self.backend.connect(client_id, access_token):
            self._connected = True
            return
        if self._try_refresh_token(client_id):
            self._connected = True

    def _reconnect_watcher(self) -> None:
        """Polls every 10 s and reconnects automatically when Discord comes back up."""
        import time
        while True:
            time.sleep(10)
            if self._connected or self._user_connecting.is_set():
                continue
            settings = self.get_settings()
            client_id = settings.get("client_id", "")
            access_token = settings.get("access_token")
            if not client_id or not access_token:
                continue
            if not self._connect_lock.acquire(blocking=False):
                continue
            try:
                if self._user_connecting.is_set():
                    continue
                self.backend.disconnect()
                if self.backend.connect(client_id, access_token):
                    self._connected = True
                elif self._try_refresh_token(client_id):
                    self._connected = True
            except Exception as e:
                log.debug(f"Reconnect watcher error: {e}")
            finally:
                self._connect_lock.release()

    def _try_connect(self, client_id: str, client_secret: str) -> None:
        """Full OAuth flow — called by the Connect button.

        Skips cached/refresh token attempts since those are handled by the
        reconnect watcher. The user clicked Connect with a secret specifically
        to run the OAuth flow.
        """
        # Signal the reconnect watcher to yield, then wait for the lock.
        self._user_connecting.set()
        if not self._connect_lock.acquire(timeout=15):
            self._user_connecting.clear()
            self._set_connect_status("Timed out waiting for background reconnect to finish", connected=False)
            return
        try:
            self._connected = False
            self.backend.disconnect()

            self._set_connect_status("Waiting for Discord approval…")
            result = self.backend.get_fresh_token(client_id, client_secret)
            token = str(result[0]) if result[0] else None
            refresh = str(result[1]) if result[1] else None
            err = str(result[2]) if result[2] else None
            if not token:
                self._set_connect_status(err or "Authorization failed", connected=False)
                return

            settings = self.get_settings()
            settings["access_token"] = token
            if refresh:
                settings["refresh_token"] = refresh
            self.set_settings(settings)

            if self.backend.connect(client_id, token):
                self._connected = True
                self._set_connect_status("Connected", connected=True)
            else:
                self._set_connect_status("Token obtained but connection failed", connected=False)
        except Exception as e:
            self._set_connect_status(f"Error: {e}", connected=False)
        finally:
            self._user_connecting.clear()
            self._connect_lock.release()

    def _try_refresh_token(self, client_id: str) -> bool:
        """Attempt to refresh the access token using a stored refresh token.

        Caller must ensure backend is already disconnected.
        """
        settings = self.get_settings()
        refresh_token = settings.get("refresh_token")
        if not refresh_token:
            return False
        result = self.backend.refresh_access_token(client_id, refresh_token)
        token = str(result[0]) if result[0] else None
        new_refresh = str(result[1]) if result[1] else None
        err = str(result[2]) if result[2] else None
        if not token:
            log.warning(f"Token refresh failed: {err}")
            settings.pop("refresh_token", None)
            self.set_settings(settings)
            return False
        settings["access_token"] = token
        if new_refresh:
            settings["refresh_token"] = new_refresh
        self.set_settings(settings)
        return self.backend.connect(client_id, token)

    # -------------------------------------------------------------- helpers

    def on_members_updated(self) -> None:
        """Called by the backend (via RPyC) after the member list changes."""
        self._connected = self.backend.is_connected()
        try:
            from gi.repository import GLib
            GLib.idle_add(self._refresh_all_pager_displays)
        except Exception:
            pass

    def _refresh_all_pager_displays(self) -> None:
        from .actions.ChannelPager.ChannelPager import ChannelPager
        for action in list(ChannelPager._instances):
            try:
                action._refresh_display()
            except Exception:
                pass

    def _on_page_changed_global(self, controller, old_path: str, new_path: str) -> None:
        pager_name = os.path.splitext(os.path.basename(self._pager_page_path))[0]
        new_name = os.path.splitext(os.path.basename(new_path or ""))[0]
        old_name = os.path.splitext(os.path.basename(old_path or ""))[0]
        if new_name == pager_name and old_path and old_name != pager_name:
            self.prev_page_path = old_path

    def reset_pager_offset(self) -> None:
        """Called by PagerExit to reset the scroll position to the top."""
        try:
            # Page down until we wrap back to 0.
            # Simpler: we know page_down cycles pages, so call it until offset == 0.
            # But to avoid an infinite loop if something is wrong, cap iterations.
            for _ in range(100):
                if self.backend.get_pager_offset() == 0:
                    break
                self.backend.page_down()
        except Exception:
            pass

    # ---------------------------------------------------------- config UI

    def get_settings_area(self):
        try:
            from gi.repository import Adw, Gtk

            group = Adw.PreferencesGroup()
            group.set_title("Discord Connection")
            group.set_description(
                "Create an application at discord.com/developers to obtain these values."
            )

            self._client_id_row = Adw.EntryRow()
            self._client_id_row.set_title("Application Client ID")

            if hasattr(Adw, "PasswordEntryRow"):
                self._client_secret_row = Adw.PasswordEntryRow()
            else:
                self._client_secret_row = Adw.EntryRow()
            self._client_secret_row.set_title("Application Client Secret")

            settings = self.get_settings()
            self._client_id_row.set_text(settings.get("client_id", ""))
            self._client_secret_row.set_text("")  # never persisted

            self._client_id_row.connect("changed", self._on_credentials_changed)

            self._connect_row = Adw.ActionRow()
            self._connect_row.set_title("Connect to Discord")

            self._status_icon = Gtk.Image()
            self._status_icon.set_visible(False)
            self._connect_row.add_prefix(self._status_icon)

            self._connect_btn = Gtk.Button(label="Connect")
            self._connect_btn.set_valign(Gtk.Align.CENTER)
            self._connect_btn.add_css_class("suggested-action")
            self._connect_btn.connect("clicked", self._on_connect_clicked)
            self._connect_row.add_suffix(self._connect_btn)
            self._connect_row.set_activatable_widget(self._connect_btn)

            if self._connected:
                self._apply_connected_state()
            else:
                self._connect_row.set_subtitle("Enter credentials above and click Connect")

            group.add(self._client_id_row)
            group.add(self._client_secret_row)
            group.add(self._connect_row)

            return group
        except Exception as e:
            log.error(f"BetterDiscord: get_settings_area failed: {e}")
            return None

    def _on_credentials_changed(self, _widget) -> None:
        settings = self.get_settings()
        new_id = self._client_id_row.get_text()
        if new_id != settings.get("client_id", ""):
            settings.pop("access_token", None)
            settings.pop("refresh_token", None)
            self._set_connect_status("Client ID changed — reconnect required", connected=False)
        settings["client_id"] = new_id
        # client_secret is intentionally NOT saved to disk
        self.set_settings(settings)

    def _on_connect_clicked(self, _widget) -> None:
        if self._user_connecting.is_set():
            return
        client_id = self._client_id_row.get_text().strip()
        client_secret = self._client_secret_row.get_text().strip()
        if not client_id or not client_secret:
            self._set_connect_status("Enter Client ID and Client Secret first", connected=False)
            return
        # Disable immediately in the main thread before spawning the worker.
        self._connect_btn.set_label("Connecting…")
        self._connect_btn.set_sensitive(False)
        threading.Thread(
            target=self._try_connect,
            args=(client_id, client_secret),
            daemon=True,
        ).start()

    def _set_connect_status(self, msg: str, connected: bool | None = None) -> None:
        """Update the connection status UI.

        connected=True  → show connected state, disable button
        connected=False → show disconnected state, re-enable button
        connected=None  → progress update, only change subtitle text
        """
        def _update():
            try:
                self._connect_row.set_subtitle(msg)
                if connected is True:
                    self._apply_connected_state()
                elif connected is False:
                    self._connect_btn.set_label("Connect")
                    self._connect_btn.set_sensitive(True)
                    self._connect_btn.remove_css_class("success")
                    self._connect_btn.add_css_class("suggested-action")
                    self._status_icon.set_visible(False)
            except Exception:
                pass
        try:
            from gi.repository import GLib
            GLib.idle_add(_update)
        except Exception:
            pass

    def _apply_connected_state(self) -> None:
        self._connect_row.set_subtitle("Connected")
        self._connect_btn.set_label("Connected")
        self._connect_btn.set_sensitive(False)
        self._connect_btn.remove_css_class("suggested-action")
        self._connect_btn.add_css_class("success")
        self._status_icon.set_from_icon_name("emblem-ok-symbolic")
        self._status_icon.add_css_class("success")
        self._status_icon.set_visible(True)
