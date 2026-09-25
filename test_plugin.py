"""Simulates Seanime's isolated runtimes for the daemon-driver plugin.

Runs the esbuild-compiled payload in one node:vm context, extracts the
captured $ui.register callback, and re-executes ONLY its source in a fresh
context with stubbed Seanime APIs (like Seanime's isolated UI runtime).
Any reference to an outer variable raises ReferenceError here.

Contract under test: the plugin NEVER sends activity itself -- it spawns
ONE daemon via $osExtra.asyncCmd and talks to it only through $TEMP
command files, while status comes back through status files.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = r"""
const vm = require("vm");
const fs = require("fs");

const compiled = fs.readFileSync(process.argv[2], "utf8");
const helperSrc = fs.readFileSync(process.argv[3], "utf8");

function makeStubs(captured) {
    const writes = [];
    const spawns = [];
    const settingsStore = {
        enabled: true, pythonBin: "python3", updateIntervalSec: 15,
        clearOnPause: true, debug: false,
    };
    const trayStub = {
        text: (t) => ({ t }), stack: (items) => items,
        button: (label, props) => ({ label, props }),
        render: (fn) => { captured.renderFn = fn; },
        onClick: () => {}, onOpen: () => {}, onClose: () => {},
        update: () => {}, open: () => {}, close: () => {},
        updateBadge: () => {},
    };
    const ctx = {
        settings: { define: (name, defaults) => ({
            get: (p, fb) => (p in settingsStore ? settingsStore[p] : (fb !== undefined ? fb : defaults[p])),
            set: (a, b) => { if (typeof a === "string") settingsStore[a] = b; else Object.assign(settingsStore, a); },
        })},
        state: (initial) => { let v = initial; return { get: () => v, set: (nv) => { v = (typeof nv === "function") ? nv(v) : nv; } }; },
        fieldRef: () => ({ current: null, setValue() {}, onValueChange() {} }),
        setTimeout: (fn) => { captured.timeouts.push(fn); fn(); return () => {}; },
        setInterval: (fn) => { captured.pollFn = fn; return () => {}; },
        playback: { registerEventListener: (cb) => { captured.playbackCb = cb; return () => {}; } },
        videoCore: {
            addEventListener: (id, cb) => { captured.vcListeners[id] = cb; },
            getPlaybackStatus: () => captured.vcStatus,
            getCurrentPlaybackInfo: () => captured.vcInfo,
            getCurrentMedia: () => captured.vcMedia,
        },
        // NOTE: no ctx.discord on purpose -- the plugin must not use it.
        newTray: () => trayStub,
        registerEventHandler: (name, cb) => { captured.handlers[name] = cb; },
        toast: { success: () => {}, error: () => {}, info: () => {} },
        screen: { loadCurrent: () => {} },
    };
    const sandbox = {
        console, JSON, Math, Date, String, Object, Array, Error,
        $filepath: { join: (...p) => p.join("/") },
        $toBytes: (s) => Buffer.from(s, "utf8"),
        $toString: (b) => Buffer.from(b).toString("utf8"),
        $os: {
            tempDir: () => "/tmp",
            writeFile: (path, data, perm) => { writes.push({ path, data: Buffer.from(data).toString("utf8"), perm }); },
            readFile: (path) => Buffer.from(captured.statusBody),
        },
        $osExtra: {
            asyncCmd: (...args) => {
                spawns.push(args);
                return { run: (cb) => { captured.daemonCb = cb; }, getCommand: () => ({}) };
            },
        },
        $ui: { register: (cb) => { captured.registerCb = cb; } },
    };
    vm.createContext(sandbox);
    return { sandbox, ctx, writes, spawns };
}

function ev(ep, playing) {
    return {
        isVideoStopped: false, isVideoCompleted: false, isStreamStopped: false, isStreamCompleted: false,
        state: { mediaId: 21, mediaTitle: "One Piece", mediaCoverImage: "http://img/x.jpg",
                 mediaTotalEpisodes: 100, episodeNumber: ep, filename: "ep.mkv", completionPercentage: 10 },
        status: { playing: playing, currentTimeInSeconds: 120, durationInSeconds: 1400, filename: "ep.mkv" },
    };
}

function cmdWrites(s) {
    return s.writes.filter((w) => w.path.indexOf("seanime-arrpc-cmd.json") >= 0).map((w) => JSON.parse(w.data));
}

// --- runtime 1: run init(), capture the register callback ---
const cap1 = { handlers: {}, timeouts: [], statusBody: "{}",
    vcListeners: {}, vcStatus: null, vcInfo: null, vcMedia: null, pollFn: null };
let s1 = makeStubs(cap1);
vm.runInContext(compiled + "\ninit();", s1.sandbox);
if (typeof cap1.registerCb !== "function") {
    console.error("FAIL: init() did not call $ui.register");
    process.exit(1);
}

// --- runtime 2 (fresh): re-run ONLY the callback source, like Seanime ---
const cap2 = { handlers: {}, timeouts: [], vcListeners: {}, vcStatus: null, vcInfo: null, vcMedia: null, pollFn: null,
    statusBody: JSON.stringify({ alive: Date.now() / 1000, pid: 999, state: "starting", transport: "", error: "", activity: "" }) };
let s2 = makeStubs(cap2);
const cbSource = "(" + cap1.registerCb.toString() + ")";
let uiFn;
try {
    uiFn = vm.runInContext(cbSource, s2.sandbox);
} catch (e) {
    console.error("FAIL: callback does not compile isolated: " + e);
    process.exit(1);
}
try {
    uiFn(s2.ctx);
} catch (e) {
    console.error("FAIL: isolated callback threw: " + (e && e.stack || e));
    process.exit(1);
}
console.log("PASS isolated UI runtime executes (no outer-scope refs)");

// helper bytes written must equal arrpc_helper.py
const helperWrite = s2.writes.find((w) => w.path.indexOf("seanime-arrpc-helper.py") >= 0);
if (!helperWrite) { console.error("FAIL: helper never written to $TEMP"); process.exit(1); }
if (helperWrite.data !== helperSrc) { console.error("FAIL: embedded helper differs from arrpc_helper.py"); process.exit(1); }
console.log("PASS embedded helper byte-identical to arrpc_helper.py");

// exactly ONE daemon spawn with the right argv
if (s2.spawns.length !== 1) { console.error("FAIL: expected 1 daemon spawn, got " + s2.spawns.length); process.exit(1); }
const sp = s2.spawns[0];
for (const need of ["--daemon", "--dir", "--client-id", "1224777421941899285"]) {
    if (sp.indexOf(need) < 0) { console.error("FAIL: daemon argv missing " + need + ": " + JSON.stringify(sp)); process.exit(1); }
}
if (sp[0] !== "python3") { console.error("FAIL: unexpected binary " + sp[0]); process.exit(1); }
console.log("PASS single daemon spawned (async, --daemon --dir --client-id)");

// episode start -> cmd file {op:set, activity One Piece 1015}
const setsBefore = cmdWrites(s2).filter((c) => c.op === "set").length;
cap2.playbackCb(ev(1015, true));
const sets = cmdWrites(s2).filter((c) => c.op === "set");
if (sets.length !== setsBefore + 1) { console.error("FAIL: no set cmd. writes=" + JSON.stringify(cmdWrites(s2))); process.exit(1); }
const act = sets[sets.length - 1].activity;
if (act.details !== "One Piece" || act.state !== "Watching Episode 1015") {
    console.error("FAIL: bad activity " + JSON.stringify(act)); process.exit(1);
}
console.log("PASS episode start -> daemon cmd set (One Piece Ep 1015)");

// tick, same episode -> throttled (no new cmd)
const nCmds = cmdWrites(s2).length;
cap2.playbackCb(ev(1015, true));
if (cmdWrites(s2).length !== nCmds) { console.error("FAIL: unthrottled duplicate cmd"); process.exit(1); }
console.log("PASS steady-state refresh throttled");

// pause with clearOnPause -> clear cmd
cap2.playbackCb(ev(1015, false));
const cmds = cmdWrites(s2);
if (!cmds.length || cmds[cmds.length - 1].op !== "clear") { console.error("FAIL: pause did not clear"); process.exit(1); }
console.log("PASS pause -> daemon cmd clear");

// stop event -> clear cmd
cap2.playbackCb({ isVideoStopped: true, isVideoCompleted: false, isStreamStopped: false, isStreamCompleted: false });
const cmds2 = cmdWrites(s2);
if (cmds2[cmds2.length - 1].op !== "clear") { console.error("FAIL: stop did not clear"); process.exit(1); }
console.log("PASS stop event -> daemon cmd clear");

// handlers + render with live status
for (const h of ["arrpc-probe", "arrpc-clear", "arrpc-toggle"]) {
    if (typeof cap2.handlers[h] !== "function") { console.error("FAIL: missing handler " + h); process.exit(1); }
}
cap2.statusBody = JSON.stringify({ alive: Date.now() / 1000, pid: 999, state: "ok",
    transport: "ipc:/run/user/1001/discord-ipc-0", error: "", activity: "One Piece Watching Episode 1015" });
const items = cap2.renderFn();
const texts = JSON.stringify(items);
for (const need of ["ok", "ipc:/run/user/1001/discord-ipc-0", "One Piece Watching Episode 1015"]) {
    if (texts.indexOf(need) < 0) { console.error("FAIL: render missing " + need + ": " + texts); process.exit(1); }
}
console.log("PASS tray render shows live daemon status");

// probe handler writes a probe cmd
cap2.handlers["arrpc-probe"]();
const cmds3 = cmdWrites(s2);
if (cmds3[cmds3.length - 1].op !== "probe") { console.error("FAIL: probe handler wrote no probe cmd"); process.exit(1); }
console.log("PASS Test button -> daemon cmd probe");

// --- VideoCore (online streaming; runs before toggle-off disables) ---
for (const evt of ["video-loaded", "video-playback-state", "video-status", "video-paused", "video-resumed",
                   "video-seeked", "video-ended", "video-completed", "video-terminated", "video-error"]) {
    if (typeof cap2.vcListeners[evt] !== "function") { console.error("FAIL: missing videocore listener " + evt); process.exit(1); }
}
console.log("PASS videocore listeners registered");
if (typeof cap2.pollFn !== "function") { console.error("FAIL: videocore poll not scheduled"); process.exit(1); }

function vcMedia() {
    return { id: 21, format: "TV", episodes: 100,
             title: { userPreferred: "One Piece", romaji: "One Piece" },
             coverImage: { large: "http://img/x.jpg" } };
}
cap2.vcStatus = { paused: false, currentTime: 60, duration: 1400 };
const vcSetsBefore = cmdWrites(s2).filter((c) => c.op === "set").length;
cap2.vcListeners["video-loaded"]({ state: { playbackInfo: {
    media: vcMedia(), episode: { episodeNumber: 5 },
    onlinestreamParams: { mediaId: 21, episodeNumber: 5 } } } });
let vcSets = cmdWrites(s2).filter((c) => c.op === "set");
if (vcSets.length !== vcSetsBefore + 1) { console.error("FAIL: video-loaded sent no set"); process.exit(1); }
if (vcSets[vcSets.length - 1].activity.state !== "Watching Episode 5") {
    console.error("FAIL: bad videocore activity " + JSON.stringify(vcSets[vcSets.length - 1].activity)); process.exit(1);
}
console.log("PASS videocore video-loaded -> set (Ep 5)");

// status tick throttled
const nVc = cmdWrites(s2).length;
cap2.vcListeners["video-status"]({ currentTime: 61, duration: 1400, paused: false });
if (cmdWrites(s2).length !== nVc) { console.error("FAIL: videocore tick unthrottled"); process.exit(1); }
console.log("PASS videocore status tick throttled");

// pause -> clear (clearOnPause default true)
cap2.vcListeners["video-paused"]({ currentTime: 62, duration: 1400 });
let vcCmds = cmdWrites(s2);
if (vcCmds[vcCmds.length - 1].op !== "clear") { console.error("FAIL: videocore pause did not clear"); process.exit(1); }
console.log("PASS videocore pause -> clear");

// poll with live getters reports; poll with nothing clears
cap2.vcInfo = { media: vcMedia(), episode: { episodeNumber: 6 },
                onlinestreamParams: { mediaId: 21, episodeNumber: 6 } };
cap2.vcStatus = { paused: false, currentTime: 10, duration: 1400 };
cap2.pollFn();
vcSets = cmdWrites(s2).filter((c) => c.op === "set");
if (vcSets[vcSets.length - 1].activity.state !== "Watching Episode 6") {
    console.error("FAIL: videocore poll sent no set"); process.exit(1);
}
console.log("PASS videocore poll -> set (Ep 6)");
cap2.vcInfo = null; cap2.vcMedia = null;
cap2.pollFn();
vcCmds = cmdWrites(s2);
if (vcCmds[vcCmds.length - 1].op !== "clear") { console.error("FAIL: idle poll did not clear"); process.exit(1); }
console.log("PASS idle poll clears stale presence");

// toggle off writes exit cmd (runs last: it disables the plugin)
cap2.handlers["arrpc-toggle"]();
const cmds4 = cmdWrites(s2);
if (cmds4[cmds4.length - 1].op !== "exit") { console.error("FAIL: toggle-off wrote no exit cmd"); process.exit(1); }
console.log("PASS Disable -> daemon cmd exit");
console.log("ALL PLUGIN ISOLATION TESTS PASSED");
"""


def main():
    compiled = "/tmp/compiled.js"
    r = subprocess.run(
        ["npx", "-y", "esbuild", os.path.join(HERE, "arrpc-bridge.ts"),
         "--loader:.ts=ts", "--target=es2018", "--outfile=" + compiled,
         "--log-level=error"],
        capture_output=True, text=True, timeout=120, cwd=HERE)
    if r.returncode != 0:
        print("esbuild failed:\n" + r.stderr)
        return 1
    harness_path = os.path.join(HERE, ".iso_harness.js")
    with open(harness_path, "w", encoding="utf-8") as f:
        f.write(HARNESS)
    try:
        r = subprocess.run(
            ["node", harness_path, compiled, os.path.join(HERE, "arrpc_helper.py")],
            capture_output=True, text=True, timeout=60, cwd=HERE)
        print(r.stdout, end="")
        print(r.stderr, end="", file=sys.stderr)
        return r.returncode
    finally:
        try:
            os.unlink(harness_path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
