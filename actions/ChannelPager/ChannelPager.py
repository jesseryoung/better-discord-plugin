import os
import weakref
from loguru import logger as log

from src.backend.PluginManager.ActionBase import ActionBase
from src.backend.DeckManagement.InputIdentifier import Input


class ChannelPager(ActionBase):
    _instances: weakref.WeakSet = weakref.WeakSet()
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
        ChannelPager._instances.add(self)
        self._slot_index = self._compute_slot_index()
        self._refresh_display()

    def on_key_down(self) -> None:
        self._handle_tap()

    def on_key_up(self) -> None:
        pass

    def event_callback(self, event, data=None) -> None:
        """Handle dial turn events for volume control in addition to legacy key events."""
        evt = str(event)
        if "Turn CW" in evt:
            steps = (data or {}).get("steps", 1) if data else 1
            self._adjust_volume(+5 * steps)
        elif "Turn CCW" in evt:
            steps = (data or {}).get("steps", 1) if data else 1
            self._adjust_volume(-5 * steps)
        elif "Short Press" in evt or "Down" in evt:
            self._handle_tap()
        else:
            super().event_callback(event, data)

    # ----------------------------------------------------------- slot mapping

    def _compute_slot_index(self) -> int | None:
        """
        Derive which of the 9 person slots this action occupies from its position.

        Fill order (touch bar first, then buttons):
          slots 0-2 → display strip / dials (index 1-3): idx 1=0  idx 2=1  idx 3=2
          slots 3-5 → bottom button row (y=1, cols 1-3): (1,1)=3  (2,1)=4  (3,1)=5
          slots 6-8 → top button row    (y=0, cols 1-3): (1,0)=6  (2,0)=7  (3,0)=8

        Col 0 and dial/touchscreen index 0 are navigation / unassigned.
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
            if isinstance(self.input_ident, Input.Dial):
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

    def _fetch_display_payload(self) -> dict | None:
        """Fetch the combined slot display data (one RPyC round-trip, by value)."""
        import json
        try:
            return json.loads(str(self.plugin_base.backend.get_slot_display_data()))
        except Exception as e:
            log.error(f"ChannelPager: get_slot_display_data failed: {e}")
            return None

    def _refresh_display(self, payload: dict | None = None) -> None:
        """Render this slot. If payload is None (interaction / on_ready), fetch it
        directly; the bulk refresh passes a shared payload to avoid per-slot calls."""
        label_size = 15

        if payload is None:
            payload = self._fetch_display_payload()

        if not payload or not payload.get("connected"):
            if self._slot_index is not None:
                warning_path = os.path.join(self.plugin_base.PATH, "assets", "not_connected.svg")
                self.set_media(media_path=warning_path, size=1.0)
                self.set_top_label("", font_size=label_size)
                self.set_bottom_label("not connected to discord", font_size=label_size)
            return

        slots = payload.get("slots") or []
        member = None
        if self._slot_index is not None and self._slot_index < len(slots):
            member = slots[self._slot_index]

        if member is None:
            self.set_media(media_path=None)
            self.set_top_label("", font_size=label_size)
            self.set_bottom_label("", font_size=label_size)
            return

        muted = member.get("muted", False)
        volume = member.get("volume", 100)
        avatar_path = member.get("avatar_path")
        icon_path = avatar_path or os.path.join(self.plugin_base.PATH, "assets", "person.svg")
        self.set_media(media_path=icon_path, size=0.85)
        self.set_top_label(member["name"], font_size=label_size)
        self.set_bottom_label("mute" if muted else f"{volume}%", font_size=label_size)
