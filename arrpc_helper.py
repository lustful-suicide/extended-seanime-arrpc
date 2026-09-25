#!/usr/bin/env python3
"""Seanime -> arRPC bridge helper (stdlib only).

Ships two modes used by the Seanime plugin:
  --daemon   persistent companion: discovers a Discord/arRPC socket
             (regular IPC dirs plus Flatpak sandbox xdg-run dirs),
             holds one connection, applies set/clear/probe/exit commands
             from <dir>/seanime-arrpc-cmd.json, reports to
             <dir>/seanime-arrpc-status.json.
  --probe / --activity / --clear   one-shot diagnostics (handshake only
             for --probe). Always exits 0; the result is an "OK ..." or
             "ERR ..." line on stdout, which is all the plugin can read.

Only the Python standard library is used.
"""

import argparse
import base64
import glob
import hashlib
import json
import os
import socket
import struct
import sys
import time
import uuid

# Discord RPC IPC framing (matches OpenAsar/arRPC transports/ipc.js).
OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4

WS_PORTS = list(range(6463, 6473))
IPC_TRIES = list(range(0, 10))
CONNECT_TIMEOUT = 2.0
IO_TIMEOUT = 5.0
# Bundled arRPC builds may accept the upgrade then go silent;
# treat that as transport failure.
READY_TIMEOUT = 2.5


def ws_ports():
    """Port list, overridable via ARRPC_WS_PORTS (e.g. "6464,6465")."""
    raw = os.environ.get("ARRPC_WS_PORTS", "")
    if raw.strip():
        ports = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ports.append(int(part))
        if ports:
            return ports
    return WS_PORTS


def eprint(*args):
    print(*args, file=sys.stderr)


class SockReader:
    """Buffered exact-reader for WebSocket framing.

    The HTTP upgrade response and the first frame often share one TCP
    segment; naive recv() loops would swallow frame bytes while scanning
    for the header terminator.
    """

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(n - len(self.buf))
            if not chunk:
                raise ConnectionError("socket closed while reading")
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def read_until(self, marker, limit=16384):
        while marker not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            self.buf += chunk
            if len(self.buf) > limit:
                break
        if marker not in self.buf:
            return None
        head, _, rest = self.buf.partition(marker)
        self.buf = bytearray(rest)
        return bytes(head)


def recv_exact(sock, n):
    """Read exactly n bytes or raise."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed while reading")
        buf += chunk
    return buf


# --------------------------------------------------------------------------
# WebSocket transport (127.0.0.1:6463-6472)
# --------------------------------------------------------------------------

def ws_send_text(sock, payload_str):
    data = payload_str.encode("utf-8")
    # Client frames MUST be masked (RFC 6455).
    mask = os.urandom(4)
    header = bytearray()
    header.append(0x81)  # FIN + text opcode
    length = len(data)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack("!H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", length)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(bytes(header) + masked)


def ws_recv_frame(reader):
    """Read one WebSocket frame from the server. Returns (opcode, payload)."""
    hdr = reader.read_exact(2)
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0F
    masked = (b2 & 0x80) != 0
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", reader.read_exact(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", reader.read_exact(8))[0]
    if masked:
        mask = reader.read_exact(4)
    else:
        mask = None
    payload = reader.read_exact(length) if length else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def ws_read_http_response(reader):
    """Read HTTP handshake response headers via the shared buffer.

    Returns (status_code, headers dict).
    """
    head = reader.read_until(b"\r\n\r\n")
    if head is None:
        return 0, {}
    try:
        lines = head.decode("latin-1").split("\r\n")
    except Exception:
        return 0, {}
    try:
        code = int(lines[0].split(" ")[1])
    except (IndexError, ValueError):
        code = 0
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return code, headers


def ws_wait_ready(reader, sock, port):
    """Wait for the server's DISPATCH/READY frame (or any first frame).

    Raises ConnectionError if the server closes, sends a close frame, or
    stays silent past READY_TIMEOUT.
    """
    old_timeout = sock.gettimeout()
    sock.settimeout(READY_TIMEOUT)
    try:
        opcode, payload = ws_recv_frame(reader)
    finally:
        sock.settimeout(old_timeout if old_timeout is not None else IO_TIMEOUT)
    if opcode == 8:
        raise ConnectionError("port %d: closed during handshake" % port)
    return payload


def ws_connect(client_id):
    """Connect + handshake to the first healthy WebSocket RPC port.

    Returns (open socket, SockReader, "websocket:127.0.0.1:<port>").
    The caller owns the socket (hold it for persistent presence).
    """
    errors = []
    for port in ws_ports():
        sock = None
        try:
            sock = socket.create_connection(("127.0.0.1", port), CONNECT_TIMEOUT)
            sock.settimeout(IO_TIMEOUT)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            # No Origin header: arRPC only accepts empty (native) origins,
            # and browsers cannot send one anyway.
            req = (
                "GET /?v=1&client_id={cid} HTTP/1.1\r\n"
                "Host: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            ).format(cid=client_id, port=port, key=key)
            sock.sendall(req.encode("latin-1"))
            reader = SockReader(sock)
            code, _ = ws_read_http_response(reader)
            if code != 101:
                raise ConnectionError("port %d: HTTP %s" % (port, code))
            ws_wait_ready(reader, sock, port)
            return sock, reader, "websocket:127.0.0.1:%d" % port
        except Exception as exc:  # try next port
            errors.append("port %d: %s" % (port, exc))
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    raise ConnectionError("websocket failed (%s)" % " | ".join(errors or ["no ports tried"]))


def ws_request(sock, reader, payload_obj):
    """Send one SET_ACTIVITY payload over a connected socket. Returns reply."""
    ws_send_text(sock, json.dumps(payload_obj))
    for _ in range(5):
        opcode, payload = ws_recv_frame(reader)
        if opcode == 9:  # ping -> pong
            pong = bytearray([0x8A, 0x00])
            sock.sendall(bytes(pong))
            continue
        if opcode == 8:
            raise ConnectionError("server closed connection")
        break
    reply = json.loads(payload.decode("utf-8"))
    if isinstance(reply, dict) and reply.get("evt") == "ERROR":
        raise RuntimeError("arRPC error: %s" % reply)
    return reply


def try_websocket(client_id, payload_obj):
    """Try SET_ACTIVITY over arRPC WebSocket RPC. Returns server reply dict."""
    sock, reader, where = ws_connect(client_id)
    try:
        reply = ws_request(sock, reader, payload_obj)
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return reply, where


def ws_probe(client_id):
    """Handshake-only check used by the plugin's Test button."""
    sock, _, where = ws_connect(client_id)
    try:
        sock.close()
    except Exception:
        pass
    return where


# --------------------------------------------------------------------------
# Unix IPC transport (discord-ipc-0..9)
# --------------------------------------------------------------------------

def ipc_candidate_dirs():
    # Overridable for tests / custom setups: comma-separated directory list.
    raw = os.environ.get("ARRPC_IPC_DIRS", "")
    if raw.strip():
        return [d for d in (p.strip() for p in raw.split(",")) if d]
    dirs = []
    for env_key in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP"):
        val = os.environ.get(env_key)
        if val:
            dirs.append(val)
    try:
        dirs.append("/run/user/%d" % os.getuid())
    except Exception:
        pass
    dirs.append("/tmp")
    # Sandbox layouts next to each base: snap / official-Discord Flatpak
    # paths, plus every Flatpak app's xdg-run dir (Equibop, Vesktop, ...).
    extra = []
    for base in list(dirs):
        for sub in ("snap.discord", "app/com.discordapp.Discord"):
            extra.append(os.path.join(base, sub))
    seen = set()
    out = []
    for d in dirs + extra + flatpak_socket_dirs():
        if d and d not in seen:
            seen.add(d)
            if os.path.isdir(d):
                out.append(d)
    return out


def flatpak_socket_dirs():
    """Host-visible xdg-run dirs of Flatpak apps (Equibop, Vesktop, ...).

    A Flatpak's $XDG_RUNTIME_DIR is visible on the host at
    /run/user/<uid>/.flatpak/<app-id>/, with its sockets in xdg-run/.
    """
    out = []
    bases = []
    if os.environ.get("XDG_RUNTIME_DIR"):
        bases.append(os.environ["XDG_RUNTIME_DIR"])
    try:
        bases.append("/run/user/%d" % os.getuid())
    except Exception:
        pass
    for base in bases:
        try:
            matches = sorted(glob.glob(os.path.join(base, ".flatpak", "*", "xdg-run")))
        except Exception:
            continue
        for m in matches:
            if os.path.isdir(m):
                out.append(m)
    return out


def ipc_send(sock, opcode, payload_str):
    data = payload_str.encode("utf-8")
    sock.sendall(struct.pack("<ii", opcode, len(data)) + data)


def ipc_recv(sock):
    hdr = recv_exact(sock, 8)
    opcode, length = struct.unpack("<ii", hdr)
    if length < 0 or length > 64 * 1024 * 1024:
        raise ConnectionError("bad IPC frame length %r" % length)
    payload = recv_exact(sock, length) if length else b""
    return opcode, json.loads(payload.decode("utf-8"))


def ipc_connect(client_id):
    """Connect + handshake to the first reachable IPC socket.

    Returns (open socket, "ipc:<path>"). The caller owns the socket
    (hold it for persistent presence, close it when done).
    """
    errors = []
    for directory in ipc_candidate_dirs():
        for i in IPC_TRIES:
            path = os.path.join(directory, "discord-ipc-%d" % i)
            if not os.path.exists(path):
                continue
            sock = None
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(IO_TIMEOUT)
                sock.connect(path)
                ipc_send(sock, OP_HANDSHAKE,
                         json.dumps({"v": "1", "client_id": client_id}))
                for _ in range(3):
                    opcode, msg = ipc_recv(sock)
                    if opcode == OP_PING:
                        ipc_send(sock, OP_PONG,
                                 json.dumps(msg) if isinstance(msg, (dict, list)) else "{}")
                        continue
                    if opcode == OP_CLOSE:
                        raise ConnectionError("closed: %s" % msg)
                    break
                return sock, "ipc:%s" % path
            except Exception as exc:
                errors.append("%s: %s" % (path, exc))
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
    raise ConnectionError("ipc failed (%s)" % " | ".join(errors or ["no sockets tried"]))


def ipc_request(sock, payload_obj):
    """Send one SET_ACTIVITY payload over a connected socket. Returns reply."""
    ipc_send(sock, OP_FRAME, json.dumps(payload_obj))
    for _ in range(3):
        opcode, reply = ipc_recv(sock)
        if opcode == OP_PING:
            ipc_send(sock, OP_PONG, "{}")
            continue
        break
    if opcode == OP_CLOSE:
        raise RuntimeError("arRPC error: %s" % reply)
    if isinstance(reply, dict):
        data = reply.get("data") or {}
        if isinstance(data, dict) and data.get("code", 0) > 1000:
            raise RuntimeError("arRPC error: %s" % data)
    return reply


def try_ipc(client_id, payload_obj):
    sock, where = ipc_connect(client_id)
    try:
        reply = ipc_request(sock, payload_obj)
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return reply, where


def ipc_probe(client_id):
    sock, where = ipc_connect(client_id)
    try:
        sock.close()
    except Exception:
        pass
    return where


# --------------------------------------------------------------------------
# Persistent daemon (holds ONE connection, applies file commands)
# --------------------------------------------------------------------------

CMD_FILENAME = "seanime-arrpc-cmd.json"
STATUS_FILENAME = "seanime-arrpc-status.json"
TAKEOVER_AFTER_SEC = 20.0
WS_RETRY_AFTER_SEC = 60.0
# Re-assert the current activity this often: heals a silently dropped
# connection (e.g. the Discord client restarted) while otherwise idle.
REASSERT_AFTER_SEC = 60.0


def _atomic_write_json(path, obj):
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


class Daemon(object):
    """Holds one arRPC connection and applies plugin commands.

    Commands arrive via <dir>/seanime-arrpc-cmd.json:
        {"op": "set", "activity": {...}, "nonce": 7}
        {"op": "clear" | "probe" | "exit", "nonce": 8}
    Heartbeat + last result go to <dir>/seanime-arrpc-status.json.
    """

    def __init__(self, client_id, directory, interval, transport):
        self.client_id = client_id
        self.directory = directory
        self.interval = interval
        self.transport = transport
        self.pid = os.getpid()
        self.cmd_path = os.path.join(directory, CMD_FILENAME)
        self.status_path = os.path.join(directory, STATUS_FILENAME)
        self.sock = None
        self.reader = None
        self.kind = None  # "ipc" | "websocket"
        self.where = ""
        self.last_nonce = None
        self.last_activity = None
        self.last_send_at = 0.0
        self.state = {"state": "starting", "transport": "", "error": "",
                      "activity": ""}
        self.ws_cooldown_until = 0.0
        self._last_note = ""

    def note(self, msg):
        # Log transitions only; per-tick logging would spam.
        if msg != self._last_note:
            self._last_note = msg
            print("DAEMON %s" % msg, flush=True)

    def dump(self):
        try:
            body = {"alive": time.time(), "pid": self.pid}
            body.update(self.state)
            _atomic_write_json(self.status_path, body)
        except Exception as exc:
            eprint("daemon status write failed: %s" % exc)

    def close_socket(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        self.reader = None
        self.kind = None
        self.where = ""

    def connect(self):
        """(Re)discover and handshake. Returns transport string."""
        self.close_socket()
        errors = []
        use_ipc = self.transport in ("auto", "ipc")
        use_ws = self.transport in ("auto", "websocket")
        if use_ws and time.time() < self.ws_cooldown_until:
            use_ws = False
        if use_ipc:
            try:
                self.sock, self.where = ipc_connect(self.client_id)
                self.kind = "ipc"
                self.state["transport"] = self.where
                return self.where
            except Exception as exc:
                errors.append("ipc: %s" % exc)
        if use_ws:
            try:
                self.sock, self.reader, self.where = ws_connect(self.client_id)
                self.kind = "websocket"
                self.state["transport"] = self.where
                return self.where
            except Exception as exc:
                errors.append("websocket: %s" % exc)
                # A broken WS endpoint (accepts then goes silent) is usually
                # broken for good -- back off before retrying it.
                self.ws_cooldown_until = time.time() + WS_RETRY_AFTER_SEC
        raise ConnectionError(" | ".join(errors) or "no transport")

    def send(self, activity):
        payload = build_payload(self.client_id, activity, self.pid)
        if self.kind == "ipc":
            return ipc_request(self.sock, payload)
        return ws_request(self.sock, self.reader, payload)

    def apply_set(self, activity):
        try:
            if self.sock is None:
                self.connect()
            self.send(activity)
        except Exception:
            self.connect()
            self.send(activity)
        if activity:
            details = activity.get("details", "")
            state = activity.get("state", "")
            self.state["activity"] = ("%s %s" % (details, state)).strip()
        else:
            self.state["activity"] = ""
        self.last_activity = activity
        self.last_send_at = time.time()
        self.state["state"] = "ok"
        self.state["error"] = ""

    def apply_probe(self):
        """Handshake test that never disturbs the held connection."""
        errors = []
        if self.transport in ("auto", "ipc"):
            try:
                where = ipc_probe(self.client_id)
                self.state["transport"] = where
                self.state["state"] = "ok"
                self.state["error"] = ""
                return
            except Exception as exc:
                errors.append("ipc: %s" % exc)
        if self.transport in ("auto", "websocket"):
            try:
                where = ws_probe(self.client_id)
                self.state["transport"] = where
                self.state["state"] = "ok"
                self.state["error"] = ""
                return
            except Exception as exc:
                errors.append("websocket: %s" % exc)
        self.state["state"] = "error"
        self.state["error"] = "PROBE FAILED: %s" % " | ".join(errors)

    def read_cmd(self):
        # The nonce is the trigger: a failed set/clear keeps its nonce, so
        # it is retried until the server is reachable again.
        try:
            with open(self.cmd_path, encoding="utf-8") as f:
                cmd = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(cmd, dict):
            return None
        if cmd.get("nonce") == self.last_nonce:
            return None
        return cmd

    def run(self):
        try:
            with open(self.status_path, encoding="utf-8") as f:
                prev = json.load(f)
            if (time.time() - prev.get("alive", 0) < TAKEOVER_AFTER_SEC
                    and prev.get("pid") not in (None, self.pid)):
                print("DAEMON another instance alive (pid %s), exiting"
                      % prev.get("pid"), flush=True)
                return 0
        except Exception:
            pass
        self.note("started (pid %d)" % self.pid)
        self.dump()
        try:
            while True:
                try:
                    cmd = self.read_cmd()
                    if cmd and cmd.get("nonce") != self.last_nonce:
                        op = cmd.get("op")
                        if op == "exit":
                            try:
                                if self.sock is None:
                                    self.connect()
                                self.send(None)
                            except Exception:
                                pass
                            self.note("exit requested")
                            return 0
                        if op == "set":
                            self.apply_set(cmd.get("activity"))
                            self.note("set %s via %s"
                                      % (self.state["activity"], self.state["transport"]))
                        elif op == "clear":
                            self.apply_set(None)
                            self.note("cleared")
                        elif op == "probe":
                            self.apply_probe()
                            self.note("probe -> %s %s"
                                      % (self.state["state"], self.state["transport"] or self.state["error"]))
                        # set/clear describe desired state: retry until applied.
                        # probe is one-shot: keep the reported result.
                        self.last_nonce = cmd.get("nonce")
                except Exception as exc:
                    self.close_socket()
                    self.state["state"] = "error"
                    self.state["error"] = str(exc)
                    self.note("error: %s" % exc)
                    if cmd and cmd.get("op") in ("set", "clear"):
                        self.last_nonce = None  # retry desired state
                    else:
                        self.last_nonce = cmd.get("nonce") if cmd else self.last_nonce
                if (self.sock is not None and self.last_activity
                        and time.time() - self.last_send_at > REASSERT_AFTER_SEC):
                    try:
                        self.send(self.last_activity)
                        self.last_send_at = time.time()
                    except Exception as exc:
                        self.close_socket()
                        self.state["state"] = "error"
                        self.state["error"] = str(exc)
                        self.note("keepalive failed, will reconnect: %s" % exc)
                self.dump()
                time.sleep(self.interval)
        finally:
            self.close_socket()
        return 0


def run_daemon(client_id, directory, interval, transport):
    try:
        os.makedirs(directory, exist_ok=True)
    except Exception as exc:
        eprint("cannot create %s: %s" % (directory, exc))
        return 1
    return Daemon(client_id, directory, interval, transport).run()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_payload(client_id, activity, pid):
    return {
        "cmd": "SET_ACTIVITY",
        "args": {"pid": pid, "activity": activity},
        "nonce": str(uuid.uuid4()),
    }


def main(argv=None):
    global IO_TIMEOUT
    ap = argparse.ArgumentParser(description="Seanime -> arRPC bridge helper")
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--daemon", action="store_true",
                    help="run persistent daemon (holds one connection, "
                         "applies JSON commands from --dir)")
    ap.add_argument("--dir", default=None,
                    help="working dir for daemon cmd/status files")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="daemon poll interval in seconds")
    group = ap.add_mutually_exclusive_group(required=False)
    group.add_argument("--activity", default=None,
                       help="JSON activity object (Discord activity struct)")
    group.add_argument("--clear", action="store_true",
                       help="clear the current activity")
    group.add_argument("--probe", action="store_true",
                       help="handshake only, do not change activity")
    ap.add_argument("--pid", type=int, default=os.getpid())
    ap.add_argument("--timeout", type=float, default=IO_TIMEOUT)
    ap.add_argument("--transport", choices=["auto", "websocket", "ipc"],
                    default="auto")
    args = ap.parse_args(argv)

    IO_TIMEOUT = args.timeout

    if args.daemon:
        if not args.dir:
            eprint("--daemon requires --dir")
            return 2
        return run_daemon(args.client_id, args.dir, args.interval,
                          args.transport)

    if args.probe:
        errors = []
        if args.transport in ("auto", "ipc"):
            try:
                where = ipc_probe(args.client_id)
                print("OK %s" % where)
                return 0
            except Exception as exc:
                errors.append(str(exc))
        if args.transport in ("auto", "websocket"):
            try:
                where = ws_probe(args.client_id)
                print("OK %s" % where)
                return 0
            except Exception as exc:
                errors.append(str(exc))
        # Exit 0 with an ERR line: $os.cmd().output() only surfaces stdout,
        # so the plugin parses the OK/ERR prefix instead of the exit code.
        msg = "PROBE FAILED: %s" % " | ".join(errors)
        print("ERR " + msg)
        eprint(msg)
        return 0

    if args.clear:
        activity = None
    else:
        try:
            activity = json.loads(args.activity)
        except Exception as exc:
            eprint("Invalid --activity JSON: %s" % exc)
            return 2

    payload = build_payload(args.client_id, activity, args.pid)
    errors = []
    if args.transport in ("auto", "ipc"):
        try:
            _, where = try_ipc(args.client_id, payload)
            print("OK %s" % where)
            return 0
        except Exception as exc:
            errors.append("ipc: %s" % exc)
    if args.transport in ("auto", "websocket"):
        try:
            _, where = try_websocket(args.client_id, payload)
            print("OK %s" % where)
            return 0
        except Exception as exc:
            errors.append("websocket: %s" % exc)
    eprint("FAILED: %s" % " | ".join(errors))
    eprint("HINT: start the Discord client or run `npx arrpc`.")
    print("ERR FAILED: %s" % " | ".join(errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
