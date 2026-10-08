#!/usr/bin/env python3
"""Sync Vivaldi settings from one profile to another on macOS.

Mac port of the idea behind captlardass/VivaldiBrowserProfileCloning (Windows
PowerShell). Instead of copying the whole Preferences file, this does a
key-level merge of the settings sections plus a sync of the search-engine
table, so the target profile keeps its identity, accounts, passwords,
bookmarks and history.

What gets synced (default mode):
  - Browser keyboard shortcuts      Preferences -> vivaldi.actions
  - Command chains                  Preferences -> vivaldi.chained_commands
  - Tab behavior, UI, panels,       Preferences -> vivaldi.tabs / panels /
    toolbars, themes, appearance        toolbars / theme(s) / appearance / ...
  - Search engines (incl. custom)   Web Data SQLite -> keywords table
  - Default engine selection        Preferences -> default_search_provider*
                                    and Secure Preferences mirror
  - Chromium-level UI prefs         browser (minus window placement), webkit,
                                    intl, spellcheck, search suggest

Never touched: Login Data, Cookies, History, Bookmarks, Local State (profile
names/avatars live there), sync/signin/account prefs, extensions.

Defaults to a dry run; pass --apply to write changes. Always quit Vivaldi
first (the browser rewrites these files on exit and would clobber the sync).
Each apply backs up modified files as <name>.bak-<timestamp> and then prunes
old backups so only the newest N remain (default 2, see --keep-backups).

Usage:
  vivaldi-profile-sync.py --list
  vivaldi-profile-sync.py --source Personal --target Presidio
  vivaldi-profile-sync.py --source Personal --target Presidio --apply
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_USER_DATA = Path.home() / "Library/Application Support/Vivaldi"

# vivaldi.* subsections that are per-profile state rather than settings
VIVALDI_EXCLUDE = {
    "sessions", "calendar", "geolocation", "workspaces", "history", "notes",
    "homepage_cache", "welcome", "language_at_install", "show_extensions_banner",
}

# vivaldi.* subsections that are settings and get synced
VIVALDI_INCLUDE = [
    "actions", "address_bar", "appearance", "auto_hide", "bookmarks",
    "chained_commands", "context_dialogs", "dashboard", "keyboard", "layouts",
    "list", "panels", "privacy", "quick_commands", "settings", "startpage",
    "startup", "status_bar", "system", "tabs", "theme", "themes", "toolbars",
    "translate", "webpages",
]

# top-level Preferences keys synced in default mode
TOP_INCLUDE = [
    "default_search_provider", "default_search_provider_data",
    "site_search_settings", "search", "intl", "spellcheck", "webkit", "browser",
]

# never synced, even with --full (identity / accounts / per-profile install state)
DENYLIST = {
    "profile", "sync", "signin", "gcm", "gaia_cookie", "google", "invalidation",
    "extensions", "account_tracker_service_last_update_time",
    "credentials_enable_autosignin", "credentials_enable_service",
    "countryid_at_install", "enterprise_profile_guid", "enterprise",
    "default_apps_install_state", "protection",
}


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump_json(path, data):
    # Chromium stores Preferences as one compact JSON line; keep the same shape
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def deep_merge(target, source):
    """Per-leaf merge, source wins. Dicts recurse, everything else replaces."""
    if isinstance(target, dict) and isinstance(source, dict):
        out = dict(target)
        for k, v in source.items():
            out[k] = deep_merge(out[k], v) if k in out else v
        return out
    return source


def profiles(user_data):
    """Return {dir_name: display_name} from Local State, excluding system/guest."""
    try:
        info = load_json(user_data / "Local State").get("profile", {}).get("info_cache", {})
    except FileNotFoundError:
        sys.exit(f"error: no Local State found in {user_data} — is this a Vivaldi user-data dir?")
    return {d: i.get("name", d) for d, i in info.items() if d not in ("System Profile", "Guest Profile")}


def resolve(user_data, name):
    profs = profiles(user_data)
    for d, disp in profs.items():
        if name.lower() in (d.lower(), disp.lower()):
            return d
    sys.exit(f"error: profile {name!r} not found. Known: " +
             ", ".join(f"{d} ({n})" for d, n in profs.items()))


def vivaldi_running():
    # escape hatch so the check can be bypassed in tests against a copied tree
    if os.environ.get("VIVALDI_SYNC_SKIP_RUNNING_CHECK"):
        return False
    return subprocess.run(["pgrep", "-x", "Vivaldi"], capture_output=True).returncode == 0


def quit_vivaldi():
    print("Asking Vivaldi to quit ...")
    subprocess.run(["osascript", "-e", 'tell application "Vivaldi" to quit'],
                   capture_output=True)
    for _ in range(20):
        if not vivaldi_running():
            print("Vivaldi has quit.")
            return True
        time.sleep(1)
    return False


def backup(path, ts):
    dest = f"{path}.bak-{ts}"
    shutil.copy2(path, dest)
    return dest


def prune_backups(profile_dir, filenames, keep, report):
    """Keep only the newest `keep` .bak-* files per managed file (keep=0 removes all)."""
    removed = []
    for name in filenames:
        baks = sorted(Path(profile_dir).glob(f"{name}.bak-*"))
        doomed = baks if keep == 0 else baks[:-keep]
        for old in doomed:
            old.unlink()
            removed.append(old.name)
    if removed:
        report["pruned"] = f"old backups pruned (keeping last {keep}): " + ", ".join(removed)


# ---------------------------------------------------------------- preferences

def sync_preferences(src_dir, tgt_dir, apply, full, extra_keys, ts, report):
    src = load_json(src_dir / "Preferences")
    tgt = load_json(tgt_dir / "Preferences")
    orig = json.dumps(tgt, sort_keys=True)

    if full:
        plan = {k: v for k, v in src.items() if k not in DENYLIST and k in src}
        new = dict(tgt)
        for k, v in plan.items():
            new[k] = v
    else:
        keys = list(TOP_INCLUDE) + [k.strip() for k in extra_keys if k.strip()]
        new = dict(tgt)
        for k in keys:
            if k in src:
                new[k] = deep_merge(tgt.get(k), src[k])
        vsrc = src.get("vivaldi", {})
        vnew = dict(tgt.get("vivaldi", {}))
        for sub in VIVALDI_INCLUDE:
            if sub in vsrc:
                vnew[sub] = deep_merge(vnew.get(sub), vsrc[sub])
        new["vivaldi"] = vnew

    # keep the target's window geometry either way
    if isinstance(new.get("browser"), dict) and "window_placement" in tgt.get("browser", {}):
        new["browser"] = {**new["browser"], "window_placement": tgt["browser"]["window_placement"]}
    # stale "engine was reset" flag from the target's past would misreport state
    if "default_search_provider" in src and "default_search_provider" in new:
        new["default_search_provider"].pop("reset_occurred", None)

    if json.dumps(new, sort_keys=True) == orig:
        report["prefs"] = "Preferences: no differences in synced keys"
        return
    changed = [k for k in new if json.dumps(new.get(k), sort_keys=True) != json.dumps(tgt.get(k), sort_keys=True)]
    report["prefs"] = f"Preferences: {len(changed)} top-level key(s) changed: {', '.join(sorted(changed))}"
    if apply:
        report["backups"].append(backup(tgt_dir / "Preferences", ts))
        dump_json(tgt_dir / "Preferences", new)


def sync_secure_prefs(src_dir, tgt_dir, apply, full, ts, report):
    sp_src_p, sp_tgt_p = src_dir / "Secure Preferences", tgt_dir / "Secure Preferences"
    if not sp_src_p.exists() or not sp_tgt_p.exists():
        return
    src = load_json(sp_src_p)
    tgt = load_json(sp_tgt_p)
    changed = False
    # the mirrored default-engine data is MAC-protected; move the value and
    # drop the target's stale MAC so Vivaldi re-hashes instead of flagging tamper
    if "default_search_provider_data" in src and src["default_search_provider_data"] != tgt.get("default_search_provider_data"):
        tgt["default_search_provider_data"] = src["default_search_provider_data"]
        changed = True
    macs = tgt.get("protection", {}).get("macs", {})
    if "default_search_provider_data" in macs:
        del macs["default_search_provider_data"]
        changed = True
    if not changed:
        return
    report["secure"] = "Secure Preferences: engine mirror updated, stale MAC dropped"
    if apply:
        report["backups"].append(backup(sp_tgt_p, ts))
        dump_json(sp_tgt_p, tgt)


# ---------------------------------------------------------------- search engines

def fetch_keywords(db):
    con = sqlite3.connect(f"file:{db}?immutable=1&mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        # Vivaldi's `position` column holds non-UTF-8 bytes; fetch as blob so it round-trips
        cols = [r[1] for r in con.execute("PRAGMA table_info(keywords)")]
        sel = ", ".join(f'CAST("{c}" AS BLOB) AS "{c}"' if c == "position" else f'"{c}"' for c in cols)
        rows = [dict(r) for r in con.execute(f"SELECT {sel} FROM keywords")]
    finally:
        con.close()
    return rows


def engine_key(row):
    if row.get("sync_guid"):
        return ("guid", row["sync_guid"])
    return ("name", str(row.get("short_name", "")), str(row.get("keyword", "")))


def sync_engines(src_dir, tgt_dir, apply, prune, ts, report):
    src_p, tgt_p = src_dir / "Web Data", tgt_dir / "Web Data"
    if not src_p.exists() or not tgt_p.exists():
        report["engines"] = "Web Data missing in source or target — engine sync skipped"
        return
    src_rows = fetch_keywords(src_p)
    tgt_rows = fetch_keywords(tgt_p)
    tgt_by_key = {engine_key(r): r for r in tgt_rows}
    src_keys = {engine_key(r) for r in src_rows}

    stats_cols = {"id", "usage_count", "last_visited", "date_created"}
    inserts, updates, deletes = [], [], []
    for s in src_rows:
        t = tgt_by_key.get(engine_key(s))
        if t is None:
            inserts.append(s)
        else:
            diff_cols = [c for c in s if c not in stats_cols and s[c] != t.get(c)]
            if diff_cols:
                updates.append((s, t, diff_cols))
    if prune:
        # Vivaldi's built-in engines are vivaldi:// entries or factory rows (date_created == 0)
        deletes = [t for t in tgt_rows
                   if engine_key(t) not in src_keys
                   and t.get("date_created", 0) > 0
                   and not str(t.get("url", "")).startswith("vivaldi://")]

    if not (inserts or updates or deletes):
        report["engines"] = f"Search engines: in sync ({len(src_rows)} engines)"
        return
    lines = []
    if inserts:
        lines.append("add " + ", ".join(f"{r['short_name']} ({r['keyword']})" for r in inserts))
    if updates:
        lines.append("update " + ", ".join(f"{s['short_name']}" for s, _, _ in updates))
    if deletes:
        lines.append("delete " + ", ".join(f"{r['short_name']} ({r['keyword']})" for r in deletes))
    report["engines"] = "Search engines: " + "; ".join(lines)
    if not apply:
        return
    report["backups"].append(backup(tgt_p, ts))
    con = sqlite3.connect(tgt_p)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(keywords)")]
        ins_cols = [c for c in cols if c != "id"]
        upd_cols = [c for c in cols if c not in stats_cols]
        for s in inserts:
            vals = [s.get(c) for c in ins_cols]
            con.execute(f"INSERT INTO keywords ({','.join(ins_cols)}) VALUES ({','.join('?' * len(ins_cols))})", vals)
        for s, t, _ in updates:
            sets = ",".join(f"{c}=?" for c in upd_cols)
            con.execute(f"UPDATE keywords SET {sets} WHERE id=?", [s.get(c) for c in upd_cols] + [t["id"]])
        for d in deletes:
            con.execute("DELETE FROM keywords WHERE id=?", [d["id"],])
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Sync Vivaldi settings between profiles (macOS).")
    ap.add_argument("--user-data", type=Path, default=DEFAULT_USER_DATA)
    ap.add_argument("--list", action="store_true", help="list profiles and exit")
    ap.add_argument("--source", help="source profile (dir or display name)")
    ap.add_argument("--target", help="target profile (dir or display name)")
    ap.add_argument("--apply", action="store_true", help="write changes (default is dry run)")
    ap.add_argument("--full", action="store_true",
                    help="copy all Preferences except identity/account keys (original repo behavior)")
    ap.add_argument("--keys", default="", help="extra top-level Preferences keys to sync, comma-separated")
    ap.add_argument("--prune-engines", action="store_true",
                    help="also delete target custom search engines that the source does not have")
    ap.add_argument("--force-quit", action="store_true", help="quit Vivaldi automatically before applying")
    ap.add_argument("--keep-backups", type=int, default=2, metavar="N",
                    help="after an apply, keep only the newest N .bak-* files per managed file (default 2; 0 keeps none)")
    args = ap.parse_args()
    if args.keep_backups < 0:
        ap.error("--keep-backups must be >= 0")

    ud = args.user_data.expanduser()
    if args.list:
        for d, name in profiles(ud).items():
            pref = ud / d / "Preferences"
            mtime = datetime.fromtimestamp(pref.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if pref.exists() else "-"
            print(f"{d:12}  {name:20}  Preferences modified: {mtime}")
        return

    if not args.source or not args.target:
        ap.error("--source and --target are required (or use --list)")
    src_d, tgt_d = resolve(ud, args.source), resolve(ud, args.target)
    if src_d == tgt_d:
        sys.exit("error: source and target are the same profile")

    names = profiles(ud)
    print(f"source: {src_d} ({names[src_d]})   ->   target: {tgt_d} ({names[tgt_d]})")
    mode = "FULL copy" if args.full else "settings-only merge"
    print(f"mode:   {mode}   |   " + ("APPLY" if args.apply else "DRY RUN (pass --apply to write)"))
    print()

    if args.apply:
        if vivaldi_running():
            if not args.force_quit:
                sys.exit("error: Vivaldi is running and would overwrite these files on exit.\n"
                         "Quit it first, or pass --force-quit.")
            if not quit_vivaldi():
                sys.exit("error: Vivaldi did not quit within 20s; aborting.")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    src_dir, tgt_dir = ud / src_d, ud / tgt_d
    report = {"backups": []}

    sync_preferences(src_dir, tgt_dir, args.apply, args.full, args.keys.split(","), ts, report)
    sync_secure_prefs(src_dir, tgt_dir, args.apply, args.full, ts, report)
    sync_engines(src_dir, tgt_dir, args.apply, args.prune_engines, ts, report)
    if args.apply:
        prune_backups(tgt_dir, ["Preferences", "Secure Preferences", "Web Data"], args.keep_backups, report)

    for section in ("prefs", "secure", "engines", "pruned"):
        if section in report:
            print(report[section])
    if args.apply and report["backups"]:
        print("backups:")
        for b in report["backups"]:
            print("  " + b)
        print("\nDone. Start Vivaldi and check Settings > Search and the keyboard/map of shortcuts in the target profile.")
    elif not args.apply:
        print("\nDry run only — nothing was written.")


if __name__ == "__main__":
    main()
