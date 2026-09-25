/// <reference path="./plugin.d.ts" />
/// <reference path="./system.d.ts" />
/// <reference path="./app.d.ts" />
/// <reference path="./core.d.ts" />

/**
 * Extended Seanime arRPC bridge - Seanime plugin payload (source).
 *
 * Tracks ctx.playback (external players) and ctx.videoCore (built-in Denshi
 * player + online streaming), and forwards states to a companion daemon
 * (embedded Python helper, --daemon mode) that holds the single Discord
 * IPC/WebSocket connection. Files in $TEMP link the two sides:
 *   seanime-arrpc-cmd.json    {"op": "set|clear|probe|exit", "activity": {...}|null, "nonce": N}
 *   seanime-arrpc-status.json {"alive": epoch, "pid": N, "state": ..., "transport": ..., "error": ..., "activity": ...}
 *
 * Generated file arrpc-bridge.ts is built by build.py -- edit here.
 */

function init() {
    // Everything the $ui callback needs must be declared inside it:
    // each Seanime runtime is isolated and cannot see outer scope.
    $ui.register(function (ctx) {
        // Same application ID as Seanime's native presence, so artwork
        // resolves identically in Discord clients.
        var CLIENT_ID = "1224777421941899285";
        // Replaced by build.py with the JSON-escaped contents of arrpc_helper.py
        var HELPER_PY = "__ARRPC_HELPER_PY__";

        var CMD_FILENAME = "seanime-arrpc-cmd.json";
        var STATUS_FILENAME = "seanime-arrpc-status.json";

        var settings = ctx.settings.define("arrpc", {
            enabled: true,
            pythonBin: "python3",
            updateIntervalSec: 15,
            // true: clear presence on pause/stop. false: leave last state up.
            clearOnPause: true,
            debug: false,
        });

        var connStatus = ctx.state("starting");
        var lastError = ctx.state("");
        var lastTransport = ctx.state("");
        var nowPlaying = ctx.state("Nothing playing");

        var lastKey = "";
        var lastSentAt = 0;
        var lastPlayingFlag = null;
        var cmdNonce = 0;
        var daemonStarted = false;

        function log(msg) {
            try {
                if (settings.get("debug")) console.log("[arrpc] " + msg);
            } catch (e) { /* ignore */ }
        }

        function workDir() {
            return $os.tempDir();
        }

        function helperPath() {
            return $filepath.join(workDir(), "seanime-arrpc-helper.py");
        }

        function cmdPath() {
            return $filepath.join(workDir(), CMD_FILENAME);
        }

        function statusPath() {
            return $filepath.join(workDir(), STATUS_FILENAME);
        }

        function ensureHelper() {
            try {
                // 493 == 0o755
                $os.writeFile(helperPath(), $toBytes(HELPER_PY), 493);
                return true;
            } catch (e) {
                connStatus.set("error");
                lastError.set("cannot write helper: " + (e && e.message ? e.message : e));
                return false;
            }
        }

        function ensureDaemon() {
            if (daemonStarted) return true;
            if (!ensureHelper()) return false;
            try {
                var bin = settings.get("pythonBin") || "python3";
                var acmd = $osExtra.asyncCmd(bin, helperPath(), "--daemon", "--dir", workDir(), "--client-id", CLIENT_ID);
                acmd.run(function (data, err, exitCode, signal) {
                    if (data) log("daemon: " + $toString(data));
                    if (err) log("daemon stderr: " + $toString(err));
                    if (exitCode !== undefined && exitCode !== null) {
                        log("daemon exited (" + exitCode + ")");
                        // Takeover-exits (code 0, another instance alive)
                        // keep the flag: a holder exists. Crashes re-arm.
                        if (exitCode !== 0) daemonStarted = false;
                    }
                });
                daemonStarted = true;
                log("daemon spawned");
                return true;
            } catch (e) {
                connStatus.set("error");
                lastError.set("cannot start daemon: " + (e && e.message ? e.message : e));
                return false;
            }
        }

        function sendCmd(op, activity) {
            // Teardown ops always go through (e.g. toggle-off sends "exit"
            // after flipping the switch); only new presence is gated.
            if (op === "set" && !settings.get("enabled")) return false;
            if (!ensureDaemon()) return false;
            try {
                cmdNonce++;
                var body = JSON.stringify({ op: op, activity: activity === undefined ? null : activity, nonce: cmdNonce });
                // 420 == 0o644
                $os.writeFile(cmdPath(), $toBytes(body), 420);
                return true;
            } catch (e) {
                lastError.set("cannot write command: " + (e && e.message ? e.message : e));
                connStatus.set("error");
                return false;
            }
        }

        function readStatus() {
            try {
                var raw = $toString($os.readFile(statusPath()));
                var st = JSON.parse(raw);
                if (st && typeof st === "object") return st;
                return null;
            } catch (e) {
                return null;
            }
        }

        function refreshFromStatus() {
            var st = readStatus();
            if (!st) return;
            if (st.transport) lastTransport.set(st.transport);
            if (st.state === "ok") {
                connStatus.set("ok");
                lastError.set("");
            } else if (st.state === "error") {
                connStatus.set("error");
                lastError.set(st.error || "daemon error");
            } else {
                connStatus.set(st.state || "starting");
            }
        }

        // Handshake-only probe through the daemon. Never touches display.
        function probe(manual) {
            if (!sendCmd("probe", null)) {
                if (manual) ctx.toast.error("arRPC: daemon not running");
                return;
            }
            connStatus.set("sending");
            ctx.setTimeout(function () {
                refreshFromStatus();
                var st = readStatus();
                if (manual) {
                    if (st && st.state === "ok") ctx.toast.success("arRPC reachable (" + (st.transport || "?") + ")");
                    else ctx.toast.error("arRPC not reachable. See plugin status.");
                }
            }, 3000);
        }

        function clearPresence(reason) {
            log("clear (" + reason + ")");
            sendCmd("clear", null);
            nowPlaying.set("Nothing playing");
            lastKey = "";
            lastSentAt = 0;
            lastPlayingFlag = null;
        }

        function pickTitle(media) {
            if (!media || !media.title) return "Unknown";
            return media.title.userPreferred || media.title.romaji || media.title.english || media.title.native || "Unknown";
        }

        function pickCover(media) {
            if (!media || !media.coverImage) return "";
            return media.coverImage.extraLarge || media.coverImage.large || media.coverImage.medium || "";
        }

        function safeCall(fn) {
            try {
                return fn();
            } catch (e) {
                return undefined;
            }
        }

        function pushNormalized(mediaId, title, cover, episode, totalEp, isMovie, progress, duration, playing, force) {
            if (!mediaId) return;
            var key = mediaId + ":" + episode;
            var now = Date.now();
            var intervalMs = Math.max(5, settings.get("updateIntervalSec") || 15) * 1000;
            var changed = !!force || (key !== lastKey) || (playing !== lastPlayingFlag);
            if (!changed && (now - lastSentAt) < intervalMs) return;
            var activity = {
                name: "Seanime",
                details: title || "Unknown",
                details_url: "https://anilist.co/anime/" + mediaId,
                state: isMovie ? "Watching Movie" : ("Watching Episode " + (episode || 0)),
                assets: {
                    large_image: cover || "",
                    large_text: title || "Unknown",
                    large_url: "https://anilist.co/anime/" + mediaId,
                },
                buttons: [{ label: "Seanime", url: "https://seanime.app" }],
                instance: true,
                type: 3,
                status_display_type: 2,
            };
            if (duration > 0) {
                var nowSec = Math.floor(now / 1000);
                activity.timestamps = {
                    start: nowSec - Math.floor(progress),
                    end: nowSec + Math.floor(Math.max(0, duration - progress)),
                };
            } else {
                activity.timestamps = { start: Math.floor(now / 1000) };
            }
            if (sendCmd("set", activity)) {
                lastKey = key;
                lastSentAt = now;
                lastPlayingFlag = playing;
                nowPlaying.set((title || "Unknown") + " - Ep " + (episode || 0) + (playing ? "" : " (paused)"));
                if (connStatus.get() !== "ok") connStatus.set("sending");
            }
        }

        function pushPlayback(st, so, playing) {
            var totalEp = st.mediaTotalEpisodes || 0;
            pushNormalized(st.mediaId, st.mediaTitle || "Unknown", st.mediaCoverImage || "", st.episodeNumber || 0,
                totalEp, totalEp === 1, so.currentTimeInSeconds || 0, so.durationInSeconds || 0, playing, false);
        }

        ctx.playback.registerEventListener(function (ev) {
            try {
                if (!settings.get("enabled")) return;
                if (ev.isVideoStopped || ev.isVideoCompleted || ev.isStreamStopped || ev.isStreamCompleted) {
                    if (settings.get("clearOnPause")) clearPresence("stopped");
                    else {
                        lastKey = "";
                        lastSentAt = 0;
                        lastPlayingFlag = null;
                    }
                    return;
                }
                if (!ev.state || !ev.status) return;
                var st = ev.state;
                var so = ev.status;
                if (!st.mediaId) return;
                var playing = !!so.playing;
                if (!playing && settings.get("clearOnPause")) {
                    clearPresence("paused");
                    lastKey = st.mediaId + ":" + st.episodeNumber;
                    lastPlayingFlag = playing;
                    lastSentAt = Date.now();
                    return;
                }
                pushPlayback(st, so, playing);
            } catch (e) {
                console.error("[arrpc] listener error: " + String((e && e.message) || e));
            }
        });

        // ---- VideoCore: built-in Denshi player + online streaming web player.
        // ctx.playback only covers external desktop players (MPV/VLC/...),
        // so online streaming would otherwise never report anything.
        var vcInfo = null; // {mediaId, title, cover, episode, totalEp, isMovie}

        function vcFromPlaybackInfo(info) {
            if (!info) return null;
            var media = info.media || null;
            var mediaId = (media && media.id) || (info.onlinestreamParams && info.onlinestreamParams.mediaId) || 0;
            if (!mediaId) return null;
            var episode = 0;
            if (info.episode && typeof info.episode.episodeNumber === "number") episode = info.episode.episodeNumber;
            else if (info.onlinestreamParams && typeof info.onlinestreamParams.episodeNumber === "number") episode = info.onlinestreamParams.episodeNumber;
            else if (vcInfo && vcInfo.mediaId === mediaId) episode = vcInfo.episode;
            var totalEp = (media && media.episodes) || 0;
            return {
                mediaId: mediaId,
                title: pickTitle(media),
                cover: pickCover(media),
                episode: episode,
                totalEp: totalEp,
                isMovie: !!media && media.format === "MOVIE",
            };
        }

        function vcStatus() {
            // All sync getters; may throw when idle -- safeCall guards.
            var status = safeCall(function () { return ctx.videoCore.getPlaybackStatus(); });
            return status || null;
        }

        function vcReportFromEvent(progress, duration, playing, force) {
            if (!vcInfo) vcPoll(true);
            if (!vcInfo) return;
            if (!playing && settings.get("clearOnPause")) {
                clearPresence("paused");
                lastKey = vcInfo.mediaId + ":" + vcInfo.episode;
                lastPlayingFlag = playing;
                lastSentAt = Date.now();
                return;
            }
            pushNormalized(vcInfo.mediaId, vcInfo.title, vcInfo.cover, vcInfo.episode, vcInfo.totalEp, vcInfo.isMovie,
                progress || 0, duration || 0, playing, force);
        }

        function vcClear() {
            vcInfo = null;
            clearPresence("videocore-stopped");
        }

        // Re-read VideoCore state (poll + cache refresh). Returns true if media present.
        function vcPoll(quiet) {
            if (!ctx.videoCore) return false;
            var info = safeCall(function () { return ctx.videoCore.getCurrentPlaybackInfo(); });
            var parsed = vcFromPlaybackInfo(info);
            if (parsed) {
                vcInfo = parsed;
                if (!quiet) {
                    var status = vcStatus();
                    var playing = status ? !status.paused : true;
                    if (!playing && settings.get("clearOnPause")) {
                        clearPresence("paused");
                        lastKey = parsed.mediaId + ":" + parsed.episode;
                        lastPlayingFlag = playing;
                        lastSentAt = Date.now();
                    } else {
                        pushNormalized(parsed.mediaId, parsed.title, parsed.cover, parsed.episode, parsed.totalEp, parsed.isMovie,
                            status ? (status.currentTime || 0) : 0, status ? (status.duration || 0) : 0, playing, false);
                    }
                }
                return true;
            }
            return false;
        }

        if (ctx.videoCore && ctx.videoCore.addEventListener) {
            try {
                ctx.videoCore.addEventListener("video-loaded", function (ev) {
                    if (!settings.get("enabled")) return;
                    var parsed = vcFromPlaybackInfo(ev && ev.state && ev.state.playbackInfo);
                    if (parsed) vcInfo = parsed;
                    var status = vcStatus();
                    vcReportFromEvent(status ? status.currentTime : 0, status ? status.duration : 0, status ? !status.paused : true, true);
                });
                ctx.videoCore.addEventListener("video-playback-state", function (ev) {
                    if (!settings.get("enabled")) return;
                    var parsed = vcFromPlaybackInfo(ev && ev.state && ev.state.playbackInfo);
                    if (parsed) vcInfo = parsed;
                    var status = vcStatus();
                    vcReportFromEvent(status ? status.currentTime : 0, status ? status.duration : 0, status ? !status.paused : true, false);
                });
                ctx.videoCore.addEventListener("video-status", function (ev) {
                    if (!settings.get("enabled")) return;
                    vcReportFromEvent(ev ? ev.currentTime : 0, ev ? ev.duration : 0, ev ? !ev.paused : true, false);
                });
                ctx.videoCore.addEventListener("video-paused", function (ev) {
                    if (!settings.get("enabled")) return;
                    vcReportFromEvent(ev ? ev.currentTime : 0, ev ? ev.duration : 0, false, true);
                });
                ctx.videoCore.addEventListener("video-resumed", function (ev) {
                    if (!settings.get("enabled")) return;
                    vcReportFromEvent(ev ? ev.currentTime : 0, ev ? ev.duration : 0, true, true);
                });
                ctx.videoCore.addEventListener("video-seeked", function (ev) {
                    if (!settings.get("enabled")) return;
                    vcReportFromEvent(ev ? ev.currentTime : 0, ev ? ev.duration : 0, ev ? !ev.paused : true, true);
                });
                ctx.videoCore.addEventListener("video-ended", function () {
                    if (!settings.get("enabled")) return;
                    vcClear();
                });
                ctx.videoCore.addEventListener("video-completed", function () {
                    if (!settings.get("enabled")) return;
                    vcClear();
                });
                ctx.videoCore.addEventListener("video-terminated", function () {
                    if (!settings.get("enabled")) return;
                    vcClear();
                });
                ctx.videoCore.addEventListener("video-error", function () {
                    if (!settings.get("enabled")) return;
                    vcClear();
                });
                log("videocore listeners registered");
            } catch (e) {
                log("videocore unavailable: " + String((e && e.message) || e));
            }
            // Steady poll: covers missed events and re-derives state.
            if (ctx.setInterval) {
                ctx.setInterval(function () {
                    try {
                        if (!settings.get("enabled")) return;
                        if (!vcPoll(false) && vcInfo) vcClear();
                    } catch (e) {
                        log("videocore poll error: " + String((e && e.message) || e));
                    }
                }, 10000);
            }
        }

        var tray = ctx.newTray({
            tooltipText: "arRPC bridge",
            iconUrl: "https://seanime.rahim.app/logo_2.png",
            withContent: true,
        });

        ctx.registerEventHandler("arrpc-probe", function () {
            probe(true);
        });
        ctx.registerEventHandler("arrpc-clear", function () {
            clearPresence("manual");
            ctx.toast.info("arRPC presence cleared");
        });
        ctx.registerEventHandler("arrpc-toggle", function () {
            var next = !settings.get("enabled");
            settings.set("enabled", next);
            if (!next) {
                sendCmd("exit", null);
                daemonStarted = false;
                nowPlaying.set("Nothing playing");
                connStatus.set("disabled");
            } else {
                connStatus.set("ready");
                ensureDaemon();
                probe(false);
            }
            ctx.toast.info(next ? "arRPC enabled" : "arRPC disabled");
        });

        tray.render(function () {
            // Live daemon state on every render; states trigger re-renders.
            refreshFromStatus();
            var statusLine = "Status: " + connStatus.get();
            if (lastTransport.get() && connStatus.get() === "ok") statusLine += " (" + lastTransport.get() + ")";
            var st = readStatus();
            var nowText = nowPlaying.get();
            if (st && st.activity) nowText = st.activity;
            var items = [
                tray.text("Seanime -> arRPC bridge"),
                tray.text(statusLine),
                tray.text("Now: " + nowText),
            ];
            if (lastError.get() && connStatus.get() === "error") {
                items.push(tray.text("Error: " + lastError.get().substring(0, 160)));
                items.push(tray.text("Start Equibop (or any arRPC), then Test."));
            }
            items.push(tray.button(settings.get("enabled") ? "Disable" : "Enable", { onClick: "arrpc-toggle" }));
            items.push(tray.button("Test connection", { onClick: "arrpc-probe" }));
            items.push(tray.button("Clear presence", { onClick: "arrpc-clear" }));
            return tray.stack(items);
        });

        if (!settings.get("enabled")) {
            connStatus.set("disabled");
        } else {
            ensureDaemon();
            probe(false);
        }
    });
}
// Seanime runs payloads as plain scripts: no import/export statements.
