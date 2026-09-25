#!/usr/bin/env python3
"""Seanime -> arRPC bridge helper (stdlib only).

Sends Discord Rich Presence updates to a local arRPC server (standalone
`npx arrpc` / `arrpc-bun`, or the arRPC built into Equibop/Vesktop) so that
Seanime playback shows up in custom Discord clients like Equibop.

Why this helper exists
----------------------
Seanime's built-in Discord RPC only tries the single IPC path
`$XDG_RUNTIME_DIR/discord-ipc-0`. When Seanime Denshi and the Discord client
disagree about socket directories (Flatpak, systemd env, Docker, multiple
clients occupying `discord-ipc-0`), presence silently never appears.
This helper tries *every* reasonable transport instead:

  1. Discord WebSocket RPC on 127.0.0.1 ports 6463-6472 (same protocol the
     Discord web client uses; arRPC accepts connections with an empty
     Origin header, which browsers cannot send but this script can).
  2. Unix IPC sockets `discord-ipc-0..9` in $XDG_RUNTIME_DIR,
     /run/user/<uid>, $TMPDIR, /tmp.

First transport that completes a handshake + SET_ACTIVITY wins.

Usage (called by the Seanime plugin, but also usable by hand):
  python3 arrpc_helper.py --client-id 1224777421941899285 --activity '<json>'
  python3 arrpc_helper.py --client-id 1224777421941899285 --clear
  python3 arrpc_helper.py --client-id 1224777421941899285 --probe

Exit code 0 + "OK <transport>" on stdout means the activity was accepted.
Anything else is an error (message on stderr, exit code 1).

Only the Python standard library is used.
"""

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
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
# How long to wait for the DISPATCH/READY frame after the WS handshake.
# Some bundled arRPC builds (e.g. Equibop Flatpak) accept the upgrade and
# then close without ever sending READY -- treat that as transport failure.
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
    """Buffered exact-reader: never loses bytes that arrive coalesced.

    The HTTP upgrade response and the first WebSocket frame often arrive in
    the same TCP segment; naive recv() loops would swallow frame bytes while
    scanning for the header terminator. Everything WS-related reads through
    this buffer.
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


def try_websocket(client_id, payload_obj):
    """Try SET_ACTIVITY over arRPC WebSocket RPC. Returns server reply dict."""
    errors = []
    for port in ws_ports():
        sock = None
        try:
            sock = socket.create_connection(("127.0.0.1", port), CONNECT_TIMEOUT)
            sock.settimeout(IO_TIMEOUT)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            # NOTE: no Origin header on purpose. arRPC rejects browser
            # origins (anything that is not discord.com); an empty origin
            # is accepted and is exactly what native RPC clients send.
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
                errors.append("port %d: HTTP %s" % (port, code))
                continue
            # arRPC sends DISPATCH/READY immediately on connect.
            try:
                ws_wait_ready(reader, sock, port)
            except Exception as exc:
                errors.append(str(exc))
                continue
            # Send SET_ACTIVITY.
            ws_send_text(sock, json.dumps(payload_obj))
            # Read reply (skip pings).
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
            return reply, "websocket:127.0.0.1:%d" % port
        except Exception as exc:  # try next port
            errors.append("port %d: %s" % (port, exc))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    raise ConnectionError("websocket failed (%s)" % " | ".join(errors or ["no ports tried"]))


def ws_probe(client_id):
    """Handshake-only check used by the plugin's Test button."""
    errors = []
    for port in ws_ports():
        sock = None
        try:
            sock = socket.create_connection(("127.0.0.1", port), CONNECT_TIMEOUT)
            sock.settimeout(IO_TIMEOUT)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
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
                errors.append("port %d: HTTP %s" % (port, code))
                continue
            try:
                ws_wait_ready(reader, sock, port)
            except Exception as exc:
                errors.append(str(exc))
                continue
            return "websocket:127.0.0.1:%d" % port
        except Exception as exc:
            errors.append("port %d: %s" % (port, exc))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    raise ConnectionError("websocket probe failed (%s)" % " | ".join(errors or ["no ports tried"]))


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
    # Dedupe, keep order, keep only existing directories.
    seen = set()
    out = []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            if os.path.isdir(d):
                out.append(d)
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


def try_ipc(client_id, payload_obj):
    last_err = "no sockets tried"
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
                # Server replies DISPATCH/READY (or PING/CLOSE).
                for _ in range(3):
                    opcode, msg = ipc_recv(sock)
                    if opcode == OP_PING:
                        ipc_send(sock, OP_PONG,
                                 json.dumps(msg) if isinstance(msg, (dict, list)) else "{}")
                        continue
                    if opcode == OP_CLOSE:
                        raise ConnectionError("closed: %s" % msg)
                    break
                ipc_send(sock, OP_FRAME, json.dumps(payload_obj))
                opcode, reply = ipc_recv(sock)
                if opcode == OP_CLOSE:
                    raise RuntimeError("arRPC error: %s" % reply)
                if isinstance(reply, dict):
                    data = reply.get("data") or {}
                    if isinstance(data, dict) and data.get("code", 0) > 1000:
                        raise RuntimeError("arRPC error: %s" % data)
                return reply, "ipc:%s" % path
            except Exception as exc:
                last_err = "%s: %s" % (path, exc)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
    raise ConnectionError("ipc failed (%s)" % last_err)


def ipc_probe(client_id):
    last_err = "no sockets tried"
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
                opcode, _ = ipc_recv(sock)
                if opcode == OP_CLOSE:
                    raise ConnectionError("closed")
                return "ipc:%s" % path
            except Exception as exc:
                last_err = "%s: %s" % (path, exc)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
    raise ConnectionError("ipc probe failed (%s)" % last_err)


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
    group = ap.add_mutually_exclusive_group(required=True)
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

    if args.probe:
        errors = []
        if args.transport in ("auto", "websocket"):
            try:
                where = ws_probe(args.client_id)
                print("OK %s" % where)
                return 0
            except Exception as exc:
                errors.append(str(exc))
        if args.transport in ("auto", "ipc"):
            try:
                where = ipc_probe(args.client_id)
                print("OK %s" % where)
                return 0
            except Exception as exc:
                errors.append(str(exc))
        # NOTE: exit 0 with an ERR line (not exit 1). Seanime's
        # $os.cmd().output() binding only surfaces stdout on success, so a
        # non-zero exit would hide these reasons behind "exit status 1".
        # The plugin parses the OK/ERR prefix instead of the exit code.
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
    if args.transport in ("auto", "websocket"):
        try:
            _, where = try_websocket(args.client_id, payload)
            print("OK %s" % where)
            return 0
        except Exception as exc:
            errors.append("websocket: %s" % exc)
    if args.transport in ("auto", "ipc"):
        try:
            _, where = try_ipc(args.client_id, payload)
            print("OK %s" % where)
            return 0
        except Exception as exc:
            errors.append("ipc: %s" % exc)
    eprint("FAILED: %s" % " | ".join(errors))
    eprint("HINT: enable arRPC in Equibop Settings -> Rich Presence, "
           "or run a standalone server with `npx arrpc`.")
    # Same OK/ERR-protocol note as probe above: report on stdout, exit 0.
    print("ERR FAILED: %s" % " | ".join(errors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
