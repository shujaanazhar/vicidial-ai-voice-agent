# P1 — Softphone setup (the simulated customer)

This is how the test loop stays at **$0**: the "customer" is a SIP softphone
running on the host. No DID, no carrier, no Twilio, no PSTN. It registers to
Asterisk inside the VM over the private libvirt network and dials the AI.

The softphone goes on the **host**, not in the VM. The VM has no desktop and no
audio devices.

## 1. Give Asterisk a peer for it

`asterisk/sip_ai_test.conf` in this repo defines a dedicated peer. Copy it into
the VM and include it:

```bash
scp asterisk/sip_ai_test.conf root@192.168.122.10:/etc/asterisk/
ssh root@192.168.122.10 '
  grep -q sip_ai_test.conf /etc/asterisk/sip.conf \
    || echo "#include sip_ai_test.conf" >> /etc/asterisk/sip.conf
  asterisk -rx "sip reload"'
```

Two things about that file worth understanding:

- **`context=ai-agent`.** VICIdial auto-generates its own phones into
  `sip-vicidial.conf` (whose header literally says "ANY EDITS YOU MAKE WILL BE
  LOST") and puts them in `context=default`. Rather than patch VICIdial's
  dialplan, this peer sits straight in `ai-agent`, so dialing 5000 reaches the
  AudioSocket agent with nothing of VICIdial's in between.
- **It is a `chan_sip` peer, not PJSIP.** `chan_sip` is deprecated in Asterisk 18
  but it is what owns UDP 5060 on ViciBox (PJSIP is parked on 5061). Matching the
  stack VICIdial already uses avoids a port fight.

Verify it registered later with `asterisk -rx "sip show peers"` — you want
`aitest ... OK (nn ms)`.

## 2. Install baresip on the host

```bash
sudo apt install -y baresip
```

**Why baresip and not Linphone.** Linphone Desktop 5.0.2 (the Ubuntu 24.04
package) placed the call correctly and then **segfaulted one second later**, in
its QML chat-room code, before sending a single RTP packet. Asterisk held the
dead call for 62 seconds. baresip is a console client, has no GUI to crash, and
is what this project is tested with.

## 3. Configure it

`baresip` writes `~/.baresip/` on first run, so start it once and quit
(`/quit`). Then:

**`~/.baresip/accounts`** — replace the contents with:

```
<sip:aitest@192.168.122.10>;auth_pass=aitest123;audio_codecs=pcmu,pcma;ptime=20;regint=60
```

`pcmu`/`pcma` are G.711, already 8 kHz, so there is **no transcoding** between
the phone and our pipeline. `ptime=20` matches AudioSocket's 20 ms framing.

**`~/.baresip/config`** — switch the audio driver from ALSA to PulseAudio, so it
follows whatever your system default output/input is:

```
module                  pulse.so
audio_player            pulse,default
audio_source            pulse,default
audio_alert             pulse,default
```

(The generated file ships `alsa.so` / `alsa,default` on those four lines. If your
default sink is a USB headset, ALSA's `default` will often be the wrong device.)

## 4. Call the agent

Start the agent on the host first (see `03_running_and_testing.md`), then:

```bash
baresip
```

Wait for `{0} sip:aitest@192.168.122.10 - Ready`, then:

```
/dial sip:5000@192.168.122.10
```

You should hear the greeting. Talk normally. `/hangup` ends the call, `/quit`
exits.

**Use a headset.** With open speakers, the agent's own voice comes back in
through your microphone and can self-trigger barge-in. There is a 250 ms grace
window and a stricter threshold to guard against this, but it is only tested
with a headset.

---

**Gotchas**

- Dial the **full SIP URI** (`sip:5000@192.168.122.10`), not bare `5000`. Bare
  extensions depend on the client's default proxy/identity being right, which is
  a common source of "nothing happens when I press call".
- If `sip show peers` says `UNKNOWN`/offline, the phone never registered. Check
  you can reach the VM at all first (`ping 192.168.122.10`).
- ViciBox's `sip.conf` has `externip` set to the host's public IP, but its
  `localnet` lines already cover RFC1918, so traffic on 192.168.122.0/24 is
  treated as local and not NAT-rewritten. Leave that alone.
