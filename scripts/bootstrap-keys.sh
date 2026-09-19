#!/usr/bin/env bash
# Generates the secrets the gallery needs. Both are written straight to SSM and
# never touch Terraform state or the repo. Each is created only if absent, so
# this is safe to re-run.
set -euo pipefail

PARAM="${PARAM:-/gallery/cloudfront/private-key}"
HMAC_PARAM="${HMAC_PARAM:-/gallery/session/hmac-key}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUBLIC_OUT="$ROOT/terraform/keys/cloudfront_public_key.pem"

if aws ssm get-parameter --name "$PARAM" >/dev/null 2>&1; then
  echo "cookie key  -> exists, left alone (rotating it signs every viewer out)"
else
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  openssl genrsa -out "$TMP/private.pem" 2048 2>/dev/null
  openssl rsa -pubout -in "$TMP/private.pem" -out "$TMP/public.pem" 2>/dev/null
  aws ssm put-parameter \
    --name "$PARAM" \
    --type SecureString \
    --description "CloudFront signed-cookie private key for the photo gallery" \
    --value "file://$TMP/private.pem" >/dev/null
  mkdir -p "$(dirname "$PUBLIC_OUT")"
  cp "$TMP/public.pem" "$PUBLIC_OUT"
  echo "cookie key  -> SSM $PARAM, public half at $PUBLIC_OUT"
fi

if aws ssm get-parameter --name "$HMAC_PARAM" >/dev/null 2>&1; then
  echo "identity key-> exists, left alone"
else
  # Signs the identity cookie, so archive attribution cannot be forged.
  aws ssm put-parameter \
    --name "$HMAC_PARAM" \
    --type SecureString \
    --description "HMAC key for the gallery identity cookie" \
    --value "$(openssl rand -hex 32)" >/dev/null
  echo "identity key-> SSM $HMAC_PARAM"
fi
