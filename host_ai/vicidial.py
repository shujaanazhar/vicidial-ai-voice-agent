#!/usr/bin/env python3
"""
vicidial.py — P5: read lead context from VICIdial and write outcomes back.

Talks to VICIdial's **Non-Agent API** over plain HTTP on port 80. That port is
already reachable from the host, so unlike AMI this needs no SSH tunnel and no
firewall change (see host_ai/outbound.py for why that matters on ViciBox).

Two jobs:
  * before/at call start — fetch who we are calling and why, so the agent states
    a real reason instead of inventing one. Left ungrounded, the model opened an
    outbound call with "I called to check in on your account", which was fiction.
  * after the call — write a disposition and the transcript onto the lead record,
    so the call is visible in VICIdial's reporting rather than only in our logs.

API PERMISSIONS, THE PART THAT WASTES AN HOUR:
The Non-Agent API gates on TWO independent layers.
  1. `api_allowed_functions` on the user must contain the function name (or
     ALL_FUNCTIONS).
  2. Each function ALSO checks its own columns. `lead_search` for instance needs
     vdc_agent_api_access='1' AND modify_leads IN('1'..'5') AND user_level > 7.
Satisfying only the first gives a bare "USER DOES NOT HAVE PERMISSION" that does
not say which flag is missing. scripts/setup_vicidial_ai.sh sets both layers.

Also note those permission columns are ENUMs like enum('0','1'): assigning the
INTEGER 1 selects the FIRST enum element, i.e. '0'. Quote them.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

import httpx

VICI_HOST = os.environ.get("VICI_HOST", "192.168.122.10")
VICI_API_USER = os.environ.get("VICI_API_USER", "aiagent")
VICI_API_PASS = os.environ.get("VICI_API_PASS", "aiagent1234")
VICI_SOURCE = "ai_voice_agent"

API_URL = f"http://{VICI_HOST}/vicidial/non_agent_api.php"

log = logging.getLogger("vicidial")


@dataclass
class Lead:
    lead_id: str
    first_name: str = ""
    last_name: str = ""
    phone_number: str = ""
    city: str = ""
    comments: str = ""
    status: str = ""
    list_id: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()

    def prompt_block(self, purpose: str = "") -> str:
        """Render the lead as instructions to bolt onto the system prompt."""
        lines = ["\nWho you are calling, from the CRM record. Use it; do not invent"
                 " anything beyond it:"]
        if self.name:
            lines.append(f"- Name: {self.name}")
        if self.city:
            lines.append(f"- City: {self.city}")
        if self.comments:
            lines.append(f"- Account note: {self.comments}")
        if purpose:
            lines.append(f"\nWhy you are calling: {purpose}")
        lines.append("\nIf they ask something the record above does not answer, say you "
                     "do not have it to hand rather than guessing.")
        return "\n".join(lines)


class Vicidial:
    """Thin Non-Agent API client. Deliberately tolerant: a CRM hiccup must never
    take a live call down, so every method degrades to None/False and logs."""

    def __init__(self, timeout: float = 6.0) -> None:
        self.http = httpx.Client(timeout=timeout)

    def _call(self, function: str, **params) -> str | None:
        try:
            r = self.http.get(API_URL, params={
                "source": VICI_SOURCE, "user": VICI_API_USER,
                "pass": VICI_API_PASS, "function": function, **params})
            r.raise_for_status()
            body = r.text.strip()
        except Exception as exc:
            log.warning("vicidial %s failed: %s", function, exc)
            return None
        if body.startswith("ERROR"):
            log.warning("vicidial %s refused: %s", function, body[:200])
            return None
        return body

    def get_lead(self, lead_id: str | int) -> Lead | None:
        body = self._call("lead_all_info", lead_id=str(lead_id), stage="json")
        if not body:
            return None
        try:
            rows = json.loads(body).get("data") or []
            d = rows[0]
        except (json.JSONDecodeError, IndexError, AttributeError) as exc:
            log.warning("lead_all_info unparseable: %s", exc)
            return None
        return Lead(
            lead_id=d.get("lead_id", str(lead_id)),
            first_name=d.get("first_name", ""), last_name=d.get("last_name", ""),
            phone_number=d.get("phone_number", ""), city=d.get("city", ""),
            comments=d.get("comments", ""), status=d.get("status", ""),
            list_id=d.get("list_id", ""), raw=d)

    def set_disposition(self, lead_id: str | int, status: str,
                        comments: str = "") -> bool:
        """Write the outcome back onto the lead.

        `status` must be a status VICIdial knows or it silently means nothing in
        reports — see vicidial_statuses / the campaign's own statuses.
        """
        params = {"lead_id": str(lead_id), "status": status}
        if comments:
            # VICIdial's comments column is modest; keep the tail, which is where
            # the outcome of a conversation actually is.
            params["comments"] = comments[-495:]
        ok = self._call("update_lead", **params) is not None
        log.info("disposition lead=%s status=%s -> %s", lead_id, status,
                 "ok" if ok else "FAILED")
        return ok

    def campaign_purpose(self, campaign_id: str) -> str:
        """The campaign's script text, i.e. what this call is actually about.

        No Non-Agent API function returns script text, so this reads the value the
        setup script stashed locally. Keeping it here means ai_agent.py does not
        care where it came from.
        """
        return CAMPAIGN_PURPOSES.get(campaign_id.upper(), "")

    def close(self) -> None:
        self.http.close()


# Filled by scripts/setup_vicidial_ai.sh's counterpart on the host. Kept as a
# plain dict because the API exposes no script-text endpoint, and shelling into
# the VM mid-call to read MySQL would put SSH on the critical path of a live call.
CAMPAIGN_PURPOSES: dict[str, str] = {
    "AIOUT": ("You are calling to confirm the delivery address for order 4471 and "
              "to ask whether Saturday or Sunday suits them better for delivery. "
              "Do not discuss pricing, and do not promise a specific delivery time."),
}


# --- UUID <-> lead plumbing ------------------------------------------------
# AudioSocket hands the server exactly one piece of metadata: a 16-byte UUID.
# Channel variables do not cross it. So the lead_id rides in the UUID's last
# segment, which the dialer fills in and the agent reads back. No side channel,
# no shared state, nothing to get out of sync.
#
#   inbound   11111111-2222-3333-4444-555555555555
#   outbound  22222222-3333-4444-5555-<lead_id as 12 hex digits>
OUTBOUND_UUID_PREFIX = "22222222-3333-4444-5555-"


def uuid_for_lead(lead_id: str | int) -> str:
    return f"{OUTBOUND_UUID_PREFIX}{int(lead_id):012x}"


def lead_id_from_uuid(uuid_hex: str) -> str | None:
    """uuid_hex is the raw 32-hex-char form the AudioSocket ID frame carries."""
    bare = uuid_hex.replace("-", "").lower()
    if len(bare) != 32 or not bare.startswith(OUTBOUND_UUID_PREFIX.replace("-", "")):
        return None
    try:
        lead = int(bare[-12:], 16)
    except ValueError:
        return None
    return str(lead) if lead else None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    v = Vicidial()
    lead_id = sys.argv[1] if len(sys.argv) > 1 else "8"
    lead = v.get_lead(lead_id)
    print(f"lead {lead_id}: {lead}")
    if lead:
        print("--- prompt block ---")
        print(lead.prompt_block(v.campaign_purpose("AIOUT")))
        print("--- uuid ---")
        u = uuid_for_lead(lead_id)
        print(u, "->", lead_id_from_uuid(u.replace("-", "")))
    v.close()
