#!/usr/bin/env python3
# Copyright 2026 Octoprox Authors
# SPDX-License-Identifier: Apache-2.0

"""Release notes in docs/_data/releases.yml: draft a release's entry, add it, or print one as Markdown.

  release_notes.py draft VERSION SINCE_TAG [--ai]   a draft entry from the commits since SINCE_TAG (--ai: Claude writes it)
  release_notes.py add FILE VERSION                  check the entry in FILE is VERSION's and put it at the top of the notes file
  release_notes.py markdown VERSION                  the entry of VERSION as Markdown, for the GitHub Release

Needs PyYAML only, so CI can run it without installing the app.
"""

from __future__ import annotations

import argparse
import datetime
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
NOTES_FILE = ROOT / "docs" / "_data" / "releases.yml"
DOCS_URL = "https://www.octoprox.com"
# Where CI pushes each tag's image (ci.yml), for the install line of its GitHub Release.
IMAGE = "ghcr.io/octoprox/octoprox"
PACKAGE_URL = "https://github.com/octoprox/octoprox/pkgs/container/octoprox"
VERSION = re.compile(r"\d+\.\d+\.\d+")
# The most of the docs diff Claude is shown, in characters.
AI_DOCS_DIFF_LIMIT = 60_000

ENTRY_HELP = """\
# One entry per change people who use Octoprox will notice, in their words:
# what they can do now or what changed for them. Plain sentences, no dashes
# other than "-". `docs` (optional) is a page on octoprox.com, such as
# connecting#exit-headers. Start a breaking change with "Upgrade note:".
# Leave `changes: []` for a release with nothing to tell users: no entry is added.
"""


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def commit_subjects(since: str) -> list[str]:
    """The subjects of the commits since ``since``, oldest first, without merges and version bumps."""
    subjects = git("log", "--no-merges", "--reverse", "--format=%s", f"{since}..HEAD").splitlines()
    return [s for s in subjects if s and not s.startswith("Bump version")]


def quoted(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def draft_from_commits(version: str, since: str) -> str:
    """An entry with one change per commit subject, for the person releasing to rewrite."""
    lines = [
        ENTRY_HELP,
        f"- version: {version}",
        f"  date: {datetime.date.today().isoformat()}",
        "  title: ",
        "  changes:",
    ]
    subjects = commit_subjects(since)
    lines += [f"    - text: {quoted(s)}" for s in subjects] or ["    []"]
    return "\n".join(lines) + "\n"


def draft_with_claude(version: str, since: str) -> str:
    """An entry Claude writes from the commits and the docs changes since ``since``."""
    docs_diff = git("diff", f"{since}..HEAD", "--", "docs/", "README.md")[:AI_DOCS_DIFF_LIMIT]
    prompt = f"""Write the release notes entry for Octoprox {version}, an open-source self-hosted proxy manager.

Readers run Octoprox and use its web UI and API. Say what they can do now or what changed for them, not how the
code changed. Plain short sentences, present tense. Never use em-dashes or en-dashes. No file, class or function
names. Backticks only for environment variables, HTTP headers, config values and CLI flags. Skip refactors, tests
and CI. Start a breaking change or a required upgrade step with "Upgrade note:". If nothing would matter to users,
give `changes: []`.

Reply with only this YAML, no code fence and no other text:

- version: {version}
  date: {datetime.date.today().isoformat()}
  title: <at most 8 words naming the main themes, no trailing period>
  changes:
    - text: "<one or two sentences, double-quoted>"
      docs: <optional: a docs page below, without .md, optionally #anchor of one of its headings>

Docs pages: {", ".join(sorted(p.stem for p in (ROOT / "docs").glob("*.md")))}

Commits since {since}:
{chr(10).join("- " + s for s in commit_subjects(since))}

Changed files:
{git("diff", "--stat", f"{since}..HEAD")}
Docs and README changes:
{docs_diff}
"""
    result = subprocess.run(["claude", "-p", prompt], cwd=ROOT, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "claude exited with an error")
    text = re.sub(r"^```\w*\n|```\s*$", "", result.stdout.strip(), flags=re.MULTILINE).strip()
    return ENTRY_HELP + text + "\n"


def check_entry(text: str) -> dict | None:
    """The one entry in ``text``; None when it lists no changes. ValueError when it is malformed."""
    data = yaml.safe_load(text)
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError("the file must hold exactly one entry, starting with '- version:'")
    entry = data[0]
    version = str(entry.get("version", ""))
    if not VERSION.fullmatch(version):
        raise ValueError(f"version {version!r} is not MAJOR.MINOR.PATCH")
    if entry.get("from") is not None and not VERSION.fullmatch(str(entry["from"])):
        raise ValueError(f"from {entry['from']!r} is not MAJOR.MINOR.PATCH")
    if not isinstance(entry.get("date"), datetime.date):
        raise ValueError("date must be YYYY-MM-DD")
    changes = entry.get("changes") or []
    if not isinstance(changes, list):
        raise ValueError("changes must be a list")
    if not changes:
        return None
    if not str(entry.get("title") or "").strip():
        raise ValueError("title is empty")
    for change in changes:
        if not isinstance(change, dict) or not str(change.get("text") or "").strip():
            raise ValueError(f"every change needs a text: {change!r}")
        if set(change) - {"text", "docs"}:
            raise ValueError(f"a change has only text and docs: {change!r}")
        if not isinstance(change.get("docs", ""), str):
            raise ValueError(f"docs is a page name such as connecting#exit-headers: {change!r}")
    return entry


def entries() -> list[dict]:
    return yaml.safe_load(NOTES_FILE.read_text()) or []


def add(path: Path, version: str) -> None:
    """Check the entry in ``path`` is ``version``'s and put it above the newest entry of the notes file, comments dropped."""
    text = path.read_text()
    entry = check_entry(text)
    if entry is None:
        print("No changes listed: no release notes entry added.")
        return
    if str(entry["version"]) != version:
        raise ValueError(f"the entry is for {entry['version']}, the release is {version}")
    if any(str(e["version"]) == version for e in entries()):
        raise ValueError(f"the notes file already has {version}")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#")).strip() + "\n\n"
    notes = NOTES_FILE.read_text()
    at = re.search(r"^- version:", notes, flags=re.MULTILINE)
    position = at.start() if at else len(notes)
    updated = notes[:position] + body + notes[position:]
    # Dropping the comment lines or a different indentation must not change what the entry says.
    if (yaml.safe_load(updated) or [None])[0] != entry:
        raise ValueError("the entry would not read the same in the notes file: start it with '- version:' and keep no line starting with '#'")
    NOTES_FILE.write_text(updated)
    print(f"Added the {version} entry to the release notes.")


def install(version: str) -> list[str]:
    """How to get ``version``: its image, which CI has pushed by the time the release is published."""
    return [
        "## Install",
        "",
        "```bash",
        f"docker pull {IMAGE}:{version}",
        "```",
        "",
        f"For amd64 and arm64. [Package]({PACKAGE_URL}) · [Upgrading]({DOCS_URL}/deployment#restarts-and-upgrades)",
    ]


def markdown(version: str) -> str:
    """The entry of ``version`` as Markdown, or a pointer to the changelog when it has none, then how to install it."""
    version = version.removeprefix("v")
    entry = next((e for e in entries() if str(e["version"]) == version), None)
    changelog = f"{DOCS_URL}/changelog"
    if entry is None:
        lines = [f"No user-facing changes in this release. See the [changelog]({changelog})."]
    else:
        lines = [f"## {entry['title']}", ""]
        if entry.get("from"):
            lines += [f"Covers {entry['from']} to {entry['version']}.", ""]
        for change in entry["changes"]:
            docs = f" ([docs]({DOCS_URL}/{change['docs']}))" if change.get("docs") else ""
            lines.append(f"- {change['text']}{docs}")
        lines += ["", f"All releases: [changelog]({changelog})"]
    return "\n".join([*lines, "", *install(version)]) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    draft = commands.add_parser("draft")
    draft.add_argument("version")
    draft.add_argument("since")
    draft.add_argument("--ai", action="store_true")
    add_command = commands.add_parser("add")
    add_command.add_argument("file", type=Path)
    add_command.add_argument("version")
    commands.add_parser("markdown").add_argument("version")
    args = parser.parse_args()

    try:
        if args.command == "draft":
            if args.ai and shutil.which("claude"):
                try:
                    sys.stdout.write(draft_with_claude(args.version, args.since))
                    return 0
                # OSError: a prompt longer than the system takes as one argument (128 KiB on Linux)
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                    print(f"Claude could not draft the notes ({e}); drafting from the commits.", file=sys.stderr)
            sys.stdout.write(draft_from_commits(args.version, args.since))
        elif args.command == "add":
            add(args.file, args.version.removeprefix("v"))
        else:
            sys.stdout.write(markdown(args.version))
    except (ValueError, yaml.YAMLError) as e:
        print(f"Release notes: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
