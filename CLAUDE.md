# Better Discord – StreamController Plugin

A StreamController plugin for controlling Discord voice channels from a Stream Deck +.

---

## StreamController Architecture

- **PluginBase** (`main.py`) – singleton, lives for the whole app session, survives page changes
- **ActionCore** (each action) – instantiated per button/dial; `on_ready()` fires after the page loads
  - `ActionBase` is a **deprecated** backward-compat shim over `ActionCore`; prefer `ActionCore` for new actions
- **BackendBase** (`backend/backend.py`) – runs in a separate process; communicates with the plugin via RPyC
- The backend calls back into the plugin via `self.frontend.method()` (RPyC proxy); works as long as the method is public (no leading `_`)
- GTK widget updates from any non-main thread must go through `GLib.idle_add(fn)`

---

## PluginBase

Defined in `src/backend/PluginManager/PluginBase.py` (part of StreamController itself, not the plugin).

```python
class MyPlugin(PluginBase):
    def __init__(self):
        super().__init__()
        # 1. Create and register ActionHolders
        self.my_holder = ActionHolder(
            plugin_base=self,
            action_base=MyAction,          # the ActionCore subclass
            action_id_suffix="MyAction",   # appended to plugin ID
            action_name="My Action",       # UI display name
            action_support={
                Input.Key:         ActionInputSupport.SUPPORTED,
                Input.Dial:        ActionInputSupport.SUPPORTED,
                Input.Touchscreen: ActionInputSupport.UNTESTED,
            }
        )
        self.add_action_holder(self.my_holder)

        # 2. Call register() LAST
        self.register(
            plugin_name="My Plugin",
            github_repo="https://github.com/...",
            plugin_version="0.0.1",
            app_version="1.0.0-alpha",   # major version must match running app
        )
```

Key attributes available on `PluginBase`:
- `self.PATH` – absolute path to the plugin directory
- `self.locale_manager` – locale/translation helper
- `self.backend` / `self.backend_connection` – RPyC proxy to backend (after `launch_backend`)

Plugin-level settings (stored separately from page/action settings):
- `self.get_settings()` / `self.set_settings(dict)` → `~/.local/share/StreamController/settings/plugins/{id}/settings.json`

Backend management (call from `__init__` if needed):
- `self.launch_backend(backend_path, venv_path=None)` – spawns backend subprocess, blocks until connected
- `self.register_backend(port)` – called internally by the backend; do not call manually
- `self.register_page(path)` – registers a page JSON so it appears in the UI

---

## ActionCore (the current action base class)

Defined in `src/backend/PluginManager/ActionCore.py`.

### Constructor arguments (injected by StreamController)
```python
def __init__(self, action_id, action_name, deck_controller, page,
             plugin_base, state, input_ident):
```
- `self.deck_controller` – the `DeckController` for this device
- `self.page` – the `Page` object currently loaded
- `self.state` – int state index (buttons can have multiple states)
- `self.input_ident` – `Input.Key`, `Input.Dial`, or `Input.Touchscreen` instance
- `self.plugin_base` – reference to the `PluginBase` singleton

### Lifecycle hooks (override these)
- `on_ready()` – called after the page finishes loading; safe to call display methods
- `on_tick()` – called on a periodic timer while the page is visible
- `on_trigger()` – called on any configured trigger event
- `event_callback(event: InputEvent, data: dict)` – raw event dispatch; override to handle events

### Display API

```python
# Image / media
self.set_media(
    image=None,          # PIL.Image object
    media_path=None,     # path to .png/.svg/.gif/.mp4/etc.
    size: float = None,  # scale factor (1.0 = full)
    valign: float = None,
    halign: float = None,
    fps: int = 30,
    loop: bool = True,
    update: bool = True, # False to batch, then call self.get_input().update()
)

# Labels (position: "top" | "center" | "bottom")
self.set_label(text, position="bottom", color=None, font_family=None,
               font_size=None, outline_width=None, outline_color=None,
               font_weight=None, font_style=None, update=True)
self.set_top_label(text, ...)
self.set_center_label(text, ...)
self.set_bottom_label(text, ...)

# Background
self.set_background_color(color=[r, g, b, a], update=True)
```

All display calls silently do nothing if the action does not hold the relevant control
permission (see page JSON section). Check with:
- `self.has_image_control()` → bool
- `self.has_background_control()` → bool
- `self.has_label_control(label_index)` → bool  (0=top, 1=center, 2=bottom)

### Action-level settings (stored in the page JSON)
```python
settings = self.get_settings()   # returns dict
settings["key"] = value
self.set_settings(settings)
```

### Signal connection
```python
self.connect(signal=Signals.ChangePage, callback=self.on_page_change)
# equivalent to: gl.signal_manager.connect_signal(signal=..., callback=...)
```

### Backend (per-action, if not using plugin-level backend)
```python
self.launch_backend(backend_path, venv_path=None, open_in_terminal=False)
self.register_backend(port)   # called by backend, not manually
```

---

## ActionBase (deprecated — backward-compat shim)

`ActionBase` extends `ActionCore` and wires up `EventAssigner`s for every event, then routes them through `event_callback(event, data)`. Its built-in `event_callback` only dispatches:
- `Input.Key.Events.DOWN` → `on_key_down()`
- `Input.Key.Events.UP` → `on_key_up()`
- `Input.Dial.Events.DOWN` → `on_key_down()`
- `Input.Dial.Events.UP` → `on_key_up()`

Everything else (TURN_CW, TURN_CCW, SHORT_TOUCH_PRESS, etc.) is dispatched through the EventAssigner but `event_callback` ignores it unless you override it. The current code in this project overrides `event_callback` to handle dial turns — that is correct.

---

## BackendBase

From `streamcontroller_plugin_tools.BackendBase`. The backend is a separate Python process.

```python
class Backend(BackendBase):
    def __init__(self):
        super().__init__()   # connects to frontend, starts RPC server, registers
        # your init here

    # call back to plugin process:
    def some_method(self):
        self.frontend.on_something()   # must be a public method on PluginBase
```

- `self.frontend` – RPyC proxy to the PluginBase (or ActionCore that launched it)
- `self.frontend_connection` – the underlying RPyC connection
- `on_disconnect(conn)` – called when the frontend closes; shuts down server

The backend script is started with `--port=N` by StreamController; `BackendBase.__init__` parses it automatically.

---

## Input Types and Events

```
Input.Key         → physical buttons
Input.Dial        → rotary knobs + touch bar above each knob
Input.Touchscreen → standalone touchscreen strip
```

Event enums and their `str()` string names used in `event_callback`:

| Type | Event enum | str(event) |
|---|---|---|
| Key | `Input.Key.Events.DOWN` | `"Key Down"` |
| Key | `Input.Key.Events.UP` | `"Key Up"` |
| Key | `Input.Key.Events.SHORT_UP` | `"Key Short Up"` |
| Key | `Input.Key.Events.HOLD_START` | `"Key Hold Start"` |
| Key | `Input.Key.Events.HOLD_STOP` | `"Key Hold Stop"` |
| Dial | `Input.Dial.Events.DOWN` | `"Dial Down"` |
| Dial | `Input.Dial.Events.UP` | `"Dial Up"` |
| Dial | `Input.Dial.Events.SHORT_UP` | `"Dial Short Up"` |
| Dial | `Input.Dial.Events.HOLD_START` | `"Dial Hold Start"` |
| Dial | `Input.Dial.Events.HOLD_STOP` | `"Dial Hold Stop"` |
| Dial | `Input.Dial.Events.TURN_CW` | `"Dial Turn CW"` |
| Dial | `Input.Dial.Events.TURN_CCW` | `"Dial Turn CCW"` |
| Dial | `Input.Dial.Events.SHORT_TOUCH_PRESS` | `"Dial Touchscreen Short Press"` |
| Dial | `Input.Dial.Events.LONG_TOUCH_PRESS` | `"Dial Touchscreen Long Press"` |
| Touchscreen | `Input.Touchscreen.Events.DRAG_LEFT` | `"Touchscreen Drag Left"` |
| Touchscreen | `Input.Touchscreen.Events.DRAG_RIGHT` | `"Touchscreen Drag Right"` |

**Do not use the enum attribute names directly as strings** (`TURN_CW` etc.) — only the `str(event)` values shown above are reliable for matching.

The `data` dict may contain `{"steps": N}` for multi-step dial turns.

---

## Critical Page JSON Requirements

StreamController silently ignores `set_media()` and `set_*_label()` calls unless the page JSON has the right permission keys in each button/dial state:

```json
"image-control-action": 0,
"label-control-actions": [0, 0, 0]
```

- `image-control-action: 0` → action at index 0 controls the image
- `label-control-actions: [top, center, bottom]` → which action index controls each label slot
- Without these, `set_media()` and `set_bottom_label()` do nothing and return no error
- The `"labels": {...}` block is display data written by the UI, NOT what enables label control

---

## Available Signals

Import from `src.Signals.Signals`. Connect via `self.connect(signal=Signals.X, callback=fn)`.

| Signal | Callback signature |
|---|---|
| `ChangePage` | `fn(controller, old_path, new_path)` |
| `PageRename` | `fn(old_path, new_path)` |
| `PageAdd` | `fn(path)` |
| `PageDelete` | `fn(path)` |
| `PluginInstall` | `fn(id)` |
| `RemoveState` | `fn(state: int, state_map: dict)` |
| `AppQuit` | `fn()` |

---

## ChangePage Signal Gotchas

- `gl.signal_manager.connect_signal(Signals.ChangePage, callback)` connects globally
- Fires as `callback(controller, old_path, new_path)` via `GLib.idle_add`
- **Do not connect signals from `on_ready()`** if you need to catch the transition that navigated TO that page — `on_ready` fires after the page loads, so it always misses its own incoming transition
- StreamController sometimes calls `load_page` twice for one navigation (causes the signal to fire twice); guard against this by checking `old_path != new_path`
- Use `os.path.basename` comparison rather than full-path equality — paths differ between dev and Flatpak installs

---

## Slot Mapping (9 person slots)

```
slot 0-2 → dials (touch bar + knobs), index 1-3
slot 3-5 → bottom button row (y=1), cols 1-3
slot 6-8 → top button row (y=0), cols 1-3
```

Col 0 on the button grid is reserved for navigation (PagerDown, PagerExit).

---

## ActionInputSupport Values

```python
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport
ActionInputSupport.SUPPORTED    # fully tested
ActionInputSupport.UNTESTED     # may work, unverified
ActionInputSupport.UNSUPPORTED  # does not apply
```

---

## manifest.json Structure

```json
{
    "id": "com_author_pluginname",
    "name": "Human-readable name",
    "description": "...",
    "version": "0.0.1",
    "min-app-version": "1.5.0",
    "app-version-stop": "",
    "author": "email or name",
    "github": "https://github.com/...",
    "tags": ["discord"],
    "thumbnail": "assets/thumbnail.png",
    "icon": "assets/icon.png"
}
```

Version compatibility: `app_version` in `register()` must share the same **major** version as the running StreamController, AND the running app must be ≥ `min-app-version`. Mismatches disable the plugin silently.

---

## Flatpak IPC (Discord socket)

Inside a Flatpak, `$XDG_RUNTIME_DIR` is sandboxed. The host Discord socket is at `/run/user/{uid}/discord-ipc-{n}`. Always try both the sandbox path and `/run/user/{os.getuid()}/` paths.

The Flatpak also needs the permission: `flatpak override --user --filesystem=xdg-run/discord-ipc-0 com.core447.StreamController`

---

## Discord IPC Notes

- Token exchange to `discord.com/api/oauth2/token` requires a `User-Agent: DiscordBot (...)` header — Cloudflare blocks requests without it (HTTP 403 code 1010)
- `client_secret` must **never** be saved to settings — only held transiently in the UI widget during OAuth
- The current user's ID is in the `READY` handshake response at `data.user.id` — use it to filter the local user out of the voice channel member list
- Muting uses `SET_USER_VOICE_SETTINGS` with `{"mute": true}`, not setting volume to 0
- Avatar URL: `https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=64`

---

## Member Refresh Flow

1. Backend listener receives `VOICE_STATE_CREATE/UPDATE/DELETE`
2. Spawns a thread → calls `GET_SELECTED_VOICE_CHANNEL` via `_send_frame`
3. Updates `self._members` (thread-safe via lock)
4. Calls `self.frontend.on_members_updated()` (RPyC → plugin process)
5. Plugin: `GLib.idle_add(_refresh_all_pager_displays)`
6. Each `ChannelPager` instance calls `_refresh_display()`

`ChannelPager._instances` is a `weakref.WeakSet` — actions self-remove when destroyed, no manual cleanup needed.

---

## Label Font Sizes

Physical keys need `font_size=8` to fit name + volume. Dials have more display space and can use the default (pass `font_size=None`). Check `isinstance(self.input_ident, Input.Key)` to branch.
