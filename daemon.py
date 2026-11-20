#!/usr/bin/env python3
"""Always-on Antigravity (agy) background session daemon with voice + dreaming + mobile PWA."""

import asyncio
import datetime
import json
import math
import os
import re
import shutil
import signal
import ssl
import struct
import subprocess
import tempfile
import threading
import urllib.parse
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).parent
AGY_BIN = os.environ.get("AGY_BIN", str(Path.home() / ".local/bin/agy"))
PW_RECORD = os.environ.get("PW_RECORD", shutil.which("pw-record") or str(Path.home() / ".local/bin/pw-record"))
MODEL = os.environ.get("LUMUS_MODEL", "gemini-4-hydrogen-medium")
HOST = "127.0.0.1"
PORT = int(os.environ.get("LUMUS_PORT", "47891"))
MOBILE_HOST = "0.0.0.0"
MOBILE_PORT = int(os.environ.get("LUMUS_MOBILE_PORT", "47892"))
DREAMS_DIR = Path.home() / ".gemini/antigravity-cli/dreams"
GEMINI_MD = Path.home() / ".gemini/GEMINI.md"
TTS_VOICE = "en-US-AndrewNeural"
REC_FILE = "/tmp/agy_ambient_rec.wav"
LATEST_TTS_MP3 = "/tmp/agy_tts_latest.mp3"
TOKEN_FILE = BASE_DIR / "token.txt"
CERT_FILE = BASE_DIR / "cert.pem"
KEY_FILE = BASE_DIR / "key.pem"
MOBILE_HTML = BASE_DIR / "mobile.html"


def make_tone(path: str, freq: int, duration_ms: int = 90, vol: float = 0.25):
    sr = 16000
    n = int(sr * duration_ms / 1000)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            env = min(1.0, i / 160.0, (n - i) / 160.0)
            val = int(32767 * vol * env * math.sin(2 * math.pi * freq * i / sr))
            frames.extend(struct.pack("<h", val))
        w.writeframes(bytes(frames))


BEEP_START = "/tmp/agy_beep_start.wav"
BEEP_STOP = "/tmp/agy_beep_stop.wav"
make_tone(BEEP_START, 880, 80, 0.22)
make_tone(BEEP_STOP, 587, 80, 0.22)


def play_sound_async(path: str):
    subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def clean_for_speech(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"```[\s\S]*?```", " I've written the code block. ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_`#>~]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 700:
        cut = text[:700].rsplit(".", 1)[0]
        text = (cut if len(cut) > 100 else text[:700]) + ". Check the panel for full details."
    return text


class AmbientDaemon:
    def __init__(self):
        self.lock = threading.RLock()
        self.proc = None
        self.reader_thread = None
        self.conversation_id = None
        self.state = "starting"
        self.turns_count = 0
        self.last_prompt = ""
        self.last_response = ""
        self.last_error = ""
        self.last_dream = ""
        self.tts_enabled = False
        self.tts_seq = 0
        self.last_silent = False
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "total_tokens": 0}
        self.recording = False
        self.rec_proc = None
        self.tts_proc = None
        self.whisper_model = None
        self.whisper_lock = threading.Lock()
        self.turn_done_event = threading.Event()
        self._tts_gen = 0
        DREAMS_DIR.mkdir(parents=True, exist_ok=True)
        self.start_session()

    def _log(self, msg: str):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)

    def start_session(self):
        with self.lock:
            self._stop_session_locked()
            self._log(f"Starting new agy session (model={MODEL})")
            self.conversation_id = None
            self.turns_count = 0
            self.last_error = ""
            self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "total_tokens": 0}
            self.state = "idle"
            self.proc = subprocess.Popen(
                [
                    AGY_BIN,
                    "-p=",
                    "--input-format", "stream-json",
                    "--output-format", "stream-json",
                    "--model", MODEL,
                    "--dangerously-skip-permissions",
                ],
                cwd=str(Path.home()),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self.reader_thread = threading.Thread(
                target=self._read_loop, args=(self.proc,), daemon=True
            )
            self.reader_thread.start()

    def _stop_session_locked(self):
        if self.proc is not None:
            proc = self.proc
            self.proc = None
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _read_loop(self, proc):
        try:
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    self._log(f"agy stdout: {line[:200]}")
                    continue

                etype = ev.get("event")
                if etype == "init":
                    self.conversation_id = ev.get("conversation_id")
                    self._log(f"Session initialized: {self.conversation_id}")
                elif etype == "result":
                    res = ev.get("result", {})
                    status = res.get("status", "")
                    resp = (res.get("response") or "").strip()
                    err = res.get("error") or ""
                    u = res.get("usage") or {}
                    with self.lock:
                        was_dreaming = self.state == "dreaming"
                        was_silent = self.last_silent
                        if err and not resp:
                            self.last_error = err
                        if resp and not was_dreaming:
                            self.last_response = resp
                            self.last_error = ""
                        for k in self.usage:
                            self.usage[k] += int(u.get(k, 0) or 0)
                        if not was_dreaming:
                            self.turns_count += 1
                            self.state = "idle"
                    self.turn_done_event.set()
                    self._log(f"Turn done (status={status}, len={len(resp)})")
                    if resp and not was_dreaming and not was_silent and self.tts_enabled:
                        self.speak_async(resp)
        except Exception as e:
            self._log(f"Reader loop ended: {e}")
        finally:
            with self.lock:
                if self.proc is proc:
                    self.proc = None
                    if self.state not in ("sleeping", "dreaming"):
                        self.state = "idle"

    def send_prompt(self, text: str, dreaming: bool = False, silent: bool = False):
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty prompt"}
        if not silent:
            self.stop_speaking()
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self.start_session()
            self.state = "dreaming" if dreaming else "thinking"
            self.last_silent = silent
            if not dreaming:
                self.last_prompt = text
                self.last_error = ""
            self.turn_done_event.clear()
            payload = json.dumps({"event": "user", "message": {"role": "user", "content": text}}) + "\n"
            try:
                self.proc.stdin.write(payload)
                self.proc.stdin.flush()
            except Exception as e:
                self.last_error = str(e)
                self.state = "idle"
                return {"ok": False, "error": str(e)}
        return {"ok": True}

    def stop_speaking(self):
        with self.lock:
            self._tts_gen += 1
            if self.tts_proc is not None and self.tts_proc.poll() is None:
                try:
                    self.tts_proc.terminate()
                except Exception:
                    pass
            self.tts_proc = None
            if self.state == "speaking":
                self.state = "idle"

    def speak_async(self, text: str):
        spoken = clean_for_speech(text)
        if not spoken:
            return
        with self.lock:
            self._tts_gen += 1
            gen = self._tts_gen
            if self.tts_proc is not None and self.tts_proc.poll() is None:
                try:
                    self.tts_proc.terminate()
                except Exception:
                    pass
            self.state = "speaking"

        def _worker():
            mp3 = os.path.join(tempfile.gettempdir(), f"agy_tts_{os.getpid()}_{gen}.mp3")
            try:
                import edge_tts

                async def _gen():
                    last_err = None
                    voices = [TTS_VOICE, TTS_VOICE, "en-GB-RyanNeural"]
                    for attempt, voice in enumerate(voices):
                        try:
                            comm = edge_tts.Communicate(spoken, voice, rate="+8%")
                            await comm.save(mp3)
                            return
                        except Exception as exc:
                            last_err = exc
                            if attempt < len(voices) - 1:
                                await asyncio.sleep(0.35)
                    raise last_err

                asyncio.run(_gen())
                shutil.copyfile(mp3, LATEST_TTS_MP3)
                with self.lock:
                    self.tts_seq += 1
                    if gen != self._tts_gen or not self.tts_enabled:
                        return
                    self.tts_proc = subprocess.Popen(
                        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", mp3],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    p = self.tts_proc
                p.wait()
            except Exception as e:
                self._log(f"edge-tts failed ({e}), falling back to espeak-ng")
                with self.lock:
                    if gen != self._tts_gen or not self.tts_enabled:
                        return
                    self.tts_proc = subprocess.Popen(
                        ["espeak-ng", "-s", "175", spoken],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    p = self.tts_proc
                p.wait()
            finally:
                try:
                    if os.path.exists(mp3):
                        os.remove(mp3)
                except OSError:
                    pass
                with self.lock:
                    if gen == self._tts_gen and self.state == "speaking":
                        self.state = "idle"
                    if gen == self._tts_gen:
                        self.tts_proc = None

        threading.Thread(target=_worker, daemon=True).start()

    def transcribe_wav(self, wav_path: str) -> str:
        with self.whisper_lock:
            if self.whisper_model is None:
                self._log("Loading faster-whisper base.en...")
                from faster_whisper import WhisperModel
                self.whisper_model = WhisperModel("base.en", device="cpu", compute_type="int8")
                self._log("Whisper loaded.")
            segments, _ = self.whisper_model.transcribe(wav_path, beam_size=2)
            return " ".join(s.text for s in segments).strip()

    def transcribe_and_send_bytes(self, audio_bytes: bytes):
        with self.lock:
            self.state = "thinking"

        def _worker():
            raw_f = os.path.join(tempfile.gettempdir(), f"agy_mob_{os.getpid()}.webm")
            wav_f = os.path.join(tempfile.gettempdir(), f"agy_mob_{os.getpid()}.wav")
            try:
                with open(raw_f, "wb") as f:
                    f.write(audio_bytes)
                subprocess.run(
                    ["ffmpeg", "-y", "-i", raw_f, "-ar", "16000", "-ac", "1", "-f", "wav", wav_f],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                )
                text = self.transcribe_wav(wav_f)
                self._log(f"Mobile transcribed: {text!r}")
                if text:
                    self.send_prompt(text)
                else:
                    with self.lock:
                        self.state = "idle"
            except Exception as e:
                self._log(f"Mobile transcription error: {e}")
                with self.lock:
                    self.last_error = str(e)
                    self.state = "idle"
            finally:
                for p in (raw_f, wav_f):
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass

        threading.Thread(target=_worker, daemon=True).start()

    def toggle_voice(self):
        with self.lock:
            if not self.recording:
                self.stop_speaking()
                if os.path.exists(REC_FILE):
                    try:
                        os.remove(REC_FILE)
                    except OSError:
                        pass
                play_sound_async(BEEP_START)
                self.rec_proc = subprocess.Popen(
                    [PW_RECORD, "--rate", "16000", "--channels", "1", "--format", "s16", REC_FILE],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.recording = True
                self.state = "listening"
                self._log("Voice recording started")
                return {"recording": True, "state": self.state}
            else:
                p = self.rec_proc
                self.rec_proc = None
                self.recording = False
                self.state = "thinking"
                play_sound_async(BEEP_STOP)

        if p is not None:
            try:
                p.send_signal(signal.SIGINT)
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

        def _transcribe_and_send():
            try:
                if not os.path.exists(REC_FILE) or os.path.getsize(REC_FILE) < 1000:
                    self._log("Recording too short/empty")
                    with self.lock:
                        self.state = "idle"
                    return
                text = self.transcribe_wav(REC_FILE)
                self._log(f"Transcribed: {text!r}")
                if text:
                    self.send_prompt(text)
                else:
                    with self.lock:
                        self.state = "idle"
            except Exception as e:
                self._log(f"Transcription error: {e}")
                with self.lock:
                    self.last_error = str(e)
                    self.state = "idle"

        threading.Thread(target=_transcribe_and_send, daemon=True).start()
        return {"recording": False, "state": "thinking"}

    def dream_and_end(self, respawn: bool = False):
        self.stop_speaking()
        with self.lock:
            should_dream = self.turns_count > 0 and self.proc is not None and self.proc.poll() is None
            conv = self.conversation_id or "unknown"

        if should_dream:
            ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            dream_file = DREAMS_DIR / f"{ts}_{conv[:8]}.md"
            dream_prompt = (
                f"The user is closing their laptop and ending this session. 'Dream' on everything we did in this session:\n"
                f"1. Save a concise reflection to {dream_file} summarizing: what was accomplished, key context/decisions, "
                f"user preferences observed, and better/faster ways to handle similar tasks next time.\n"
                f"2. Update {GEMINI_MD} under `## Gemini Added Memories` with only the most important persistent takeaways, "
                f"preferences, and shortcuts (keep the whole file concise, under ~60 lines, merging/pruning outdated items).\n"
                f"Reply briefly with 'Dream complete.'"
            )
            self._log(f"Dreaming for session {conv}...")
            self.send_prompt(dream_prompt, dreaming=True)
            self.turn_done_event.wait(timeout=25)
            with self.lock:
                self.last_dream = ts
            self._log("Dream finished (or timed out), closing session.")

        with self.lock:
            self._stop_session_locked()
            self.state = "sleeping" if not respawn else "idle"

        if respawn:
            self.start_session()
        return {"ok": True, "dreamed": should_dream}

    def status(self):
        with self.lock:
            return {
                "state": self.state,
                "recording": self.recording,
                "tts_enabled": self.tts_enabled,
                "tts_seq": self.tts_seq,
                "model": MODEL,
                "conversation_id": self.conversation_id,
                "turns_count": self.turns_count,
                "last_prompt": self.last_prompt,
                "last_response": self.last_response,
                "last_error": self.last_error,
                "last_dream": self.last_dream,
                "usage": dict(self.usage),
            }


DAEMON = AmbientDaemon()
AUTH_TOKEN = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""


class LocalHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/status":
            self._send_json(200, DAEMON.status())
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = {}

        if self.path == "/prompt":
            res = DAEMON.send_prompt(data.get("text", ""), silent=bool(data.get("silent", False)))
            self._send_json(200 if res.get("ok") else 400, res)
        elif self.path == "/voice/toggle":
            self._send_json(200, DAEMON.toggle_voice())
        elif self.path == "/tts/toggle":
            with DAEMON.lock:
                DAEMON.tts_enabled = not DAEMON.tts_enabled
                if not DAEMON.tts_enabled:
                    DAEMON.stop_speaking()
                enabled = DAEMON.tts_enabled
            self._send_json(200, {"tts_enabled": enabled})
        elif self.path == "/dream":
            threading.Thread(target=DAEMON.dream_and_end, kwargs={"respawn": True}, daemon=True).start()
            self._send_json(200, {"ok": True})
        elif self.path == "/new-session":
            DAEMON.start_session()
            self._send_json(200, {"ok": True})
        elif self.path == "/sleep":
            res = DAEMON.dream_and_end(respawn=False)
            self._send_json(200, res)
        elif self.path == "/wake":
            DAEMON.start_session()
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "not found"})


class MobileHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _authed(self) -> bool:
        if not AUTH_TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {AUTH_TOKEN}":
            return True
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        return qs.get("token", [""])[0] == AUTH_TOKEN

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/" and self._authed():
            self._send(200, "text/html; charset=utf-8", MOBILE_HTML.read_bytes())
            return
        if not self._authed():
            self._send(401, "application/json", b'{"error":"unauthorized"}')
            return
        if parsed.path == "/api/status":
            self._send(200, "application/json", json.dumps(DAEMON.status()).encode("utf-8"))
        elif parsed.path.startswith("/api/tts/") and os.path.exists(LATEST_TTS_MP3):
            self._send(200, "audio/mpeg", Path(LATEST_TTS_MP3).read_bytes())
        else:
            self._send(404, "application/json", b'{"error":"not found"}')

    def do_POST(self):
        if not self._authed():
            self._send(401, "application/json", b'{"error":"unauthorized"}')
            return
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        if parsed.path == "/api/prompt":
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                data = {}
            res = DAEMON.send_prompt(data.get("text", ""))
            self._send(200 if res.get("ok") else 400, "application/json", json.dumps(res).encode("utf-8"))
        elif parsed.path == "/api/voice":
            DAEMON.transcribe_and_send_bytes(raw)
            self._send(200, "application/json", b'{"ok":true}')
        else:
            self._send(404, "application/json", b'{"error":"not found"}')


def main():
    def _shutdown(signum, frame):
        with DAEMON.lock:
            DAEMON._stop_session_locked()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    if CERT_FILE.exists() and KEY_FILE.exists():
        mob = ThreadingHTTPServer((MOBILE_HOST, MOBILE_PORT), MobileHandler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(CERT_FILE), keyfile=str(KEY_FILE))
        mob.socket = ctx.wrap_socket(mob.socket, server_side=True)
        threading.Thread(target=mob.serve_forever, daemon=True).start()
        print(f"agy-ambient mobile HTTPS listening on https://{MOBILE_HOST}:{MOBILE_PORT}", flush=True)

    srv = ThreadingHTTPServer((HOST, PORT), LocalHandler)
    print(f"agy-ambient daemon listening on http://{HOST}:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
