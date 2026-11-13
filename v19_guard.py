#!/usr/bin/env python3
"""Fail-closed OpenAI quota circuit for the SANDBOX-only AI trader.

A credit-balance error blocks all further billable requests until explicitly
reset by the operator after verifying OpenAI billing. No automatic reset.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from datetime import datetime, timezone

BLOCK_PATH = Path.home() / ".local/share/tinvest-market/openai-quota-block.json"

def is_quota_error(message):
    msg = str(message).lower()
    return ("credit_balance_exhausted" in msg or
            "insufficient_quota" in msg)

def block_status():
    if not BLOCK_PATH.exists():
        return {"blocked": False}
    try:
        data = json.loads(BLOCK_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("blocked") is True and data.get("reason") == "insufficient_quota":
            return {"blocked": True, "reason": "insufficient_quota", "since_utc": data.get("since_utc")}
        if isinstance(data, dict) and data.get("blocked") is False:
            return {"blocked": False}
    except (OSError, ValueError, TypeError):
        pass
    return {"blocked": True, "reason": "breaker_state_unverified"}


def is_blocked():
    return block_status()["blocked"]


def block_quota():
    BLOCK_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    old = os.umask(0o077)
    temp = BLOCK_PATH.with_suffix(".tmp")
    try:
        temp.write_text(json.dumps({"blocked": True, "reason": "insufficient_quota",
            "since_utc": datetime.now(timezone.utc).isoformat()}) + "\n",
            encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(BLOCK_PATH)
    finally:
        os.umask(old)

def reset_quota():
    # Explicit maintenance operation; never triggered by a scheduled run.
    BLOCK_PATH.unlink(missing_ok=True)
