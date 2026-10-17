#!/usr/bin/env python3
"""
gameinfo_updater.py - auto-update Deadlock's gameinfo.gi from OptimizationLock

grabs a config variant from the repo, re-applies your locked convars from an
overrides file, backs up the old file and writes the result

usage:
    python gameinfo_updater.py update [options]
    python gameinfo_updater.py list-configs
    python gameinfo_updater.py restore --latest
    python gameinfo_updater.py backup
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

VERSION = "1.0.0"

REPO = "Sqooky/OptimizationLock"
RAW_BASE = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"
DEFAULT_REF = "main"

# name -> (description, path in repo)
CONFIGS: Dict[str, Tuple[str, str]] = {
    "sqooky": ("Sqooky's OptimizationLock (recommended)", "Sqooky's .gi/gameinfo.gi"),
    "performance":   ("Sqooky's Max FPS config (best performance)", "maximumfps or minimum spec config and resources here/gameinfo.gi"),
    "boot":   ("Boot's Maximum FPS", "boot's maxium fps config/gameinfo.gi"), # "maxium" minor spelling error !!1!
    "kaiz":   ("Kaizuchaneru's Minimum Spec", "kaizuchanerus minimum spec/gameinfo.gi"),
    "piggy":  ("Piggy's Config", "Piggy's config/gameinfo.gi"),
    "eskay": ("Eskay's modified fork of Sqooky's", "Eskay's config/gameinfo.gi"),
    "clean":  ("Clean / Near-Vanilla", "clean gameinfo.gi/gameinfo.gi"),
}

# mark injected convars that aren't arlready present
MANAGED_TAG = "// ===== gameinfo-updater added convars"
MANAGED_HEADER = "        " + MANAGED_TAG + " -- do not edit this block manually ====="
MANAGED_FOOTER = "        // ===== end gameinfo-updater added convars ====="
CONVAR_INDENT = "        "

# convar regex
CONVAR_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<slashes>//[ \t]*)?(?P<name>\w+)(?P<pad>[ \t]+)"
    r"(?P<value>\"[^\"\n]*\"|\S+)"
    r"(?P<trail>[ \t]+//[^\n]*)?"
    r"[ \t]*$"
)


def detect_gameinfo() -> Optional[Path]:
    """look in the usual steam install spots for gameinfo.gi"""
    suffix = Path("steamapps/common/Deadlock/game/citadel/gameinfo.gi")

    if sys.platform == "win32":
        roots = [
            Path(r"C:\Program Files (x86)\Steam"),
            Path(r"C:\Program Files\Steam"),
        ]
        for env in ("STEAM_PATH", "STEAMPATH"):
            val = os.environ.get(env)
            if val:
                roots.insert(0, Path(val))
    else:
        home = Path.home()
        roots = [
            home / ".steam" / "steam",
            home / ".local" / "share" / "Steam",
            home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
        ]

    for root in roots:
        p = root / suffix
        if p.is_file():
            return p
    return None


def resolve_gi(args: argparse.Namespace) -> Path:
    if args.gameinfo:
        return Path(args.gameinfo)
    p = detect_gameinfo()
    if p is None:
        sys.exit(
            "ERROR: Could not auto-detect gameinfo.gi\n"
            "       Use --gameinfo <path> to specify it"
        )
    return p


def config_to_url(name: str) -> str:
    _, repo_path = CONFIGS[name]
    return RAW_BASE.format(repo=REPO, ref=DEFAULT_REF, path=urllib.parse.quote(repo_path, safe="/"))


def normalize_github_url(url: str) -> str:
    if "raw.githubusercontent.com" in url:
        return url
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/blob/([^/]+)/(.+)", url)
    if m:
        repo, ref, path = m.group(1), m.group(2), m.group(3)
        return "https://raw.githubusercontent.com/{}/{}/{}".format(repo, ref, path)
    return url


def fetch_text(url: str) -> str:
    print("  Fetching: " + url)
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        sys.exit("ERROR: HTTP {} {}\n  URL: {}".format(exc.code, exc.reason, url))
    except urllib.error.URLError as exc:
        sys.exit("ERROR: Network error -- {}\n  URL: {}".format(exc.reason, url))
    return raw.decode("utf-8")


def validate_braces(text: str) -> None:
    """make sure braces are balanced before writing"""
    stripped = re.sub(r"//[^\n]*", "", text)
    balance = stripped.count("{") - stripped.count("}")
    if balance != 0:
        sys.exit(
            "ERROR: Brace balance of modified file is off by {:+d}\n"
            "Refusing to write. Please report this as a bug".format(balance)
        )


def parse_overrides(path: Path) -> Tuple[Dict[str, str], Set[str]]:
    """
    read the overrides file into (set_map, comment_set)

    line formats:
      # ...          ignored comment
      // name        comment that convar out in the output
      name value     set convar to value (uncomments it if needed)
    """
    set_map: Dict[str, str] = {}
    comment_set: Set[str] = set()

    with path.open(encoding="utf-8-sig", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            # "// name", optional trailing "# note"
            m = re.match(r"^//\s*(\w+)(?:\s+#.*)?$", line)
            if m:
                comment_set.add(m.group(1))
                continue

            # "name value [# note]"
            m = re.match(r"^(\w+)\s+(.+)$", line)
            if m:
                name = m.group(1)
                value = re.sub(r"\s+#.*$", "", m.group(2)).strip()
                set_map[name] = value
                continue

            print("  [overrides] Warning line {}: unrecognized -- {!r}".format(lineno, raw.rstrip()))

    return set_map, comment_set


def find_convars_block(lines: List[str]) -> Tuple[int, int]:
    """
    find convars block
    returns (open_line, close_line) as line indices, or (-1, -1) if not found
    the body is lines[open_line+1 : close_line]
    """
    text = "".join(lines)
    m = re.search(r"\bConVars\b\s*\{", text)
    if not m:
        return -1, -1

    open_line = text.count("\n", 0, m.end() - 1)

    depth = 0
    for i in range(open_line, len(lines)):
        no_comment = re.sub(r"//[^\n]*", "", lines[i])
        depth += no_comment.count("{") - no_comment.count("}")
        if depth == 0:
            return open_line, i

    return -1, -1


def strip_managed_block(text: str) -> str:
    """drop any managed block injected on a previous run"""
    pattern = (
        r"[ \t]*"
        + re.escape(MANAGED_TAG)
        + r".*?"
        + re.escape(MANAGED_FOOTER)
        + r"[^\n]*\n?"
    )
    return re.sub(pattern, "", text, flags=re.DOTALL)


def quote(val: str) -> str:
    is_quoted = val.startswith('"') and val.endswith('"')
    return val if is_quoted else '"{}"'.format(val)


def count_braces(line: str) -> Tuple[int, int]:
    no_comment = re.sub(r"//[^\n]*", "", line)
    return no_comment.count("{"), no_comment.count("}")


def process_body(
    body: List[str],
    set_map: Dict[str, str],
    comment_set: Set[str],
) -> Tuple[List[str], Dict]:
    """
    apply value/comment locks

    returns (new_body_lines, summary)
    """
    new_lines: List[str] = []
    sub_depth = 0
    handled_set: Set[str] = set()
    handled_comment: Set[str] = set()
    changes_set: List[Tuple[str, str, str, str]] = []   # name, old, new, trail
    changes_comment: List[Tuple[str, str, str]] = []    # name, value, trail
    comment_values: Dict[str, str] = {}
    comment_trails: Dict[str, str] = {}

    for line in body:
        opens, closes = count_braces(line)

        if sub_depth > 0 or opens > closes:
            sub_depth += opens - closes
            new_lines.append(line)
            continue

        m = CONVAR_RE.match(line.rstrip("\r\n"))
        if not m:
            new_lines.append(line)
            continue

        name = m.group("name")
        is_comment = m.group("slashes") is not None
        old_val = m.group("value")
        indent = m.group("indent")
        pad = m.group("pad")
        trail = m.group("trail") or ""
        eol = "\r\n" if line.endswith("\r\n") else "\n"

        if name in set_map:
            new_val = quote(set_map[name])
            new_lines.append("{}{}{}{}{}{}".format(indent, name, pad, new_val, trail, eol))
            if name not in handled_set and (is_comment or old_val != new_val):
                display_old = ("//" + old_val) if is_comment else old_val
                changes_set.append((name, display_old, new_val, trail))
            handled_set.add(name)

        elif name in comment_set:
            if not is_comment:
                new_lines.append("{}// {}{}{}{}{}".format(indent, name, pad, old_val, trail, eol))
                if name not in handled_comment:
                    changes_comment.append((name, old_val, trail))
            else:
                new_lines.append(line)  # already commented
            handled_comment.add(name)
            comment_values[name] = old_val
            comment_trails[name] = trail

        else:
            new_lines.append(line)

    return new_lines, {
        "set": changes_set,
        "commented": changes_comment,
        "comment_values": comment_values,
        "comment_trails": comment_trails,
        "handled_set": handled_set,
        "handled_comment": handled_comment,
    }


def apply_overrides(
    text: str,
    set_map: Dict[str, str],
    comment_set: Set[str],
) -> Tuple[str, Dict]:
    """
    apply overrides to convars block, injecting a managed block for
    any value-lock that wasn't found upstream. 
    returns (new_text, summary)
    """
    text = strip_managed_block(text)
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)

    open_line, close_line = find_convars_block(lines)
    if open_line < 0:
        sys.exit("ERROR: Could not locate ConVars block in the gameinfo file")

    pre = lines[: open_line + 1]
    body = lines[open_line + 1 : close_line]
    post = lines[close_line :]

    new_body, summary = process_body(body, set_map, comment_set)

    # anything set that wasn't already existing in convars gets its own managed block
    unmatched = [n for n in set_map if n not in summary["handled_set"]]
    if unmatched:
        inj = [MANAGED_HEADER + eol]
        for name in unmatched:
            inj.append("{}{} {}{}".format(CONVAR_INDENT, name, quote(set_map[name]), eol))
        inj.append(MANAGED_FOOTER + eol)
        new_body = inj + new_body

    summary["injected"] = unmatched
    return "".join(pre + new_body + post), summary


def make_backup(gi_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = gi_path.parent / "gameinfo.{}.bak".format(stamp)
    shutil.copy2(gi_path, bak)
    print("  Backup:   {}".format(bak))
    return bak


def write_backup(gi_path: Path, data: bytes) -> Path:
    """write a backup from pre-captured bytes"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = gi_path.parent / "gameinfo.{}.bak".format(stamp)
    bak.write_bytes(data)
    print("  Backup:   {}".format(bak))
    return bak


def find_latest_backup(gi_path: Path) -> Optional[Path]:
    candidates = sorted(
        gi_path.parent.glob("gameinfo.????????_??????.bak"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def print_diff(original: str, modified: str, label_a: str, label_b: str) -> None:
    a = original.splitlines(keepends=True)
    b = modified.splitlines(keepends=True)
    diff = list(difflib.unified_diff(a, b, fromfile=label_a, tofile=label_b, n=3))
    if diff:
        sys.stdout.buffer.write("".join(diff).encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    else:
        print("  (no diff -- file unchanged)")


def fmt_comment(trail: str) -> str:
    s = trail.strip()
    if s.startswith("//"):
        s = s[2:]
    return s.strip()


def print_summary(summary: Dict, set_map: Dict, comment_set: Set) -> None:
    out = ["\n  -- Override Summary --------------------------------------------------"]

    if summary["set"]:
        out.append("  SET (value-locked):")
        for name, old, new, trail in summary["set"]:
            line = "    {}  {}  ->  {}".format(name, old, new)
            cmt = fmt_comment(trail)
            if cmt:
                line += "   // {}".format(cmt)
            out.append(line)

    if summary["injected"]:
        out.append("  INJECTED (not found upstream; added to managed block):")
        for name in summary["injected"]:
            out.append("    {}  {}".format(name, quote(set_map[name])))

    comment_values = summary.get("comment_values", {})
    comment_trails = summary.get("comment_trails", {})

    if summary["commented"]:
        out.append("  COMMENTED OUT (upstream value shown):")
        for name, val, trail in summary["commented"]:
            line = "    {}  {}".format(name, val)
            cmt = fmt_comment(trail)
            if cmt:
                line += "   // {}".format(cmt)
            out.append(line)

    already_ok = summary.get("handled_comment", set()) - {n for n, _, _ in summary["commented"]}
    if already_ok:
        out.append("  ALREADY COMMENTED (no change needed):")
        for name in sorted(already_ok):
            line = "    {}  {}".format(name, comment_values.get(name, "?"))
            cmt = fmt_comment(comment_trails.get(name, ""))
            if cmt:
                line += "   // {}".format(cmt)
            out.append(line)

    not_found = comment_set - summary.get("handled_comment", set())
    if not_found:
        out.append("  NOT FOUND in upstream (comment directive had no effect):")
        for name in sorted(not_found):
            out.append("    {}".format(name))

    if not any([summary["set"], summary["injected"], summary["commented"], already_ok, not_found]):
        out.append("  No changes -- all overrides already match the upstream")

    out.append("  ---------------------------------------------------------------------")
    print("\n".join(out))


def extract_convars(text: str) -> Dict[str, Tuple[str, bool]]:
    """
    returns name -> (value, is_commented). sub-blocks are skipped and if a name
    shows up twice the last one takes prio
    """
    lines = text.splitlines(keepends=True)
    open_line, close_line = find_convars_block(lines)
    if open_line < 0:
        return {}
    body = lines[open_line + 1 : close_line]

    result: Dict[str, Tuple[str, bool]] = {}
    sub_depth = 0
    for line in body:
        opens, closes = count_braces(line)
        if sub_depth > 0 or opens > closes:
            sub_depth += opens - closes
            continue

        m = CONVAR_RE.match(line.rstrip("\r\n"))
        if not m:
            continue
        result[m.group("name")] = (m.group("value"), m.group("slashes") is not None)

    return result


def compare_convars(
    old: Dict[str, Tuple[str, bool]],
    new: Dict[str, Tuple[str, bool]],
    ignore: Set[str],
) -> Dict[str, List]:
    """
    diff two extract_convars() dicts, skipping names in `ignore`

    keys: added/removed (name, value, is_commented), changed (name, old, new),
    toggled (name, value, old_commented, new_commented)
    """
    added: List[Tuple[str, str, bool]] = []
    removed: List[Tuple[str, str, bool]] = []
    changed: List[Tuple[str, str, str]] = []
    toggled: List[Tuple[str, str, bool, bool]] = []

    for name, (val, is_c) in new.items():
        if name in ignore:
            continue
        if name not in old:
            added.append((name, val, is_c))
            continue
        old_val, old_c = old[name]
        if old_val != val:
            changed.append((name, old_val, val))
        elif old_c != is_c:
            toggled.append((name, val, old_c, is_c))

    for name, (val, is_c) in old.items():
        if name not in ignore and name not in new:
            removed.append((name, val, is_c))

    return {"added": added, "removed": removed, "changed": changed, "toggled": toggled}


def print_upstream_changes(changes: Dict[str, List]) -> None:
    out = ["\n  -- Upstream ConVar Changes (ignoring overrides) ----------------------"]

    if changes["added"]:
        out.append("  ADDED in new version:")
        for name, val, is_c in changes["added"]:
            out.append("    {}{}  {}".format("// " if is_c else "", name, val))

    if changes["removed"]:
        out.append("  REMOVED in new version:")
        for name, val, is_c in changes["removed"]:
            out.append("    {}{}  {}".format("// " if is_c else "", name, val))

    if changes["changed"]:
        out.append("  CHANGED:")
        for name, old_val, new_val in changes["changed"]:
            out.append("    {}  {}  ->  {}".format(name, old_val, new_val))

    if changes["toggled"]:
        out.append("  COMMENT STATE TOGGLED:")
        for name, val, old_c, new_c in changes["toggled"]:
            old_s = "commented" if old_c else "active"
            new_s = "commented" if new_c else "active"
            out.append("    {}  {}  ({} -> {})".format(name, val, old_s, new_s))

    if not any(changes.values()):
        out.append("  No upstream convar changes (ignoring overrides)")

    out.append("  ---------------------------------------------------------------------")
    print("\n".join(out))


def load_upstream(args: argparse.Namespace) -> Tuple[str, str]:
    """figure out where the new config comes from and return (text, label)"""
    if args.url:
        raw_url = normalize_github_url(args.url)
        return fetch_text(raw_url), raw_url

    if args.config and args.config not in CONFIGS:
        sys.exit(
            "ERROR: Unknown config '{}'\n"
            "       Run 'list-configs' to see available names".format(args.config)
        )
    raw_url = config_to_url(args.config or "sqooky")
    return fetch_text(raw_url), raw_url


def load_overrides(args: argparse.Namespace) -> Tuple[Dict[str, str], Set[str]]:
    if args.overrides:
        ov_path = Path(args.overrides)
        if not ov_path.is_file():
            sys.exit("ERROR: --overrides file not found: {}".format(ov_path))
    else:
        ov_path = Path(__file__).parent / "overrides.gi"
        if not ov_path.is_file():
            print("  No overrides.gi found -- proceeding without overrides")
            return {}, set()

    set_map, comment_set = parse_overrides(ov_path)
    print("  Overrides: {}  ({} value-lock(s), {} comment-lock(s))".format(
        ov_path, len(set_map), len(comment_set)))
    return set_map, comment_set


def cmd_update(args: argparse.Namespace) -> None:
    upstream, source_label = load_upstream(args)

    set_map, comment_set = load_overrides(args)

    # value-lock takes prio if a name is somehow both
    for name in sorted(set(set_map) & comment_set):
        print(
            "  [overrides] Warning: '{}' is both value-locked and comment-locked; "
            "value-lock takes precedence".format(name)
        )

    gi_path = resolve_gi(args)
    print("  Target:   {}".format(gi_path))

    # show upstream changes vs the current file, ignoring overrides
    if gi_path.is_file():
        try:
            current_text = gi_path.read_text(encoding="utf-8", errors="replace")
            ignore = set(set_map) | comment_set
            changes = compare_convars(extract_convars(current_text), extract_convars(upstream), ignore)
            print_upstream_changes(changes)
        except Exception as exc:
            print("  [upstream-changes] Could not compute: {}".format(exc))

    if set_map or comment_set:
        modified, summary = apply_overrides(upstream, set_map, comment_set)
    else:
        modified = upstream
        summary = {
            "set": [], "commented": [], "injected": [],
            "comment_values": {}, "comment_trails": {},
            "handled_set": set(), "handled_comment": set(),
        }

    validate_braces(modified)

    if args.diff:
        original = gi_path.read_text(encoding="utf-8", errors="replace") if gi_path.is_file() else ""
        print("\n  -- Unified diff (current file -> new) --------------------------------")
        print_diff(original, modified, label_a=str(gi_path), label_b=source_label)

    if args.dry_run:
        print_summary(summary, set_map, comment_set)
        print("\n  DRY RUN -- nothing was written")
        return

    pre_write_bytes = gi_path.read_bytes() if gi_path.is_file() else None

    gi_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = gi_path.with_name(gi_path.name + ".tmp")
    try:
        tmp_path.write_text(modified, encoding="utf-8")
        os.replace(tmp_path, gi_path)
    except PermissionError:
        tmp_path.unlink(missing_ok=True)
        sys.exit(
            "\nERROR: Could not write to {} (Permission denied)\n"
            "       The game (or another program) is likely running and has the file locked\n"
            "       Please close Deadlock and run this command again"
            .format(gi_path)
        )

    if not args.no_backup and pre_write_bytes is not None:
        write_backup(gi_path, pre_write_bytes)

    print("  Written:  {}".format(gi_path))
    print_summary(summary, set_map, comment_set)


def cmd_list_configs(_args: argparse.Namespace) -> None:
    print("Available configs (use with --config <name>):\n")
    for name, (desc, _) in CONFIGS.items():
        print("  {:<8}  {}".format(name, desc))
        print("             {}".format(config_to_url(name)))
        print()


def cmd_restore(args: argparse.Namespace) -> None:
    gi_path = resolve_gi(args)

    if args.latest:
        bak = find_latest_backup(gi_path)
        if bak is None:
            sys.exit("ERROR: No backups found in {}".format(gi_path.parent))
    else:
        bak = Path(args.backup_file)
        if not bak.is_file():
            sys.exit("ERROR: Backup file not found: {}".format(bak))

    if gi_path.is_file():
        make_backup(gi_path)

    shutil.copy2(bak, gi_path)
    print("  Restored: {}\n       ->   {}".format(bak, gi_path))


def cmd_backup(args: argparse.Namespace) -> None:
    gi_path = resolve_gi(args)
    if not gi_path.is_file():
        sys.exit("ERROR: gameinfo.gi not found: {}".format(gi_path))
    make_backup(gi_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gameinfo_updater",
        description=(
            "Auto-update Deadlock's gameinfo.gi from the OptimizationLock repo, preserving your locked convars from an overrides file"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python gameinfo_updater.py update\n"
            "  python gameinfo_updater.py update --config boot --dry-run\n"
            "  python gameinfo_updater.py update --overrides ~/my-locks.gi\n"
            "  python gameinfo_updater.py update --url https://github.com/Sqooky/OptimizationLock/blob/main/test_cfg/gameinfo.gi\n"
            "  python gameinfo_updater.py list-configs\n"
            "  python gameinfo_updater.py restore --latest\n"
            "  python gameinfo_updater.py backup\n"
        ),
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.required = True

    update_parser = subparsers.add_parser(
        "update",
        help="Fetch a config from the repo and write gameinfo.gi",
        description=("Download a gameinfo.gi variant from OptimizationLock and re-apply your locked convars"),
    )
    source_group = update_parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--config", metavar="NAME",
        help="Config shortcut name (default: sqooky). See list-configs",
    )
    source_group.add_argument(
        "--url", metavar="URL",
        help="GitHub URL (blob or raw) to any gameinfo.gi in the repo",
    )
    update_parser.add_argument(
        "--overrides", metavar="PATH",
        help="Path to overrides file. Default: overrides.gi in same directory as this script",
    )
    update_parser.add_argument(
        "--gameinfo", metavar="PATH",
        help="Path to gameinfo.gi to write. Tries to auto-detect from Steam if omitted",
    )
    update_parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the override summary (and diff, if --diff) without writing",
    )
    update_parser.add_argument(
        "--diff", action="store_true",
        help="Print a unified diff of the current file vs the new one (off by default)",
    )
    update_parser.add_argument(
        "--no-backup", action="store_true",
        help="Skip creating a timestamped backup before overwriting",
    )
    update_parser.set_defaults(func=cmd_update)

    list_configs_parser = subparsers.add_parser("list-configs", help="List available config names and their URLs")
    list_configs_parser.set_defaults(func=cmd_list_configs)

    restore_parser = subparsers.add_parser("restore", help="Restore a previous backup of gameinfo.gi")
    restore_group = restore_parser.add_mutually_exclusive_group(required=True)
    restore_group.add_argument("--latest", action="store_true", help="Restore the most recent backup")
    restore_group.add_argument("--backup", dest="backup_file", metavar="PATH", help="Restore a specific backup file")
    restore_parser.add_argument("--gameinfo", metavar="PATH", help="Path to gameinfo.gi. Auto-detected if omitted")
    restore_parser.set_defaults(func=cmd_restore)

    backup_parser = subparsers.add_parser("backup", help="Snapshot current gameinfo.gi with a timestamp")
    backup_parser.add_argument("--gameinfo", metavar="PATH", help="Path to gameinfo.gi. Tries to auto-detect if omitted")
    backup_parser.set_defaults(func=cmd_backup)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
