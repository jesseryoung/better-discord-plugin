import os
from loguru import logger as log

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw

from src.backend.PluginManager.ActionBase import ActionBase
from src.backend.DeckManagement.InputIdentifier import Input


class ChannelPager(ActionBase):
    """
    Displays one person slot in the Discord channel pager.

    Slot mapping (9 slots total):
      slot 0-2 → display strip (row 2 / knob row), cols 1-3
      slot 3-5 → bottom buttons (row 1), cols 1-3
      slot 6-8 → top buttons (row 0), cols 1-3

    Place this action on any of the 9 person-slot buttons/segments.
    The slot is derived automatically from the input's grid position.

    Key presses / touch presses toggle the person's local mute.
    Dial turns (CW / CCW) adjust the person's output volume by ±5.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slot_index: int | None = None

    def on_ready(self) -> None:
        self._slot_index = self._compute_slot_index()
        self._refresh_display()

    def on_key_down(self) -> None:
        self._handle_tap()

    def on_key_up(self) -> None:
        pass

    def event_callback(self, event, data=None) -> None:
        """Handle dial turn events for volume control in addition to legacy key events."""
        evt = str(event)
        if "TURN_CW" in evt:
            steps = (data or {}).get("steps", 1) if data else 1
            self._adjust_volume(+5 * steps)
        elif "TURN_CCW" in evt:
            steps = (data or {}).get("steps", 1) if data else 1
            self._adjust_volume(-5 * steps)
        elif "SHORT_TOUCH_PRESS" in evt or "DOWN" in evt:
            self._handle_tap()
        else:
            super().event_callback(event, data)

    # ----------------------------------------------------------- slot mapping

    def _compute_slot_index(self) -> int | None:
        """
        Derive which of the 9 person slots this action occupies from its position.

        Key inputs  (rows 0–1, cols 1–3):
          coords (x, y) where x=col, y=row
          slot = (1 - y) * 3 + x + 2
            → (1,0)=6  (2,0)=7  (3,0)=8
            → (1,1)=3  (2,1)=4  (3,1)=5

        Touchscreen inputs (display strip, col 1–3 → index 1–3):
          slot = index - 1
            → index 1=0  index 2=1  index 3=2

        Col 0 and touchscreen index 0 are navigation / unassigned.
        """
        try:
            if isinstance(self.input_ident, Input.Key):
                x, y = self.input_ident.coords
                if x == 0:
                    return None
                return (1 - y) * 3 + x + 2
            if isinstance(self.input_ident, Input.Touchscreen):
                idx = int(self.input_ident.index)
                return None if idx == 0 else idx - 1
        except Exception as e:
            log.error(f"ChannelPager: could not compute slot index: {e}")
        return None

    # ---------------------------------------------------------- member lookup

    def _get_my_member(self) -> dict | None:
        if self._slot_index is None:
            return None
        try:
            visible = self.plugin_base.backend.get_visible_members()
            return visible[self._slot_index]
        except Exception as e:
            log.error(f"ChannelPager: get_visible_members failed: {e}")
            return None

    # ---------------------------------------------------------- interactions

    def _handle_tap(self) -> None:
        member = self._get_my_member()
        if member is None:
            return
        try:
            self.plugin_base.backend.toggle_mute(member["user_id"])
            self._refresh_display()
        except Exception as e:
            log.error(f"ChannelPager: toggle_mute failed: {e}")
            self.show_error()

    def _adjust_volume(self, delta: int) -> None:
        member = self._get_my_member()
        if member is None:
            return
        try:
            user_id = member["user_id"]
            current = self.plugin_base.backend.get_user_volume(user_id)
            self.plugin_base.backend.set_user_volume(user_id, current + delta)
            self._refresh_display()
        except Exception as e:
            log.error(f"ChannelPager: volume adjust failed: {e}")

    # ------------------------------------------------------------ display

    def _refresh_display(self) -> None:
        member = self._get_my_member()

        if member is None:
            self.set_media(media_path=None)
            self.set_center_label("")
            return

        try:
            muted = self.plugin_base.backend.is_muted(member["user_id"])
        except Exception:
            muted = False

        icon_file = "mic_off.svg" if muted else "mic.svg"
        icon_path = os.path.join(self.plugin_base.PATH, "assets", icon_file)
        self.set_media(media_path=icon_path, size=0.75)
        self.set_center_label(member["name"])
