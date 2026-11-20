#!/usr/bin/env python3
"""CLI helper to list, search, and read local PDF and Markdown notes."""

import argparse
import hashlib
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

NOTES_DIR = Path(os.environ.get("LUMUS_NOTES_DIR", str(Path.home() / "Notes")))
DOWNLOADS_DIR = Path(os.environ.get("LUMUS_DOWNLOADS_DIR", str(Path.home() / "Downloads")))
CACHE_DIR = Path.home() / ".cache" / "notes_api"
TEXT_EXTS = {".md", ".txt", ".csv"}
PDF_EXTS = {".pdf"}


def iter_files(subject: str | None = None) -> list[Path]:
    """Return sorted list of note files, optionally filtered by subject substring."""
    files: list[Path] = []
    if NOTES_DIR.exists():
        for p in NOTES_DIR.rglob("*"):
            if p.is_file() and p.suffix.lower() in (TEXT_EXTS | PDF_EXTS):
                files.append(p)
    if DOWNLOADS_DIR.exists():
        for p in DOWNLOADS_DIR.iterdir():
            if p.is_file() and p.suffix.lower() in (TEXT_EXTS | PDF_EXTS) and p.name.upper().startswith("CR"):
                files.append(p)
    if subject:
        sub = subject.lower()
        files = [f for f in files if sub in str(f).lower()]
    return sorted(set(files))


def extract_pdf_text(pdf_path: Path) -> str:
    """Extract full text from a PDF using pdftotext with mtime-keyed caching."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    st = pdf_path.stat()
    key = hashlib.sha256(f"{pdf_path}:{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:16]
    cache_file = CACHE_DIR / f"{key}.txt"
    if cache_file.exists():
        return cache_file.read_text(errors="replace")
    res = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    text = res.stdout if res.returncode == 0 else ""
    if text:
        cache_file.write_text(text)
    return text


def get_text(path: Path) -> str:
    """Return text content of a markdown, text, or PDF file."""
    if path.suffix.lower() in PDF_EXTS:
        return extract_pdf_text(path)
    return path.read_text(errors="replace")


def cmd_list(args: argparse.Namespace) -> int:
    """List available semester note files."""
    files = iter_files(args.subject)
    if not files:
        print("No matching note files found.")
        return 0
    for f in files:
        size_kb = f.stat().st_size // 1024
        print(f"{size_kb:5d} KB  {f}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Search across all notes and PDFs for a regex/keyword query."""
    files = iter_files(args.subject)
    pattern = re.compile(args.query, re.IGNORECASE)
    hits = 0
    for f in files:
        text = get_text(f)
        if not text:
            continue
        pages = text.split("\f") if f.suffix.lower() in PDF_EXTS else [text]
        for page_idx, page_text in enumerate(pages, start=1):
            lines = page_text.splitlines()
            for line_idx, line in enumerate(lines, start=1):
                if pattern.search(line):
                    loc = f"p.{page_idx}:L{line_idx}" if f.suffix.lower() in PDF_EXTS else f"L{line_idx}"
                    snippet = " ".join(line.split())[:160]
                    print(f"[{f.name} | {loc}] {snippet}")
                    hits += 1
                    if hits >= args.max:
                        return 0
    if hits == 0:
        print(f"No matches for '{args.query}'.")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    """Read a note or specific page range from a PDF."""
    files = iter_files(args.subject)
    target_q = args.file.lower()
    matches = [f for f in files if target_q in f.name.lower() or target_q in str(f).lower()]
    if not matches:
        print(f"No file matching '{args.file}'.", file=sys.stderr)
        return 1
    target = matches[0]
    if len(matches) > 1:
        print(f"# Multiple matches ({len(matches)}); showing: {target}", file=sys.stderr)

    if target.suffix.lower() in PDF_EXTS and args.pages:
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", args.pages.strip())
        if not m:
            print("Invalid --pages format; use N or N-M (e.g. 1-3).", file=sys.stderr)
            return 1
        start_p = m.group(1)
        end_p = m.group(2) or start_p
        res = subprocess.run(
            ["pdftotext", "-layout", "-f", start_p, "-l", end_p, str(target), "-"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        content = res.stdout
    else:
        content = get_text(target)

    if args.max_chars and len(content) > args.max_chars:
        content = content[: args.max_chars] + f"\n\n... [truncated at {args.max_chars} chars]"
    print(content)
    return 0


def main() -> int:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    parser = argparse.ArgumentParser(description="Search & read 5th-semester PDF/Markdown notes.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List note files")
    p_list.add_argument("subject", nargs="?", help="Optional subject filter (e.g. CR3101)")
    p_list.set_defaults(func=cmd_list)

    p_search = sub.add_parser("search", help="Search across notes & PDFs")
    p_search.add_argument("query", help="Keyword or regex to search")
    p_search.add_argument("--subject", help="Optional subject filter (e.g. CR3101)")
    p_search.add_argument("--max", type=int, default=15, help="Max results (default: 15)")
    p_search.set_defaults(func=cmd_search)

    p_read = sub.add_parser("read", help="Read a note or PDF page range")
    p_read.add_argument("file", help="Filename or substring to match")
    p_read.add_argument("--subject", help="Optional subject filter")
    p_read.add_argument("--pages", help="PDF page or range, e.g. 1-3")
    p_read.add_argument("--max-chars", type=int, default=8000, help="Max output chars (default: 8000)")
    p_read.set_defaults(func=cmd_read)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
