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

        # Attempt connection in the background so startup isn't blocked.
        threading.Thread(target=self._try_connect, daemon=True).start()

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

    def _try_connect(self) -> None:
        settings = self.get_settings()
        client_id = settings.get("client_id", "")
        client_secret = settings.get("client_secret", "")
        access_token = settings.get("access_token")

        if not client_id or not client_secret:
            return

        ok = self.backend.connect(client_id, client_secret, access_token)
        if ok:
            return

        # Cached token missing or expired — run the full OAuth flow.
        # This will open a Discord approval modal; blocks until approved or timed out.
        token = self.backend.get_fresh_token(client_id, client_secret)
        if token:
            settings["access_token"] = token
            self.set_settings(settings)
            self.backend.connect(client_id, client_secret, token)

    # -------------------------------------------------------------- helpers

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
            from gi.repository import Adw

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
            self._client_secret_row.set_text(settings.get("client_secret", ""))

            self._client_id_row.connect("changed", self._on_credentials_changed)
            self._client_secret_row.connect("changed", self._on_credentials_changed)

            group.add(self._client_id_row)
            group.add(self._client_secret_row)

            return group
        except Exception as e:
            log.error(f"BetterDiscord: get_settings_area failed: {e}")
            return None

    def _on_credentials_changed(self, _widget) -> None:
        settings = self.get_settings()
        settings["client_id"] = self._client_id_row.get_text()
        settings["client_secret"] = self._client_secret_row.get_text()
        # Invalidate cached token whenever credentials change
        settings.pop("access_token", None)
        self.set_settings(settings)
