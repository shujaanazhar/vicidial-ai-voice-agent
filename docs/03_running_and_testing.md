# Running and testing the agent

## 1. Install the dialplan in the VM

The agent is reached by dialing extension **5000**. That extension lives in
`asterisk/extensions_ai.conf` and hands the call to AudioSocket, pointed at the
host:

```bash
scp asterisk/extensions_ai.conf      root@192.168.122.10:/etc/asterisk/
scp asterisk/extensions_ai_test.conf root@192.168.122.10:/etc/asterisk/
ssh root@192.168.122.10 '
  for f in extensions_ai.conf extensions_ai_test.conf; do
    grep -q "$f" /etc/asterisk/extensions.conf \
      || echo "#include $f" >> /etc/asterisk/extensions.conf
  done
  asterisk -rx "dialplan reload"
  asterisk -rx "dialplan show ai-agent"'
```

Confirm AudioSocket is actually available on your build — without it nothing
else here works:

```bash
ssh root@192.168.122.10 'asterisk -rx "module show like audiosocket"'
# want: app_audiosocket.so + chan_audiosocket.so + res_audiosocket.so, all Running
```

## 2. Start the agent on the host

```bash
host_ai/run_agent.sh
```

Always use the wrapper, never `python ai_agent.py` directly. It sets the
`LD_LIBRARY_PATH` that CTranslate2 needs to find cuDNN 9.x; without it model init
fails with an error that reads like a GPU fault but is really a path problem.

First run downloads the Whisper model (`small.en`, ~480 MB, ~140 s). After that
startup is a few seconds. Wait for:

```
whisper small.en on cuda ready
piper en_US-lessac-medium.onnx ready
ollama llama3.2:3b warm
AI agent listening on 0.0.0.0:8090
```

The Ollama pre-warm matters: a cold model load costs ~30 s, and without warming
it that delay lands in the middle of your first call.

## 3. Test without a softphone

This is the fast path for regressions. A Local channel has two halves: one is
bridged into the AudioSocket extension, the other plays a prerecorded caller into
it. Full STT → LLM → TTS proof, unattended, no SIP client involved.

Generate the audio and install it once:

```bash
host_ai/.venv/bin/python scripts/make_test_audio.py
scp /tmp/ai_test*.wav root@192.168.122.10:/usr/share/asterisk/sounds/
```

Note the sounds directory is **flat** on ViciBox — there is no `en/`
subdirectory, unlike a stock Asterisk install.

Then run any of the three:

```bash
ssh root@192.168.122.10 \
  'asterisk -rx "channel originate Local/5000@ai-agent extension 6000@ai-test"'
```

| Ext | What it checks |
|-----|----------------|
| 6000 | Clean single turn: full transcript, sensible reply |
| 6001 | Barge-in: talks over the greeting from millisecond zero |
| 6002 | Regression for the two worst defects found in live testing: must not claim to be human, must not invent an account balance, must not truncate a sentence containing an "um" pause |

Watch the agent's stdout. A healthy turn looks like:

```
USER (3.5s audio, stt 0.32s): So, are you an AI bot or a real person?
AGENT (first audio +0.30s): I'm an artificial intelligence voice assistant for Teravox.
```

## 4. Test with a real voice

See `02_softphone_setup.md`. Start the agent, then `baresip`, then
`/dial sip:5000@192.168.122.10`.

Worth doing even though the scripted tests pass. **Every defect that mattered was
found by talking to it, not by the scripted tests** — the VAD cutting people off
mid-sentence, and the model insisting it was a human named Karen. Prerecorded
audio has clean endpoints and no hesitation; people do not.

## 5. Measured latency

On an RTX 4060 8 GB with `small.en` + `llama3.2:3b`:

| Stage | Time |
|-------|------|
| STT (1–5 s of 8 kHz audio) | 0.03–0.32 s |
| LLM, to first speakable sentence | +0.21–0.41 s |
| TTS (3 s of speech) | 0.16 s |
| **End of caller speech → first agent audio** | **~0.2–0.5 s** |

On top of that sits the VAD endpoint: the agent waits **1.0 s** of silence before
deciding you have finished. That is deliberate. At 700 ms it cut people off
during normal mid-sentence pauses.

Two things keep the perceived latency low:

1. The LLM is consumed as a **token stream cut into sentences**, each synthesized
   and queued the moment it completes. The caller hears sentence 1 while
   sentence 2 is still generating.
2. Outbound audio is **paced at 20 ms** by a sender thread. Blasting it would
   make barge-in impossible, because the whole reply would already be sitting in
   Asterisk's buffer with no way to unsend it.

---

**Troubleshooting**

| Symptom | Cause |
|---------|-------|
| Call connects, total silence | Host firewall dropping Asterisk's outbound connection to :8090. If `ufw` is active: `sudo ufw allow in on virbr0 to any port 8090 proto tcp` |
| Call connects, agent logs the UUID but no `USER` lines | No RTP arriving. The softphone died, or its mic is muted |
| cuDNN / library load error at startup | You ran `ai_agent.py` directly instead of `run_agent.sh` |
| First reply takes ~30 s | Ollama cold-loaded. The pre-warm failed, check Ollama is running |
| Agent replies to its own voice | Speaker feedback into the mic. Use a headset |
