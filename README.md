# VICIdial AI Voice Agent

AI voice agents (STT → LLM → TTS) wired into **VICIdial** for **inbound** and
**outbound** calls, with barge-in. Sub-second response latency.

**Everything runs locally.** No cloud APIs, no OpenAI, no Deepgram, no
ElevenLabs, no Twilio, no DID, no carrier, no PSTN. Speech recognition, the
language model, and speech synthesis all run on your own GPU/CPU. The
"customers" are SIP softphones on a private subnet. Total running cost: **$0**,
and no audio ever leaves the machine.

```
  [Host: your machine]                    [KVM/libvirt VM: VICIbox]
  ┌─────────────────────────┐            ┌──────────────────────────┐
  │ AI agent (Python)        │            │ VICIdial                 │
  │  VAD → STT → LLM → TTS    │◄──────────►│  Asterisk (SIP/RTP)      │
  │  AudioSocket TCP server  │ AudioSocket│  MySQL / Perl dialer     │
  │                          │  (8k PCM)  │  Apache/PHP web UI       │
  │ Softphone (fake customer)│◄──SIP/RTP─►│                          │
  └─────────────────────────┘            └──────────────────────────┘
     all on the libvirt NAT network virbr0 (192.168.122.0/24), zero PSTN
     host = 192.168.122.1   ·   VM = 192.168.122.10
```

VICIdial is **not** the telephony engine — **Asterisk** is. VICIdial is the
campaign manager, dialer daemons, lead DB, and web UI on top. The AI hooks into
**Asterisk audio** via AudioSocket; VICIdial's lead/campaign/reporting layer is
wired in last.

## AI stack

| Layer | Tool | Runs on |
|-------|------|---------|
| STT | faster-whisper (`small.en`) | GPU |
| LLM | Ollama (`qwen2.5:7b`; `llama3.2:3b` is faster but less reliable) | GPU |
| TTS | Piper (`en_US-lessac-medium`) | CPU |
| VAD | energy-based, adaptive noise floor | CPU |

**AudioSocket** is the integration mechanism: a simple TCP protocol in Asterisk
18+ that streams 8 kHz/16-bit mono PCM both ways. Asterisk connects *out* to our
Python server. (ARI + externalMedia was the alternative; more moving parts.)

## Requirements

### Hardware

| | Minimum | This was built and tested on |
|---|---|---|
| GPU | NVIDIA, 6 GB VRAM, CUDA 12 capable | RTX 4060 Laptop, 8 GB |
| RAM | 16 GB (8 GB goes to the VM) | 31 GB |
| CPU | 4 cores free for the VM | 24 threads |
| Disk | **~20 GB free** (VM ~12 GB, models ~3 GB, ISO 2.2 GB) | — |

A GPU is not strictly required — faster-whisper falls back to CPU — but latency
stops being conversational.

### Software (host)

- **Ubuntu 24.04** (or similar; tested on 24.04.4, kernel 7.0)
- **Python 3.12**
- **KVM/libvirt** — `qemu-system-x86`, `libvirt-daemon-system`, `libvirt-clients`,
  `virt-manager`, `virtinst`, `cpu-checker`
- **baresip** — the test softphone
- **NVIDIA driver** with CUDA 12 support
- Ollama (installed by `scripts/setup_host_ai.sh`)

> **VirtualBox will not work on recent kernels.** On Linux 7.0, `vboxdrv` fails
> at `MODPOST` because the kernel reserves `kvm_enable_virtualization` and
> friends to KVM's own modules by name, and `MODULE_IMPORT_NS` cannot bypass a
> module-name-restricted export. Use KVM. `docs/01_vm_setup.md` §0 has the full
> diagnosis.

### Guest

- **ViciBox V12.0.2** (openSUSE-based, ships Asterisk 18 + PJSIP + VICIdial)
- Asterisk **18+** is mandatory — AudioSocket does not exist before it. ViciBox
  v9 (Asterisk 13/16) has no `app_audiosocket` and cannot run this project.

## Setup

Follow in order. Steps 1 and 2 are independent, so run them in parallel — the
host stack installs while the VM builds.

| # | Step | Doc |
|---|------|-----|
| 1 | Build the VM (KVM + ViciBox + networking) | [`docs/01_vm_setup.md`](docs/01_vm_setup.md) |
| 2 | Install the host AI stack | `bash scripts/setup_host_ai.sh` |
| 3 | Set up the test softphone | [`docs/02_softphone_setup.md`](docs/02_softphone_setup.md) |
| 4 | Install the dialplan, run the agent, test it | [`docs/03_running_and_testing.md`](docs/03_running_and_testing.md) |

Quick version once the VM exists:

```bash
bash scripts/setup_host_ai.sh                       # Ollama + venv + Piper voice
scp asterisk/*.conf root@192.168.122.10:/etc/asterisk/   # then #include them
host_ai/run_agent.sh                                # start the agent
baresip                                             # then /dial sip:5000@192.168.122.10
```

## Layout

```
docs/01_vm_setup.md              KVM/libvirt + ViciBox + networking runbook
docs/02_softphone_setup.md       baresip as the simulated customer
docs/03_running_and_testing.md   dialplan install, running, tests, latency
scripts/setup_host_ai.sh         installs Ollama + model, faster-whisper, Piper
scripts/make_test_audio.py       generates the simulated-caller WAVs
requirements.txt                 pinned host Python deps
host_ai/ai_agent.py              the agent: VAD→STT→LLM→TTS + barge-in
host_ai/run_agent.sh             launcher (sets the LD_LIBRARY_PATH CTranslate2 needs)
host_ai/outbound.py              P4b outbound dialer: AMI Originate + call outcomes
host_ai/audiosocket_echo.py      echo server — proves the audio pipe in isolation
asterisk/extensions_ai.conf      dialplan: route inbound ext 5000 to AudioSocket
asterisk/extensions_ai_outbound.conf  dialplan: outbound ext 5001 + scripted answerer
asterisk/extensions_ai_test.conf test harness: drive the agent with no softphone
asterisk/sip_ai_test.conf        chan_sip peer for the test softphone
```

## Phases

| Phase | What | Status |
|-------|------|--------|
| P0 | VM up (KVM/libvirt + ViciBox at 192.168.122.10) | **done** |
| — | Host AI stack install | **done**, GPU-verified |
| P1 | Prove SIP: softphone registers, real voice call | **done** |
| P2 | AudioSocket echo POC (prove host↔Asterisk audio) | **done** — 808 frames echoed |
| P3 | AI pipeline: VAD→STT→LLM→TTS + barge-in | **done** |
| P4a | **Inbound** feature: inbound route → AI answers | **done** — ext 5000 |
| P4b | **Outbound** feature: Originate/dialer → bridge AI | **done** — `host_ai/outbound.py`, ext 5001 |
| P5 | Wire into VICIdial (in-groups, campaigns, dispositions) | after P4 |

## Measured latency

RTX 4060 8 GB, `small.en` + `qwen2.5:7b` (both resident on GPU, 5.6/8 GB):

| Stage | Time |
|-------|------|
| STT (1–5 s of 8 kHz audio) | 0.03–0.32 s |
| LLM, to first speakable sentence | +0.21–0.41 s |
| TTS (3 s of speech) | 0.16 s |
| **End of caller speech → first agent audio** | **~0.2–0.5 s** |

Plus a deliberate **1.0 s** VAD endpoint before the agent decides you have
finished speaking. At 700 ms it cut people off during normal pauses.

Two design choices carry most of that: the LLM is consumed as a token stream cut
into sentences (so sentence 1 plays while sentence 2 generates), and outbound
audio is paced at 20 ms (so barge-in can actually interrupt, rather than the
whole reply already sitting in Asterisk's buffer).

## Known limitations

- **The model still invents facts, and a bigger model only moves the problem.**
  `llama3.2:3b` made up business hours; `qwen2.5:7b` stopped doing that but then
  asserted a completely invented description of the company. Neither can be
  prompted into reliability. This needs grounding, not a model swap.
- **Barge-in is only tested with a headset.** With open speakers the agent's own
  voice can re-enter the mic and self-trigger it. There is a 250 ms grace window
  and a stricter threshold, but it is untested.
- The agent offers to "pass you to a representative" but cannot yet — that
  arrives with P5.
- English only (`small.en`, `en_US-lessac-medium`).
- **Outbound invents a reason for calling.** The opening line is fixed, but the
  model's follow-up made up a pretext ("I called to check in on your account").
  The call's purpose has to be injected from campaign/lead data, not left to the
  model. That arrives with P5.

## Security note

`asterisk/sip_ai_test.conf` contains a throwaway SIP credential
(`aitest`/`aitest123`) for a peer that only exists on an isolated host-only
subnet with no route from outside. Change it if you expose this to anything real.

## License

MIT
