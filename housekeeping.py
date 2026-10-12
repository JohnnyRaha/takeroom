#!/usr/bin/env python3
"""Inventory and clean up a Takeroom install.

    python3 housekeeping.py                    inventory only, changes nothing
    python3 housekeeping.py --clean raw        show what removing raw would free
    python3 housekeeping.py --clean raw --yes  actually remove it

Nothing is deleted without both --clean and --yes. Run it without --yes first;
that is the dry run, and it prints every path it would touch.

Kept takes are never touched by any category. Neither are mastered WAVs or
MP3s — those are the deliverables, and there is no undo.

Categories:

  raw          Raw generator output for takes that already have a master.
               Re-derivable only by regenerating, but the master is what you
               actually use. Usually the biggest single win.
  acestep-tmp  ACE-Step's own scratch copies in .cache/acestep/tmp/api_audio.
               Takeroom already copied these; the originals are dead weight
               and nothing cleans them up.
  failed       Takes that never produced audio, and their rows.
  orphans      Audio on disk with no library row, and rows pointing at files
               that no longer exist.
  backups      backup-<timestamp>/ directories left by install.sh.
  logs         Truncate logs/*.log to zero.
  pycache      __pycache__ directories and .pyc files.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

CATEGORIES = ["raw", "acestep-tmp", "failed", "orphans", "backups", "logs", "pycache"]


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


@dataclass
class Plan:
    label: str
    paths: list[Path] = field(default_factory=list)
    rows: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def bytes(self) -> int:
        return sum(tree_size(p) for p in self.paths)


class Install:
    def __init__(self, root: Path, acestep: Path | None) -> None:
        self.root = root.resolve()
        self.data = Path(os.environ.get("TAKEROOM_DATA", self.root / "data"))
        if not self.data.is_absolute():
            self.data = (self.root / self.data).resolve()
        self.acestep = (acestep or Path.home() / "ACE-Step-1.5").expanduser()
        self.db = self.data / "library.sqlite3"
        self.takes: list[dict] = []
        if self.db.is_file():
            conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                self.takes = [dict(r) for r in conn.execute("SELECT * FROM takes")]
            except sqlite3.DatabaseError as exc:
                print(f"  warning: cannot read library ({exc})", file=sys.stderr)
            conn.close()

    # -- reporting ----------------------------------------------------------

    def inventory(self) -> None:
        print(f"\n  {self.root}")
        print(f"  data: {self.data}\n")

        if not self.data.exists():
            print("  no data directory — nothing generated yet\n")
            return

        rows = [
            ("masters", self.data / "masters", "keep — these are the deliverables"),
            ("raw", self.data / "raw", "removable once mastered"),
            ("stems", self.data / "stems", "re-derivable, slowly"),
            ("library.sqlite3", self.db, "keep"),
            ("logs", self.root / "logs", "truncatable"),
        ]
        for name, path, note in rows:
            print(f"  {name:18} {human(tree_size(path)):>10}   {note}")

        tmp = self.acestep_tmp()
        if tmp.exists():
            print(f"  {'acestep scratch':18} {human(tree_size(tmp)):>10}   duplicate of raw, removable")

        backups = self.backup_dirs()
        if backups:
            print(f"  {'backups':18} {human(sum(tree_size(b) for b in backups)):>10}   "
                  f"{len(backups)} from install.sh")

        print()
        by_status: dict[str, int] = {}
        for take in self.takes:
            by_status[take["status"]] = by_status.get(take["status"], 0) + 1
        kept = sum(1 for t in self.takes if t["starred"])
        summary = ", ".join(f"{v} {k}" for k, v in sorted(by_status.items()))
        print(f"  {len(self.takes)} takes ({summary or 'none'}), {kept} kept")

        total = tree_size(self.data) + tree_size(self.root / "logs") + tree_size(tmp)
        total += sum(tree_size(b) for b in backups)
        print(f"  {human(total)} total\n")

    # -- plans --------------------------------------------------------------

    def acestep_tmp(self) -> Path:
        return self.acestep / ".cache" / "acestep" / "tmp" / "api_audio"

    def backup_dirs(self) -> list[Path]:
        return sorted(p for p in self.root.glob("backup-*") if p.is_dir())

    def known_files(self) -> set[Path]:
        known: set[Path] = set()
        for take in self.takes:
            for key in ("raw_path", "master_path", "mp3_path"):
                if take.get(key):
                    known.add(Path(take[key]))
            if take.get("stem_dir"):
                known.add(Path(take["stem_dir"]))
        return known

    def plan(self, category: str) -> Plan:
        if category == "raw":
            p = Plan("raw renders", note="takes that already have a master")
            for take in self.takes:
                if take["starred"]:
                    continue
                raw, master = take.get("raw_path"), take.get("master_path")
                if raw and master and Path(raw).is_file() and Path(master).is_file():
                    p.paths.append(Path(raw))
                    # clear the column too, or the row points at a file that
                    # no longer exists and ?kind=raw starts 404ing
                    p.rows.append(take["id"])
            return p

        if category == "acestep-tmp":
            tmp = self.acestep_tmp()
            p = Plan("acestep scratch", note="ACE-Step's own copies, already imported")
            if tmp.is_dir():
                p.paths = sorted(f for f in tmp.iterdir() if f.is_file())
            return p

        if category == "failed":
            p = Plan("failed takes", note="no audio was produced")
            for take in self.takes:
                if take["status"] != "failed" or take["starred"]:
                    continue
                p.rows.append(take["id"])
                for key in ("raw_path", "master_path", "mp3_path"):
                    if take.get(key) and Path(take[key]).exists():
                        p.paths.append(Path(take[key]))
            return p

        if category == "orphans":
            p = Plan("orphans", note="files with no row, rows with no files")
            known = self.known_files()
            for sub in ("raw", "masters"):
                folder = self.data / sub
                if folder.is_dir():
                    for f in folder.iterdir():
                        if f.is_file() and f not in known:
                            p.paths.append(f)
            stems = self.data / "stems"
            if stems.is_dir():
                ids = {t["id"] for t in self.takes}
                for d in stems.iterdir():
                    if d.is_dir() and d.name not in ids:
                        p.paths.append(d)
            for take in self.takes:
                if take["starred"] or take["status"] != "ready":
                    continue
                master = take.get("master_path")
                if master and not Path(master).is_file():
                    p.rows.append(take["id"])
                    # Take the row's other files with it. Otherwise removing
                    # the row orphans them, and only a second run would notice.
                    for key in ("raw_path", "mp3_path", "stem_dir"):
                        if take.get(key) and Path(take[key]).exists():
                            path = Path(take[key])
                            if path not in p.paths:
                                p.paths.append(path)
            return p

        if category == "backups":
            return Plan("install backups", paths=self.backup_dirs(),
                        note="originals replaced by install.sh")

        if category == "logs":
            logs = self.root / "logs"
            p = Plan("logs", note="truncated to zero, not deleted")
            if logs.is_dir():
                p.paths = sorted(f for f in logs.glob("*.log") if f.stat().st_size > 0)
            return p

        if category == "pycache":
            p = Plan("bytecode", note="regenerated automatically")
            for root, dirs, _files in os.walk(self.root):
                if ".venv" in root or "demucs-venv" in root:
                    continue
                for d in list(dirs):
                    if d == "__pycache__":
                        p.paths.append(Path(root) / d)
            return p

        raise ValueError(f"unknown category: {category}")

    # -- execution ----------------------------------------------------------

    def apply(self, category: str, plan: Plan, commit: bool) -> int:
        freed = plan.bytes
        if not commit:
            return freed

        for path in plan.paths:
            try:
                if category == "logs" and path.is_file():
                    path.write_text("")
                elif path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            except OSError as exc:
                print(f"    could not remove {path}: {exc}", file=sys.stderr)

        if plan.rows and self.db.is_file():
            conn = sqlite3.connect(self.db)
            if category == "raw":
                conn.executemany("UPDATE takes SET raw_path='' WHERE id=?",
                                 [(r,) for r in plan.rows])
            else:
                conn.executemany("DELETE FROM takes WHERE id=?", [(r,) for r in plan.rows])
            conn.commit()
            conn.close()
        return freed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inventory and clean a Takeroom install",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="categories: " + ", ".join(CATEGORIES) + ", or all")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--acestep", type=Path, default=None)
    parser.add_argument("--clean", nargs="+", metavar="CATEGORY", default=[])
    parser.add_argument("--yes", action="store_true", help="actually delete")
    args = parser.parse_args()

    install = Install(args.root, args.acestep)

    if not (args.root / "takeroom" / "main.py").is_file():
        print(f"\n  {args.root} is not a Takeroom project root "
              f"(no takeroom/main.py)\n", file=sys.stderr)
        return 1

    install.inventory()

    if not args.clean:
        print("  Nothing cleaned. Pass --clean with one or more of:")
        print(f"    {', '.join(CATEGORIES)}, all\n")
        return 0

    categories = CATEGORIES if "all" in args.clean else args.clean
    unknown = [c for c in categories if c not in CATEGORIES]
    if unknown:
        print(f"  unknown category: {', '.join(unknown)}", file=sys.stderr)
        return 2

    if args.yes:
        print("  Removing\n")
    else:
        print("  Dry run — nothing will be changed\n")

    total = 0
    for category in categories:
        plan = install.plan(category)
        if not plan.paths and not plan.rows:
            continue
        freed = install.apply(category, plan, args.yes)
        total += freed
        detail = []
        if plan.paths:
            detail.append(f"{len(plan.paths)} item(s)")
        if plan.rows:
            detail.append(f"{len(plan.rows)} row(s)")
        print(f"  {plan.label:18} {human(freed):>10}   {', '.join(detail)}  — {plan.note}")
        for path in plan.paths[:4]:
            print(f"      {path}")
        if len(plan.paths) > 4:
            print(f"      … and {len(plan.paths) - 4} more")

    print()
    if args.yes:
        print(f"  freed {human(total)}\n")
    else:
        print(f"  would free {human(total)} — add --yes to do it\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
