#!/usr/bin/env python3
"""Periodic WhatsApp watcher for Lumus (agy-ambient).

Polls whatsapp_api.py when the dedicated WhatsApp Chrome profile (port 9223) is
running, detects new/changed chats, asks Lumus to extract schedules/action items
and update whatsapp_schedule.md, and forwards important alerts to Telegram +
desktop notifications.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path(__file__).parent
CFG_FILE = BASE_DIR / "telegram.json"
STATE_DIR = Path.home() / ".local/state/agy-ambient"
SEEN_FILE = STATE_DIR / "whatsapp_seen.json"
SCHEDULE_FILE = STATE_DIR / "whatsapp_schedule.md"
TOKEN_LOG = STATE_DIR / "token_usage.jsonl"
WHATSAPP_API = (
    BASE_DIR / "tools" / "whatsapp_api.py"
    if (BASE_DIR / "tools" / "whatsapp_api.py").exists()
    else Path.home() / "whatsapp_api.py"
)
AGY_BIN = os.environ.get("AGY_BIN", str(Path.home() / ".local/bin/agy"))
DAEMON_URL = "http://127.0.0.1:47891"
CDP_PORT = int(os.environ.get("WHATSAPP_CDP_PORT", "9223"))


def cdp_is_up() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=1.5):
            return True
    except Exception:
        return False


def run_wa_api(*args: str) -> Dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, str(WHATSAPP_API), *args],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return {"error": proc.stderr.strip() or f"exit {proc.returncode}"}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": proc.stdout[:200]}


def daemon_get(path: str) -> Dict[str, Any]:
    with urllib.request.urlopen(f"{DAEMON_URL}{path}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def daemon_post(path: str, payload: dict) -> Dict[str, Any]:
    req = urllib.request.Request(
        f"{DAEMON_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def send_telegram(text: str) -> None:
    if not CFG_FILE.exists():
        return
    cfg = json.loads(CFG_FILE.read_text(encoding="utf-8"))
    token = cfg["bot_token"].strip()
    chat_id = str(cfg.get("chat_id", "")).strip()
    if not token or not chat_id:
        return
    for i in range(0, len(text), 4000):
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text[i : i + 4000]}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        with urllib.request.urlopen(req, timeout=15):
            pass


def notify_desktop(title: str, body: str) -> None:
    try:
        subprocess.run(
            ["notify-send", "-a", "Lumus WhatsApp", title, body[:300]],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def chat_fingerprint(chat: Dict[str, Any]) -> str:
    raw = f"{chat.get('time', '')}|{chat.get('unread', '')}|{chat.get('preview', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def ask_minie(prompt: str) -> str:
    before = daemon_get("/status")
    if before.get("state") == "thinking":
        return ""
    prev_turns = before.get("turns_count", 0)
    res = daemon_post("/prompt", {"text": prompt, "silent": True})
    if not res.get("ok"):
        return ""

    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(0.6)
        st = daemon_get("/status")
        if st.get("turns_count", 0) > prev_turns and st.get("state") != "thinking":
            return (st.get("last_response") or "").strip()
    return ""


def ask_model(
    prompt: str, model: str = "gemini-3.8-flash-medium", timeout: int = 120, tag: str = "bg"
) -> str:
    """Run a stateless agy turn on a chosen model and log token usage."""
    try:
        proc = subprocess.run(
            [
                AGY_BIN,
                "-p", prompt,
                "--output-format", "json",
                "--model", model,
                "--dangerously-skip-permissions",
            ],
            cwd=str(Path.home()),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return ""
        data = json.loads(proc.stdout)
        usage = data.get("usage") or {}
        if usage:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "tag": tag,
                "model": model,
                "total_tokens": usage.get("total_tokens", 0),
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            }
            with TOKEN_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        return (data.get("response") or "").strip()
    except Exception:
        return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Lumus WhatsApp background watcher")
    parser.add_argument("--launch", action="store_true", help="Auto-launch Chrome if not running")
    parser.add_argument("--force", action="store_true", help="Analyze top chats even if unchanged")
    args = parser.parse_args()

    if not args.launch and not cdp_is_up():
        print("WhatsApp Chrome not running; skipping.", flush=True)
        return

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    seen: Dict[str, str] = {}
    if SEEN_FILE.exists():
        try:
            seen = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            seen = {}

    chats_res = run_wa_api("chats", "--limit", "15")
    chats: List[Dict[str, Any]] = chats_res.get("chats", [])
    if not chats:
        print(f"No chats returned: {chats_res}", flush=True)
        return

    changed: List[Dict[str, Any]] = []
    new_seen = dict(seen)
    for c in chats:
        name = c.get("name", "")
        if not name:
            continue
        fp = chat_fingerprint(c)
        if args.force or seen.get(name) != fp:
            changed.append(c)
        new_seen[name] = fp

    if not changed:
        print("No new WhatsApp activity.", flush=True)
        return

    # Prioritize unread chats first, cap at 5 reads per cycle
    changed.sort(key=lambda c: (0 if c.get("unread") else 1))
    digests = []
    for c in changed[:5]:
        name = c["name"]
        read_res = run_wa_api("read", name, "--limit", "10")
        msgs = read_res.get("messages", [])
        digests.append({"chat": name, "unread": c.get("unread", ""), "messages": msgs})

    prompt = (
        "[Background WhatsApp Watch]\n"
        "New/updated WhatsApp chats were detected. Review them for:\n"
        "1. Schedules, classes, meetings, deadlines, or events.\n"
        "2. Places/times where the user is needed, tagged, or asked to do something.\n"
        f"If any schedule/event/action item is found, update `{SCHEDULE_FILE}` concisely.\n"
        "Then reply with ONLY `NO_ACTION` if nothing needs the user's attention right now, "
        "or a concise bullet-point summary of what needs their attention.\n\n"
        f"Data:\n{json.dumps(digests, ensure_ascii=False, indent=2)}"
    )

    reply = ask_model(prompt, model="gemini-3.8-flash-medium", tag="whatsapp")
    SEEN_FILE.write_text(json.dumps(new_seen, indent=2, ensure_ascii=False), encoding="utf-8")

    if not reply or reply.strip().upper() == "NO_ACTION":
        print("Checked new chats; NO_ACTION.", flush=True)
        return

    alert = f"📱 WhatsApp Update\n{reply}"
    send_telegram(alert)
    notify_desktop("Lumus — WhatsApp Update", reply)
    print("Sent WhatsApp alert to Telegram & desktop.", flush=True)


if __name__ == "__main__":
    main()
