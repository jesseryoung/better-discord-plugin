import os
from loguru import logger as log

from src.backend.PluginManager.ActionBase import ActionBase


class PagerExit(ActionBase):
    """
    The Exit navigation button (row 1, col 0).
    Navigates back to whichever page was active before the channel pager was opened.
    The plugin tracks prev_page_path via a global ChangePage signal listener.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def on_ready(self) -> None:
        icon_path = os.path.join(self.plugin_base.PATH, "assets", "arrow_back.svg")
        self.set_media(media_path=icon_path, size=0.75)
        self.set_center_label("")

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
