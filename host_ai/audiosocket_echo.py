#!/usr/bin/env python3
"""
audiosocket_echo.py — minimal Asterisk AudioSocket server (P2: prove the pipe).

Asterisk connects OUT to this server (via the AudioSocket() dialplan app) and
streams the caller's audio as 8kHz, 16-bit, mono PCM. This server echoes every
audio frame straight back, so the caller hears themselves. That proves the full
host <-> Asterisk audio path over the host-only network before we add STT/LLM/TTS.

AudioSocket wire format (one message):
    byte 0     : type
    bytes 1-2  : payload length, big-endian uint16
    bytes 3..  : payload
Types:
    0x00 HANGUP  payload len 0    -> peer hung up
    0x01 ID      payload 16 bytes -> call UUID (sent once on connect)
    0x10 AUDIO   payload PCM      -> 20ms slin frame (320 B @ 8kHz/16-bit)
    0xff ERROR   payload 1 byte   -> error code
Ref: Asterisk res_audiosocket / app_audiosocket.

Run:  python3 audiosocket_echo.py      (dial extension 5000 from a softphone)
"""
import logging
import socketserver
import struct

HOST = "0.0.0.0"   # Asterisk reaches us at the host-only IP (192.168.56.1)
PORT = 8090

KIND_HANGUP = 0x00
KIND_ID = 0x01
KIND_AUDIO = 0x10
KIND_ERROR = 0xff

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("audiosocket-echo")


def _recv_exact(conn, n):
    """Read exactly n bytes, or return None on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _frame(kind, payload=b""):
    return struct.pack(">BH", kind, len(payload)) + payload


class EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        peer = self.client_address
        log.info("connection from %s", peer)
        frames = 0
        while True:
            header = _recv_exact(self.request, 3)
            if header is None:
                break
            kind, length = struct.unpack(">BH", header)
            payload = _recv_exact(self.request, length) if length else b""
            if length and payload is None:
                break

            if kind == KIND_AUDIO:
                self.request.sendall(_frame(KIND_AUDIO, payload))  # echo back
                frames += 1
            elif kind == KIND_ID:
                log.info("call UUID: %s", payload.hex())
            elif kind == KIND_HANGUP:
                log.info("hangup from %s", peer)
                break
            elif kind == KIND_ERROR:
                log.warning("error frame: %r", payload)
            else:
                log.debug("ignoring kind 0x%02x len=%d", kind, length)
        log.info("closed %s (%d audio frames echoed)", peer, frames)


class ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    log.info("AudioSocket echo server on %s:%d (Ctrl-C to stop)", HOST, PORT)
    with ThreadedServer((HOST, PORT), EchoHandler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            log.info("bye")
