# Photo gallery

Self-hosted private photo gallery on AWS. Drop files in a folder, get a categorised
gallery behind a passkey login, for about a dollar a month.

Media lives in S3 and is served by CloudFront, which validates a signed cookie itself
so browsing costs no compute. Uploads are content-addressed, so identical copies store
once. Anything a browser can't play is transcoded locally first. Nothing in the running
system can delete your originals.

MIT licensed.

## How it works

```
photos.example.com
   │
   └── CloudFront
        ├── /auth/*    → auth Lambda (function URL, OAC + AWS_IAM)
        ├── /media/*   ─┐
        ├── /thumbs/*   ├→ S3, signed cookies required
        ├── /archive/* ─┘
        └── *          → S3, signed cookies required
                         + CloudFront Function redirecting to login
```

**Auth.** A Cognito user pool (Essentials tier) handles sign-in with passwords or passkeys. The auth
Lambda completes the PKCE code exchange and issues **CloudFront signed cookies**, which CloudFront
then validates itself — so normal browsing costs no compute. The CloudFront Function only decides
whether to bounce a visitor to the login page; it does not enforce anything, and a forged hint
cookie just earns a 403 from the signature check one hop later.

The signing key is generated out of band. The private half lives in SSM as a SecureString and is
never in Terraform state or the repo; `terraform/keys/cloudfront_public_key.pem` is its public
counterpart and is committed on purpose so the pair stays reproducible.

**Media.** Uploads to `media/` trigger `gallery-media-processor`, which writes a WebP thumbnail to
`thumbs/` and a metadata sidecar to `meta/`, then rebuilds `manifest.json`. Photos go through
Pillow, video poster frames and audio cover art through a static ffmpeg binary. Deletes are handled
too, so removing a file removes it from the gallery. Anything it cannot render still gets a sidecar
marked `renderable: false` with a reason, so files are never silently dropped.

**Archive.** `archive/` holds cold backups at Glacier Instant Retrieval. They are listed in the UI
rather than hidden, and playable formats stream straight from Glacier — no restore job.

## Layout

    terraform/         infrastructure, one module per concern
    lambdas/auth/      login, callback, logout
    lambdas/media_processor/
    layers/build.sh    ffmpeg + Pillow + cryptography, built without Docker
    site/              vanilla JS frontend, no build step
    scripts/           key bootstrap, migration, backfill, deploy

## First-time setup

DNS for `example.com` lives at the registrar, not Route53, so two records are added by hand.

    cp config.env.example config.env        # fill in bucket, domain, paths
    cp terraform/backend.hcl.example terraform/backend.hcl
    make venv
    make layers
    ./scripts/bootstrap-keys.sh

    cd terraform
    cp terraform.tfvars.example terraform.tfvars
    terraform init -backend-config=backend.hcl
    terraform apply -target=module.cdn.aws_acm_certificate.gallery

Add the printed CNAME at the registrar and wait for `make cert-status` to report `ISSUED`, then:

    terraform apply

Add a second CNAME pointing `photos` at the `distribution_domain` output. Then publish, create
yourself a user, and populate:

    make site
    aws cognito-idp admin-create-user \
      --user-pool-id "$(cd terraform && terraform output -raw user_pool_id)" \
      --username you@example.com \
      --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true
    make backfill

## Day to day

Upload to `media/` — that is the whole workflow. The gallery updates itself.

    make plan          # what would change
    make site          # after editing anything in site/
    make outputs

Adding a family member:

    aws cognito-idp admin-create-user \
      --user-pool-id "$(cd terraform && terraform output -raw user_pool_id)" \
      --username them@example.com \
      --user-attributes Name=email,Value=them@example.com Name=email_verified,Value=true

They set a password on first sign-in and can register a passkey from there.

## Notes

- Rotating the signing key invalidates every live session. `bootstrap-keys.sh` refuses to overwrite
  an existing key for that reason.
- `manifest.json` is a single file. Fine into the low thousands of items; shard it by year past that.
- Bulk uploads rebuild the manifest once per object. Harmless at this size, and concurrency is
  capped at 2 to keep the writes from racing.
- `deploy-site.sh` copies named files and must never sync with `--delete`: the site lives at the
  bucket root alongside the media, and a prune there once destroyed 37GB of media.
- CloudFront checks trusted key groups *before* viewer-request functions, which is why the default
  behaviour is gated by the function and `manifest.json` has its own signed behaviour.

## License

MIT — see [LICENSE](LICENSE).
