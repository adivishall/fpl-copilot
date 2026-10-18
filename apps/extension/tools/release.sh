#!/usr/bin/env bash
# Build, test and publish the extension as a GitHub Release (the zero-cost distribution):
#   ./tools/release.sh --url https://<site> --name "<product name>" [--repo owner/name] [--dry-run]
# Produces dist/<slug>-extension-<version>.zip + .sha256, runs both test suites, checks the ZIP
# holds only the extension's files, then creates release tag extension-v<version> with the ZIP,
# its checksum and installation notes. --dry-run stops before creating the release.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
url="" name="" repo="" dry=""
while [ $# -gt 0 ]; do
  case "$1" in
    --url) url=$2; shift 2 ;;
    --name) name=$2; shift 2 ;;
    --repo) repo=$2; shift 2 ;;
    --dry-run) dry=1; shift ;;
    *) echo "usage: release.sh --url <origin> --name <product> [--repo owner/name] [--dry-run]" >&2; exit 2 ;;
  esac
done
[ -n "$url" ] && [ -n "$name" ] || { echo "release.sh: --url and --name are required" >&2; exit 2; }
cd "$here"
rm -rf dist
./build.sh --url "$url" --name "$name" >/dev/null
zip=$(ls dist/*.zip)
sum=$(cut -d' ' -f1 "$zip.sha256")
version=$(python3 -c 'import json;print(json.load(open("src/manifest.json"))["version"])')
echo "built $zip ($(wc -c < "$zip" | tr -d ' ') bytes) sha256 $sum"

echo "== tests"
npm test 2>&1 | grep -E "^# (pass|fail)"
npx playwright test 2>&1 | grep -E "passed|failed"

echo "== contents (must be exactly the extension's files)"
expected="config.js icons/icon-128.png icons/icon-16.png icons/icon-32.png icons/icon-48.png lib.js manifest.json popup.css popup.html popup.js"
actual=$(unzip -Z1 "$zip" | sort | tr '\n' ' ' | sed 's/ $//')
[ "$actual" = "$expected" ] || { echo "release.sh: unexpected ZIP contents: $actual" >&2; exit 1; }
# nothing that looks like a credential, and the configured address is the one asked for
unzip -p "$zip" config.js | grep -q "APP_URL = \"${url%/}\"" || { echo "release.sh: APP_URL mismatch" >&2; exit 1; }
if unzip -p "$zip" | grep -q -i -E "api[_-]?key|secret|token|password|BEGIN (RSA|OPENSSH)"; then
  echo "release.sh: the package mentions a credential-like word; inspect before publishing" >&2; exit 1
fi
./tools/store_shots.sh --url "$url" --name "$name" >/dev/null 2>&1 || true

tag="extension-v$version"
notes="dist/RELEASE_NOTES.md"
cat > "$notes" <<NOTES
$name browser extension $version (Chrome, Manifest V3).

One button: on a fantasy.premierleague.com team page (an address containing /entry/<ID>/) it opens the squad import at $url with that team's ID filled in; elsewhere it opens $url. Only permission: activeTab (the clicked tab's address, per click). It reads nothing else, holds no credential and collects nothing.

Install (Chrome Developer mode):
1. Download \`$(basename "$zip")\` and check it: \`shasum -a 256 -c $(basename "$zip").sha256\` (SHA-256 \`$sum\`).
2. Unzip it into a folder you keep.
3. Open chrome://extensions, switch on Developer mode, click "Load unpacked", choose that folder.
4. Pin the extension; open a team's Points page on fantasy.premierleague.com and click the button.

Not listed in the Chrome Web Store (store publication needs a paid developer registration). Privacy notice: docs/EXTENSION_PRIVACY.md. Source: apps/extension at this tag.
NOTES
if [ -n "$dry" ]; then echo "== dry run: would create release $tag with $zip and $zip.sha256"; cat "$notes"; exit 0; fi
echo "== creating GitHub release $tag"
gh release create "$tag" "$zip" "$zip.sha256" ${repo:+--repo "$repo"} --title "$name browser extension $version" --notes-file "$notes" --target "$(git rev-parse HEAD)"
gh release view "$tag" ${repo:+--repo "$repo"} --json url,assets --jq '.url, (.assets[] | "\(.name) \(.size) bytes")'
