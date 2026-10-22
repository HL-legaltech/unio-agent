#!/bin/sh
# The release rules in AGENTS.md (Versions), as CI enforces them:
# - plugin.json pins MAJOR.MINOR.PATCH, and marketplace.json sets no version;
# - the skill's metadata.version and its body line are plugin.json's
#   MAJOR.MINOR;
# - the CHANGELOG's first section is plugin.json's version;
# - with a base ref, a change under plugins/unio raises the version above the
#   base's.
#
# Usage: sh tests/check_versions.sh [<base ref>]   e.g. origin/main
set -u

cd "$(dirname -- "$0")/.." || exit 1
manifest=plugins/unio/.claude-plugin/plugin.json
skill=plugins/unio/skills/unio/SKILL.md
changelog=plugins/unio/CHANGELOG.md

failed=0
problem() { echo "$1"; failed=1; }
version_of() {
  sed -n 's/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1
}
# True when version $1 > version $2.
newer() {
  awk -v a="$1" -v b="$2" 'BEGIN {
    n = split(a, x, "."); m = split(b, y, ".")
    for (i = 1; i <= (n > m ? n : m); i++) {
      if ((x[i] + 0) > (y[i] + 0)) exit 0
      if ((x[i] + 0) < (y[i] + 0)) exit 1
    }
    exit 1
  }'
}

version=$(version_of < "$manifest")
if ! printf '%s\n' "$version" | grep -qx '[0-9]\{1,3\}\.[0-9]\{1,3\}\.[0-9]\{1,3\}'; then
  problem "$manifest: version \"$version\" is not MAJOR.MINOR.PATCH with 1 to 3 digits each"
fi
if grep -q '"version"' .claude-plugin/marketplace.json; then
  problem ".claude-plugin/marketplace.json sets a version; only $manifest does"
fi

major_minor=${version%.*}
metadata=$(awk '
  NR == 1 && /^---$/ { front = 1; next }
  front && /^---$/ { exit }
  front && /^metadata:/ { inside = 1; next }
  front && /^[^[:space:]]/ { inside = 0 }
  inside && /^[[:space:]]+version:/ {
    sub(/^[[:space:]]+version:[[:space:]]*/, ""); gsub(/["'\''[:space:]]/, ""); print; exit
  }
' "$skill")
body=$(sed -n 's/^This is version \([0-9.]*\) of the Unio skill\.$/\1/p' "$skill")
if [ "$metadata" != "$major_minor" ]; then
  problem "$skill: metadata.version is \"$metadata\"; it must be $major_minor, plugin.json's MAJOR.MINOR"
fi
if [ "$body" != "$major_minor" ]; then
  problem "$skill: the line \"This is version $major_minor of the Unio skill.\" is missing or says \"$body\""
fi

first=$(sed -n 's/^## //p' "$changelog" | head -n 1)
if [ "$first" != "$version" ]; then
  problem "$changelog: the first section is \"$first\"; add \"## $version\" above it"
fi

if [ -n "${1:-}" ] && ! git diff --quiet "$1"...HEAD -- plugins/unio; then
  # A base without the plugin is the first release.
  old=$(git show "$1:$manifest" 2>/dev/null | version_of)
  if [ -n "$old" ] && ! newer "$version" "$old"; then
    problem "plugins/unio changed since $1, so $manifest's version must rise above $old (it is $version)"
  fi
fi

if [ "$failed" -eq 0 ]; then
  echo "Versions agree: plugin $version, skill $major_minor, CHANGELOG $first."
fi
exit "$failed"
