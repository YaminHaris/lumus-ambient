#!/usr/bin/env python3
"""Read-only Gmail CLI over Chrome DevTools Protocol (port 9223).

Reuses whatsapp_api's prompt-free dedicated Chrome profile (~/.config/chrome-whatsapp)
where your Google account(s) are signed in.

Usage:
  python3 tools/gmail_api.py inbox [--limit 20] [--user 0|1]
  python3 tools/gmail_api.py search "<query>" [--limit 20] [--user 0|1]
  python3 tools/gmail_api.py read "<subject_or_sender>" [--user 0|1]
"""

import argparse
import asyncio
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
import whatsapp_api  # noqa: E402


async def _open_gmail(url: str) -> tuple[whatsapp_api.CDPClient, str, Any]:
    ws_url = whatsapp_api.get_browser_ws_url()
    cdp = whatsapp_api.CDPClient()
    cdp.ws = await websockets.connect(
        ws_url, max_size=20_000_000, open_timeout=15, ping_interval=None
    )
    created = await cdp._call("Target.createTarget", {"url": url, "background": True})
    tid = created["targetId"]
    pws = await websockets.connect(
        f"ws://127.0.0.1:9223/devtools/page/{tid}", max_size=20_000_000, open_timeout=15
    )
    return cdp, tid, pws


async def _eval_page(pws: Any, expr: str, msg_id: int = 1) -> Any:
    await pws.send(
        json.dumps(
            {
                "id": msg_id,
                "method": "Runtime.evaluate",
                "params": {"expression": expr, "returnByValue": True},
            }
        )
    )
    while True:
        msg = json.loads(await pws.recv())
        if msg.get("id") == msg_id:
            return msg.get("result", {}).get("result", {}).get("value")


async def _wait_for_rows(pws: Any, timeout_s: float = 12.0) -> None:
    steps = int(timeout_s / 0.5)
    for idx in range(steps):
        ready = await _eval_page(
            pws,
            "document.readyState === 'complete' && (document.querySelectorAll('tr.zA').length > 0 || location.hostname.includes('accounts.google.com'))",
            msg_id=100 + idx,
        )
        if ready:
            await asyncio.sleep(0.5)
            return
        await asyncio.sleep(0.5)


async def list_emails(url: str, limit: int) -> Dict[str, Any]:
    cdp, tid, pws = await _open_gmail(url)
    try:
        await _wait_for_rows(pws)
        js = f"""
        (() => {{
          if (location.hostname.includes('accounts.google.com')) {{
            return {{error: 'Not signed in to Gmail in profile 9223'}};
          }}
          const rows = Array.from(document.querySelectorAll('tr.zA')).slice(0, {limit}).map((tr, idx) => {{
            const sender = tr.querySelector('.yW span[email], .yW span')?.textContent?.trim() || '';
            const email = tr.querySelector('.yW span[email]')?.getAttribute('email') || '';
            const subj = tr.querySelector('.bog, .bqe')?.textContent?.trim() || '';
            const snip = tr.querySelector('.y2')?.textContent?.trim().replace(/^[\\s\\-\\u00a0]+/, '') || '';
            const date = tr.querySelector('.xW span')?.getAttribute('title') || tr.querySelector('.xW')?.textContent?.trim() || '';
            const unread = tr.classList.contains('zE');
            return {{index: idx, sender, email, subj, snip, date, unread}};
          }});
          return {{title: document.title, count: rows.length, emails: rows}};
        }})()
        """
        return await _eval_page(pws, js, msg_id=200)
    finally:
        await pws.close()
        await cdp._call("Target.closeTarget", {"targetId": tid})
        await cdp.ws.close()


async def read_email(query: str, user_idx: int = 0) -> Dict[str, Any]:
    encoded = urllib.parse.quote(query, safe="")
    url = f"https://mail.google.com/mail/u/{user_idx}/#search/{encoded}"
    cdp, tid, pws = await _open_gmail(url)
    try:
        await _wait_for_rows(pws)
        clicked = await _eval_page(
            pws,
            """
            (() => {
              const first = document.querySelector('tr.zA');
              if (!first) return false;
              first.click();
              return true;
            })()
            """,
            msg_id=201,
        )
        if not clicked:
            return {"error": f"No emails matched: {query}"}
        await asyncio.sleep(2.5)
        body = await _eval_page(
            pws,
            """
            (() => {
              const subj = document.querySelector('h2.hP')?.textContent?.trim() || '';
              const msgs = Array.from(document.querySelectorAll('.adn.ads')).map(m => {
                const from = m.querySelector('.gD')?.getAttribute('email') || m.querySelector('.gD')?.textContent?.trim() || '';
                const date = m.querySelector('.g3')?.getAttribute('title') || m.querySelector('.g3')?.textContent?.trim() || '';
                const text = m.querySelector('.a3s')?.textContent?.trim() || '';
                return {from, date, text: text.slice(0, 4000)};
              });
              return {subject: subj, message_count: msgs.length, messages: msgs};
            })()
            """,
            msg_id=202,
        )
        return body
    finally:
        await pws.close()
        await cdp._call("Target.closeTarget", {"targetId": tid})
        await cdp.ws.close()


def _resolve_user(val: str) -> int:
    v = val.strip().lower()
    if v in ("1", "work", "college", "secondary"):
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Gmail CLI via Chrome 9223.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inbox = sub.add_parser("inbox", help="List recent inbox emails")
    p_inbox.add_argument("--limit", type=int, default=20)
    p_inbox.add_argument("--user", default="0", help="0/personal or 1/college")

    p_search = sub.add_parser("search", help="Search Gmail")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--user", default="0", help="0/personal or 1/college")

    p_read = sub.add_parser("read", help="Open and read the top matching email thread")
    p_read.add_argument("query")
    p_read.add_argument("--user", default="0", help="0/personal or 1/college")

    args = parser.parse_args()
    u = _resolve_user(args.user)
    if args.cmd == "inbox":
        res = asyncio.run(
            list_emails(f"https://mail.google.com/mail/u/{u}/#search/in%3Ainbox", args.limit)
        )
    elif args.cmd == "search":
        q = urllib.parse.quote(args.query, safe="")
        res = asyncio.run(
            list_emails(f"https://mail.google.com/mail/u/{u}/#search/{q}", args.limit)
        )
    else:
        res = asyncio.run(read_email(args.query, u))

    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
