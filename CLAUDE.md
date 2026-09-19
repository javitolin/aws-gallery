# Working on this project

A private photo gallery on S3 + CloudFront, built to replace paid cloud photo
storage. Drop files in a folder, run one command, and they appear — thumbnailed,
dated, categorised, behind a real login. Runs at roughly **$1/month**.

## Orientation

```
  LOCAL                                          AWS
  transcode.py  source/ -> web/                  CloudFront
  publish.py    web/    -> S3                      /auth/*  -> gallery-auth -> Cognito
  dev_server.py preview, no AWS                    /api/*   -> gallery-api   (signed)
                                                   /media/* -> S3            (signed)
                                                   *        -> S3 site/ only (gate fn)
                                                 S3 event -> gallery-media-processor
```

Nothing runs on a schedule. Everything local is invoked by you; everything on
AWS is triggered by a request or an S3 event.

## Setup

    cp config.env.example config.env                    # bucket, domain, paths
    cp terraform/backend.hcl.example terraform/backend.hcl
    make venv && make layers
    ./scripts/bootstrap-keys.sh                         # writes 2 secrets to SSM
    cd terraform && terraform init -backend-config=backend.hcl

`config.env` and `backend.hcl` are gitignored and hold everything
installation-specific. No identifiers are hardcoded.

## Daily use

    make dev          # preview at localhost:8000 against local files
    make publish-check  # what would transcode and upload
    make publish      # transcode what needs it, then upload
    make site         # publish frontend changes
    make test         # 22 tests: python + node

Add media by dropping it in `$GALLERY_SOURCE/<Category>/`. Nested folders become
nested categories. `make publish` is resumable and safe to re-run.

## Testing locally

`make dev` serves `site/` from disk and builds a manifest straight from your
files, generating thumbnails with ffmpeg into `.devcache/`. It implements the
same `/api/*` endpoints against JSON files, so archiving, moving between
categories and renaming can all be exercised without touching AWS.

It does **not** replicate Cognito or signed-cookie enforcement — `/auth/*`
returns a stub. Auth changes need a real deploy to test.

Thumbnails are JPEG locally because Homebrew's ffmpeg often lacks libwebp; the
Lambda uses Pillow and writes WebP. Cosmetic difference only.

## Invariants — do not break these

**Nothing deletes media.** `gallery-api` has no `s3:DeleteObject` at all.
`gallery-media-processor` has it scoped to `thumbs/*` and `meta/*` only, so it
cannot reach an original even with a bug in its key helpers. `deploy-site.sh`
copies named files and must never use `aws s3 sync --delete` — that once wiped
37GB from the bucket root. Bucket versioning is on with a 30-day window.

**Keys are content hashes**, `media/<sha256[:16]><ext>`. Over a thousand files
in a real library share a filename with a *different* file, so a flat layout
keyed on names silently overwrites. Hashing also deduplicates identical copies.

**Category lives in the sidecar and a tag, never in the key.** That is what
makes recategorising a 5GB video two small writes instead of a copy.

**The default cache behaviour serves only `site/`**, via an origin with
`origin_path = "/site"`. It is the one behaviour without a signature
requirement, so it must be structurally unable to reach anything else. Listing
sensitive prefixes instead is a denylist and will leak whatever is added next.

## Sidecars vs tags

Each media object has `meta/<key>.json` holding name, source path, category,
EXIF date, dimensions, duration, thumbnail, archive state and who changed what.
Three writers merge into it rather than overwrite: `publish.py` (name, path,
category — written *before* the object so the S3 trigger merges onto it), the
processor (technical fields), and `gallery-api` (archive/category plus
attribution). A gallery edit beats the folder layout on the next publish.

Tags are **not** an alternative: S3 caps them at 10 per object, they hold no
structured values, and `list_objects_v2` does not return them, so reading them
in bulk costs the same N calls as sidecars. Tags are used only where S3 itself
must act on the value — a lifecycle rule can filter on `archived=true`, and
cannot read a JSON file.

## Gotchas learned the hard way

- **CloudFront validates trusted key groups BEFORE viewer-request functions.**
  Gating a behaviour and expecting the function to redirect first does not work;
  you get raw 403 `MissingKey` XML. Verified empirically, twice.
- **Lambda function URL OAC needs both** `lambda:InvokeFunctionUrl` *and*
  `lambda:InvokeFunction`, in two resources — `function_url_auth_type` is
  rejected on the plain invoke action.
- **`trusted_key_groups` must be set to `[]` explicitly** to remove it. Omitting
  the attribute leaves the previous value in place.
- **`put_object_tagging` replaces the whole tag set.** Always read-modify-write,
  or archiving wipes the category tag.
- **Category matching needs the trailing slash.** `Ofer` must not match
  `Ofer Trip`. Covered by `tests/nesting.test.mjs`.
- **`ast.parse` does not catch undefined names.** Import Lambda modules for real
  before deploying; a missing `import re` passed a syntax check and failed live.
- **Every Lambda has a `handler.py`**, so tests load them by path under unique
  names via `tests/loader.py`, or `sys.modules` caching makes them collide.
- S3 user metadata is US-ASCII only and rejects Hebrew; tags accept Unicode
  letters but reject parentheses, apostrophes and ampersands.

## Publishing changes

Frontend only: `make site` — uploads three files and invalidates the edge.

Infrastructure: `cd terraform && terraform plan` then apply. Check the plan says
**0 to destroy** before applying; the bucket carries `prevent_destroy`.

Lambda code: `terraform apply` repackages and updates in place.

Adding a family member:

    aws cognito-idp admin-create-user \
      --user-pool-id "$(cd terraform && terraform output -raw user_pool_id)" \
      --username them@example.com \
      --user-attributes Name=email,Value=them@example.com Name=email_verified,Value=true

Cognito's built-in email has poor deliverability; `admin-set-user-password`
without `--permanent` is more reliable, and forces a change at first sign-in.

## Known limitations

- Signed cookies grant the whole site, so every signed-in user sees everything.
  Fine for one owner; needs per-user scoping before sharing widely.
- `manifest.json` is rebuilt whole on every change, one GetObject per sidecar.
  Fine into the low thousands; beyond that it wants incremental updates.
- Archiving is one-way from the UI. Un-archiving means dropping the tag and
  copying the object back to Standard, and Glacier IR bills a 90-day minimum.
- Lifecycle transitions run daily, so an archived item stays at Standard
  pricing for up to ~48h.
