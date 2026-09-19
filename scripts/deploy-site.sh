#!/usr/bin/env bash
# Publishes site/ and invalidates the edge copies.
#
# Deliberately uploads named files rather than syncing. An `aws s3 sync --delete`
# here once wiped 37GB of media, because the site/ directory is a handful of
# files and the destination is the bucket root that everything else lives in.
# There is no pruning step and there must not be one.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUCKET="${GALLERY_BUCKET:?set it in config.env}"
DIST="${DIST:-$(cd "$ROOT/terraform" && terraform output -raw distribution_id)}"

FILES=(index.html app.js styles.css)

for file in "${FILES[@]}"; do
  aws s3 cp "$ROOT/site/$file" "s3://$BUCKET/site/$file" \
    --cache-control 'public, max-age=300' \
    --only-show-errors
  echo "  uploaded $file"
done

aws cloudfront create-invalidation \
  --distribution-id "$DIST" \
  --paths "/" "/index.html" "/app.js" "/styles.css" \
  --query 'Invalidation.Status' --output text
