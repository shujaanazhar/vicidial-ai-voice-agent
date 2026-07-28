#!/usr/bin/env python3
"""
ai_agent.py — P3: the AI voice agent behind Asterisk's AudioSocket.

Pipeline per call:   VAD -> faster-whisper (STT) -> Ollama (LLM) -> Piper (TTS)
with barge-in: if the caller starts talking while we are speaking, we stop
mid-sentence, drop the queued audio, and listen instead.

Asterisk connects OUT to us (see asterisk/extensions_ai.conf) and streams
8 kHz/16-bit/mono PCM in 20 ms frames (320 bytes). We reply on the same socket.

Latency trick that matters: the LLM is consumed as a token stream and cut into
sentences, each sentence is synthesized and queued the moment it is complete.
The caller hears sentence 1 while sentence 2 is still being generated.

Run via  ./run_agent.sh   (it sets the LD_LIBRARY_PATH that CTranslate2 needs)
"""
from __future__ import annotations

import json
import logging
import queue
import re
import socketserver
import struct
import threading
import time
from collections import deque
from pathlib import Path

import av
import httpx
import numpy as np
from faster_whisper import WhisperModel
from piper import PiperVoice

# ---------------------------------------------------------------- config

HOST, PORT = "0.0.0.0", 8090

HERE = Path(__file__).resolve().parent
PIPER_VOICE = HERE / "piper_voices" / "en_US-lessac-medium.onnx"

WHISPER_MODEL = "small.en"         # base.en is measurably worse on real mic audio
                                   # at 8 kHz (it truncated and garbled words in
                                   # live testing); small.en still runs ~0.2s/turn
WHISPER_DEVICE = "cuda"
WHISPER_COMPUTE = "float16"

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "llama3.2:3b"
OLLAMA_KEEP_ALIVE = "30m"          # never let it cold-load mid-call (~30 s hit)

AGENT_NAME = "Alex"
COMPANY = "Teravox"

# Every rule here exists because live testing produced the opposite. Left vague,
# llama3.2:3b invented an identity ("Karen from XYZ Communications"), insisted it
# was human when asked directly, and claimed capabilities it does not have
# ("let me check if there's an active call"). An outbound dialer that denies
# being an AI is also a compliance problem in most jurisdictions.
SYSTEM_PROMPT = (
    f"You are {AGENT_NAME}, an AI voice assistant for {COMPANY}. You are speaking "
    "out loud on a live phone call.\n"
    "Rules you must never break:\n"
    "- You ARE an AI. If asked whether you are a bot, an AI, or a real person, say "
    "so plainly. Never claim to be human.\n"
    "- One or two short sentences per reply. This is speech, not writing.\n"
    "- Never use lists, markdown, emoji, bullet points or stage directions.\n"
    "- Never invent facts about the caller, their account, prices, hours or orders. "
    "If you do not know, say so and offer to pass them to a person.\n"
    "- You cannot transfer, hold, hang up, or look anything up. Do not pretend to.\n"
    "- If you did not understand, ask them to repeat."
)
GREETING = f"Hi, this is {AGENT_NAME}, an AI assistant at {COMPANY}. How can I help?"
MAX_TURNS = 12                     # conversation history cap (user+assistant pairs)

# --- audio / telephony constants (AudioSocket is fixed at 8 kHz s16 mono)
RATE = 8000
FRAME_MS = 20
FRAME_SAMPLES = RATE * FRAME_MS // 1000        # 160
FRAME_BYTES = FRAME_SAMPLES * 2                # 320
STT_RATE = 16000                               # what Whisper wants

# --- VAD (energy based, adaptive noise floor)
VAD_START_FRAMES = 3               # 60 ms of speech opens an utterance
VAD_END_FRAMES = 50                # 1.0 s of silence closes it. 700 ms cut people
                                   # off mid-sentence on natural thinking pauses;
                                   # the extra 300 ms of dead air is the price.
VAD_SPEECH_MULT = 3.0              # RMS must exceed noise_floor * this
VAD_ABS_FLOOR = 180.0              # ...and this, to ignore pure line noise
VAD_MAX_UTTERANCE_S = 20
VAD_PREROLL_FRAMES = 12            # 240 ms kept before onset, else we clip the
                                   # first word (and barge-in eats even more)
BARGE_IN_FRAMES = 5                # 100 ms while we speak = real interruption
BARGE_IN_MULT = 3.5                # still stricter than normal VAD so our own audio
                                   # tail cannot trip it, but 5.0 was so strict that
                                   # live speech barely triggered it at all
BARGE_IN_GRACE_MS = 250            # ignore the first moments of our playback

# --- AudioSocket frame kinds
KIND_HANGUP, KIND_ID, KIND_AUDIO, KIND_ERROR = 0x00, 0x01, 0x10, 0xFF

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
log = logging.getLogger("ai-agent")


# ---------------------------------------------------------------- audio utils

def resample(pcm: np.ndarray, in_rate: int, out_rate: int) -> np.ndarray:
    """s16 mono -> s16 mono at out_rate. libswresample, so properly anti-aliased.

    A naive linear interpolation would alias badly on the 22050 -> 8000 TTS
    downsample; that is why this goes through PyAV rather than numpy.
    """
    if in_rate == out_rate:
        return pcm
    r = av.AudioResampler(format="s16", layout="mono", rate=out_rate)
    frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
    frame.sample_rate = in_rate
    out = [f.to_ndarray().reshape(-1) for f in r.resample(frame)]
    out += [f.to_ndarray().reshape(-1) for f in r.resample(None)]     # flush
    return np.concatenate(out) if out else np.zeros(0, dtype=np.int16)


def frame_kind(kind: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BH", kind, len(payload)) + payload


# ---------------------------------------------------------------- models (shared)

class Models:
    """Loaded once for the process. GPU/session access is serialized by locks."""

    def __init__(self) -> None:
        t = time.time()
        self.stt = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE,
                                compute_type=WHISPER_COMPUTE)
        log.info("whisper %s on %s ready (%.1fs)", WHISPER_MODEL, WHISPER_DEVICE,
                 time.time() - t)

        t = time.time()
        self.tts = PiperVoice.load(str(PIPER_VOICE))
        log.info("piper %s ready (%.1fs)", PIPER_VOICE.name, time.time() - t)

        self._stt_lock = threading.Lock()
        self._tts_lock = threading.Lock()
        self.http = httpx.Client(timeout=httpx.Timeout(60.0, connect=5.0))
        self._prewarm_llm()

    def _prewarm_llm(self) -> None:
        """Force the model into VRAM now. A cold load costs ~30 s, which would
        otherwise land in the middle of the first call."""
        t = time.time()
        try:
            self.http.post(f"{OLLAMA_URL}/api/chat", json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False, "keep_alive": OLLAMA_KEEP_ALIVE,
            })
            log.info("ollama %s warm (%.1fs)", OLLAMA_MODEL, time.time() - t)
        except Exception as exc:
            log.warning("ollama pre-warm failed: %s", exc)

    # -- STT
    def transcribe(self, pcm8k: np.ndarray) -> str:
        audio = resample(pcm8k, RATE, STT_RATE).astype(np.float32) / 32768.0
        with self._stt_lock:
            segs, _ = self.stt.transcribe(audio, language="en", beam_size=1,
                                          vad_filter=False)
            return " ".join(s.text for s in segs).strip()

    # -- LLM (streaming)
    def chat_stream(self, messages: list[dict], cancel: threading.Event):
        with self.http.stream("POST", f"{OLLAMA_URL}/api/chat", json={
            "model": OLLAMA_MODEL, "messages": messages,
            "stream": True, "keep_alive": OLLAMA_KEEP_ALIVE,
            "options": {"temperature": 0.6, "num_predict": 120},
        }) as r:
            for line in r.iter_lines():
                if cancel.is_set():
                    return
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tok = obj.get("message", {}).get("content", "")
                if tok:
                    yield tok
                if obj.get("done"):
                    return

    # -- TTS
    def synth_8k(self, text: str) -> bytes:
        with self._tts_lock:
            chunks = list(self.tts.synthesize(text))
        if not chunks:
            return b""
        pcm = np.concatenate(
            [np.frombuffer(c.audio_int16_bytes, dtype=np.int16) for c in chunks])
        return resample(pcm, chunks[0].sample_rate, RATE).tobytes()


MODELS: Models  # set in __main__


# ---------------------------------------------------------------- VAD

class Vad:
    """Energy VAD with an adaptive noise floor.

    Deliberately dependency-free: webrtcvad/silero would be better on noisy
    lines, but on a clean 8 kHz digital path this is enough and adds nothing
    to install. Swap in silero-vad (onnxruntime is already here) if the real
    world proves noisier.
    """

    def __init__(self) -> None:
        self.noise = 150.0
        self.speech_run = 0
        self.silence_run = 0
        self.in_speech = False
        self.buf: list[np.ndarray] = []
        # Every inbound frame lands here, including while we are speaking. When
        # an utterance opens we seed it with this, so the onset (and anything
        # consumed proving a barge-in) is not lost.
        self.pre: deque[np.ndarray] = deque(maxlen=VAD_PREROLL_FRAMES)

    def observe(self, pcm: np.ndarray) -> None:
        self.pre.append(pcm)

    @staticmethod
    def _rms(pcm: np.ndarray) -> float:
        return float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)) + 1e-9)

    def is_speech(self, pcm: np.ndarray, mult: float) -> bool:
        rms = self._rms(pcm)
        speech = rms > max(self.noise * mult, VAD_ABS_FLOOR)
        # track the floor only on non-speech, and rise slower than we fall
        if not speech:
            self.noise = 0.995 * self.noise + 0.005 * rms
        return speech

    def push(self, pcm: np.ndarray) -> np.ndarray | None:
        """Feed one 20 ms frame. Returns the utterance PCM when it completes."""
        speech = self.is_speech(pcm, VAD_SPEECH_MULT)

        if speech:
            self.speech_run += 1
            self.silence_run = 0
        else:
            self.silence_run += 1
            self.speech_run = 0

        if not self.in_speech:
            if self.speech_run >= VAD_START_FRAMES:
                self.open()
            return None

        self.buf.append(pcm)
        too_long = len(self.buf) * FRAME_MS / 1000.0 >= VAD_MAX_UTTERANCE_S
        if self.silence_run >= VAD_END_FRAMES or too_long:
            utt = np.concatenate(self.buf)
            self.reset()
            return utt
        return None

    def open(self) -> None:
        """Enter speech capture, seeded with the pre-roll already buffered."""
        self.in_speech = True
        self.buf = list(self.pre)
        self.silence_run = 0

    def reset(self) -> None:
        self.in_speech = False
        self.speech_run = self.silence_run = 0
        self.buf = []


# ---------------------------------------------------------------- sentence cutter

_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")
_SOFT = re.compile(r"(?<=[,;:])\s+")
MIN_SENTENCE_CHARS = 12
SOFT_BREAK_AT = 60          # long clause with no full stop yet -> flush on comma


def sentences(tokens, cancel: threading.Event):
    """Turn an LLM token stream into speakable chunks as early as possible."""
    buf = ""
    for tok in tokens:
        if cancel.is_set():
            return
        buf += tok
        while True:
            m = _BREAK.search(buf)
            if m and len(buf[:m.start()].strip()) >= MIN_SENTENCE_CHARS:
                head, buf = buf[:m.end()].strip(), buf[m.end():]
                if head:
                    yield head
                continue
            if len(buf) >= SOFT_BREAK_AT:
                sm = list(_SOFT.finditer(buf))
                if sm:
                    cut = sm[-1].end()
                    head, buf = buf[:cut].strip(), buf[cut:]
                    if head:
                        yield head
                    continue
            break
    tail = buf.strip()
    if tail and not cancel.is_set():
        yield tail


# ---------------------------------------------------------------- call handler

class CallHandler(socketserver.BaseRequestHandler):

    def setup(self) -> None:
        self.uuid = "?"
        self.vad = Vad()
        self.out: queue.Queue[bytes | None] = queue.Queue()
        self.speaking = threading.Event()      # we are emitting audio
        self.barge = threading.Event()         # caller interrupted
        self.cancel = threading.Event()        # kill the in-flight worker
        self.stop = threading.Event()          # call is over
        self.history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.worker: threading.Thread | None = None
        self.speak_started = 0.0
        self.barge_run = 0

    # -- outbound: pace frames at 20 ms so barge-in can actually cut us off
    def _sender(self) -> None:
        next_at = time.monotonic()
        while not self.stop.is_set():
            try:
                chunk = self.out.get(timeout=0.1)
            except queue.Empty:
                if self.speaking.is_set() and self.out.empty():
                    self.speaking.clear()
                continue
            if chunk is None:
                self.speaking.clear()
                continue
            if self.barge.is_set():
                continue
            if not self.speaking.is_set():
                self.speaking.set()
                self.speak_started = time.monotonic()
                next_at = time.monotonic()
            for i in range(0, len(chunk), FRAME_BYTES):
                if self.stop.is_set() or self.barge.is_set():
                    break
                frame = chunk[i:i + FRAME_BYTES].ljust(FRAME_BYTES, b"\x00")
                try:
                    self.request.sendall(frame_kind(KIND_AUDIO, frame))
                except OSError:
                    self.stop.set()
                    return
                next_at += FRAME_MS / 1000.0
                delay = next_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_at = time.monotonic()      # we fell behind, resync

    def _drop_queued_audio(self) -> None:
        while True:
            try:
                self.out.get_nowait()
            except queue.Empty:
                break

    def say(self, text: str) -> None:
        pcm = MODELS.synth_8k(text)
        if pcm and not self.cancel.is_set():
            self.out.put(pcm)

    # -- the turn: STT -> LLM -> TTS
    def _handle_utterance(self, pcm: np.ndarray) -> None:
        cancel = self.cancel
        dur = len(pcm) / RATE
        t0 = time.time()
        try:
            text = MODELS.transcribe(pcm)
        except Exception as exc:
            log.exception("stt failed: %s", exc)
            return
        if cancel.is_set():
            return
        if not text or len(text) < 2:
            log.info("stt: (nothing usable) %.1fs audio", dur)
            return
        log.info("USER (%.1fs audio, stt %.2fs): %s", dur, time.time() - t0, text)

        self.history.append({"role": "user", "content": text})
        if len(self.history) > 1 + MAX_TURNS * 2:
            self.history = [self.history[0]] + self.history[-MAX_TURNS * 2:]

        reply_parts: list[str] = []
        t0 = time.time()
        first = None
        try:
            for sent in sentences(MODELS.chat_stream(self.history, cancel), cancel):
                if cancel.is_set():
                    return
                if first is None:
                    first = time.time() - t0
                reply_parts.append(sent)
                self.say(sent)
        except Exception as exc:
            log.exception("llm failed: %s", exc)
            return
        if reply_parts and not cancel.is_set():
            reply = " ".join(reply_parts)
            self.history.append({"role": "assistant", "content": reply})
            log.info("AGENT (first audio +%.2fs): %s", first or 0.0, reply)

    def _start_worker(self, pcm: np.ndarray) -> None:
        self.cancel = threading.Event()
        self.worker = threading.Thread(target=self._handle_utterance, args=(pcm,),
                                       name="turn", daemon=True)
        self.worker.start()

    # -- inbound
    def _recv_exact(self, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.request.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _on_audio(self, payload: bytes) -> None:
        pcm = np.frombuffer(payload, dtype=np.int16)
        self.vad.observe(pcm)          # always, so barge-in keeps its pre-roll

        if self.speaking.is_set():
            # barge-in: stricter threshold + a grace window, so the tail of our
            # own audio (or acoustic echo at the far end) cannot interrupt us
            grace = (time.monotonic() - self.speak_started) * 1000 < BARGE_IN_GRACE_MS
            if not grace and self.vad.is_speech(pcm, BARGE_IN_MULT):
                self.barge_run += 1
                if self.barge_run >= BARGE_IN_FRAMES:
                    log.info("barge-in")
                    self.barge.set()
                    self.cancel.set()
                    self._drop_queued_audio()
                    self.speaking.clear()
                    self.barge.clear()
                    self.barge_run = 0
                    # go straight into capture: the words that proved the
                    # interruption are the start of the caller's utterance
                    self.vad.open()
            else:
                self.barge_run = 0
            return

        utt = self.vad.push(pcm)
        if utt is not None:
            if self.worker and self.worker.is_alive():
                self.cancel.set()          # user talked over a turn still in flight
            self._start_worker(utt)

    def handle(self) -> None:
        peer = self.client_address
        log.info("call from %s", peer)
        threading.Thread(target=self._sender, name="send", daemon=True).start()
        self.say(GREETING)
        try:
            while not self.stop.is_set():
                header = self._recv_exact(3)
                if header is None:
                    break
                kind, length = struct.unpack(">BH", header)
                payload = self._recv_exact(length) if length else b""
                if length and payload is None:
                    break

                if kind == KIND_AUDIO:
                    self._on_audio(payload)
                elif kind == KIND_ID:
                    self.uuid = payload.hex()
                    log.info("call uuid %s", self.uuid)
                elif kind == KIND_HANGUP:
                    log.info("hangup")
                    break
                elif kind == KIND_ERROR:
                    log.warning("asterisk error frame: %r", payload)
        except OSError as exc:
            log.info("socket closed: %s", exc)
        finally:
            self.stop.set()
            self.cancel.set()
            log.info("call ended %s", peer)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    MODELS = Models()
    log.info("AI agent listening on %s:%d", HOST, PORT)
    with Server((HOST, PORT), CallHandler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            log.info("bye")
