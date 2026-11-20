#!/usr/bin/env python3
"""Telegram bridge for agy-ambient: supports text + voice notes in & out."""

import json
import os
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent
CFG_FILE = BASE_DIR / "telegram.json"
OFFSET_FILE = BASE_DIR / "telegram_offset.txt"
DAEMON_URL = "http://127.0.0.1:47891"
TTS_MP3 = "/tmp/agy_tts_latest.mp3"


def api(token: str, method: str, data=None, timeout: int = 40):
    url = f"https://api.telegram.org/bot{token}/{method}"
    if data is not None:
        encoded = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=encoded, method="POST")
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def send_text(token: str, chat_id: str, text: str):
    if not text:
        return
    for i in range(0, len(text), 4000):
        api(token, "sendMessage", {"chat_id": chat_id, "text": text[i : i + 4000]})


def send_voice(token: str, chat_id: str, audio_path: str):
    if not os.path.exists(audio_path):
        return
    subprocess.run(
        [
            "curl", "-s", "-X", "POST",
            f"https://api.telegram.org/bot{token}/sendVoice",
            "-F", f"chat_id={chat_id}",
            "-F", f"voice=@{audio_path}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def daemon_get(path: str):
    with urllib.request.urlopen(f"{DAEMON_URL}{path}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def daemon_post(path: str, payload: dict):
    req = urllib.request.Request(
        f"{DAEMON_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def transcribe_ogg(ogg_bytes: bytes) -> str:
    ogg_f = os.path.join(tempfile.gettempdir(), f"tg_in_{os.getpid()}.ogg")
    wav_f = os.path.join(tempfile.gettempdir(), f"tg_in_{os.getpid()}.wav")
    try:
        with open(ogg_f, "wb") as f:
            f.write(ogg_bytes)
        subprocess.run(
            ["ffmpeg", "-y", "-i", ogg_f, "-ar", "16000", "-ac", "1", "-f", "wav", wav_f],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        import wave
        import numpy as np
        from faster_whisper import WhisperModel
        with wave.open(wav_f, "rb") as wf:
            audio = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        if not hasattr(transcribe_ogg, "_model"):
            transcribe_ogg._model = WhisperModel("base.en", device="cpu", compute_type="int8")
        segs, _ = transcribe_ogg._model.transcribe(audio, beam_size=2)
        return " ".join(s.text for s in segs).strip()
    finally:
        for p in (ogg_f, wav_f):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


def ask_angie(token: str, chat_id: str, prompt: str, want_voice: bool = False):
    before = daemon_get("/status")
    prev_turns = before.get("turns_count", 0)
    prev_tts = before.get("tts_seq", 0)
    res = daemon_post("/prompt", {"text": prompt, "silent": True})
    if not res.get("ok"):
        send_text(token, chat_id, f"Error: {res.get('error', 'failed')}")
        return

    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(0.5)
        st = daemon_get("/status")
        if st.get("turns_count", 0) > prev_turns and st.get("state") != "thinking":
            reply = st.get("last_response") or st.get("last_error") or "(empty)"
            send_text(token, chat_id, reply)
            if want_voice and st.get("tts_enabled"):
                # Wait briefly for TTS mp3 generation
                for _ in range(15):
                    st2 = daemon_get("/status")
                    if st2.get("tts_seq", 0) > prev_tts and os.path.exists(TTS_MP3):
                        send_voice(token, chat_id, TTS_MP3)
                        break
                    time.sleep(0.3)
            return
    send_text(token, chat_id, "Timed out waiting for Lumus.")


def main():
    if not CFG_FILE.exists():
        print(f"Missing {CFG_FILE}", flush=True)
        return
    cfg = json.loads(CFG_FILE.read_text())
    token = cfg["bot_token"].strip()
    chat_id = str(cfg.get("chat_id", "")).strip()

    offset = int(OFFSET_FILE.read_text().strip()) if OFFSET_FILE.exists() else 0
    print("agy-ambient telegram_bridge running", flush=True)

    while True:
        try:
            res = api(token, "getUpdates", {"timeout": 30, "offset": offset}, timeout=40)
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                OFFSET_FILE.write_text(str(offset))
                msg = upd.get("message") or {}
                cid = str((msg.get("chat") or {}).get("id", ""))
                if not cid:
                    continue
                if not chat_id:
                    chat_id = cid
                    cfg["chat_id"] = cid
                    CFG_FILE.write_text(json.dumps(cfg, indent=2))
                    send_text(token, cid, "Paired with Lumus (Hydrogen). Send text or a voice note anytime!")
                    continue
                if cid != chat_id:
                    continue

                if "text" in msg:
                    txt = msg["text"].strip()
                    if txt == "/status":
                        st = daemon_get("/status")
                        send_text(token, cid, f"State: {st['state']}\nModel: {st['model']}\nTurns: {st['turns_count']}\nSession: {st['conversation_id']}")
                    elif txt == "/dream":
                        daemon_post("/dream", {})
                        send_text(token, cid, "Dreaming and starting a fresh session...")
                    elif txt == "/new":
                        daemon_post("/new-session", {})
                        send_text(token, cid, "Started a fresh Hydrogen session.")
                    else:
                        ask_angie(token, cid, txt, want_voice=False)
                elif "voice" in msg:
                    fid = msg["voice"]["file_id"]
                    finfo = api(token, "getFile", {"file_id": fid})
                    fpath = finfo["result"]["file_path"]
                    furl = f"https://api.telegram.org/file/bot{token}/{fpath}"
                    with urllib.request.urlopen(furl, timeout=30) as r:
                        ogg_bytes = r.read()
                    prompt = transcribe_ogg(ogg_bytes)
                    if prompt:
                        send_text(token, cid, f"🎤 {prompt}")
                        ask_angie(token, cid, prompt, want_voice=False)
                    else:
                        send_text(token, cid, "Couldn't hear any speech in that voice note.")
        except Exception as e:
            print(f"Telegram poll error: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
