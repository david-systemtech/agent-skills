# agent-skills

Skills from other people's GitHub repos, copied here at a pinned commit, and
installed the same way on every machine.

`skills.json` records where each skill came from and the exact commit and
content digest that is vendored under `skills/`. Machines clone this repo and
link `skills/` in as `~/.agents/skills` - the one directory that both Claude
(through Artemis, as `artemis-skills:<name>`) and Codex read.

Private on purpose: some upstream skills carry no license, or one that does
not allow redistribution.

## Put it on a machine

Linux and macOS:

    git clone https://github.com/david-systemtech/agent-skills.git ~/.agents/skills-repo
    ~/.agents/skills-repo/install.sh --schedule

Windows (PowerShell):

    git clone https://github.com/david-systemtech/agent-skills.git $HOME\.agents\skills-repo
    & $HOME\.agents\skills-repo\install.ps1 -Schedule

The installer links `~/.agents/skills` to the clone (a symlink, or a directory
junction on Windows) and `--schedule` / `-Schedule` pulls hourly. If
`~/.agents/skills` already holds something it stops and lists it; `--backup` /
`-Backup` moves that aside instead of deleting it. Running it again is safe.

A container with no cron of its own (the Artemis server) is pulled from its
host instead:

    docker exec <container> git -C /data/agent/.agents/skills-repo pull --ff-only --quiet

Stock Claude Code reads `~/.claude/skills` rather than `~/.agents/skills`; link
a skill across with `ln -s ~/.agents/skills/<name> ~/.claude/skills/<name>`.

## Add a skill

    python3 scripts/skills.py list mattpocock/skills          # what does the repo hold?
    python3 scripts/skills.py add mattpocock/skills skills/engineering/prototype
    python3 scripts/skills.py add theclaymethod/unslop        # one-skill repo: no path needed
    git add -A && git commit -m "Add prototype" && git push

`add` follows the upstream's default branch. `--ref v1.2.0` follows a tag or
branch instead, a full commit SHA pins the skill for good, `--exclude evals`
leaves part of it out, and `--name` renames it. `remove <name>` drops one.

Your own skills live here too: any directory under `skills/` that
`skills.json` does not list is left alone by every command.

## How updates arrive

Every Monday `.github/workflows/update.yml` runs `skills.py update`, opens a
pull request whose body links the upstream diff for each skill that moved, and
merges it once `skills.py verify --fetch` has shown every vendored skill to be
byte-identical to its upstream at the pinned commit. If that check fails the
pull request stays open and the run fails. Machines pick the merge up on their
next hourly pull.

Nobody reads an update before it merges, so:

- hold a skill at its current commit with `"pin": true` in `skills.json`;
- undo a bad update with `git revert <merge commit>` and push - every machine
  follows within the hour.

`SKILLS_UPSTREAM_TOKEN` lets `skills.py` reach a private upstream.
