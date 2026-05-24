import os
import threading

from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.ActionHolder import ActionHolder

from .actions.ChannelPager.ChannelPager import ChannelPager
from .actions.PagerDown.PagerDown import PagerDown
from .actions.PagerExit.PagerExit import PagerExit


class BetterDiscord(PluginBase):
    def __init__(self):
        super().__init__()

        # Stores the page path to return to when Exit is pressed.
        # Set this before navigating TO the channel pager page:
        #   self.plugin_base.prev_page_path = deck_controller.active_page.json_path
        self.prev_page_path: str | None = None

        self.launch_backend(
            backend_path=os.path.join(self.PATH, "backend", "backend.py"),
            open_in_terminal=False,
        )

        self._connect_lock = threading.Lock()

        # On startup only try the cached token — never auto-prompt for OAuth.
        threading.Thread(target=self._try_cached_connect, daemon=True).start()

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
        self.backend.connect(client_id, access_token)

    def _try_connect(self, client_id: str, client_secret: str) -> None:
        """Full OAuth flow — called by the Connect button."""
        if not self._connect_lock.acquire(blocking=False):
            self._set_connect_status("Already connecting…")
            return
        try:
            self.backend.disconnect()

            # Try cached token first.
            access_token = self.get_settings().get("access_token")
            self._set_connect_status("Connecting…")
            ok = self.backend.connect(client_id, access_token)
            if ok:
                self._set_connect_status("Connected", connected=True)
                return

            # Full OAuth flow — opens Discord approval dialog.
            self._set_connect_status("Waiting for Discord approval…")
            token, err = self.backend.get_fresh_token(client_id, client_secret)
            if not token:
                self._set_connect_status(err or "Authorization failed")
                return

            settings = self.get_settings()
            settings["access_token"] = token
            self.set_settings(settings)

            ok = self.backend.connect(client_id, token)
            if ok:
                self._set_connect_status("Connected", connected=True)
            else:
                self._set_connect_status("Token obtained but connection failed")
        except Exception as e:
            self._set_connect_status(f"Error: {e}")
        finally:
            self._connect_lock.release()

    # -------------------------------------------------------------- helpers

    def on_members_updated(self) -> None:
        """Called by the backend (via RPyC) after the member list changes."""
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
        from loguru import logger as log
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

            if self.backend.is_connected():
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
            self._set_connect_status("Client ID changed — reconnect required", connected=False)
        settings["client_id"] = new_id
        # client_secret is intentionally NOT saved to disk
        self.set_settings(settings)

    def _on_connect_clicked(self, _widget) -> None:
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

    def _set_connect_status(self, msg: str, connected: bool = False) -> None:
        def _update():
            try:
                self._connect_row.set_subtitle(msg)
                if connected:
                    self._apply_connected_state()
                else:
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
