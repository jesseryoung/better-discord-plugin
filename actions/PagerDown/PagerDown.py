import os
from loguru import logger as log

from src.backend.PluginManager.ActionBase import ActionBase


class PagerDown(ActionBase):
    """
    The Down navigation button (row 0, col 0).
    Rotates the visible member window down by one page (3 people).
    No-op when there are 3 or fewer people in the channel.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def on_ready(self) -> None:
        icon_path = os.path.join(self.plugin_base.PATH, "assets", "arrow_down.svg")
        self.set_media(media_path=icon_path, size=0.75)
        self.set_center_label("")

    def on_key_down(self) -> None:
        try:
            self.plugin_base.backend.page_down()
        except Exception as e:
            log.error(f"PagerDown: page_down failed: {e}")
            self.show_error()

    def on_key_up(self) -> None:
        pass
