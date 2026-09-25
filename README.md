# Extended Seanime arRPC

A Seanime plugin for Discord Rich Presence in custom clients like
**[Equibop](https://github.com/Equicord/Equibop)** (and Vesktop, ArmCord, …).

**Zero setup: install, grant permissions, done.** No terminal commands, no
symlinks, no extra daemons to run — the plugin brings its own companion that
finds Discord/arRPC sockets itself, including Flatpak sandbox sockets that
Seanime's built-in client never looks at.

## How it works

- On load, the plugin spawns a small persistent companion (the embedded
  stdlib-only Python helper in `--daemon` mode, via a non-blocking async
  command — the UI never freezes).
- The daemon searches every IPC location like dedicated RPC libraries do
  (e.g. LiquidBounce's `DiscordIpcPipeLocator` checks `$XDG_RUNTIME_DIR`,
  `$TMPDIR`, `/tmp` plus `snap.discord` / `app/com.discordapp.Discord`
  sandbox subdirs) — **and goes one further**, globbing
  `/run/user/<uid>/.flatpak/*/xdg-run`, which is where Equibop/Vesktop
  Flatpak sockets actually live on the host but which those libraries miss.
- It holds **one** connection open (presence requires persistence — a
  fire-and-forget send is cleared by the server the moment its socket
  closes) and applies playback states the plugin drops into a `$TEMP`
  command file. Takeover protection, auto-reconnect, and error reporting
  are built in; live state is mirrored to a `$TEMP` status file the tray
  reads.
- The plugin listens to **all** playback events (local files,
  torrent/debrid/online streams, manual tracking) and forwards them.

It uses Seanime's own Discord application ID (`1224777421941899285`), so
artwork resolves exactly like native presence.

## Requirements

- Seanime server (Denshi desktop app or standalone) on the **same machine**
  as the Discord client — Rich Presence is a local-machine protocol.
- `python3` on the Seanime server machine (stdlib only, no pip packages).
  Windows users: set the plugin's `pythonBin` setting to `python` or `py`.
- A Discord client with IPC enabled and visible to the server user
  (Equibop/Vesktop native or Flatpak, official Discord — all auto-detected).

## Install

1. In Seanime: **Extensions → Add extension**, paste the manifest URL:
   `https://raw.githubusercontent.com/lustful-suicide/extended-seanime-arrpc/main/extended-seanime-arrpc.json`
2. Grant the requested permissions: `playback`, `system`, `storage`.
   The `system` scope spawns the bundled companion as
   `python3 <temp>/seanime-arrpc-helper.py --daemon ...` — if Extension
   Secure Mode prompts you, approve it (and remember the choice).
3. Open the plugin tray (**arRPC bridge**) and press **Test connection**.
   - `Status: ok (ipc:...)` / `ok (websocket:...)` → play an episode,
     presence appears within seconds.
   - `Status: error` → the tray shows why; see Troubleshooting.

That's it. You can turn Seanime's built-in Discord Rich Presence off to
avoid a second writer, or leave it on — the plugin owns anime playback
either way. (Manga presence stays on the built-in path.)

## Settings

| Key | Default | What it does |
| --- | ------- | ------------ |
| `enabled` | `true` | Master switch (tray button too; disabling clears presence and stops the companion). |
| `pythonBin` | `python3` | Python binary used to spawn the companion. |
| `updateIntervalSec` | `15` | Re-push progress at most this often while the episode/track is unchanged (episode changes and play/pause always push immediately). |
| `clearOnPause` | `true` | `true`: clear presence on pause/stop. `false`: leave the last state up. |
| `debug` | `false` | Verbose `[arrpc]` logging (including companion output) to the Seanime console. |

## Troubleshooting

- **No status, tray says `error`**: press **Test connection** and read the
  error line — it lists what was tried (regular socket dirs, Flatpak sandbox
  dirs, `:6463–6472`). Start (or unlock) the Discord client so its socket
  exists, then test again.
- **Works for local files but not streams (or vice versa)**: the plugin
  tracks everything Seanime reports via playback events (local,
  torrent/debrid/online streams, manual tracking). If an event never fires
  for some player, nothing can forward it — check Seanime's playback state
  first.
- **`python3` not found / permission denied**: install Python 3 on the server
  machine, or point `pythonBin` at the right binary; approve the Secure Mode
  prompt if shown.
- **Equibop shows nothing even though Test says `ok`**: in Discord/Equibop
  check **Activity Privacy → Share your detected activities**; if Streamer
  Mode is on, check it isn't hiding the status.
- **Two Seanime instances**: the companions coordinate — the second one sees
  the first's heartbeat and exits, so exactly one holds presence.

## Development

- `plugin.src.ts` — plugin source (edit this).
- `arrpc_helper.py` — companion source of truth (stdlib only: one-shot
  send/probe + `--daemon` mode).
- `build.py` — embeds the helper into `plugin.src.ts` → `arrpc-bridge.ts`
  (the published payload). Run after every edit: `python3 build.py`.
- `test_helper.py` — one-shot/probe tests against fake arRPC servers.
- `test_daemon.py` — daemon protocol tests (set/clear/probe/exit,
  takeover, reconnect) with a real subprocess.
- `test_plugin.py` — runs the compiled payload in isolated `node:vm`
  contexts like Seanime's runtimes and drives playback/tray handlers.
- `extended-seanime-arrpc.json` — extension manifest (`id` matches filename).
