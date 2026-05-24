import os
from loguru import logger as log

from src.backend.PluginManager.ActionBase import ActionBase
from src.Signals import Signals


class PagerExit(ActionBase):
    """
    The Exit navigation button (row 1, col 0).

    Automatically captures the previous page by subscribing to the
    ChangePage signal. No custom "launch" action is needed — just use
    StreamController's built-in Change Page action on your Home page to
    navigate here, and Exit will always know where to go back.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def on_ready(self) -> None:
        icon_path = os.path.join(self.plugin_base.PATH, "assets", "arrow_back.svg")
        self.set_media(media_path=icon_path, size=0.75)
        self.set_center_label("")

        # Subscribe to page changes so we always know where we came from.
        # ChangePage fires with (controller, old_path, new_path) on every switch.
        self.connect(signal=Signals.ChangePage, callback=self._on_page_changed)

    def _on_page_changed(self, controller, old_path: str, new_path: str) -> None:
        # Only care about switches TO this action's own page on this deck.
        if controller is not self.deck_controller:
            return
        if not hasattr(self.page, "json_path"):
            return
        if new_path == self.page.json_path and old_path:
            self.plugin_base.prev_page_path = old_path
            log.debug(f"PagerExit: captured prev_page_path = {old_path}")

    def on_key_down(self) -> None:
        try:
            self.plugin_base.reset_pager_offset()
        except Exception as e:
            log.error(f"PagerExit: reset_pager_offset failed: {e}")

        prev_path = getattr(self.plugin_base, "prev_page_path", None)
        if not prev_path:
            log.warning("PagerExit: no prev_page_path — navigate to this page first")
            return
        try:
            import globals as gl
            prev_page = gl.page_manager.get_page(prev_path, self.deck_controller)
            self.deck_controller.load_page(prev_page)
        except Exception as e:
            log.error(f"PagerExit: page navigation failed: {e}")
            self.show_error()

    def on_key_up(self) -> None:
        pass
