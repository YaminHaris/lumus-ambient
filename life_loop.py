#!/usr/bin/env python3
"""Periodic ReAct life-simplifier heartbeat for Lumus (agy-ambient).

Runs every ~25 minutes via systemd timer when agy-ambient is idle, observes
schedule/projects/tools, handles safe upkeep quietly, and asks the user on
Telegram before taking any real action or starting background project work.
"""

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import whatsapp_watcher as ww

STATE_DIR = Path.home() / ".local/state/agy-ambient"
LIFE_STATE_FILE = STATE_DIR / "life_loop_state.json"
PROFILE_FILE = STATE_DIR / "user_profile.md"
SCHEDULE_FILE = STATE_DIR / "whatsapp_schedule.md"
BUSINESS_FILE = STATE_DIR / "business_tracker.md"
TOOLS_DIR = Path(__file__).parent / "tools"
GEMINI_MD = Path.home() / ".gemini/GEMINI.md"
QUIET_START_HOUR = 1  # 01:00 AM
QUIET_END_HOUR = 7    # 07:00 AM


def _load_state() -> Dict[str, Any]:
    if LIFE_STATE_FILE.exists():
        try:
            return json.loads(LIFE_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"recently_proposed": []}


def _save_state(state: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["recently_proposed"] = state.get("recently_proposed", [])[-15:]
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    LIFE_STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _recent_dev_projects() -> List[str]:
    dev_dir = Path.home() / "Development"
    if not dev_dir.exists():
        return []
    entries = []
    for p in dev_dir.iterdir():
        if p.is_dir() and not p.name.startswith("."):
            try:
                entries.append((p.stat().st_mtime, p.name))
            except OSError:
                pass
    entries.sort(reverse=True)
    return [name for _, name in entries[:25]]


def main() -> None:
    now = datetime.now()
    if QUIET_START_HOUR <= now.hour < QUIET_END_HOUR:
        print(
            f"Quiet hours ({QUIET_START_HOUR:02d}:00–{QUIET_END_HOUR:02d}:00); skipping heartbeat.",
            flush=True,
        )
        return

    try:
        st = ww.daemon_get("/status")
    except Exception as exc:
        print(f"agy-ambient daemon unreachable: {exc}", flush=True)
        return

    if st.get("state") != "idle":
        print(f"Daemon busy ({st.get('state')}); skipping heartbeat.", flush=True)
        return

    state = _load_state()
    recent_props = state.get("recently_proposed", [])
    schedule_txt = (
        SCHEDULE_FILE.read_text(encoding="utf-8")[:2500]
        if SCHEDULE_FILE.exists()
        else "(none)"
    )
    business_txt = (
        BUSINESS_FILE.read_text(encoding="utf-8")[:2000]
        if BUSINESS_FILE.exists()
        else "(none)"
    )
    chrome_up = ww.cdp_is_up()
    recent_projects = _recent_dev_projects()
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    existing_tools = sorted(p.name for p in TOOLS_DIR.iterdir() if p.is_file())
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M (%a)")

    prompt = (
        "[Background ReAct Heartbeat — Goal: Make the user's life easier]\n"
        f"Current time: {now_str}\n"
        f"Chrome 9223 (WhatsApp/Keep) running: {chrome_up}\n"
        f"Existing custom tools in {TOOLS_DIR}: {existing_tools}\n"
        f"Recently touched ~/Development projects: {', '.join(recent_projects)}\n"
        f"Previously proposed (do NOT repeat): {json.dumps(recent_props[-8:], ensure_ascii=False)}\n\n"
        f"Current schedule ({SCHEDULE_FILE}):\n{schedule_txt}\n\n"
        f"Business tracker ({BUSINESS_FILE}):\n{business_txt}\n\n"
        "Instructions:\n"
        f"1. Read `{PROFILE_FILE}` if needed, and actively inspect 1-2 promising folders in "
        "~/Development/ (READMEs, TODOs, broken/unfinished value) to see what can benefit the user.\n"
        "2. Business & Strategy Watch: Track what the user is doing in their business (from WhatsApp, "
        f"Keep, and desktop folders), update `{BUSINESS_FILE}` quietly when you learn something new, "
        "and if you spot a strategic mistake, bottleneck, time-for-money trap, or a moment where "
        "they should switch strategies, tell them directly on Telegram.\n"
        "3. Autonomous tool-building (NO permission needed): If you spot a genuinely useful "
        f"new helper tool/CLI (like whatsapp_api.py or keep_api.py) that would save the user time, "
        f"you may build and test it directly inside `{TOOLS_DIR}/`, add a 1-line entry to "
        f"`{GEMINI_MD}` so future sessions know about it, and reply with a short update: "
        "'New update: built <tool> — here's what we can do with it: ...'\n"
        "3. Existing projects & external actions (ASK first): Never modify existing repos in "
        "~/Development, send messages/emails to others, or delete user data without asking. "
        "If you see high-value work on an existing project, propose it in 1-3 sentences ending "
        "with: 'Reply \"yes\" if you want me to go ahead.'\n"
        "4. If nothing genuinely useful or timely stands out, reply with ONLY `NO_ACTION`."
    )

    reply = ww.ask_model(prompt, model="gemini-3.8-flash-medium", timeout=150, tag="lifeloop")
    cleaned = (reply or "").strip().rstrip(".!`*_")
    if not cleaned or cleaned.upper() == "NO_ACTION" or cleaned.upper().endswith("NO_ACTION"):
        _save_state(state)
        print("Heartbeat complete: NO_ACTION.", flush=True)
        return

    state.setdefault("recently_proposed", []).append(
        {"at": now_str, "proposal": reply[:500]}
    )
    _save_state(state)

    msg = f"🤖 Lumus\n{reply}"
    ww.send_telegram(msg)
    ww.notify_desktop("Lumus — Suggestion", reply)
    print("Sent ReAct proposal to Telegram & desktop.", flush=True)


if __name__ == "__main__":
    main()
