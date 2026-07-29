#!/usr/bin/env python3
"""Trusted_Leadbot (prod) polling bridge — serves the client's buttons while
the Hermes gateway stays on the test bot (@Pparestertesterbot).

Why this exists: the Hermes gateway supports one telegram platform (one bot
token) per process. The operator's agent channel must stay on the test bot
untouched, so this lightweight bridge long-polls the PROD bot and reuses the
exact same script-button contract the gateway uses:

  callback_query "cb:<verb>"  → ~/.hermes/scripts/callback-buttons/<verb>.sh <chat_id> <msg_id> <user_id>
                                env: PPA_TG_BOT_TOKEN=<prod token> so scripts answer AS the prod bot
                                toast = last non-empty stdout line
  text message                → armed pending file pending/<chat>_<user>.json →
                                <verb>.sh <chat_id> <msg_id> <user_id> <text>
  other text (allowlisted)    → fixed polite reply
  unknown users               → logged as NEW-USER (id capture) and ignored

Allowlist: TELEGRAM_ALLOWED_USER_IDS (comma-separated) from the ppa .env.
Offset persists in state/.prod_bridge_offset so restarts never reprocess.

launchd: com.ppa.prod-bot-bridge (KeepAlive). Logs: logs/prod-bot-bridge.log.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path("/Users/a2.0/ppa-leadengine")
SCRIPTS_DIR = Path.home() / ".hermes" / "scripts" / "callback-buttons"
PENDING_DIR = SCRIPTS_DIR / "pending"
OFFSET_FILE = ROOT / "state" / ".prod_bridge_offset"
PENDING_TTL = 600          # seconds an armed popup stays valid
SCRIPT_TIMEOUT = 300       # export can take a minute on a full pool
POLL_TIMEOUT = 25          # telegram long-poll
MAX_BACKOFF = 60

AUTO_REPLY = (
    "This is an automated lead-delivery bot. Use the buttons on the daily "
    "report to request reports or lead files. For help, contact your operator."
)


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _env(key: str) -> str:
    if os.environ.get(key):
        return os.environ[key]
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return ""


TOKEN = _env("TELEGRAM_BOT_TOKEN_PROD")
if not TOKEN:
    sys.exit("TELEGRAM_BOT_TOKEN_PROD missing in .env")
API = f"https://api.telegram.org/bot{TOKEN}"


def _allowed_users() -> set[str]:
    return {p.strip() for p in _env("TELEGRAM_ALLOWED_USER_IDS").split(",") if p.strip()}


def _api(method: str, payload: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        f"{API}/{method}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text().strip())
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OFFSET_FILE.with_suffix(".tmp")
    tmp.write_text(str(offset))
    tmp.replace(OFFSET_FILE)


def _run_script(verb: str, args: list[str]) -> tuple[int, str]:
    """Run callback script with prod-token override; return (rc, last stdout line)."""
    script = SCRIPTS_DIR / f"{verb}.sh"
    if not script.exists():
        return 127, f"no handler for {verb}"
    env = dict(os.environ)
    env["PPA_TG_BOT_TOKEN"] = TOKEN
    env.setdefault("HOME", str(Path.home()))
    try:
        proc = subprocess.run(
            [str(script)] + args, env=env,
            capture_output=True, text=True, timeout=SCRIPT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
    if proc.returncode != 0:
        err = (proc.stderr.strip().splitlines() or ["script error"])[-1][:150]
        return proc.returncode, f"failed: {err}"
    return 0, (lines[-1][:180] if lines else "Done")


def _handle_callback(query: dict) -> None:
    qid = query.get("id", "")
    user = query.get("from", {})
    user_id = str(user.get("id", ""))
    msg = query.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    msg_id = str(msg.get("message_id", ""))
    data = query.get("data") or ""

    if user_id not in _allowed_users():
        _log(f"unauthorized callback ignored: user={user_id} @{user.get('username','')}")
        try:
            _api("answerCallbackQuery", {"callback_query_id": qid, "text": "Not authorized."})
        except Exception:
            pass
        return
    if not data.startswith("cb:"):
        _api("answerCallbackQuery", {"callback_query_id": qid})
        return

    verb = data[3:]
    _log(f"callback: verb={verb} user={user_id} chat={chat_id}")
    rc, toast = _run_script(verb, [chat_id, msg_id, user_id])
    _log(f"script-button {'ok' if rc == 0 else 'FAIL'}: verb={verb} rc={rc} toast={toast!r}")
    try:
        _api("answerCallbackQuery", {"callback_query_id": qid, "text": toast})
    except Exception as exc:
        _log(f"answerCallbackQuery error: {exc}")


def _handle_message(msg: dict) -> None:
    user = msg.get("from", {})
    user_id = str(user.get("id", ""))
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    msg_id = str(msg.get("message_id", ""))
    text = (msg.get("text") or "").strip()

    if user_id not in _allowed_users():
        _log(f"NEW-USER id={user_id} @{user.get('username','')} "
             f"name={user.get('first_name','')} {user.get('last_name','')} text={text[:40]!r}")
        return

    # armed pending-input? (ForceReply popup protocol, same as gateway router)
    state_file = PENDING_DIR / f"{chat_id}_{user_id}.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except Exception:
            state = {}
        if time.time() - float(state.get("armed_at", 0)) <= PENDING_TTL and state.get("verb"):
            verb = state["verb"]
            state_file.unlink(missing_ok=True)
            _log(f"pending-input: verb={verb} user={user_id} text={text[:30]!r}")
            rc, out = _run_script(verb, [chat_id, msg_id, user_id, text])
            _log(f"script-pending {'ok' if rc == 0 else 'FAIL'}: verb={verb} rc={rc}")
            return
        state_file.unlink(missing_ok=True)  # expired

    if text:
        try:
            _api("sendMessage", {"chat_id": chat_id, "text": AUTO_REPLY})
        except Exception as exc:
            _log(f"auto-reply error: {exc}")


def main() -> None:
    offset = _load_offset()
    backoff = 5
    _log("prod bridge starting (Trusted_Leadbot)")
    while True:
        try:
            resp = _api("getUpdates", {
                "offset": offset, "timeout": POLL_TIMEOUT,
                "allowed_updates": ["message", "callback_query"],
            }, timeout=POLL_TIMEOUT + 15)
            backoff = 5
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                _log("409 conflict — another poller owns this token; backing off")
            else:
                _log(f"http error {exc.code}; retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue
        except Exception as exc:
            _log(f"poll error {type(exc).__name__}; retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue

        if not resp.get("ok"):
            _log(f"getUpdates not ok: {str(resp)[:120]}")
            time.sleep(backoff)
            continue

        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            try:
                if upd.get("callback_query"):
                    _handle_callback(upd["callback_query"])
                elif upd.get("message"):
                    _handle_message(upd["message"])
            except Exception as exc:
                _log(f"handler error: {type(exc).__name__}: {exc}")
        _save_offset(offset)


if __name__ == "__main__":
    main()
