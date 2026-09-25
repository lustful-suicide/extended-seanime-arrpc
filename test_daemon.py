"""Daemon protocol tests for arrpc_helper.py --daemon.

Spawns the real daemon as a subprocess against fake arRPC servers, drives it
through the $TEMP command file, and asserts on the status file plus what the
fake servers received. Everything runs in temp dirs; the real machine's
sockets/ports are never touched (ARRPC_WS_PORTS / ARRPC_IPC_DIRS).
"""
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
WS_PORT = 6481

received = {"frames": []}


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def ipc_serve_forever(sock_path, stop):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    srv.bind(sock_path)
    srv.listen(5)
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
                recv_exact(conn, ln)  # handshake
                ready = json.dumps({"cmd": "DISPATCH", "evt": "READY"}).encode()
                conn.sendall(struct.pack("<ii", 1, len(ready)) + ready)
                # Hold the connection: serve every SET_ACTIVITY on it.
                while not stop.is_set():
                    try:
                        conn.settimeout(0.5)
                        hdr = recv_exact(conn, 8)
                    except socket.timeout:
                        continue
                    conn.settimeout(10)
                    op, ln = struct.unpack("<ii", hdr)
                    msg = json.loads(recv_exact(conn, ln).decode())
                    received["frames"].append(msg)
                    reply = json.dumps({"cmd": msg.get("cmd"), "data": {},
                                        "evt": None,
                                        "nonce": msg.get("nonce")}).encode()
                    conn.sendall(struct.pack("<ii", 1, len(reply)) + reply)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    finally:
        srv.close()


def write_cmd(workdir, nonce, op, activity=None):
    body = {"op": op, "activity": activity, "nonce": nonce}
    tmp = os.path.join(workdir, "cmd.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f)
    os.replace(tmp, os.path.join(workdir, "seanime-arrpc-cmd.json"))


def read_status(workdir):
    with open(os.path.join(workdir, "seanime-arrpc-status.json"),
              encoding="utf-8") as f:
        return json.load(f)


def wait_for(workdir, cond, timeout=15):
    start = time.time()
    while time.time() - start < timeout:
        try:
            st = read_status(workdir)
        except Exception:
            time.sleep(0.2)
            continue
        if cond(st):
            return st
        time.sleep(0.2)
    raise AssertionError("timed out waiting for status in %s (last: %s)"
                         % (workdir, st if "st" in dir() else None))


def start_daemon(workdir, ipc_dir, extra_env=None):
    env = dict(os.environ)
    env["ARRPC_WS_PORTS"] = "6497"
    env["ARRPC_IPC_DIRS"] = ipc_dir
    env.update(extra_env or {})
    proc = subprocess.Popen(
        [sys.executable, HELPER, "--client-id", CLIENT_ID,
         "--daemon", "--dir", workdir, "--interval", "0.2"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env)
    return proc


def test_daemon_set_clear_probe():
    workdir = tempfile.mkdtemp(prefix="arrpc-d-")
    ipc_dir = tempfile.mkdtemp(prefix="arrpc-ipc-")
    stop = threading.Event()
    t = threading.Thread(target=ipc_serve_forever,
                         args=(os.path.join(ipc_dir, "discord-ipc-0"), stop))
    t.start()
    proc = start_daemon(workdir, ipc_dir)
    try:
        st = wait_for(workdir, lambda s: s.get("alive"), 15)
        print("PASS daemon starts + heartbeat (pid %s)" % st.get("pid"))

        write_cmd(workdir, 1, "set",
                  {"name": "Seanime", "details": "Daemon Anime",
                   "state": "Watching Episode 2", "type": 3})
        st = wait_for(workdir,
                      lambda s: s.get("activity") == "Daemon Anime Watching Episode 2", 15)
        assert st["state"] == "ok", st
        assert st["transport"].startswith("ipc:"), st
        assert received["frames"][-1]["args"]["activity"]["details"] == "Daemon Anime"
        print("PASS daemon set -> server got activity, status ok (%s)"
              % st["transport"])

        write_cmd(workdir, 2, "clear")
        st = wait_for(workdir, lambda s: s.get("activity") == "", 15)
        assert received["frames"][-1]["args"]["activity"] is None
        print("PASS daemon clear -> server got null")

        write_cmd(workdir, 3, "probe")
        st = wait_for(workdir, lambda s: s.get("state") == "ok"
                      and "ipc:" in s.get("transport", ""), 15)
        print("PASS daemon probe reports transport")

        # Takeover: a second daemon must exit while the first is alive.
        proc2 = start_daemon(workdir, ipc_dir)
        rc = proc2.wait(timeout=15)
        out = proc2.stdout.read()
        assert rc == 0 and "another instance alive" in out, (rc, out)
        print("PASS second daemon exits (takeover protection)")

        write_cmd(workdir, 4, "exit")
        rc = proc.wait(timeout=15)
        assert rc == 0, rc
        print("PASS daemon exits cleanly on exit op")
    finally:
        for p in ("proc",):
            try:
                if "proc" in dir() and proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        stop.set()
        t.join(timeout=10)


def test_daemon_reconnects():
    # Daemon starts with no server, then the server appears later.
    workdir = tempfile.mkdtemp(prefix="arrpc-d2-")
    ipc_dir = tempfile.mkdtemp(prefix="arrpc-ipc2-")
    proc = start_daemon(workdir, ipc_dir)
    try:
        write_cmd(workdir, 1, "set",
                  {"name": "Seanime", "details": "Late Anime", "type": 3})
        st = wait_for(workdir, lambda s: s.get("state") == "error", 15)
        assert "activity" not in st or st.get("activity") == "", st
        print("PASS daemon reports error with no server")

        stop = threading.Event()
        t = threading.Thread(target=ipc_serve_forever,
                             args=(os.path.join(ipc_dir, "discord-ipc-0"), stop))
        t.start()
        try:
            st = wait_for(workdir,
                          lambda s: s.get("activity") == "Late Anime", 20)
            assert st["state"] == "ok", st
            print("PASS daemon reconnects when server appears")
        finally:
            stop.set()
            t.join(timeout=10)

        write_cmd(workdir, 2, "exit")
        assert proc.wait(timeout=15) == 0
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass


if __name__ == "__main__":
    test_daemon_set_clear_probe()
    test_daemon_reconnects()
    print("ALL DAEMON TESTS PASSED")
