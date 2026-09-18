#!/bin/sh
# Put this repo's skills on a Linux or macOS machine.
#
#   install.sh              clone (or fast-forward) and link ~/.agents/skills
#   install.sh --backup     same, moving an existing ~/.agents/skills aside first
#   install.sh --schedule   also pull hourly (cron on Linux, launchd on macOS)
#
# ~/.agents/skills is the one directory both Claude (through Artemis) and
# Codex read, so linking it is the whole install. Safe to run again.
#
# AGENT_SKILLS_REPO overrides the clone URL, AGENT_SKILLS_HOME the ~/.agents root.
set -eu

REPO_URL="${AGENT_SKILLS_REPO:-https://github.com/david-systemtech/agent-skills.git}"
ROOT="${AGENT_SKILLS_HOME:-$HOME/.agents}"
CLONE="$ROOT/skills-repo"
LINK="$ROOT/skills"
LABEL="dev.systemtech.agent-skills"

backup=no
schedule=no
for arg in "$@"; do
  case "$arg" in
    --backup) backup=yes ;;
    --schedule) schedule=yes ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

command -v git >/dev/null || { echo "git is not installed" >&2; exit 1; }
mkdir -p "$ROOT"

if [ -d "$CLONE/.git" ]; then
  git -C "$CLONE" pull --ff-only --quiet
  echo "updated $CLONE"
else
  git clone --quiet "$REPO_URL" "$CLONE"
  echo "cloned into $CLONE"
fi

if [ -L "$LINK" ]; then
  current="$(readlink "$LINK")"
  if [ "$current" != "$CLONE/skills" ]; then
    echo "$LINK already links to $current - remove that link and run again" >&2
    exit 1
  fi
elif [ -e "$LINK" ]; then
  if [ -z "$(ls -A "$LINK")" ]; then
    rmdir "$LINK"
  elif [ "$backup" = yes ]; then
    aside="$ROOT/skills.backup-$(date +%Y%m%d-%H%M%S)"
    mv "$LINK" "$aside"
    echo "moved the existing $LINK to $aside"
  else
    echo "$LINK already holds:" >&2
    ls -A "$LINK" | sed 's/^/  /' >&2
    echo "run again with --backup to move it aside (nothing is deleted)" >&2
    exit 1
  fi
fi
[ -L "$LINK" ] || ln -s "$CLONE/skills" "$LINK"
echo "$LINK -> $CLONE/skills"

if [ "$schedule" = yes ]; then
  if [ "$(uname)" = Darwin ]; then
    plist="$HOME/Library/LaunchAgents/$LABEL.plist"
    mkdir -p "$(dirname "$plist")"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$(command -v git)</string><string>-C</string><string>$CLONE</string>
    <string>pull</string><string>--ff-only</string><string>--quiet</string>
  </array>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    echo "launchd pulls hourly ($plist)"
  elif command -v crontab >/dev/null; then
    line="17 * * * * git -C \"$CLONE\" pull --ff-only --quiet # $LABEL"
    { crontab -l 2>/dev/null | grep -v "# $LABEL" || true; echo "$line"; } | crontab -
    echo "cron pulls hourly"
  else
    echo "no crontab here - schedule this yourself:" >&2
    echo "  git -C \"$CLONE\" pull --ff-only --quiet" >&2
  fi
fi

count=$(find "$LINK/" -mindepth 2 -maxdepth 2 -name SKILL.md | wc -l | tr -d ' ')
echo "$count skill(s) installed"
