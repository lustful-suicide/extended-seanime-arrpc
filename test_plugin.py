"""Simulates Seanime's isolated runtimes for the plugin payload.

Seanime compiles init() in one runtime, captures the $ui.register callback,
and re-runs it in a SEPARATE UI runtime where outer-scope variables do NOT
exist. This test reproduces that: it runs the esbuild-compiled payload in
one node:vm context, extracts the captured callback source, and executes it
in a fresh context with stubbed Seanime APIs. Any reference to an outer
variable (like the old HELPER_PY bug) raises ReferenceError here.

It then drives the plugin: probe(), a playback event, tray render, and the
registered event handlers -- asserting the helper bytes written equal
arrpc_helper.py and that --activity/--clear invocations happen.
"""
import json
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
    const cmds = [];
    const settingsStore = {
        enabled: true, pythonBin: "python3", updateIntervalSec: 15,
        showButtons: true, showTimestamps: true,
        pauseBehaviour: "show-paused", debug: false,
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
        playback: { registerEventListener: (cb) => { captured.playbackCb = cb; return () => {}; } },
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
            cmd: (...args) => {
                cmds.push(args);
                return { output: () => (captured.failCmd
                    ? "ERR FAILED: websocket: port 6463: timed out | ipc: no sockets tried"
                    : "OK websocket:127.0.0.1:6463") };
            },
        },
        $ui: { register: (cb) => { captured.registerCb = cb; } },
    };
    vm.createContext(sandbox);
    return { sandbox, ctx, writes, cmds };
}

// --- runtime 1: run init(), capture the register callback ---
const cap1 = { handlers: {} };
let s1 = makeStubs(cap1);
vm.runInContext(compiled + "\ninit();", s1.sandbox);
if (typeof cap1.registerCb !== "function") {
    console.error("FAIL: init() did not call $ui.register");
    process.exit(1);
}

// --- runtime 2 (fresh): re-run ONLY the callback source, like Seanime ---
const cap2 = { handlers: {} };
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

// drive a playback event -> expect an --activity send via python3
s2.cmds.length = 0;
cap2.playbackCb({
    isVideoStopped: false, isVideoCompleted: false, isStreamStopped: false, isStreamCompleted: false,
    state: { mediaId: 21, mediaTitle: "One Piece", mediaCoverImage: "http://img/x.jpg",
             mediaTotalEpisodes: 100, episodeNumber: 1015, filename: "ep.mkv", completionPercentage: 10 },
    status: { playing: true, currentTimeInSeconds: 120, durationInSeconds: 1400, filename: "ep.mkv" },
});
const activityCmd = s2.cmds.find((c) => c.indexOf("--activity") >= 0);
if (!activityCmd) { console.error("FAIL: playback event sent no --activity. cmds=" + JSON.stringify(s2.cmds)); process.exit(1); }
if (activityCmd[0] !== "python3") { console.error("FAIL: unexpected binary " + activityCmd[0]); process.exit(1); }
const act = JSON.parse(activityCmd[activityCmd.indexOf("--activity") + 1]);
if (act.details !== "One Piece" || act.state !== "Watching Episode 1015") {
    console.error("FAIL: bad activity " + JSON.stringify(act)); process.exit(1);
}
console.log("PASS playback event -> SET_ACTIVITY (One Piece Ep 1015)");

// stop event -> expect --clear
s2.cmds.length = 0;
cap2.playbackCb({ isVideoStopped: true, isVideoCompleted: false, isStreamStopped: false, isStreamCompleted: false });
if (!s2.cmds.find((c) => c.indexOf("--clear") >= 0)) { console.error("FAIL: stop event sent no --clear"); process.exit(1); }
console.log("PASS stop event -> clear");

// handlers registered?
for (const h of ["arrpc-probe", "arrpc-clear", "arrpc-toggle"]) {
    if (typeof cap2.handlers[h] !== "function") { console.error("FAIL: missing handler " + h); process.exit(1); }
}
cap2.handlers["arrpc-probe"]();
cap2.handlers["arrpc-clear"]();
if (typeof cap2.renderFn !== "function") { console.error("FAIL: tray render fn missing"); process.exit(1); }
cap2.renderFn();
console.log("PASS tray handlers + render execute");

// failing transport: ERR surfaces in tray, next send skips websocket
cap2.failCmd = true;
s2.cmds.length = 0;
cap2.playbackCb({
    isVideoStopped: false, isVideoCompleted: false, isStreamStopped: false, isStreamCompleted: false,
    state: { mediaId: 21, mediaTitle: "One Piece", mediaCoverImage: "",
             mediaTotalEpisodes: 100, episodeNumber: 1016, filename: "ep.mkv", completionPercentage: 11 },
    status: { playing: true, currentTimeInSeconds: 130, durationInSeconds: 1400, filename: "ep.mkv" },
});
const items = cap2.renderFn();
const texts = JSON.stringify(items);
if (texts.indexOf("timed out") < 0) { console.error("FAIL: ERR reason not shown in tray: " + texts); process.exit(1); }
console.log("PASS ERR reason surfaces in tray status");
// next (changed-episode) send must go straight to IPC, no websocket attempt
s2.cmds.length = 0;
cap2.playbackCb({
    isVideoStopped: false, isVideoCompleted: false, isStreamStopped: false, isStreamCompleted: false,
    state: { mediaId: 21, mediaTitle: "One Piece", mediaCoverImage: "",
             mediaTotalEpisodes: 100, episodeNumber: 1017, filename: "ep.mkv", completionPercentage: 12 },
    status: { playing: true, currentTimeInSeconds: 140, durationInSeconds: 1400, filename: "ep.mkv" },
});
const lastCmd = s2.cmds[s2.cmds.length - 1];
if (!lastCmd || lastCmd.indexOf("--transport") < 0 || lastCmd[lastCmd.indexOf("--transport") + 1] !== "ipc") {
    console.error("FAIL: websocket not skipped after failure: " + JSON.stringify(s2.cmds)); process.exit(1);
}
console.log("PASS websocket skipped for session after failure");
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
