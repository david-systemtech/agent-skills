#!/usr/bin/env python3
"""Vendor agent skills from other people's GitHub repos, pinned by commit.

    skills.py list   owner/repo [--ref REF]
    skills.py add    owner/repo [path] [--ref REF] [--name NAME] [--exclude PAT ...]
    skills.py update [name ...] [--summary FILE]
    skills.py verify [--fetch]
    skills.py remove name

skills.json is the manifest: for each skill, where it came from (repo, path,
ref) and the exact commit and content digest that is vendored under skills/.
A directory under skills/ that the manifest does not list is one of your own
skills and is left alone.

Standard library only, Python 3.9+. Needs git on PATH to resolve refs.
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "skills.json"
SKILLS_DIR = ROOT / "skills"

# Never vendored: git plumbing, and upstream CI that cannot run from a subfolder.
ALWAYS_EXCLUDE = (".git", ".github", ".gitignore", ".gitattributes")
DEFAULT_MAX_MB = 25
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class Problem(Exception):
    """A failure with a message fit to show as-is."""


# --------------------------------------------------------------------------- #
# Manifest                                                                    #
# --------------------------------------------------------------------------- #

def load_manifest() -> dict:
    if not MANIFEST.exists():
        return {"version": 1, "skills": {}}
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    data.setdefault("skills", {})
    return data


def save_manifest(data: dict) -> None:
    data["skills"] = dict(sorted(data["skills"].items()))
    MANIFEST.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Upstream                                                                    #
# --------------------------------------------------------------------------- #

def resolve_ref(repo: str, ref: str) -> str:
    """The commit a branch, tag or HEAD points at right now. A full SHA is itself."""
    if SHA_RE.match(ref):
        return ref
    url = f"https://github.com/{repo}.git"
    try:
        out = subprocess.run(
            ["git", "ls-remote", url, ref, f"{ref}^{{}}"],
            check=True, capture_output=True, text=True, timeout=60,
        ).stdout
    except FileNotFoundError:
        raise Problem("git is not on PATH")
    except subprocess.CalledProcessError as error:
        raise Problem(f"cannot reach {repo}: {error.stderr.strip() or error}")
    found = {}
    for line in out.splitlines():
        sha, _, name = line.partition("\t")
        found[name] = sha
    # A peeled annotated tag first, then the branch, the tag, and HEAD itself.
    for candidate in (f"refs/tags/{ref}^{{}}", f"refs/heads/{ref}", f"refs/tags/{ref}", ref):
        if candidate in found:
            return found[candidate]
    raise Problem(f"{repo} has no ref named {ref!r}")


def download(repo: str, sha: str) -> bytes:
    """The repo's tarball at one commit. SKILLS_UPSTREAM_TOKEN reaches private repos."""
    token = os.environ.get("SKILLS_UPSTREAM_TOKEN")
    if token:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/tarball/{sha}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
    else:
        request = urllib.request.Request(f"https://codeload.github.com/{repo}/tar.gz/{sha}")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise Problem(f"cannot download {repo}@{sha[:7]}: HTTP {error.code}")
    except urllib.error.URLError as error:
        raise Problem(f"cannot download {repo}@{sha[:7]}: {error.reason}")


def read_tree(blob: bytes) -> dict[str, tuple[bytes, bool]]:
    """Every regular file in the tarball as {posix path: (bytes, executable)}.

    The archive's single top-level directory is stripped. Links and devices are
    skipped rather than followed, and a path that climbs out is refused.
    """
    files: dict[str, tuple[bytes, bool]] = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts[1:]
            if not parts:
                continue
            if ".." in parts or PurePosixPath(*parts).is_absolute():
                raise Problem(f"refusing archive path {member.name!r}")
            if not member.isfile():
                if not member.isdir():
                    print(f"  skipped (not a regular file): {'/'.join(parts)}", file=sys.stderr)
                continue
            handle = archive.extractfile(member)
            if handle is not None:
                files["/".join(parts)] = (handle.read(), bool(member.mode & 0o100))
    return files


def skill_dirs(tree: dict[str, tuple[bytes, bool]]) -> list[str]:
    """Every directory holding a SKILL.md, '.' for the repo root."""
    return sorted(
        str(PurePosixPath(path).parent) for path in tree if PurePosixPath(path).name == "SKILL.md"
    )


def select(tree: dict, path: str, exclude: list[str]) -> dict[str, tuple[bytes, bool]]:
    """The files of one skill, keyed relative to its directory."""
    prefix = "" if path in (".", "") else path.strip("/") + "/"
    patterns = list(ALWAYS_EXCLUDE) + list(exclude)
    chosen = {}
    for name, value in tree.items():
        if not name.startswith(prefix):
            continue
        relative = name[len(prefix):]
        parts = relative.split("/")
        # A pattern matches the whole relative path or any leading directory of it.
        heads = ["/".join(parts[: i + 1]) for i in range(len(parts))]
        if any(fnmatch.fnmatch(head, pattern) for head in heads for pattern in patterns):
            continue
        chosen[relative] = value
    return chosen


# --------------------------------------------------------------------------- #
# Skill content                                                               #
# --------------------------------------------------------------------------- #

def frontmatter(text: str) -> dict[str, str]:
    """Top-level scalar keys of a SKILL.md header. Tolerant, not a YAML parser."""
    match = re.match(r"^﻿?---\r?\n(.*?)\r?\n---\s*(\r?\n|$)", text, re.S)
    if not match:
        return {}
    fields: dict[str, str] = {}
    key = None
    for line in match.group(1).splitlines():
        top = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if top:
            key, value = top.group(1), top.group(2).strip()
            fields[key] = "" if value in (">", ">-", "|", "|-", ">+", "|+") else value.strip("\"'")
        elif key and line.startswith((" ", "\t")) and line.strip():
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return fields


def guess_license(files: dict, tree: dict) -> str:
    """What SKILL.md declares, else what a LICENSE file reads like, else 'unknown'."""
    declared = frontmatter(files["SKILL.md"][0].decode("utf-8", "replace")).get("license")
    if declared:
        return declared
    for source in (files, tree):
        for name, (body, _) in source.items():
            if "/" in name or not name.upper().startswith(("LICENSE", "LICENCE", "COPYING")):
                continue
            text = body.decode("utf-8", "replace")
            for needle, label in (
                ("Apache License", "Apache-2.0"),
                ("MIT License", "MIT"),
                ("Permission is hereby granted, free of charge", "MIT"),
                ("GNU GENERAL PUBLIC LICENSE", "GPL"),
                ("Mozilla Public License", "MPL-2.0"),
                ("Redistribution and use in source and binary forms", "BSD"),
            ):
                if needle in text:
                    return label
            return "see LICENSE file"
    return "unknown"


def digest_of(files: dict[str, bytes]) -> str:
    """One hash for a skill's content: every path and its bytes, modes left out
    so the value is the same on a filesystem that has no executable bit."""
    total = hashlib.sha256()
    for name in sorted(files):
        total.update(name.encode("utf-8") + b"\0" + hashlib.sha256(files[name]).digest())
    return "sha256:" + total.hexdigest()


def read_disk(name: str) -> dict[str, bytes]:
    base = SKILLS_DIR / name
    return {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in sorted(base.rglob("*"))
        if path.is_file()
    }


def write_skill(name: str, files: dict[str, tuple[bytes, bool]]) -> None:
    target = SKILLS_DIR / name
    if target.exists():
        shutil.rmtree(target)
    for relative, (body, executable) in files.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        if executable and os.name != "nt":
            path.chmod(0o755)


def fetch_skill(entry: dict, sha: str, max_mb: int = DEFAULT_MAX_MB) -> tuple[dict, dict]:
    """(this skill's files, the whole upstream tree) at one commit."""
    tree = read_tree(download(entry["repo"], sha))
    files = select(tree, entry.get("path", "."), entry.get("exclude", []))
    if "SKILL.md" not in files:
        raise Problem(f"{entry['repo']}@{sha[:7]} has no SKILL.md at {entry.get('path', '.')!r}")
    size = sum(len(body) for body, _ in files.values())
    if size > max_mb * 1024 * 1024:
        raise Problem(
            f"{entry['repo']}:{entry.get('path', '.')} is {size / 1048576:.1f} MB, over the "
            f"{max_mb} MB guard - narrow it with --exclude or raise --max-mb"
        )
    return files, tree


def today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# Commands                                                                    #
# --------------------------------------------------------------------------- #

def cmd_list(args) -> int:
    sha = resolve_ref(args.repo, args.ref)
    tree = read_tree(download(args.repo, sha))
    found = skill_dirs(tree)
    if not found:
        raise Problem(f"{args.repo}@{sha[:7]} holds no SKILL.md")
    print(f"{args.repo} @ {sha[:7]} - {len(found)} skill(s)\n")
    for path in found:
        prefix = "" if path == "." else path + "/"
        fields = frontmatter(tree[prefix + "SKILL.md"][0].decode("utf-8", "replace"))
        description = fields.get("description", "")
        if len(description) > 110:
            description = description[:107] + "..."
        print(f"  {path}\n      {fields.get('name', '?')}: {description}")
    return 0


def cmd_add(args) -> int:
    if not REPO_RE.match(args.repo):
        raise Problem(f"expected owner/repo, got {args.repo!r}")
    manifest = load_manifest()
    sha = resolve_ref(args.repo, args.ref)
    tree = read_tree(download(args.repo, sha))

    path = (args.path or "").strip("/") or None
    if path is None:
        found = skill_dirs(tree)
        if len(found) != 1:
            listing = "\n".join(f"  {item}" for item in found) or "  (none)"
            raise Problem(f"{args.repo} holds {len(found)} skills - name one path:\n{listing}")
        path = found[0]

    entry = {"repo": args.repo, "path": path, "ref": args.ref}
    if SHA_RE.match(args.ref):
        entry["pin"] = True  # a commit cannot move, so `update` has nothing to follow
    if args.exclude:
        entry["exclude"] = args.exclude
    files = select(tree, path, args.exclude)
    if "SKILL.md" not in files:
        raise Problem(f"{args.repo}@{sha[:7]} has no SKILL.md at {path!r}")
    size = sum(len(body) for body, _ in files.values())
    if size > args.max_mb * 1024 * 1024:
        raise Problem(f"{size / 1048576:.1f} MB is over the {args.max_mb} MB guard - use --exclude or --max-mb")

    fields = frontmatter(files["SKILL.md"][0].decode("utf-8", "replace"))
    fallback = args.repo.split("/")[1] if path == "." else PurePosixPath(path).name
    name = (args.name or fields.get("name") or fallback).strip().lower().replace(" ", "-")
    if not NAME_RE.match(name):
        raise Problem(f"{name!r} is not a usable directory name - pass --name")

    existing = manifest["skills"].get(name)
    if existing and (existing["repo"], existing.get("path", ".")) != (args.repo, path) and not args.force:
        raise Problem(f"{name} already comes from {existing['repo']}:{existing.get('path', '.')} - pass --name or --force")
    if not existing and (SKILLS_DIR / name).exists() and not args.force:
        raise Problem(f"skills/{name} exists and is not in skills.json (one of your own?) - pass --name or --force")

    write_skill(name, files)
    entry.update(
        sha=sha,
        license=guess_license(files, tree),
        digest=digest_of({key: body for key, (body, _) in files.items()}),
        updated=today(),
    )
    manifest["skills"][name] = entry
    save_manifest(manifest)
    print(f"added {name}: {args.repo}:{path} @ {sha[:7]} - {len(files)} files, license {entry['license']}")
    if entry["license"] == "unknown":
        print("  WARNING: no license found upstream. Keep this repo private.", file=sys.stderr)
    return 0


def cmd_update(args) -> int:
    manifest = load_manifest()
    names = args.names or sorted(manifest["skills"])
    unknown = [name for name in names if name not in manifest["skills"]]
    if unknown:
        raise Problem(f"not in skills.json: {', '.join(unknown)}")

    changes, failures = [], []
    for name in names:
        entry = manifest["skills"][name]
        if entry.get("pin") and not args.names:
            print(f"  {name}: pinned, skipped")
            continue
        try:
            sha = resolve_ref(entry["repo"], entry.get("ref", "HEAD"))
            if sha == entry.get("sha"):
                print(f"  {name}: current ({sha[:7]})")
                continue
            files, tree = fetch_skill(entry, sha, args.max_mb)
            digest = digest_of({key: body for key, (body, _) in files.items()})
            old = entry.get("sha", "")
            if digest == entry.get("digest"):
                # Upstream moved but not inside this skill: advance the pin quietly.
                entry.update(sha=sha)
                print(f"  {name}: upstream moved to {sha[:7]}, skill unchanged")
                changes.append((name, entry["repo"], old, sha, False))
                continue
            write_skill(name, files)
            entry.update(sha=sha, digest=digest, license=guess_license(files, tree), updated=today())
            changes.append((name, entry["repo"], old, sha, True))
            print(f"  {name}: {old[:7]} -> {sha[:7]}")
        except Problem as problem:
            failures.append((name, str(problem)))
            print(f"  {name}: FAILED - {problem}", file=sys.stderr)

    if changes:
        save_manifest(manifest)
    content_changed = [change for change in changes if change[4]]

    lines = ["Upstream updates picked up by `scripts/skills.py update`.", ""]
    for name, repo, old, new, touched in changes:
        note = "" if touched else " (pin moved, skill content unchanged)"
        lines.append(f"- **{name}**: [`{old[:7]}...{new[:7]}`](https://github.com/{repo}/compare/{old}...{new}){note}")
    for name, reason in failures:
        lines.append(f"- **{name}**: update failed - {reason}")
    if args.summary:
        Path(args.summary).write_text("\n".join(lines) + "\n", encoding="utf-8")

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"changed={'true' if changes else 'false'}\n")
    print(f"{len(content_changed)} skill(s) changed, {len(changes) - len(content_changed)} pin(s) moved, {len(failures)} failed")
    return 1 if failures and not changes else 0


def cmd_verify(args) -> int:
    manifest = load_manifest()
    errors, warnings = [], []
    for name, entry in sorted(manifest["skills"].items()):
        base = SKILLS_DIR / name
        if not (base / "SKILL.md").is_file():
            errors.append(f"{name}: skills/{name}/SKILL.md is missing")
            continue
        fields = frontmatter((base / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
        for key in ("name", "description"):
            if key not in fields:
                errors.append(f"{name}: SKILL.md frontmatter has no {key}")
        local = digest_of(read_disk(name))
        if local != entry.get("digest"):
            errors.append(f"{name}: skills/{name} does not match its recorded digest (edited by hand?)")
            continue
        if args.fetch:
            try:
                files, _ = fetch_skill(entry, entry["sha"], args.max_mb)
            except Problem as problem:
                errors.append(f"{name}: {problem}")
                continue
            if digest_of({key: body for key, (body, _) in files.items()}) != local:
                errors.append(f"{name}: vendored copy differs from {entry['repo']}@{entry['sha'][:7]}")
    if SKILLS_DIR.is_dir():
        for path in sorted(SKILLS_DIR.iterdir()):
            if path.is_dir() and path.name not in manifest["skills"]:
                if (path / "SKILL.md").is_file():
                    warnings.append(f"{path.name}: not in skills.json, treated as one of your own")
                else:
                    errors.append(f"{path.name}: a directory under skills/ with no SKILL.md")
    for line in warnings:
        print(f"note: {line}")
    for line in errors:
        print(f"ERROR: {line}", file=sys.stderr)
    checked = "against upstream" if args.fetch else "against recorded digests"
    print(f"{len(manifest['skills'])} vendored skill(s) checked {checked}: {'FAILED' if errors else 'ok'}")
    return 1 if errors else 0


def cmd_remove(args) -> int:
    manifest = load_manifest()
    if args.name not in manifest["skills"]:
        raise Problem(f"{args.name} is not in skills.json")
    del manifest["skills"][args.name]
    shutil.rmtree(SKILLS_DIR / args.name, ignore_errors=True)
    save_manifest(manifest)
    print(f"removed {args.name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="show the skills an upstream repo holds")
    listing.add_argument("repo")
    listing.add_argument("--ref", default="HEAD")
    listing.set_defaults(run=cmd_list)

    add = commands.add_parser("add", help="vendor one skill and pin it")
    add.add_argument("repo", help="owner/repo on GitHub")
    add.add_argument("path", nargs="?", help="directory holding SKILL.md; found for you when the repo has one skill")
    add.add_argument("--ref", default="HEAD", help="branch or tag to follow (default: the default branch)")
    add.add_argument("--name", help="directory name under skills/ (default: the skill's own name)")
    add.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                     help="path or glob inside the skill to leave out; repeatable")
    add.add_argument("--max-mb", type=int, default=DEFAULT_MAX_MB)
    add.add_argument("--force", action="store_true")
    add.set_defaults(run=cmd_add)

    update = commands.add_parser("update", help="move skills to their ref's newest commit")
    update.add_argument("names", nargs="*")
    update.add_argument("--summary", metavar="FILE", help="write a markdown summary, for a pull request body")
    update.add_argument("--max-mb", type=int, default=DEFAULT_MAX_MB)
    update.set_defaults(run=cmd_update)

    verify = commands.add_parser("verify", help="check skills/ against skills.json")
    verify.add_argument("--fetch", action="store_true", help="also re-download each pin and compare with upstream")
    verify.add_argument("--max-mb", type=int, default=DEFAULT_MAX_MB)
    verify.set_defaults(run=cmd_verify)

    remove = commands.add_parser("remove", help="drop a vendored skill")
    remove.add_argument("name")
    remove.set_defaults(run=cmd_remove)

    args = parser.parse_args()
    try:
        return args.run(args)
    except Problem as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
