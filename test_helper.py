"""Self-tests for arrpc_helper.py using fake arRPC servers (stdlib only).

Uses ARRPC_WS_PORTS / ARRPC_IPC_DIRS overrides so tests never touch the
real ports/sockets on the machine running them.
"""
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "arrpc_helper.py")
CLIENT_ID = "1224777421941899285"
WS_PORT = 6479  # outside the 6463-6472 default range on purpose

received = {}


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def ws_send_server_frame(conn, obj):
    raw = json.dumps(obj).encode()
    assert len(raw) < 126
    conn.sendall(bytes([0x81, len(raw)]) + raw)


def ws_handle_client_message(conn):
    hdr = recv_exact(conn, 2)
    masked = (hdr[1] & 0x80) != 0
    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(conn, 8))[0]
    mask = recv_exact(conn, 4) if masked else None
    payload = recv_exact(conn, length)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return json.loads(payload.decode())


def ws_serve_forever(listen_sock, store_key, stop):
    while not stop.is_set():
        try:
            listen_sock.settimeout(0.5)
            conn, _ = listen_sock.accept()
        except socket.timeout:
            continue
        conn.settimeout(10)
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                req += chunk
            if b"Upgrade: websocket" not in req:
                conn.close()
                continue
            key = [l.split(":", 1)[1].strip()
                   for l in req.decode("latin-1").split("\r\n")
                   if l.lower().startswith("sec-websocket-key")][0]
            accept = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode()
            ready = json.dumps({"cmd": "DISPATCH", "evt": "READY", "nonce": None}).encode()
            assert len(ready) < 126
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                          "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                          "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode("latin-1")
                         + bytes([0x81, len(ready)]) + ready)
            # NOTE: headers + READY go out in one segment on purpose: the
            # helper must not lose frame bytes coalesced with the headers.
            try:
                msg = ws_handle_client_message(conn)
            except ConnectionError:
                continue  # probe connections close after READY
            received[store_key] = msg
            ws_send_server_frame(conn, {"cmd": msg.get("cmd"), "data": {"ok": True},
                                        "evt": None, "nonce": msg.get("nonce")})
        except Exception as exc:
            print("fake ws server error: %r" % exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass


def run_helper(args, env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, HELPER] + args,
                          capture_output=True, text=True, timeout=25, env=env)


def test_websocket():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", WS_PORT))
    srv.listen(5)
    stop = threading.Event()
    t = threading.Thread(target=ws_serve_forever, args=(srv, "set", stop))
    t.start()
    env = {"ARRPC_WS_PORTS": str(WS_PORT)}
    try:
        time.sleep(0.2)
        act = {"name": "Seanime", "details": "Test Anime",
               "state": "Watching Episode 1", "type": 3, "instance": True}
        r = run_helper(["--client-id", CLIENT_ID, "--activity", json.dumps(act)], env)
        assert r.returncode == 0, "helper failed: %s" % r.stderr
        assert r.stdout.startswith("OK websocket"), r.stdout
        got = received["set"]["args"]["activity"]
        assert got["details"] == "Test Anime", got
        print("PASS websocket SET_ACTIVITY")

        r = run_helper(["--client-id", CLIENT_ID, "--clear"], env)
        assert r.returncode == 0, "clear failed: %s" % r.stderr
        assert received["set"]["args"]["activity"] is None, received["set"]
        print("PASS websocket CLEAR")

        r = run_helper(["--client-id", CLIENT_ID, "--probe"], env)
        assert r.returncode == 0, "probe failed: %s" % r.stderr
        print("PASS websocket PROBE")
    finally:
        stop.set()
        t.join(timeout=10)
        srv.close()


def ipc_serve_forever(sock_path, store_key, stop):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    srv.bind(sock_path)
    srv.listen(1)
    try:
        while not stop.is_set():
            try:
                srv.settimeout(0.5)
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            conn.settimeout(10)
            try:
                hdr = recv_exact(conn, 8)
                op, ln = struct.unpack("<ii", hdr)
                assert op == 0, op
                hs = json.loads(recv_exact(conn, ln).decode())
                assert hs["client_id"] == CLIENT_ID, hs
                ready = json.dumps({"cmd": "DISPATCH", "evt": "READY"}).encode()
                conn.sendall(struct.pack("<ii", 1, len(ready)) + ready)
                try:
                    hdr = recv_exact(conn, 8)
                except ConnectionError:
                    continue  # probe: handshake only
                op, ln = struct.unpack("<ii", hdr)
                assert op == 1, op
                msg = json.loads(recv_exact(conn, ln).decode())
                received[store_key] = msg
                reply = json.dumps({"cmd": msg.get("cmd"), "data": {},
                                    "evt": None, "nonce": msg.get("nonce")}).encode()
                conn.sendall(struct.pack("<ii", 1, len(reply)) + reply)
            except Exception as exc:
                print("fake ipc server error: %r" % exc)
            finally:
                conn.close()
    finally:
        srv.close()


def test_ipc():
    tmp = tempfile.mkdtemp(prefix="arrpc-test-")
    path = os.path.join(tmp, "discord-ipc-0")
    stop = threading.Event()
    t = threading.Thread(target=ipc_serve_forever, args=(path, "ipc", stop))
    t.start()
    env = {"ARRPC_IPC_DIRS": tmp, "ARRPC_WS_PORTS": "6499"}
    try:
        time.sleep(0.3)
        act = {"name": "Seanime", "details": "IPC Anime", "type": 3}
        r = run_helper(["--client-id", CLIENT_ID, "--activity", json.dumps(act),
                        "--transport", "ipc"], env)
        assert r.returncode == 0, "ipc helper failed: %s" % r.stderr
        assert r.stdout.startswith("OK ipc"), r.stdout
        assert received["ipc"]["args"]["activity"]["details"] == "IPC Anime"
        print("PASS ipc SET_ACTIVITY")

        r = run_helper(["--client-id", CLIENT_ID, "--probe", "--transport", "ipc"], env)
        assert r.returncode == 0, "ipc probe failed: %s" % r.stderr
        print("PASS ipc PROBE")
    finally:
        stop.set()
        t.join(timeout=10)


def test_bad_activity_json():
    r = run_helper(["--client-id", CLIENT_ID, "--activity", "{not json"])
    assert r.returncode == 2, r
    print("PASS invalid JSON rejected")


def test_no_server_fails_cleanly():
    env = {"ARRPC_WS_PORTS": "6498", "ARRPC_IPC_DIRS": "/nonexistent-dir-xyz"}
    r = run_helper(["--client-id", CLIENT_ID, "--probe"], env)
    assert r.returncode == 1, r
    assert "FAILED" in r.stderr or "PROBE FAILED" in r.stderr, r.stderr
    print("PASS clean failure with no server")


if __name__ == "__main__":
    test_websocket()
    test_ipc()
    test_bad_activity_json()
    test_no_server_fails_cleanly()
    print("ALL HELPER TESTS PASSED")
