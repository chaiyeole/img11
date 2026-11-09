#!/usr/bin/env bash
# Publish photos: register new albums/photos, encode them, upload the image
# files to Cloudflare Pages, and leave the site changes ready to commit.
#
#   scripts/publish.sh            # full run
#   scripts/publish.sh --no-ai    # don't call Claude for tags (passed to sync_content.py)
#
# Photos are served by Cloudflare (a static-files-only Worker, configured in
# wrangler.jsonc) and the website itself by GitHub Pages. After this finishes, review on localhost, then
# commit the CSVs + web/manifest.json and push.
#
# One-time setup: a free Cloudflare account, then
#   npx -y -p node@22 -p wrangler@4.158.0 wrangler login
set -euo pipefail

IMAGE_BASE="https://img11-photos.chaitanyayeole7.workers.dev/"
# Wrangler needs Node 22+; this runs it with a private Node 22 and leaves the
# system Node alone. The version is pinned so uploads behave the same every time.
WRANGLER=(npx -y -p node@22 -p wrangler@4.158.0 wrangler)

cd "$(dirname "$0")/.."

echo "== 1/4  Register new albums and photos (albums.csv, tags.csv)"
python3 scripts/sync_content.py "$@"

echo
echo "== 2/4  Encode photos (only new ones; the rest come from .build_cache)"
python3 scripts/build_assets.py --image-base "$IMAGE_BASE"

echo
echo "== 3/4  Upload photo files to Cloudflare ($IMAGE_BASE)"
# Only the image folders go to Cloudflare. Hard links: instant, no extra disk.
rm -rf dist-images
mkdir dist-images
cp -al dist/thumbs dist/full dist/full-* dist-images/
# Headers for every photo file:
#  - CORS so the viewer's histogram may read pixels from this other site
#  - cache for a year: URLs carry ?v=<photo hash>, so a changed photo gets a new URL
cat > dist-images/_headers <<'EOF'
/*
  Access-Control-Allow-Origin: *
  Cache-Control: public, max-age=31536000, immutable
EOF
# Uploads only files Cloudflare doesn't already have. (npm warns that the
# *system* Node is 20; wrangler itself runs on Node 22, so those lines are hidden.)
"${WRANGLER[@]}" deploy 2>&1 | grep -v -e 'npm WARN'

echo
echo "== 4/4  Ready to commit"
git status --short -- content web scripts .github
cat <<EOF

Next:
  1. Preview:  python3 -m http.server 8765 -d dist   ->  http://localhost:8765/
     (the preview loads photos from ${IMAGE_BASE}, like the live site)
  2. Commit content/*.csv and web/manifest.json, then push. GitHub publishes in ~30s.
EOF
