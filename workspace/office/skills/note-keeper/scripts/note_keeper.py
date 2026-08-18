#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path


NOTES_DIR = Path.cwd() / "notes"


def ensure_notes_dir() -> None:
    NOTES_DIR.mkdir(parents=True, exist_ok=True)


def safe_note_path(filename: str | None = None) -> Path:
    ensure_notes_dir()
    if not filename:
        filename = f"{datetime.now().date().isoformat()}.md"
    if not filename.endswith(".md"):
        filename += ".md"

    target = (NOTES_DIR / filename).resolve()
    notes_root = NOTES_DIR.resolve()
    if target != notes_root and notes_root not in target.parents:
        raise SystemExit("Refusing to write outside notes directory.")
    return target


def timestamp() -> str:
    return datetime.now().strftime("%H:%M")


def append_entry(path: Path, heading: str, text: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = []
    if not existing.strip():
        lines.append(f"# {path.stem}")
        lines.append("")
    lines.append(f"## {heading} {timestamp()}")
    lines.append("")
    lines.append(text.strip())
    lines.append("")
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def add_note(args) -> None:
    path = safe_note_path(args.file)
    append_entry(path, "Note", args.text)
    print(f"Added note to {path.relative_to(Path.cwd()).as_posix()}")


def add_todo(args) -> None:
    path = safe_note_path(args.file)
    append_entry(path, "Todo", f"- [ ] {args.text.strip()}")
    print(f"Added todo to {path.relative_to(Path.cwd()).as_posix()}")


def list_notes(_args) -> None:
    ensure_notes_dir()
    files = sorted(NOTES_DIR.glob("*.md"))
    if not files:
        print("No note files found.")
        return

    print("# Note Files")
    for path in files:
        size = path.stat().st_size
        rel = path.relative_to(Path.cwd()).as_posix()
        print(f"- `{rel}` ({size} bytes)")


def search_notes(args) -> None:
    ensure_notes_dir()
    query = args.query.lower()
    matches = []
    for path in sorted(NOTES_DIR.glob("*.md")):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue

        for line_no, line in enumerate(lines, start=1):
            if query in line.lower():
                matches.append((path, line_no, line.strip()))
                if len(matches) >= args.limit:
                    break
        if len(matches) >= args.limit:
            break

    if not matches:
        print(f"No matches for: {args.query}")
        return

    print(f"# Search Results: {args.query}")
    for path, line_no, line in matches:
        rel = path.relative_to(Path.cwd()).as_posix()
        print(f"- `{rel}:{line_no}` {line}")


def daily_report(args) -> None:
    path = safe_note_path(args.file)
    if not path.exists():
        print(f"No notes found for {path.name}.")
        return

    text = path.read_text(encoding="utf-8", errors="ignore")
    notes = []
    todos = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- [ ]") or stripped.startswith("- [x]"):
            todos.append(stripped)
        elif stripped and not stripped.startswith("#"):
            notes.append(stripped)

    print(f"# Daily Report: {path.stem}")
    print("")
    print("## Notes")
    if notes:
        for item in notes[:40]:
            print(f"- {item}")
    else:
        print("- No notes recorded.")

    print("")
    print("## Todos")
    if todos:
        for item in todos[:40]:
            print(item)
    else:
        print("- No todos recorded.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage Markdown notes inside office/notes.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Append a note.")
    add_parser.add_argument("--text", required=True)
    add_parser.add_argument("--file", default=None)
    add_parser.set_defaults(func=add_note)

    todo_parser = subparsers.add_parser("todo", help="Append a todo item.")
    todo_parser.add_argument("--text", required=True)
    todo_parser.add_argument("--file", default=None)
    todo_parser.set_defaults(func=add_todo)

    list_parser = subparsers.add_parser("list", help="List note files.")
    list_parser.set_defaults(func=list_notes)

    search_parser = subparsers.add_parser("search", help="Search note files.")
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.set_defaults(func=search_notes)

    daily_parser = subparsers.add_parser("daily", help="Render a daily report.")
    daily_parser.add_argument("--file", default=None)
    daily_parser.set_defaults(func=daily_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
