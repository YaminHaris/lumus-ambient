#!/usr/bin/env python3
"""Periodic Dream Cycle for Lumus (agy-ambient).

Runs on a systemd timer (every 4 hours) when agy-ambient is idle, reads 1-2
unreviewed past session transcripts from ~/.gemini/antigravity-cli/brain/,
and distills new insights into dreams/, user_profile.md, business_tracker.md,
and GEMINI.md.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import whatsapp_watcher as ww

BRAIN_DIR = Path.home() / ".gemini/antigravity-cli/brain"
DREAMS_DIR = Path.home() / ".gemini/antigravity-cli/dreams"
STATE_DIR = Path.home() / ".local/state/agy-ambient"
DREAMED_FILE = STATE_DIR / "dreamed_sessions.json"
PROFILE_FILE = STATE_DIR / "user_profile.md"
BUSINESS_FILE = STATE_DIR / "business_tracker.md"
GEMINI_MD = Path.home() / ".gemini/GEMINI.md"


def _load_dreamed() -> List[str]:
    if DREAMED_FILE.exists():
        try:
            data = json.loads(DREAMED_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def _save_dreamed(ids: List[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DREAMED_FILE.write_text(
        json.dumps(ids[-200:], indent=2), encoding="utf-8"
    )


def _extract_digest(transcript_path: Path, max_chars: int = 3500) -> str:
    """Extract only USER_INPUT and non-empty PLANNER_RESPONSE text from JSONL."""
    lines_out: List[str] = []
    total = 0
    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                step_type = obj.get("type", "")
                content = (obj.get("content") or "").strip()
                if not content:
                    continue
                if step_type == "USER_INPUT":
                    entry = f"USER: {content[:400]}"
                elif step_type == "PLANNER_RESPONSE":
                    entry = f"AI: {content[:300]}"
                else:
                    continue
                lines_out.append(entry)
                total += len(entry)
                if total >= max_chars:
                    break
    except OSError:
        return ""
    return "\n".join(lines_out)


def _pick_unreviewed_sessions(dreamed: List[str], count: int = 2) -> List[Tuple[str, str]]:
    if not BRAIN_DIR.exists():
        return []
    candidates: List[Tuple[float, str, Path]] = []
    dreamed_set = set(dreamed)
    for tpath in BRAIN_DIR.glob("*/.system_generated/logs/transcript.jsonl"):
        cid = tpath.parent.parent.parent.name
        if cid in dreamed_set:
            continue
        try:
            st = tpath.stat()
            if st.st_size < 1500:
                continue
            candidates.append((st.st_mtime, cid, tpath))
        except OSError:
            continue

    candidates.sort(reverse=True)
    picked: List[Tuple[str, str]] = []
    for _, cid, tpath in candidates[:count]:
        digest = _extract_digest(tpath)
        if digest:
            picked.append((cid, digest))
    return picked


def main() -> None:
    try:
        st = ww.daemon_get("/status")
    except Exception as exc:
        print(f"agy-ambient daemon unreachable: {exc}", flush=True)
        return

    if st.get("state") != "idle":
        print(f"Daemon busy ({st.get('state')}); skipping dream cycle.", flush=True)
        return

    dreamed = _load_dreamed()
    sessions = _pick_unreviewed_sessions(dreamed, count=2)
    if not sessions:
        print("No unreviewed past sessions to dream on.", flush=True)
        return

    DREAMS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dream_file = DREAMS_DIR / f"{ts}_cycle.md"

    sessions_block = "\n\n".join(
        f"=== Session {cid} ===\n{digest}" for cid, digest in sessions
    )

    prompt = (
        "[Background Dream Cycle — Consolidate Memory & Learn From Past Sessions]\n"
        "Review these unreviewed past session excerpts and anything learned recently:\n"
        f"1. Save a concise dream reflection to `{dream_file}` (new facts about the user, "
        "preferences, recurring workflows, mistakes to avoid).\n"
        f"2. If you learned anything genuinely new and durable about the user, their projects, "
        f"or their business, update `{PROFILE_FILE}` or `{BUSINESS_FILE}` concisely.\n"
        f"3. Only if you found a clear, durable workflow shortcut/preference, add a 1-line "
        f"bullet to `{GEMINI_MD}` (keep `{GEMINI_MD}` clean and concise).\n"
        "Reply briefly with `Dream complete.` plus a 1-line summary.\n\n"
        f"{sessions_block}"
    )

    reply = ww.ask_model(prompt, model="gemini-3.8-flash-high", timeout=150, tag="dream")
    if not reply:
        print("Dream cycle timed out or daemon busy.", flush=True)
        return

    dreamed.extend(cid for cid, _ in sessions)
    _save_dreamed(dreamed)
    print(f"Dreamed on {[cid for cid, _ in sessions]}: {reply[:200]}", flush=True)


if __name__ == "__main__":
    main()
