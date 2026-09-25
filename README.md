# Extended Seanime arRPC

A Seanime plugin that forwards playback to a local **arRPC** server, so Discord
Rich Presence shows up in custom clients like **[Equibop](https://github.com/Equicord/Equibop)**
(and Vesktop, ArmCord, etc.).

## Why this exists

Seanime's built-in Discord RPC only tries the single IPC path
`$XDG_RUNTIME_DIR/discord-ipc-0`. When the server and the Discord client
disagree about socket locations — Flatpak sandboxing, systemd env differences,
Docker, or another app already holding `discord-ipc-0` — presence silently
never appears, with no retry or status anywhere.

This plugin ships a tiny **stdlib-only Python helper** (`arrpc_helper.py`,
embedded in the plugin file) that tries *every* transport arRPC understands:

1. Discord WebSocket RPC on `127.0.0.1:6463–6472`
2. Unix sockets `discord-ipc-0..9` in `$XDG_RUNTIME_DIR`, `/run/user/<uid>`,
   `$TMPDIR`, `/tmp`

…plus a tray widget with live status, a **Test connection** button, and settings.
It uses Seanime's own Discord application ID (`1224777421941899285`), so
artwork and buttons resolve exactly like native presence.

## Requirements

- Seanime server (Denshi desktop app or standalone) on the **same machine**
  as the Discord client — Rich Presence is a local-machine protocol.
- `python3` on the machine running the Seanime server (stdlib only, no pip
  packages). Windows users: set the plugin's `pythonBin` setting to
  `python` or `py`.
- An arRPC endpoint. One of:
  - **Equibop built-in arRPC**: Equibop Settings → enable **Rich Presence
    (arRPC)**. (Flatpak note below.)
  - **Standalone server**: `npx arrpc` or `bunx arrpc-bun` (needs Node ≥ 18
    or Bun). Recommended if the built-in one misbehaves.

## Install

1. In Seanime: **Extensions → Add extension**, paste the manifest URL:
   `https://raw.githubusercontent.com/lustful-suicide/extended-seanime-arrpc/main/extended-seanime-arrpc.json`
   (or drop `extended-seanime-arrpc.json` into your [extensions
   directory](https://seanime.rahim.app/docs/config#data-directory)).
2. Grant the requested permissions: `playback`, `system`, `storage`.
   The `system` scope runs the bundled sender as
   `python3 <temp>/seanime-arrpc-helper.py ...` — if Extension Secure Mode
   prompts you, approve it (and remember the choice).
3. Open the plugin tray (**arRPC bridge**) and press **Test connection**.
   - `Status: ok (websocket)` / `ok (ipc)` → start an episode, presence
     appears in Equibop within seconds.
   - `Status: error` → see Troubleshooting.
4. Recommended: turn **off** Seanime's built-in *anime* Rich Presence
   (Settings → Discord) so the two don't fight over the same status.
   Manga presence can stay on the built-in path.

## Equibop + Flatpak notes

Equibop's Flatpak sandbox hides its `discord-ipc-*` sockets from the host, so
host apps (including Seanime Denshi) can't use IPC to reach its bundled arRPC.
Worse, I verified live that the bundled `arrpc-bun` accepts a WebSocket
upgrade on `:6463` and then dies (the process exits; a new PID appears on the
next check). External apps cannot use it at all.
If **Test connection** fails with everything enabled, use a standalone server:

- **Option A — standalone arRPC on the host** (most reliable):
  1. In Equibop settings, turn **off** its built-in Rich Presence (arRPC).
     This stops the bundled server and frees ports `6463`/`1337`.
  2. Run a standalone server on the host and keep it running:
     `npx arrpc` (needs Node ≥ 18) or `bunx arrpc-bun`.
  3. In Equibop, enable the **WebRichPresence (arRPC)** plugin so it displays
     what the host server receives. The plugin is hidden on Vesktop-based
     clients — if you don't see it, close Equibop and edit
     `~/.var/app/org.equicord.equibop/config/equibop/settings/settings.json`,
     adding `"WebRichPresence (arRPC)": true` under `plugins`, then restart.
     It only connects to `ws://127.0.0.1:1337`, which is why step 1 matters.
  4. Back in Seanime, press **Test connection** — expect `ok (websocket)`
     or `ok (ipc)`. Play an episode; presence appears within seconds.
  The plugin remembers the working transport and, after a websocket failure,
  sticks to IPC until the next manual probe, so a broken bundled endpoint
  can't flap your status.
- **Option B — expose the sandbox socket**: follow Equibop's Flatpak wiki
  (`discord-ipc-0` tmpfiles symlink) so the sandbox socket appears at
  `/run/user/$UID/discord-ipc-0` on the host. Then IPC works directly.

## Settings

| Key | Default | What it does |
| --- | ------- | ------------ |
| `enabled` | `true` | Master switch (tray button too). |
| `pythonBin` | `python3` | Python binary used to run the sender. |
| `updateIntervalSec` | `15` | Re-send presence at most this often while the episode/track is unchanged (episode changes and play/pause always send immediately). |
| `showButtons` | `true` | Include the “Seanime” button. |
| `showTimestamps` | `true` | Include start/end timestamps (progress bar). |
| `pauseBehaviour` | `show-paused` | `show-paused` keeps a “Paused – …” status; `clear` hides presence while paused (native-like). |
| `debug` | `false` | Verbose `[arrpc]` logging to the Seanime console. |

## Troubleshooting

- **No status, tray says `error`**: press **Test connection** and read the
  error line. `websocket failed (...)` on every port + `ipc failed (no
  sockets tried)` means no arRPC is reachable — complete the Requirements /
  Flatpak steps above, then test again.
- **Works for local files but not streams (or vice versa)**: the plugin tracks
  everything Seanime reports via playback events (local, torrent/debrid/online
  streams, manual tracking). If an event never fires for some player, nothing
  can forward it — check Seanime's playback state first.
- **Presence flaps between two states**: another app (or Seanime's built-in
  anime RPC) is writing the same application ID. Disable one writer.
- **`python3` not found / permission denied**: install Python 3 on the server
  machine, or point `pythonBin` at the right binary; approve the Secure Mode
  prompt if shown.
- **Equibop shows nothing even though Test says `ok`**: in Discord/Equibop
  check **Activity Privacy → Share your detected activities**; Vencord
  streamers: check Streamer Mode isn't hiding it.

## Development

- `plugin.src.ts` — plugin source (edit this).
- `arrpc_helper.py` — sender source of truth (stdlib only).
- `build.py` — embeds the helper into `plugin.src.ts` → `arrpc-bridge.ts`
  (the published payload). Run after every edit: `python3 build.py`.
- `test_helper.py` — helper self-tests against fake arRPC servers:
  `python3 test_helper.py`.
- `test_plugin.py` — runs the compiled payload in isolated `node:vm`
  contexts like Seanime's runtimes, drives playback/tray handlers:
  `python3 test_plugin.py`.
- `node --check arrpc-bridge.ts` — payload syntax check.
- `extended-seanime-arrpc.json` — extension manifest (`id` matches filename).
