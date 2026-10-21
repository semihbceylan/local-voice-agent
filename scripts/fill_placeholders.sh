#!/usr/bin/env bash
#
# Fill in the personal details of the repository.
#
#   ./scripts/fill_placeholders.sh "Your Name" "you@example.com" "github-user" "repo-name"
#
# It always starts from the committed template, so it can be run again with
# different values without stacking substitutions.
#
# It touches exactly two files: LICENSE and README.md.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NAME="${1:-}"
EMAIL="${2:-}"
GITHUB_USER="${3:-}"
REPO_NAME="${4:-}"

if [ -z "$NAME" ] || [ -z "$EMAIL" ] || [ -z "$GITHUB_USER" ] || [ -z "$REPO_NAME" ]; then
  echo "usage: $0 \"<full name>\" \"<email>\" \"<github user>\" \"<repo name>\"" >&2
  exit 1
fi

echo "==> Restoring the template"
git checkout -- LICENSE README.md

echo "==> Filling in the details"
NAME="$NAME" EMAIL="$EMAIL" GITHUB_USER="$GITHUB_USER" REPO_NAME="$REPO_NAME" \
python3 - <<'PY'
import os
from pathlib import Path

name = os.environ["NAME"]
email = os.environ["EMAIL"]
user = os.environ["GITHUB_USER"]
repo = os.environ["REPO_NAME"]

licence = Path("LICENSE")
text = licence.read_text()
text = text.replace("<COPYRIGHT HOLDER>", name)
licence.write_text(text)

readme = Path("README.md")
text = readme.read_text()
text = text.replace(
    "git clone <this repository>",
    f"git clone git@github.com:{user}/{repo}.git",
)
readme.write_text(text)
print(f"    LICENSE  copyright holder: {name}")
print(f"    README   clone url:          git@github.com:{user}/{repo}.git")
PY

echo "==> Setting the git identity for this repository"
git config --local user.name "$NAME"
git config --local user.email "$EMAIL"

echo
echo "Done. The repository now says:"
git diff --stat
echo
echo "Remaining steps:"
echo "  git add -A && git commit -m \"Add licence details\""
echo "  git remote add origin git@github.com:${GITHUB_USER}/${REPO_NAME}.git"
echo "  git push -u origin main"
