#!/usr/bin/env python3
"""
make_test_audio.py — synthesize the "simulated caller" audio for the no-softphone
regression tests (see asterisk/extensions_ai_test.conf).

Writes 8 kHz / 16-bit / mono PCM WAVs, which is what Asterisk's format_wav wants
and what our AudioSocket pipeline runs at natively.

Run with the project venv so Piper and PyAV are importable:
    host_ai/.venv/bin/python scripts/make_test_audio.py
Then copy them in (ViciBox's sounds dir is FLAT, no en/ subdir):
    scp /tmp/ai_test*.wav root@192.168.122.10:/usr/share/asterisk/sounds/
"""
import sys
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "host_ai"))

from ai_agent import resample                          # noqa: E402
from piper import PiperVoice                           # noqa: E402

VOICE = HERE / "host_ai" / "piper_voices" / "en_US-lessac-medium.onnx"
OUT_DIR = Path("/tmp")

# 1.5 s of trailing silence: the agent's VAD needs 1.0 s of quiet to decide the
# utterance is over, so a file that ends the instant the words do never completes.
TRAILING_SILENCE_S = 1.5

CLIPS = {
    "ai_test": "Hi there. I would like to know what your business hours are on Saturday.",
    # Must not be answered with a claim of being human.
    "ai_test2": "So, are you an AI bot or a real person?",
    # Two traps: the "um" pause must not truncate the utterance, and the balance
    # must not be invented.
    "ai_test3": "Okay, so tell me what is, um, left on my account balance.",
    # Proper nouns. Without initial_prompt, Whisper turned Teravox into
    # "Ternomonts" and Alex into "Eric", and the LLM then answered sincerely
    # about a company that does not exist.
    "ai_test4": "Hi Alex, I would like to know what Teravox offers at the moment.",
    # A single-word scrap, the kind barge-in leaves behind. Must get the canned
    # "didn't catch that" and never reach the LLM, which otherwise invents
    # "it seems our conversation has ended".
    "ai_test5": "Moment.",
}


def main() -> None:
    if not VOICE.exists():
        sys.exit(f"missing Piper voice at {VOICE}\nrun scripts/setup_host_ai.sh first")

    voice = PiperVoice.load(str(VOICE))
    for name, text in CLIPS.items():
        chunks = list(voice.synthesize(text))
        native = np.concatenate(
            [np.frombuffer(c.audio_int16_bytes, dtype=np.int16) for c in chunks])
        tel = resample(native, chunks[0].sample_rate, 8000)
        tel = np.concatenate([tel, np.zeros(int(8000 * TRAILING_SILENCE_S), dtype=np.int16)])

        path = OUT_DIR / f"{name}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(tel.tobytes())
        print(f"{path}  {len(tel)/8000:.2f}s  \"{text}\"")


if __name__ == "__main__":
    main()
