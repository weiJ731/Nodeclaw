#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


TEXT_EXTENSIONS = {
    ".bat",
    ".cfg",
    ".css",
    ".csv",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

NOTE_MARKERS = ("TODO", "FIXME", "NOTE")
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}


def safe_root(relative_root: str) -> Path:
    office_root = Path.cwd().resolve()
    target = (office_root / relative_root).resolve()
    if target != office_root and office_root not in target.parents:
        raise SystemExit("Refusing to scan outside the office workspace.")
    if not target.exists():
        raise SystemExit(f"Path does not exist: {relative_root}")
    if not target.is_dir():
        raise SystemExit(f"Path is not a directory: {relative_root}")
    return target


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS


def iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def audit(root: Path, max_notes: int) -> dict:
    files = []
    directories = 0
    extension_counts: Counter[str] = Counter()
    total_bytes = 0
    total_text_lines = 0
    notes = []

    for path in iter_files(root):
        rel = path.relative_to(Path.cwd()).as_posix()
        if path.is_dir():
            directories += 1
            continue
        if not path.is_file():
            continue

        size = path.stat().st_size
        suffix = path.suffix.lower() or "[no extension]"
        extension_counts[suffix] += 1
        total_bytes += size

        file_info = {
            "path": rel,
            "size": size,
            "extension": suffix,
            "text_lines": None,
        }

        if is_text_file(path):
            text = read_text(path)
            lines = text.splitlines()
            file_info["text_lines"] = len(lines)
            total_text_lines += len(lines)

            for line_no, line in enumerate(lines, start=1):
                upper = line.upper()
                if any(marker in upper for marker in NOTE_MARKERS):
                    if len(notes) < max_notes:
                        notes.append({
                            "path": rel,
                            "line": line_no,
                            "text": line.strip()[:220],
                        })

        files.append(file_info)

    largest_files = sorted(files, key=lambda item: item["size"], reverse=True)[:10]

    return {
        "root": root.relative_to(Path.cwd()).as_posix() if root != Path.cwd() else ".",
        "file_count": len(files),
        "directory_count": directories,
        "total_bytes": total_bytes,
        "total_text_lines": total_text_lines,
        "extensions": dict(extension_counts.most_common()),
        "largest_files": largest_files,
        "notes": notes,
    }


def render_markdown(report: dict) -> str:
    lines = [
        "# Office Auditor Report",
        "",
        f"- Root: `{report['root']}`",
        f"- Files: {report['file_count']}",
        f"- Directories: {report['directory_count']}",
        f"- Total bytes: {report['total_bytes']}",
        f"- Text lines: {report['total_text_lines']}",
        "",
        "## File Types",
    ]

    if report["extensions"]:
        for ext, count in report["extensions"].items():
            lines.append(f"- `{ext}`: {count}")
    else:
        lines.append("- No files found.")

    lines.extend(["", "## Largest Files"])
    if report["largest_files"]:
        for item in report["largest_files"]:
            lines.append(f"- `{item['path']}` ({item['size']} bytes)")
    else:
        lines.append("- No files found.")

    lines.extend(["", "## TODO / FIXME / NOTE"])
    if report["notes"]:
        for note in report["notes"]:
            lines.append(f"- `{note['path']}:{note['line']}` {note['text']}")
    else:
        lines.append("- No TODO/FIXME/NOTE markers found.")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit files inside the office workspace.")
    parser.add_argument("--root", default=".", help="Relative directory inside office to scan.")
    parser.add_argument("--max-notes", type=int, default=30, help="Maximum TODO/FIXME/NOTE lines to show.")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of Markdown.")
    args = parser.parse_args()

    root = safe_root(args.root)
    report = audit(root, max_notes=max(args.max_notes, 0))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report))


if __name__ == "__main__":
    main()
