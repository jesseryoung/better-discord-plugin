import os
from loguru import logger as log

from src.backend.PluginManager.ActionBase import ActionBase


class PagerExit(ActionBase):
    """
    The Exit navigation button (row 1, col 0).
    Resets the pager offset to 0 and navigates back to whichever page
    was active before the user switched to the channel pager page.

    The previous page path must be stored on plugin_base.prev_page_path
    before the switch to the pager page occurs. Any action or button that
    triggers the switch TO this page should set:
        self.plugin_base.prev_page_path = self.deck_controller.active_page.json_path
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def on_ready(self) -> None:
        icon_path = os.path.join(self.plugin_base.PATH, "assets", "icon.png")
        self.set_media(media_path=icon_path, size=0.6)
        self.set_center_label("Exit")

    def on_key_down(self) -> None:
        # Reset the pager scroll position
        try:
            # Walk offset back to 0 by calling page_down until we wrap,
            # or just set it directly (simpler).
            # The backend doesn't expose set_offset, so we reset via plugin_base.
            self.plugin_base.reset_pager_offset()
        except Exception as e:
            log.error(f"PagerExit: reset_pager_offset failed: {e}")

        # Navigate back to the previous page
        prev_path = getattr(self.plugin_base, "prev_page_path", None)
        if not prev_path:
            log.warning("PagerExit: no prev_page_path stored on plugin_base")
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
