#!/usr/bin/env bash
# Publish docs/wiki/ to the GitHub Wiki tab. docs/wiki/ is the source of truth: edit there,
# review in a PR. The `wiki` workflow runs this on every merge to main; run it by hand to
# publish sooner. Needs the wiki enabled and its first page created once in the web UI
# (GitHub only creates the wiki's git repo then).
#
# WIKI_REMOTE overrides where it pushes (CI uses HTTPS with the workflow's token).
set -euo pipefail
repo="$(git remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')"
src="$(git rev-parse --show-toplevel)/docs/wiki"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

git clone -q "${WIKI_REMOTE:-git@github.com:${repo}.wiki.git}" "$tmp/wiki"
find "$tmp/wiki" -maxdepth 1 -name '*.md' -delete
for f in "$src"/*.md; do
  # Repo links point at Page.md (works when browsing the repo); wiki links drop the .md.
  # Links out of docs/wiki/ (../decisions/…) become absolute links into the repo.
  sed -E -e 's/\]\(([A-Za-z0-9_-]+)\.md(#[^)]*)?\)/](\1\2)/g' \
    -e "s#\]\(\.\./#](https://github.com/${repo}/blob/main/docs/#g" \
    "$f" > "$tmp/wiki/$(basename "$f")"
done
cd "$tmp/wiki"
# Commit as whoever runs this (the main repo's config, not a global default); in CI, as
# the author of the commit being published.
git config user.name "$(git -C "$src" config user.name || git -C "$src" log -1 --format=%an)"
git config user.email "$(git -C "$src" config user.email || git -C "$src" log -1 --format=%ae)"
git add -A
if git diff --cached --quiet; then echo "wiki already up to date"; exit 0; fi
git commit -q -m "Sync from docs/wiki @ $(git -C "$src" rev-parse --short HEAD)"
git push -q
echo "published https://github.com/${repo}/wiki"
