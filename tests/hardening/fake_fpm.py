#!/usr/bin/env python3
"""Test-only FastCGI peer and process group owned by the real Rust supervisor."""

import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.parse

EVENT_LOCK = threading.Lock()


def event(kind, **details):
    record = {"kind": kind, "time": time.monotonic(), **details}
    with EVENT_LOCK:
        with open(os.environ["HARDENING_EVENTS"], "a") as output:
            output.write(json.dumps(record) + "\n")


def read_exact(stream, count):
    result = bytearray()
    while len(result) < count:
        data = stream.recv(count - len(result))
        if not data:
            raise EOFError("FastCGI socket closed")
        result.extend(data)
    return bytes(result)


def parameter_length(payload, offset):
    if payload[offset] & 0x80:
        return struct.unpack("!I", payload[offset:offset + 4])[0] & 0x7fffffff, offset + 4
    return payload[offset], offset + 1


def parameters(payload):
    values = {}
    offset = 0
    while offset < len(payload):
        name_length, offset = parameter_length(payload, offset)
        value_length, offset = parameter_length(payload, offset)
        name = payload[offset:offset + name_length].decode()
        offset += name_length
        values[name] = payload[offset:offset + value_length].decode()
        offset += value_length
    return values


def record(stream, kind, payload):
    stream.sendall(struct.pack("!BBHHBB", 1, kind, 1, len(payload), 0, 0) + payload)


def end(stream):
    record(stream, 6, b"")
    record(stream, 3, b"\x00" * 8)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        stream = self.request
        stream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        stream.settimeout(20)
        payload = bytearray()
        try:
            while True:
                version, kind, identity, length, padding, _ = struct.unpack("!BBHHBB", read_exact(stream, 8))
                if version != 1 or identity != 1:
                    raise ValueError("Unexpected FastCGI header")
                data = read_exact(stream, length)
                read_exact(stream, padding)
                if kind == 4:
                    payload.extend(data)
                if kind == 5 and not data:
                    break
            values = parameters(payload)
            path = urllib.parse.urlsplit(values.get("REQUEST_URI", "/")).path
            event("request", path=path)
            if path == "/never-end":
                event("unconfirmed_started")
                # Keep the socket open without a response/completion. The fake
                # deliberately does not implement FPM's own request timeout.
                while True:
                    time.sleep(.1)
            if path == "/drain":
                body = b"drain-ack\ndrain-done\n"
                head = b"Status: 200 OK\r\nContent-Type: text/plain\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n"
                record(stream, 6, head + b"drain-ack\n")
                event("drain_waiting")
                deadline = time.monotonic() + 10
                while not Path(os.environ["HARDENING_RELEASE"]).exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Drain release marker was not written")
                    time.sleep(.005)
                record(stream, 6, b"drain-done\n")
                end(stream)
                event("drain_completed")
                return
            if path == "/large":
                size = 64 * 1024 * 1024
                head = b"Status: 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: " + str(size).encode() + b"\r\n\r\n"
                record(stream, 6, head)
                event("large_started")
                chunk = b"x" * 32768
                for _ in range(size // len(chunk)):
                    record(stream, 6, chunk)
                end(stream)
                event("large_completed", bytes=size)
                return
            body = (json.dumps({"ok": True, "fake_fpm_pid": os.getpid(), "rust_pid": os.getppid()}) + "\n").encode()
            head = b"Status: 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n"
            record(stream, 6, head + body)
            end(stream)
        except EOFError:
            # Both supervisors use a bare TCP readiness connection.
            return
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            event("connection_closed")
        except Exception as error:
            event("fake_error", error=f"{type(error).__name__}: {error}")


def main():
    if "--descendant" in sys.argv:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGQUIT, signal.SIG_IGN)
        while True:
            time.sleep(60)
    child = subprocess.Popen([sys.executable, __file__, "--descendant"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    state = {"master_pid": os.getpid(), "descendant_pid": child.pid,
             "group_id": os.getpgrp(), "rust_pid": os.getppid()}
    destination = Path(os.environ["HARDENING_PIDS"])
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(destination)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGQUIT, lambda *_: sys.exit(0))
    host, port = os.environ["BREF_WEB_FPM_ADDR"].rsplit(":", 1)

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = False

    with Server((host, int(port)), Handler) as server:
        event("listening", **state)
        server.serve_forever(poll_interval=.02)


if __name__ == "__main__":
    main()
