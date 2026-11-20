#!/usr/bin/env python3
"""Local WhatsApp Web API & CLI over Chrome DevTools Protocol (background tab).

Connects to Chrome's existing remote-debugging WebSocket (`DevToolsActivePort`)
and interacts with `https://web.whatsapp.com/` in the background without stealing
focus from your active tab. Designed for AI agents and scripts.

Usage:
  python3 ~/whatsapp_api.py chats [--limit 20] [--unread]
  python3 ~/whatsapp_api.py read "<chat_name>" [--limit 30]
  python3 ~/whatsapp_api.py search "<query>" [--limit 20]
  python3 ~/whatsapp_api.py send "<chat_name>" "<message>" --confirm
  python3 ~/whatsapp_api.py serve [--host 127.0.0.1] [--port 8765]
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import websockets
from aiohttp import web

WHATSAPP_CDP_PORT = int(os.environ.get("WHATSAPP_CDP_PORT", "9223"))
DEDICATED_PROFILE_DIR = Path.home() / ".config/chrome-whatsapp"
DEDICATED_DEVTOOLS_PORT_FILE = DEDICATED_PROFILE_DIR / "DevToolsActivePort"
DEVTOOLS_PORT_FILE = Path.home() / ".config/google-chrome/DevToolsActivePort"


def _fetch_cdp_ws_url() -> Optional[str]:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{WHATSAPP_CDP_PORT}/json/version", timeout=1.5
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("webSocketDebuggerUrl")
    except Exception:
        return None


def _launch_dedicated_chrome() -> Optional[str]:
    """Start the dedicated WhatsApp Chrome profile on demand in headless mode and return its WS URL."""
    chrome_bin = "/opt/google/chrome/chrome"
    if not os.path.exists(chrome_bin):
        return None
    subprocess.Popen(
        [
            chrome_bin,
            f"--user-data-dir={DEDICATED_PROFILE_DIR}",
            f"--remote-debugging-port={WHATSAPP_CDP_PORT}",
            "--headless=new",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "https://web.whatsapp.com/",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        ws_url = _fetch_cdp_ws_url()
        if ws_url:
            return ws_url
        time.sleep(0.4)
    return None


def get_browser_ws_url() -> str:
    """Read dedicated Chrome's DevTools WebSocket URL, auto-launching it if needed. Never touches main Chrome."""
    ws_url = _fetch_cdp_ws_url()
    if ws_url:
        return ws_url

    if DEDICATED_PROFILE_DIR.exists():
        ws_url = _launch_dedicated_chrome()
        if ws_url:
            return ws_url

    if DEDICATED_DEVTOOLS_PORT_FILE.exists():
        lines = DEDICATED_DEVTOOLS_PORT_FILE.read_text(encoding="utf-8").strip().splitlines()
        if len(lines) >= 2:
            return f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"

    raise RuntimeError(
        f"Dedicated Chrome CDP not found on port {WHATSAPP_CDP_PORT} ({DEDICATED_PROFILE_DIR})."
    )


class CDPClient:
    """Minimal async Chrome DevTools Protocol client for a background WhatsApp tab."""

    def __init__(self) -> None:
        self._msg_id = 0
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.session_id: Optional[str] = None

    async def __aenter__(self) -> "CDPClient":
        ws_url = get_browser_ws_url()
        self.ws = await websockets.connect(
            ws_url, max_size=20_000_000, open_timeout=10, ping_interval=None
        )
        targets = await self._call("Target.getTargets")
        wa_targets = [
            t
            for t in targets.get("targetInfos", [])
            if t.get("type") == "page" and "web.whatsapp.com" in t.get("url", "")
        ]
        if wa_targets:
            target_id = wa_targets[0]["targetId"]
        else:
            created = await self._call(
                "Target.createTarget",
                {"url": "https://web.whatsapp.com/", "background": True},
            )
            target_id = created["targetId"]
            await asyncio.sleep(5)

        att = await self._call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        self.session_id = att["sessionId"]
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.ws is not None:
            await self.ws.close()

    async def _call(
        self, method: str, params: Optional[Dict[str, Any]] = None, use_session: bool = False
    ) -> Dict[str, Any]:
        assert self.ws is not None
        self._msg_id += 1
        cid = self._msg_id
        payload: Dict[str, Any] = {"id": cid, "method": method}
        if params:
            payload["params"] = params
        if use_session and self.session_id:
            payload["sessionId"] = self.session_id
        await self.ws.send(json.dumps(payload))
        while True:
            raw = await self.ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == cid:
                if "error" in msg:
                    raise RuntimeError(f"CDP error in {method}: {msg['error']}")
                return msg.get("result", {})

    async def eval_js(self, expression: str, await_promise: bool = False) -> Any:
        res = await self._call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
            use_session=True,
        )
        if "exceptionDetails" in res:
            raise RuntimeError(f"JS exception: {res['exceptionDetails']}")
        return res.get("result", {}).get("value")

    async def insert_text(self, text: str) -> None:
        await self._call("Input.insertText", {"text": text}, use_session=True)

    async def click_at(self, x: float, y: float) -> None:
        for ev_type in ("mouseMoved", "mousePressed", "mouseReleased"):
            params: Dict[str, Any] = {"type": ev_type, "x": x, "y": y, "button": "left"}
            if ev_type in ("mousePressed", "mouseReleased"):
                params["clickCount"] = 1
            await self._call("Input.dispatchMouseEvent", params, use_session=True)


async def _focus_search_and_type(cdp: CDPClient, query: str) -> Optional[str]:
    ok = await cdp.eval_js("""(() => {
        const s = document.querySelector('input[aria-label="Search or start a new chat"]') ||
                  document.querySelector('[data-testid="chat-list-search"]') ||
                  document.querySelector('input[data-tab="3"]') ||
                  document.querySelector('[contenteditable="true"][data-tab="3"]') ||
                  document.querySelector('div[role="textbox"][contenteditable="true"]');
        if (!s) return false;
        s.focus();
        if (s.tagName === 'INPUT') {
            s.select();
        } else {
            document.execCommand('selectAll', false, null);
        }
        document.execCommand('delete', false, null);
        return true;
    })()""")
    if not ok:
        return "Search box not found"
    await cdp.insert_text(query)
    await asyncio.sleep(1.3)
    return None


async def _clear_search(cdp: CDPClient) -> None:
    await cdp.eval_js("""(() => {
        const clearBtn = document.querySelector('[data-testid="x-alt"], [aria-label="Cancel search"]');
        if (clearBtn) clearBtn.click();
    })()""")


async def _open_chat(cdp: CDPClient, chat_name: str) -> Dict[str, Any]:
    async def click_row_js() -> Optional[str]:
        hit = await cdp.eval_js(f"""((chatName) => {{
            const norm = s => (s || '').toLowerCase().trim();
            const target = norm(chatName);
            const pane = document.querySelector('#pane-side');
            if (!pane) return null;
            const rows = Array.from(pane.querySelectorAll('[role="row"]'));
            let best = null;
            let bestName = '';
            for (const r of rows) {{
                const lines = (r.innerText || '').split('\\n').map(s => s.trim())
                    .filter(s => s && !/^\\d+\\s+unread\\s+messages?$/i.test(s));
                const name = lines[0] || '';
                const name2 = lines[1] || '';
                const combined = norm(name + ' ' + name2);
                if (norm(name) === target || norm(name2) === target) {{
                    best = r; bestName = name; break;
                }}
                if (!best && combined.includes(target)) {{
                    best = r; bestName = name;
                }}
            }}
            if (!best) return null;
            best.scrollIntoView({{block: 'center'}});
            const rect = best.getBoundingClientRect();
            return {{
                name: bestName,
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2,
            }};
        }})({json.dumps(chat_name)})""")
        if not hit:
            return None
        await cdp.click_at(hit["x"], hit["y"])
        return hit["name"]

    matched = await click_row_js()
    if not matched:
        err = await _focus_search_and_type(cdp, chat_name)
        if not err:
            matched = await click_row_js()
            await asyncio.sleep(0.4)
            await _clear_search(cdp)
    if not matched:
        matched = await _open_via_new_chat(cdp, chat_name)
    if not matched:
        return {"error": f"Chat not found: {chat_name}"}
    await asyncio.sleep(0.9)
    return {"opened": matched}


async def _open_via_new_chat(cdp: CDPClient, query: str) -> Optional[str]:
    """Fallback: open a chat or phone number via WhatsApp Web's New Chat drawer."""
    already_open = await cdp.eval_js(
        "Boolean(document.querySelector('input[aria-label*=\"Search name\"]')?.offsetParent !== null)"
    )
    if not already_open:
        clicked_new = await cdp.eval_js("""(() => {
            const b = document.querySelector('[data-icon="new-chat-outline"]')?.closest('button')
                   || document.querySelector('button[aria-label="New chat"]')
                   || document.querySelector('[title="New chat"]');
            if (!b) return false;
            b.click();
            return true;
        })()""")
        if not clicked_new:
            return None
        await asyncio.sleep(0.8)

    focused = await cdp.eval_js("""(() => {
        const inp = document.querySelector('input[aria-label*="Search name"]')
                 || Array.from(document.querySelectorAll('input, [contenteditable="true"]'))
                        .find(el => !el.closest('#main') && el.offsetParent !== null);
        if (!inp) return false;
        inp.focus();
        if (inp.tagName === 'INPUT') inp.select();
        else document.execCommand('selectAll', false, null);
        document.execCommand('delete', false, null);
        return true;
    })()""")
    if not focused:
        return None

    await cdp.insert_text(query)
    await asyncio.sleep(1.3)

    picked = await cdp.eval_js(f"""((q) => {{
        const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
        const digits = q.replace(/\\D/g, '');
        const items = Array.from(document.querySelectorAll('[role="listitem"], [data-testid="cell-frame-container"]'))
            .filter(el => el.offsetParent !== null && !el.closest('#pane-side'));
        const target = items.find(el => {{
            const txt = norm(el.innerText);
            const txtDigits = txt.replace(/\\D/g, '');
            return txt.toLowerCase().includes(q.toLowerCase())
                || (digits && txtDigits.includes(digits))
                || /\\(you\\)|message yourself/i.test(txt);
        }}) || items[0];
        if (!target) return null;
        const rect = target.getBoundingClientRect();
        return {{
            name: norm(target.innerText.split('\\n')[0] || q),
            x: rect.left + rect.width / 2,
            y: rect.top + rect.height / 2,
        }};
    }})({json.dumps(query)})""")
    if not picked:
        # Close New chat drawer if nothing found
        await cdp.eval_js("document.querySelector('[aria-label=\"Back\"], [data-icon=\"back\"]')?.closest('button')?.click()")
        return None

    await cdp.click_at(picked["x"], picked["y"])
    await asyncio.sleep(0.8)
    return picked["name"]


JS_LIST_CHATS = """((limit, unreadOnly) => {
  const pane = document.querySelector('#pane-side');
  if (!pane) return {error: 'WhatsApp #pane-side not found (is WhatsApp Web logged in?)'};
  const rows = Array.from(pane.querySelectorAll('[role="row"]'));
  const seen = new Set();
  const out = [];
  for (const r of rows) {
    const text = (r.innerText || '').trim();
    if (!text || text === 'Archived') continue;
    const lines = text.split('\\n').map(s => s.trim())
      .filter(s => s && !/^\\d+\\s+unread\\s+messages?$/i.test(s));
    const name = lines[0] || '';
    if (!name || seen.has(name)) continue;
    seen.add(name);
    const time = lines[1] || '';
    const unreadEl = r.querySelector('[aria-label*="unread" i], [data-testid="icon-unread-count"]');
    const unread = unreadEl ? (unreadEl.innerText || unreadEl.getAttribute('aria-label') || 'unread').trim() : '';
    if (unreadOnly && !unread) continue;
    const preview = lines.slice(2).join(' | ');
    out.push({name, time, unread, preview});
    if (out.length >= limit) break;
  }
  return {chats: out};
})"""


JS_OPEN_CHAT = """(async (chatName) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => (s || '').toLowerCase().trim();
  const target = norm(chatName);

  const findAndClickRow = () => {
    const pane = document.querySelector('#pane-side');
    if (!pane) return null;
    const rows = Array.from(pane.querySelectorAll('[role="row"]'));
    let best = null;
    for (const r of rows) {
      const firstLine = ((r.innerText || '').split('\\n')[0] || '').trim();
      if (norm(firstLine) === target) { best = r; break; }
      if (!best && norm(firstLine).includes(target)) best = r;
    }
    if (best) {
      const clickable = best.querySelector('[role="gridcell"]') || best;
      clickable.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
      clickable.click();
      return ((best.innerText || '').split('\\n')[0] || '').trim();
    }
    return null;
  };

  let matched = findAndClickRow();
  if (!matched) {
    const searchBox = document.querySelector('[data-testid="chat-list-search"]') ||
                      document.querySelector('[contenteditable="true"][data-tab="3"]') ||
                      document.querySelector('div[role="textbox"][contenteditable="true"]');
    if (!searchBox) return {error: 'Search box not found'};
    searchBox.focus();
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, chatName);
    await sleep(1200);
    matched = findAndClickRow();
    // Clear search box after selecting
    const clearBtn = document.querySelector('[data-testid="x-alt"], [aria-label="Cancel search"]');
    if (clearBtn) clearBtn.click();
  }
  if (!matched) return {error: `Chat not found: ${chatName}`};
  await sleep(900);
  return {opened: matched};
})"""


JS_READ_MESSAGES = """((limit) => {
  const main = document.querySelector('#main');
  if (!main) return {error: 'No open chat (#main not found)'};
  const headerEl = main.querySelector('header');
  const title = headerEl ? (headerEl.innerText || '').split('\\n')[0].trim() : '';
  const nodes = Array.from(main.querySelectorAll('[data-pre-plain-text]'));
  const out = [];
  for (const n of nodes.slice(-limit)) {
    const meta = (n.getAttribute('data-pre-plain-text') || '').trim();
    const copyable = n.querySelector('.selectable-text') || n;
    const text = (copyable.innerText || '').trim();
    if (!text) continue;
    const isOut = !!n.closest('.message-out');
    out.push({
      direction: isOut ? 'out' : 'in',
      meta,
      text
    });
  }
  return {chat: title, count: out.length, messages: out};
})"""


JS_SEARCH = """(async (query, limit) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const searchBox = document.querySelector('[data-testid="chat-list-search"]') ||
                    document.querySelector('[contenteditable="true"][data-tab="3"]') ||
                    document.querySelector('div[role="textbox"][contenteditable="true"]');
  if (!searchBox) return {error: 'Search box not found'};
  searchBox.focus();
  document.execCommand('selectAll', false, null);
  document.execCommand('insertText', false, query);
  await sleep(1500);
  const pane = document.querySelector('#pane-side');
  const rows = pane ? Array.from(pane.querySelectorAll('[role="row"]')) : [];
  const results = [];
  for (const r of rows) {
    const lines = (r.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
    if (!lines.length) continue;
    results.push({name: lines[0], snippet: lines.slice(1).join(' | ')});
    if (results.length >= limit) break;
  }
  const clearBtn = document.querySelector('[data-testid="x-alt"], [aria-label="Cancel search"]');
  if (clearBtn) clearBtn.click();
  return {query, results};
})"""


JS_SEND_MESSAGE = """(async (message) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const main = document.querySelector('#main');
  if (!main) return {error: 'No open chat (#main not found)'};
  const input = main.querySelector('footer div[contenteditable="true"][role="textbox"]') ||
                main.querySelector('footer div[contenteditable="true"]');
  if (!input) return {error: 'Message input box not found'};
  input.focus();
  document.execCommand('insertText', false, message);
  await sleep(300);
  const sendBtn = main.querySelector('footer button[aria-label="Send"], footer [data-testid="send"]');
  if (!sendBtn) return {error: 'Send button not found'};
  sendBtn.click();
  await sleep(400);
  return {sent: true, message};
})"""


async def _run_chats(cdp: CDPClient, limit: int, unread: bool) -> Dict[str, Any]:
    return await cdp.eval_js(f"{JS_LIST_CHATS}({limit}, {'true' if unread else 'false'})")


async def _run_read(cdp: CDPClient, chat_name: str, limit: int) -> Dict[str, Any]:
    opened = await _open_chat(cdp, chat_name)
    if opened.get("error"):
        return opened
    return await cdp.eval_js(f"{JS_READ_MESSAGES}({limit})")


async def _run_search(cdp: CDPClient, query: str, limit: int) -> Dict[str, Any]:
    err = await _focus_search_and_type(cdp, query)
    if err:
        return {"error": err}
    res = await cdp.eval_js(f"""((query, limit) => {{
        const pane = document.querySelector('#pane-side');
        const rows = pane ? Array.from(pane.querySelectorAll('[role="row"]')) : [];
        const results = [];
        for (const r of rows) {{
            const lines = (r.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
            if (!lines.length) continue;
            results.push({{name: lines[0], snippet: lines.slice(1).join(' | ')}});
            if (results.length >= limit) break;
        }}
        return {{query, results}};
    }})({json.dumps(query)}, {limit})""")
    await _clear_search(cdp)
    return res


async def _run_send(cdp: CDPClient, chat_name: str, message: str, confirm: bool) -> Dict[str, Any]:
    if not confirm:
        return {
            "error": "Refusing to send without --confirm flag. Pass --confirm to actually send."
        }
    opened = await _open_chat(cdp, chat_name)
    if opened.get("error"):
        return opened
    ok = await cdp.eval_js("""(() => {
        const main = document.querySelector('#main');
        const input = main && (main.querySelector('footer div[contenteditable="true"][role="textbox"]') ||
                               main.querySelector('footer div[contenteditable="true"]'));
        if (!input) return false;
        input.focus();
        return true;
    })()""")
    if not ok:
        return {"error": "Message input box not found"}
    await cdp.insert_text(message)
    await asyncio.sleep(0.3)
    return await cdp.eval_js(f"""((message) => {{
        const main = document.querySelector('#main');
        const sendBtn = main && main.querySelector('footer button[aria-label="Send"], footer [data-testid="send"]');
        if (!sendBtn) return {{error: 'Send button not found'}};
        sendBtn.click();
        return {{sent: true, message}};
    }})({json.dumps(message)})""")


async def cmd_chats(limit: int, unread: bool) -> Dict[str, Any]:
    async with CDPClient() as cdp:
        return await _run_chats(cdp, limit, unread)


async def cmd_read(chat_name: str, limit: int) -> Dict[str, Any]:
    async with CDPClient() as cdp:
        return await _run_read(cdp, chat_name, limit)


async def cmd_search(query: str, limit: int) -> Dict[str, Any]:
    async with CDPClient() as cdp:
        return await _run_search(cdp, query, limit)


async def cmd_send(chat_name: str, message: str, confirm: bool) -> Dict[str, Any]:
    if not confirm:
        return {
            "error": "Refusing to send without --confirm flag. Pass --confirm to actually send."
        }
    async with CDPClient() as cdp:
        return await _run_send(cdp, chat_name, message, confirm)


def run_server(host: str, port: int) -> None:
    app = web.Application()
    lock = asyncio.Lock()
    state: Dict[str, Any] = {"cdp": None}

    async def with_cdp(coro_fn):
        async with lock:
            if state["cdp"] is None:
                cdp = CDPClient()
                await cdp.__aenter__()
                state["cdp"] = cdp
            try:
                return await coro_fn(state["cdp"])
            except Exception:
                try:
                    await state["cdp"].__aexit__(None, None, None)
                except Exception:
                    pass
                cdp = CDPClient()
                await cdp.__aenter__()
                state["cdp"] = cdp
                return await coro_fn(state["cdp"])

    async def handle_chats(request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", "20"))
        unread = request.query.get("unread", "false").lower() == "true"
        return web.json_response(await with_cdp(lambda c: _run_chats(c, limit, unread)))

    async def handle_read(request: web.Request) -> web.Response:
        chat = request.query.get("chat", "")
        if not chat:
            return web.json_response({"error": "Missing ?chat= parameter"}, status=400)
        limit = int(request.query.get("limit", "30"))
        return web.json_response(await with_cdp(lambda c: _run_read(c, chat, limit)))

    async def handle_search(request: web.Request) -> web.Response:
        q = request.query.get("q", "")
        if not q:
            return web.json_response({"error": "Missing ?q= parameter"}, status=400)
        limit = int(request.query.get("limit", "20"))
        return web.json_response(await with_cdp(lambda c: _run_search(c, q, limit)))

    async def handle_send(request: web.Request) -> web.Response:
        body = await request.json()
        chat = body.get("chat", "")
        message = body.get("message", "")
        confirm = bool(body.get("confirm", False))
        if not chat or not message:
            return web.json_response({"error": "Missing chat or message"}, status=400)
        return web.json_response(
            await with_cdp(lambda c: _run_send(c, chat, message, confirm))
        )

    app.router.add_get("/chats", handle_chats)
    app.router.add_get("/read", handle_read)
    app.router.add_get("/search", handle_search)
    app.router.add_post("/send", handle_send)
    print(f"WhatsApp API listening on http://{host}:{port}", file=sys.stderr)
    web.run_app(app, host=host, port=port)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local WhatsApp Web API for AI agents")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_chats = sub.add_parser("chats", help="List recent WhatsApp chats")
    p_chats.add_argument("--limit", type=int, default=20)
    p_chats.add_argument("--unread", action="store_true")

    p_read = sub.add_parser("read", help="Read messages from a chat")
    p_read.add_argument("chat", help="Chat or group name (substring match)")
    p_read.add_argument("--limit", type=int, default=30)

    p_search = sub.add_parser("search", help="Search WhatsApp chats/messages")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=20)

    p_send = sub.add_parser("send", help="Send a WhatsApp message (requires --confirm)")
    p_send.add_argument("chat", help="Chat or group name")
    p_send.add_argument("message", help="Message text")
    p_send.add_argument("--confirm", action="store_true", help="Required safety flag")

    p_serve = sub.add_parser("serve", help="Run local HTTP JSON API server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)

    args = parser.parse_args()

    if args.cmd == "serve":
        run_server(args.host, args.port)
        return

    if args.cmd == "chats":
        res = asyncio.run(cmd_chats(args.limit, args.unread))
    elif args.cmd == "read":
        res = asyncio.run(cmd_read(args.chat, args.limit))
    elif args.cmd == "search":
        res = asyncio.run(cmd_search(args.query, args.limit))
    elif args.cmd == "send":
        res = asyncio.run(cmd_send(args.chat, args.message, args.confirm))
    else:
        res = {"error": f"Unknown command: {args.cmd}"}

    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
