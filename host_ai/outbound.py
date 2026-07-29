#!/usr/bin/env python3
"""
outbound.py — P4b: place outbound calls and bridge the AI agent onto them.

Drives Asterisk's Manager Interface (AMI) to Originate a call to a lead. When the
lead answers, Asterisk sends the answered channel to extension 5001, which hands
the audio to the AI over AudioSocket (see asterisk/extensions_ai_outbound.conf).
The agent recognises the outbound UUID and opens the conversation itself.

AMI, not `asterisk -rx` over ssh, because AMI also streams the call-progress
events — so we can tell answered from busy from no-answer, which is the whole
point of a dialer.

  ACCESS: use an SSH tunnel, do NOT open port 5038 on the VM.
  ViciBox's firewalld carries huge geoblock ipsets; a `firewall-cmd --reload`
  crashes it and leaves a deny-all ruleset that locks you out of the VM
  entirely. The tunnel needs no firewall change at all:

      ssh -N -L 5038:127.0.0.1:5038 root@192.168.122.10 &

  or let this script do it for you with --tunnel.

Usage:
    # one call to the registered test softphone, tunnel managed for you
    host_ai/outbound.py --tunnel --to SIP/aitest

    # a small campaign, sequentially, against the scripted answerer
    host_ai/outbound.py --tunnel --to Local/6100@ai-outbound-test --count 3

    # already have a tunnel open
    host_ai/outbound.py --to SIP/aitest
"""
from __future__ import annotations

import argparse
import logging
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import vicidial

# --- AMI ------------------------------------------------------------------
AMI_HOST, AMI_PORT = "127.0.0.1", 5038      # via the ssh tunnel
AMI_USER, AMI_SECRET = "cron", "1234"       # ViciBox's stock AMI account
VM_SSH = "root@192.168.122.10"

# --- what we dial and where the answered call goes ------------------------
OUTBOUND_CONTEXT = "ai-agent-outbound"
OUTBOUND_EXTEN = "5001"
# Lab stand-in for a carrier trunk; maps any lead number to the test softphone.
LEAD_DIAL_CONTEXT = "ai-lead-dial"
CALLER_ID = "Teravox AI <9000>"
ANSWER_TIMEOUT_MS = 30000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("outbound")


class Ami:
    """Minimal AMI client. Enough to Originate and follow one call's outcome."""

    def __init__(self, host: str = AMI_HOST, port: int = AMI_PORT) -> None:
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(1.0)
        self.buf = ""
        banner = self._read_line(timeout=5)
        log.info("AMI: %s", (banner or "").strip())

    # -- wire helpers
    def _read_line(self, timeout: float = 1.0) -> str | None:
        deadline = time.monotonic() + timeout
        while "\r\n" not in self.buf:
            if time.monotonic() > deadline:
                return None
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                return None
            self.buf += chunk.decode("utf-8", "replace")
        line, self.buf = self.buf.split("\r\n", 1)
        return line

    def read_message(self, timeout: float = 1.0) -> dict | None:
        """AMI messages are key: value lines terminated by a blank line."""
        msg: dict[str, str] = {}
        deadline = time.monotonic() + timeout
        while True:
            line = self._read_line(timeout=max(0.05, deadline - time.monotonic()))
            if line is None:
                return msg or None
            if line == "":
                if msg:
                    return msg
                continue
            if ": " in line:
                k, v = line.split(": ", 1)
                msg[k.strip()] = v.strip()

    def send(self, **fields) -> None:
        payload = "".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n"
        self.sock.sendall(payload.encode())

    # -- actions
    def login(self, user: str = AMI_USER, secret: str = AMI_SECRET) -> bool:
        self.send(Action="Login", Username=user, Secret=secret, Events="on")
        for _ in range(20):
            m = self.read_message(timeout=2)
            if m and m.get("Response") == "Success":
                log.info("logged in as %s", user)
                return True
            if m and m.get("Response") == "Error":
                log.error("login failed: %s", m.get("Message"))
                return False
        return False

    def originate(self, channel: str, action_id: str,
                  ai_uuid: str | None = None) -> None:
        """Async so AMI keeps streaming events while the call progresses.

        ai_uuid carries the lead_id through to the agent (see vicidial.py). It is
        passed as a channel variable and picked up by ext 5001's Set/AudioSocket,
        because AudioSocket itself passes nothing but that UUID.
        """
        log.info("originate -> %s (answered call goes to %s@%s)%s",
                 channel, OUTBOUND_EXTEN, OUTBOUND_CONTEXT,
                 f" uuid={ai_uuid}" if ai_uuid else "")
        fields = dict(
            Action="Originate", ActionID=action_id, Channel=channel,
            Context=OUTBOUND_CONTEXT, Exten=OUTBOUND_EXTEN, Priority=1,
            CallerID=CALLER_ID, Timeout=ANSWER_TIMEOUT_MS, Async="true",
        )
        if ai_uuid:
            fields["Variable"] = f"AI_UUID={ai_uuid}"
        self.send(**fields)

    def follow(self, seconds: float) -> str:
        """Watch events for one call and report how it ended.

        Asterisk reports the dial result on OriginateResponse; Newstate/Hangup
        give us the detail. Cause 16 is normal clearing, 17 busy, 19 no answer.
        """
        outcome = "unknown"
        answered = False
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            m = self.read_message(timeout=1.0)
            if not m:
                continue
            ev = m.get("Event", "")
            if ev == "Newstate" and m.get("ChannelStateDesc") == "Up":
                if not answered:
                    answered = True
                    outcome = "answered"
                    log.info("ANSWERED: %s", m.get("Channel"))
            elif ev == "OriginateResponse":
                if m.get("Response") != "Success" and not answered:
                    outcome = f"failed ({m.get('Reason', '?')})"
                    log.info("originate response: %s reason=%s",
                             m.get("Response"), m.get("Reason"))
            elif ev == "Hangup":
                cause = m.get("Cause", "?")
                txt = m.get("Cause-txt", "")
                log.info("hangup %s cause=%s %s", m.get("Channel"), cause, txt)
                if not answered:
                    outcome = {"17": "busy", "19": "no answer",
                               "21": "rejected"}.get(cause, f"cause {cause}")
                    break
        return outcome

    def close(self) -> None:
        try:
            self.send(Action="Logoff")
        except OSError:
            pass
        self.sock.close()


def open_tunnel(port: int = AMI_PORT) -> subprocess.Popen:
    """Forward AMI over SSH instead of poking a hole in the VM's firewall."""
    log.info("opening ssh tunnel localhost:%d -> %s:5038", port, VM_SSH)
    proc = subprocess.Popen(
        ["ssh", "-N", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
         "-o", "ExitOnForwardFailure=yes",
         "-L", f"{port}:127.0.0.1:5038", VM_SSH],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(40):                       # wait for the forward to be usable
        time.sleep(0.25)
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return proc
        except OSError:
            if proc.poll() is not None:
                err = (proc.stderr.read() or b"").decode(errors="replace")
                raise RuntimeError(f"ssh tunnel died: {err.strip()}")
    raise RuntimeError("ssh tunnel did not come up")


def leads_for_campaign(campaign: str, limit: int) -> list[dict]:
    """Pull dialable leads for a campaign straight out of VICIdial.

    Uses MySQL over the same SSH channel rather than the Non-Agent API, because no
    API function returns "the next N leads to call for this campaign" — that is
    the dialer's job, and here we are the dialer.
    """
    sql = (
        "SELECT l.lead_id, l.phone_number, l.first_name, l.last_name "
        "FROM vicidial_list l JOIN vicidial_lists s ON l.list_id = s.list_id "
        f"WHERE s.campaign_id = '{campaign}' AND s.active = 'Y' "
        f"AND l.status = 'NEW' ORDER BY l.lead_id LIMIT {int(limit)};"
    )
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", VM_SSH,
         f"mysql -u cron -p1234 asterisk -B -N -e \"{sql}\""],
        capture_output=True, text=True, timeout=20)
    leads = []
    for line in out.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            leads.append({"lead_id": parts[0], "phone": parts[1],
                          "name": " ".join(parts[2:]).strip()})
    if not leads:
        log.warning("no NEW leads for campaign %s (stderr: %s)",
                    campaign, out.stderr.strip()[:200])
    return leads


def main() -> None:
    ap = argparse.ArgumentParser(description="Place outbound AI calls via Asterisk AMI.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--to",
                     help="Asterisk channel to dial directly, e.g. SIP/aitest or "
                          "Local/6100@ai-outbound-test")
    src.add_argument("--campaign",
                     help="pull NEW leads from this VICIdial campaign and dial them, "
                          "passing each lead_id through to the agent (e.g. AIOUT)")
    ap.add_argument("--count", type=int, default=1, help="how many calls to place")
    ap.add_argument("--gap", type=float, default=3.0,
                    help="seconds to wait between calls")
    ap.add_argument("--watch", type=float, default=45.0,
                    help="seconds to follow each call's events")
    ap.add_argument("--tunnel", action="store_true",
                    help="open the ssh tunnel to AMI automatically")
    ap.add_argument("--lead-context", default=LEAD_DIAL_CONTEXT,
                    help="dialplan context that routes lead phone numbers "
                         "(ai-lead-dial = real softphone, ai-lead-dial-sim = "
                         "scripted answerer, no human needed)")
    ap.add_argument("--ami-host", default=AMI_HOST)
    ap.add_argument("--ami-port", type=int, default=AMI_PORT)
    args = ap.parse_args()

    # Build the call list: either one repeated channel, or real VICIdial leads.
    if args.campaign:
        leads = leads_for_campaign(args.campaign, args.count)
        if not leads:
            raise SystemExit(f"no dialable NEW leads in campaign {args.campaign}")
        calls = [(f"Local/{d['phone']}@{args.lead_context}",
                  vicidial.uuid_for_lead(d["lead_id"]),
                  f"lead {d['lead_id']} {d['name']}".strip()) for d in leads]
    else:
        calls = [(args.to, None, args.to)] * args.count

    tunnel = open_tunnel(args.ami_port) if args.tunnel else None
    try:
        ami = Ami(args.ami_host, args.ami_port)
        if not ami.login():
            raise SystemExit(1)
        results = []
        for i, (channel, ai_uuid, label) in enumerate(calls):
            outcome = None
            try:
                ami.originate(channel, action_id=f"aicall-{i}", ai_uuid=ai_uuid)
                outcome = ami.follow(args.watch)
            finally:
                results.append((label, outcome or "error"))
            log.info("call %d/%d [%s]: %s", i + 1, len(calls), label, results[-1][1])
            if i + 1 < len(calls):
                time.sleep(args.gap)
        ami.close()

        print("\n=== outbound results ===")
        for i, (label, r) in enumerate(results, 1):
            print(f"  {i}. {label}: {r}")
        answered = sum(1 for _, r in results if r == "answered")
        print(f"  answered {answered}/{len(results)}")
    finally:
        if tunnel:
            tunnel.terminate()
            log.info("tunnel closed")


if __name__ == "__main__":
    main()
