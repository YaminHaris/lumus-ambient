#!/usr/bin/env python3
"""Local Google Keep API & CLI over Chrome DevTools Protocol (port 9223).

Reuses whatsapp_api's prompt-free dedicated Chrome profile (~/.config/chrome-whatsapp)
and auto-launches it if closed.

Usage:
  python3 ~/keep_api.py list [--limit 50]
  python3 ~/keep_api.py search "<query>"
  python3 ~/keep_api.py open
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
import whatsapp_api


async def _connect_keep(activate: bool = False) -> whatsapp_api.CDPClient:
    ws_url = whatsapp_api.get_browser_ws_url()
    cdp = whatsapp_api.CDPClient()
    cdp.ws = await websockets.connect(
        ws_url, max_size=20_000_000, open_timeout=15, ping_interval=None
    )
    targets = await cdp._call("Target.getTargets")
    keep_targets = [
        t
        for t in targets.get("targetInfos", [])
        if t.get("type") == "page" and "keep.google.com" in t.get("url", "")
    ]
    if keep_targets:
        tid = keep_targets[0]["targetId"]
    else:
        created = await cdp._call(
            "Target.createTarget",
            {"url": "https://keep.google.com/", "newWindow": activate, "background": not activate},
        )
        tid = created["targetId"]
        await asyncio.sleep(3.5)

    if activate:
        await cdp._call("Target.activateTarget", {"targetId": tid})

    att = await cdp._call("Target.attachToTarget", {"targetId": tid, "flatten": True})
    cdp.session_id = att["sessionId"]

    # Wait briefly for notes to render if Keep just loaded
    for _ in range(10):
        ready = await cdp.eval_js(
            "Boolean(document.querySelector('[role=listitem], .IZ65Hb-n0tgWb'))"
        )
        if ready:
            break
        await asyncio.sleep(0.5)
    return cdp


async def _fetch_notes(cdp: whatsapp_api.CDPClient, limit: int = 50) -> Dict[str, Any]:
    return await cdp.eval_js(
        f"""((limit) => {{
            if (location.hostname.includes('accounts.google.com')) {{
                return {{error: 'Not signed in to Google Keep in profile 9223'}};
            }}
            const raw = Array.from(document.querySelectorAll('[role=listitem], .IZ65Hb-n0tgWb'))
                .map(el => (el.innerText || '').trim())
                .filter(t => t && t !== 'Take a note…' && t !== 'Take a note...');
            const seen = new Set();
            const notes = [];
            for (const t of raw) {{
                if (seen.has(t)) continue;
                seen.add(t);
                const lines = t.split('\\n').map(s => s.trim()).filter(Boolean);
                notes.push({{
                    title: lines[0] || '',
                    body: lines.slice(1).join('\\n')
                }});
                if (notes.length >= limit) break;
            }}
            return {{count: notes.length, notes}};
        }})({limit})"""
    )


async def cmd_list(limit: int) -> Dict[str, Any]:
    cdp = await _connect_keep(activate=False)
    try:
        return await _fetch_notes(cdp, limit)
    finally:
        await cdp.__aexit__(None, None, None)


async def cmd_search(query: str, limit: int) -> Dict[str, Any]:
    res = await cmd_list(max(limit, 100))
    if "error" in res:
        return res
    q = query.lower()
    matches: List[Dict[str, str]] = [
        n
        for n in res.get("notes", [])
        if q in n.get("title", "").lower() or q in n.get("body", "").lower()
    ][:limit]
    return {"query": query, "count": len(matches), "notes": matches}


async def cmd_open() -> Dict[str, Any]:
    cdp = await _connect_keep(activate=True)
    try:
        url = await cdp.eval_js("location.href")
        return {"opened": url}
    finally:
        await cdp.__aexit__(None, None, None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Google Keep CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List Google Keep notes")
    p_list.add_argument("--limit", type=int, default=50)

    p_search = sub.add_parser("search", help="Search Google Keep notes")
    p_search.add_argument("query", help="Search text")
    p_search.add_argument("--limit", type=int, default=20)

    sub.add_parser("open", help="Open/focus Google Keep window")

    args = parser.parse_args()
    if args.cmd == "list":
        res = asyncio.run(cmd_list(args.limit))
    elif args.cmd == "search":
        res = asyncio.run(cmd_search(args.query, args.limit))
    else:
        res = asyncio.run(cmd_open())

    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
