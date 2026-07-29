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

## 3b. Outbound calls (P4b)

Inbound is a call arriving at ext 5000. **Outbound is the reverse**: we place the
call, and when the lead answers, Asterisk sends the answered channel to ext 5001,
which hands it to the agent. The agent speaks first, because it rang them.

Install the outbound dialplan the same way:

```bash
scp asterisk/extensions_ai_outbound.conf root@192.168.122.10:/etc/asterisk/
ssh root@192.168.122.10 '
  grep -q extensions_ai_outbound.conf /etc/asterisk/extensions.conf \
    || echo "#include extensions_ai_outbound.conf" >> /etc/asterisk/extensions.conf
  asterisk -rx "dialplan reload"'
```

Then place calls with the dialer:

```bash
# against the scripted answerer, no human needed
host_ai/outbound.py --tunnel --to Local/6100@ai-outbound-test

# to the real softphone (you have to pick up)
host_ai/outbound.py --tunnel --to SIP/aitest

# a small sequential campaign
host_ai/outbound.py --tunnel --to Local/6100@ai-outbound-test --count 3
```

It reports a per-call outcome — `answered`, `busy`, `no answer`, `failed` — which
is the whole reason it speaks AMI rather than shelling out to `asterisk -rx`.

**`--tunnel` forwards AMI over SSH. Do not open port 5038 on the VM instead.**
ViciBox's firewalld carries enormous geoblock ipsets; a `firewall-cmd --reload`
times out on dbus, leaves firewalld in state `failed`, and installs a deny-all
nftables ruleset that locks you out of the VM completely — SSH, HTTP and ping all
gone. Recovering means driving the console with `virsh send-key` to run
`nft flush ruleset`. The tunnel needs no firewall change at all.

### How the agent knows which direction a call is

AudioSocket passes the server exactly one piece of metadata: the 16-byte UUID.
Channel variables do not cross it. So direction is encoded in the UUID, and
`CALL_PROFILES` in `ai_agent.py` maps it:

| Direction | Extension | UUID |
|---|---|---|
| inbound | 5000 | `11111111-2222-3333-4444-555555555555` |
| outbound | 5001 | `22222222-3333-4444-5555-666666666666` |

This matters because outbound must open the conversation and disclose that it is
an automated AI call up front, which is a legal requirement for outbound dialing
in many jurisdictions. Keep the dialplan UUIDs and `CALL_PROFILES` in sync.

## 3c. VICIdial integration (P5)

Everything above bypasses VICIdial: the test peer sits in `context=ai-agent` and
calls are placed by our own script. P5 connects the agent to VICIdial's own data,
so a call knows **who** it is to and **why**, and its outcome lands back on the
lead record.

```bash
bash scripts/setup_vicidial_ai.sh                  # idempotent
scp asterisk/extensions_ai_outbound.conf root@192.168.122.10:/etc/asterisk/
ssh root@192.168.122.10 'asterisk -rx "dialplan reload"'
```

That creates an API user (`aiagent`), campaign `AIOUT`, a script holding the call
purpose, list `1001`, three leads, and the `AICOMP` / `AINOCO` dispositions.

Run a campaign:

```bash
# unattended: the "leads" are the scripted answerer, nobody picks up
host_ai/outbound.py --tunnel --campaign AIOUT --lead-context ai-lead-dial-sim --count 3

# real: each lead's number rings the softphone, you answer
host_ai/outbound.py --tunnel --campaign AIOUT --count 1
```

A grounded call looks like this — note the agent greets by name and city from the
CRM rather than inventing an opening:

```
lead 8: Ayesha Khan (9001)
call direction: outbound
USER: Yes, speaking. What is this about?
AGENT: Hello Ayesha Khan in Lahore. This is Alex from Teravox.
disposition lead=8 status=AICOMP -> ok
```

and afterwards, in VICIdial:

```
lead_id  status   comments
8        AICOMP   AI call, 1 caller turn(s). caller: Yes, speaking... | agent: Hello Ayesha...
```

### How the lead reaches the agent

AudioSocket passes the server **only** the 16-byte UUID, and channel variables do
not cross it. So the `lead_id` rides in the UUID's last segment:

```
inbound   11111111-2222-3333-4444-555555555555
outbound  22222222-3333-4444-5555-<lead_id as 12 hex digits>
```

`outbound.py` builds it, passes it as the `AI_UUID` channel variable on the
Originate, ext 5001 feeds it to `AudioSocket()`, and the agent decodes it. No side
channel and no shared state to fall out of sync.

**Direction must therefore be matched by PREFIX, not by exact UUID.** An
exact-match table classified every campaign call as *inbound*, which silently
skipped both the lead lookup and the write-back — the call worked, it just was not
integrated with anything.

### VICIdial API gotchas that cost real time

- **The Non-Agent API gates on two independent layers.** `api_allowed_functions`
  must list the function, *and* each function checks its own columns.
  `lead_search` alone needs `vdc_agent_api_access='1'`,
  `modify_leads IN('1'..'5')` and `user_level > 7`. Satisfy only the first and you
  get a bare `USER DOES NOT HAVE PERMISSION` that never says which flag is missing.
- **Those permission columns are ENUMs** like `enum('0','1')`. Assigning the
  *integer* `1` selects the **first** enum element, which is `'0'` — the opposite
  of what you meant. Quote them.
- **`vicidial_statuses.status` is `varchar(6)`.** Longer codes are silently
  truncated (`AINOCON` became `AINOCO`), so the agent then writes a status that
  does not match any defined one.
- `add_user` via the API needs a permission the stock admin lacks, so the setup
  script creates the API user directly in the database.
- `stage=json` on any function returns parseable JSON instead of positional
  pipe-delimited text. Use it.

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
