# Vivaldi Profile Settings Sync (macOS)

One-way settings sync between Vivaldi browser profiles on macOS. Keeps two
profiles (for example work and personal) behaving identically — same
shortcuts, tab behavior, UI, and search engines — without copying passwords,
cookies, history, or bookmarks.

Inspired by [captlardass/VivaldiBrowserProfileCloning](https://github.com/captlardass/VivaldiBrowserProfileCloning)
(Windows PowerShell, whole-file copy). Instead of overwriting the whole
`Preferences` file, this script merges only the settings sections and syncs
the search-engine database, so the target profile keeps its own identity and
personal data.

## What it syncs

- Keyboard shortcuts (`vivaldi.actions`) and command chains
- Tab behavior, panels, toolbars, themes, appearance, layouts, start page
- Search engines, including custom ones (`Web Data` → `keywords` table)
- Default search engine selection (`default_search_provider*`, including the
  Secure Preferences mirror and its stale tamper hash)
- Chromium-level prefs: fonts, languages, spellcheck, search suggestions

## What it never touches

- Passwords, cookies, sessions, history, bookmarks, autofill
- Profile identity (name, avatar — these live in `Local State`)
- Accounts, sync/sign-in state
- Extensions and their keyboard shortcuts (install those per profile)

## Requirements

- macOS with Vivaldi installed (other platforms: pass `--user-data`)
- Python 3.8+, standard library only

## Usage

```bash
# see your profiles (display names and folder names both work)
python3 vivaldi-profile-sync.py --list

# dry run — shows what would change, writes nothing (this is the default)
python3 vivaldi-profile-sync.py --source Personal --target Work

# apply for real; quits Vivaldi automatically first
python3 vivaldi-profile-sync.py --source Personal --target Work --apply --force-quit
```

Vivaldi rewrites these files on exit, so it must be closed before `--apply`.
The script refuses to apply while Vivaldi is running unless you pass
`--force-quit` (a graceful quit that saves the session).

Every apply first backs up each modified file as `<name>.bak-<timestamp>`
inside the profile folder, then prunes old backups so only the newest N
remain (default 2, `--keep-backups N`). To restore, quit Vivaldi and rename
the `.bak` file back over the original.

## Options

| Flag | Effect |
|---|---|
| `--list` | list profiles and exit |
| `--apply` | write changes (default is dry run) |
| `--force-quit` | quit Vivaldi automatically before applying |
| `--full` | copy all Preferences except identity/account keys (original repo behavior) |
| `--keys a,b` | sync extra top-level Preferences keys |
| `--prune-engines` | also delete target custom search engines missing from the source |
| `--keep-backups N` | how many backups to keep per file (default 2) |
| `--user-data PATH` | non-default Vivaldi user-data directory |

## Notes and limitations

- Sync is one-way: whichever profile is `--source` at run time wins. Conflicts
  are never merged — source values replace target values.
- Chromium's tamper protection may reset the target's default search engine
  once after a sync; if that happens, re-pick it in Settings → Search and it
  sticks.
- Extension keyboard shortcuts are per-profile by design (they live in
  extension storage the script does not touch).
