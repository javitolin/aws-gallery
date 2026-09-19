Generated, not committed.

`scripts/bootstrap-keys.sh` writes `cloudfront_public_key.pem` here and puts the
matching private key in SSM. The pair must stay together: regenerating signs
every viewer out, so the script refuses to overwrite an existing key.
