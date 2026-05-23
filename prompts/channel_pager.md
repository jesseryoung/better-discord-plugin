# Feature: Discord Channel Member Pager

## Project context

This is a StreamController plugin (`net_jesseyoung_betterdiscord`) that connects to a locally running
Discord client via Discord's local IPC socket (no bot required). The plugin is written in Python and
follows the StreamController plugin conventions: a `PluginBase` subclass in `main.py`, actions that
extend `ActionBase`, and a shared backend process that extends `BackendBase` from
`streamcontroller-plugin-tools`.

## Hardware

**Stream Deck +** — 8 physical buttons arranged in a 2×4 grid (2 rows, 4 columns), plus a
touch-screen display strip above 4 rotary knobs.

StreamController exposes the display strip as a third addressable row, giving an effective 3×4 grid:

```
Row 0 (top buttons):     [ col 0 ] [ col 1 ] [ col 2 ] [ col 3 ]
Row 1 (bottom buttons):  [ col 0 ] [ col 1 ] [ col 2 ] [ col 3 ]
Row 2 (display strip):   [ col 0 ] [ col 1 ] [ col 2 ] [ col 3 ]
                           knob 0    knob 1    knob 2    knob 3
```

## Feature: channel member pager

### Layout

```
Row 0:  [ Down  ] [ Person A ] [ Person B ] [ Person C ]
Row 1:  [ Exit  ] [ Person D ] [ Person E ] [ Person F ]
Row 2:  [ ??    ] [ Person G ] [ Person H ] [ Person I ]
         (knob 0)  (knob 1)    (knob 2)    (knob 3)
```

- **Column 0** is reserved for navigation: `Down` on row 0, `Exit` on row 1. Row 2 col 0 is
  unassigned for now (leave blank / unused). Knob 3 is also unassigned for now.
- **Columns 1–3** across all three rows are person slots (up to 9 visible at once).
- The person slots fill **bottom-up**: row 2 first, then row 1, then row 0. This keeps the people
  closest to the knobs when there are only a few.
- **Knobs 0–2** control the output volume of whichever person occupies the slot in row 2 at that
  column (cols 1–3 respectively). Turning a knob with nobody in the slot is a no-op.
- **Tapping any person button** mutes or unmutes that person (local volume set to 0 / restored).

### Pagination

The visible window is a 3-row × 3-column grid of person slots. The window rotates through the full
member list in steps of one row (3 people) when the user presses **Down**. The rotation is circular.

Concretely, let the full member list be `[P0, P1, P2, ...]` sorted **alphabetically by display
name** (case-insensitive). Let `offset` be the index of the first person currently shown in row 0
col 1.

| Member count | Visible rows used | Down behavior |
|---|---|---|
| 0–3 | Row 2 only | Down is a no-op |
| 4–6 | Rows 1–2 | Down swaps rows 1 and 2 (offset += 3, mod total) |
| 7–9 | Rows 0–2 | Down shifts everyone up one row; old row 2 wraps to row 0 (offset += 3, mod total) |
| 10+ | All three rows | Same as 7–9: offset += 3, mod total, rotating continuously |

After any Down press, row 2 always contains the "current bottom 3", which are the people whose
volume the knobs control.

### Person button display

Each person button should show:
- The user's **display name** (truncated if needed) as the center label.
- A **mute indicator**: show `assets/mic_off.svg` when locally muted, `assets/mic.svg` when
  unmuted. Use `set_media(media_path=..., size=0.75)` from `ActionBase`.
- If the slot is empty (fewer members than slots), show no media and no label (blank/inactive).

Placeholder assets (Material Design Icons, Apache 2.0):
- `assets/mic.svg` — white microphone, for unmuted state
- `assets/mic_off.svg` — red crossed-out microphone, for muted state
- `assets/person.svg` — white person silhouette, fallback when no Discord avatar is available

### Discord IPC connection

The backend connects to Discord's local IPC socket:
- Linux/Mac: `/tmp/discord-ipc-{n}` (try n = 0..9 until one connects)
- Windows: `\\.\pipe\discord-ipc-{n}`

IPC protocol (little-endian framing):
```
header: [op: uint32][length: uint32]
body:   UTF-8 JSON
```

Opcodes: `HANDSHAKE=0`, `FRAME=1`, `CLOSE=2`, `PING=3`, `PONG=4`

#### Authentication flow (four steps)

Discord RPC requires a full OAuth2 handshake before any commands work. The backend must walk
through this once per session (or reuse a cached access token from a prior session).

**Step 1 — Handshake** (op 0, not a FRAME):
```json
{"v": 1, "client_id": "<CLIENT_ID>"}
```
Discord responds with a READY event.

**Step 2 — AUTHORIZE** (op 1 / FRAME):
```json
{
  "nonce": "<uuid>",
  "cmd": "AUTHORIZE",
  "args": {
    "client_id": "<CLIENT_ID>",
    "scopes": ["rpc", "rpc.voice.read", "identify"]
  }
}
```
Discord shows an approval modal to the user and responds with a one-time `code`.

**Step 3 — Token exchange** (HTTP POST, done by the backend, not over IPC):
```
POST https://discord.com/api/oauth2/token
Content-Type: application/x-www-form-urlencoded

client_id=<CLIENT_ID>&client_secret=<CLIENT_SECRET>
&grant_type=authorization_code&code=<CODE>
```
Response contains `access_token`. Cache this in plugin settings so the user only authorises once.

**Step 4 — AUTHENTICATE** (op 1 / FRAME):
```json
{
  "nonce": "<uuid>",
  "cmd": "AUTHENTICATE",
  "args": {"access_token": "<ACCESS_TOKEN>"}
}
```
On success Discord returns a READY-like payload and the connection is fully authorised.

On subsequent launches, skip steps 2–3 and go straight to step 4 with the cached token. If
AUTHENTICATE fails (token expired), redo steps 2–3 to get a fresh token.

#### Relevant RPC commands (post-auth)

- `GET_SELECTED_VOICE_CHANNEL` — returns the channel the local user is currently in, including a
  `voice_states` list with each member's `user.id`, `user.username`, `nick`, and `voice_state` flags.
- `SET_USER_VOICE_SETTINGS` with `{"user_id": "...", "volume": 0–200}` — sets local output volume
  for a specific user (200 = default 100%, 0 = muted locally).
- `SUBSCRIBE` to `VOICE_STATE_UPDATE` and `VOICE_CONNECTION_STATUS` events to keep the member list
  live without polling.

#### Credentials — what must never be hardcoded

| Field | Where it comes from | Where to store it |
|---|---|---|
| `client_id` | User creates a Discord application at discord.com/developers | Plugin settings (`get_settings`) |
| `client_secret` | Same Discord application, "OAuth2" tab | Plugin settings — treat as sensitive |
| `access_token` | Obtained during auth flow, expires | Plugin settings — cache, refresh on 401 |

No other credentials are needed. The OAuth scopes (`rpc`, `rpc.voice.read`, `identify`) can be
hardcoded as a constant in the backend since they are fixed by this feature's requirements.

### Plugin configuration UI

StreamController exposes a plugin-level settings panel. Override `get_config_rows()` on the
`PluginBase` subclass to return `list[Adw.PreferencesRow]`. Settings persist via
`plugin_base.get_settings()` / `plugin_base.set_settings(settings)` to
`{DATA_PATH}/settings/plugins/net_jesseyoung_betterdiscord/settings.json`.

Required config rows (all in `main.py`'s `BetterDiscord` class):

```python
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw

def get_config_rows(self) -> list:
    self.client_id_row = Adw.EntryRow()
    self.client_id_row.set_title("Discord Application Client ID")

    self.client_secret_row = Adw.PasswordEntryRow()  # hides input
    self.client_secret_row.set_title("Discord Application Client Secret")

    self._load_config_values()

    self.client_id_row.connect("changed", self._on_credentials_changed)
    self.client_secret_row.connect("changed", self._on_credentials_changed)

    return [self.client_id_row, self.client_secret_row]

def _load_config_values(self):
    settings = self.get_settings()
    self.client_id_row.set_text(settings.get("client_id", ""))
    self.client_secret_row.set_text(settings.get("client_secret", ""))

def _on_credentials_changed(self, widget):
    settings = self.get_settings()
    settings["client_id"] = self.client_id_row.get_text()
    settings["client_secret"] = self.client_secret_row.get_text()
    self.set_settings(settings)
```

The cached `access_token` is also stored in settings under the key `"access_token"` but has no
config row — it is written/cleared automatically by the backend auth flow.

### Actions to implement

1. **`actions/ChannelPager/ChannelPager.py`** — a single `ActionBase` subclass that handles all
   person slots. It receives its grid position (row, col) and renders the correct person. On
   `on_key_down` it toggles mute. It subscribes to backend state changes to refresh its display when
   the member list or the pager offset changes.

2. **`actions/PagerDown/PagerDown.py`** — the Down button. On `on_key_down` it calls
   `plugin_base.backend.page_down()`.

3. **`actions/PagerExit/PagerExit.py`** — the Exit button. On `on_key_down` it resets the pager
   offset to 0 and navigates back to the page that was active before the channel pager page was
   opened. Navigation pattern researched from StreamController source:

   ```python
   # Store previous page path before switching to the pager page (call this from wherever
   # you trigger the switch TO the pager — not from Exit itself).
   prev_path = self.deck_controller.active_page.json_path

   # In PagerExit.on_key_down, retrieve and load the stored page:
   import src.backend.GlobalEnv as gl
   prev_page = gl.page_manager.get_page(prev_path, self.deck_controller)
   self.deck_controller.load_page(prev_page)
   ```

   Store `prev_path` in plugin-level settings or as an attribute on `plugin_base` so Exit can
   read it without a hard-coded path.

4. **`backend/backend.py`** — `BackendBase` subclass with:
   - `connect(client_id: str, client_secret: str, access_token: str | None) -> bool`
     Attempts to authenticate using the cached token first; falls back to the full OAuth flow if
     the token is missing or expired. Returns `True` on success.
   - `is_connected() -> bool`
   - `get_fresh_token(client_id: str, client_secret: str) -> str | None`
     Runs steps 2–3 of the auth flow (AUTHORIZE + HTTP token exchange). Returns the new
     `access_token` or `None` on failure. The caller (main.py) must save the token to settings.
   - `get_channel_members() -> list[dict]`  (cached; updated by subscribed events)
   - `page_down() -> None`  (increments offset by 3 mod len(members), wraps)
   - `get_pager_offset() -> int`
   - `set_user_volume(user_id: str, volume: int) -> None`  (0–200)
   - `get_user_volume(user_id: str) -> int`
   - `toggle_mute(user_id: str) -> None`

5. **`main.py`** — `PluginBase` subclass that launches the backend, registers all action holders,
   and drives the auth flow on startup:
   ```python
   settings = self.get_settings()
   ok = self.backend.connect(
       client_id=settings.get("client_id", ""),
       client_secret=settings.get("client_secret", ""),
       access_token=settings.get("access_token"),
   )
   if not ok:
       # Fresh token needed; save it back so future launches skip re-auth
       token = self.backend.get_fresh_token(settings["client_id"], settings["client_secret"])
       if token:
           settings["access_token"] = token
           self.set_settings(settings)
           self.backend.connect(settings["client_id"], settings["client_secret"], token)
   ```

### Open questions / future work

- Knob 3 and display strip col 0 are intentionally unassigned in this iteration.
- Discord user avatars (fetched via CDN) as button images — currently using `person.svg` as
  fallback for all slots.
