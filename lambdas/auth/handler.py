import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request

import boto3
import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

COGNITO_DOMAIN = os.environ["COGNITO_DOMAIN"]
CLIENT_ID = os.environ["CLIENT_ID"]
USER_POOL_ID = os.environ["USER_POOL_ID"]
REGION = os.environ["REGION"]
SITE_URL = os.environ["SITE_URL"].rstrip("/")
KEY_PAIR_ID = os.environ["KEY_PAIR_ID"]
PRIVATE_KEY_PARAM = os.environ["PRIVATE_KEY_PARAM"]
HMAC_PARAM = os.environ["HMAC_PARAM"]
SESSION_TTL = int(os.environ.get("SESSION_TTL", "604800"))

ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{USER_POOL_ID}"
REDIRECT_URI = f"{SITE_URL}/auth/callback"

_private_key = None
_jwks_client = None
_hmac_key = None


def _key():
    global _private_key
    if _private_key is None:
        pem = boto3.client("ssm").get_parameter(
            Name=PRIVATE_KEY_PARAM, WithDecryption=True
        )["Parameter"]["Value"]
        _private_key = serialization.load_pem_private_key(pem.encode(), password=None)
    return _private_key


def _identity_secret() -> bytes:
    global _hmac_key
    if _hmac_key is None:
        _hmac_key = boto3.client("ssm").get_parameter(
            Name=HMAC_PARAM, WithDecryption=True
        )["Parameter"]["Value"].encode()
    return _hmac_key


def identity_cookie(email: str, expires: int) -> str:
    """email|expiry|signature — the API trusts this only after verifying it."""
    payload = f"{email}|{expires}"
    digest = hmac.new(_identity_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{digest}"


def _jwks():
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(f"{ISSUER}/.well-known/jwks.json")
    return _jwks_client


def _cf_b64(raw: bytes) -> str:
    """CloudFront's cookie-safe base64 variant."""
    return base64.b64encode(raw).decode().translate(str.maketrans("+=/", "-_~"))


def _signed_cookies(expires: int) -> dict:
    policy = json.dumps(
        {
            "Statement": [
                {
                    "Resource": f"{SITE_URL}/*",
                    "Condition": {"DateLessThan": {"AWS:EpochTime": expires}},
                }
            ]
        },
        separators=(",", ":"),
    ).encode()
    # CloudFront verifies RSA-SHA1 over the policy document; the algorithm is not ours to choose.
    signature = _key().sign(policy, padding.PKCS1v15(), hashes.SHA1())
    return {
        "CloudFront-Policy": _cf_b64(policy),
        "CloudFront-Signature": _cf_b64(signature),
        "CloudFront-Key-Pair-Id": KEY_PAIR_ID,
    }


def _cookie(name, value, max_age, http_only=True):
    parts = [f"{name}={value}", "Path=/", "Secure", "SameSite=Lax", f"Max-Age={max_age}"]
    if http_only:
        parts.insert(2, "HttpOnly")
    return "; ".join(parts)


def _parse_cookies(event) -> dict:
    out = {}
    for raw in event.get("cookies") or []:
        name, _, value = raw.partition("=")
        out[name.strip()] = value
    return out


def _redirect(location, cookies=None):
    return {
        "statusCode": 302,
        "headers": {"location": location, "cache-control": "no-store"},
        "cookies": cookies or [],
    }


def _error(code, message):
    return {
        "statusCode": code,
        "headers": {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"},
        "body": f"<!doctype html><meta charset=utf-8><title>Sign-in problem</title>"
        f"<body style='font-family:system-ui;background:#111;color:#eee;padding:3rem'>"
        f"<h1>Sign-in problem</h1><p>{message}</p>"
        f"<p><a style='color:#8ab4f8' href='/auth/login'>Try again</a></p>",
    }


def _login(event):
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    state = secrets.token_urlsafe(24)
    next_path = (event.get("queryStringParameters") or {}).get("next", "/")
    if not next_path.startswith("/"):
        next_path = "/"

    params = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "scope": "openid email profile",
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return _redirect(
        f"https://{COGNITO_DOMAIN}/oauth2/authorize?{params}",
        [
            _cookie("pkce", verifier, 600),
            _cookie("state", state, 600),
            _cookie("next", urllib.parse.quote(next_path), 600),
        ],
    )


def _callback(event):
    query = event.get("queryStringParameters") or {}
    cookies = _parse_cookies(event)

    if "error" in query:
        return _error(400, f"Cognito returned: {query.get('error_description', query['error'])}")
    if not query.get("code"):
        return _error(400, "No authorization code was returned.")
    if not cookies.get("state") or not secrets.compare_digest(
        cookies["state"], query.get("state", "")
    ):
        return _error(400, "State mismatch — the sign-in was not completed in this browser.")
    if not cookies.get("pkce"):
        return _error(400, "Sign-in took too long. Start again.")

    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": query["code"],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": cookies["pkce"],
        }
    ).encode()
    request = urllib.request.Request(
        f"https://{COGNITO_DOMAIN}/oauth2/token",
        data=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            tokens = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return _error(502, f"Token exchange failed: {exc.read().decode()[:200]}")

    # Back-channel TLS already authenticates the issuer; verifying anyway so a
    # misrouted or replayed token cannot mint a session.
    try:
        claims = jwt.decode(
            tokens["id_token"],
            _jwks().get_signing_key_from_jwt(tokens["id_token"]).key,
            algorithms=["RS256"],
            audience=CLIENT_ID,
            issuer=ISSUER,
        )
    except jwt.PyJWTError as exc:
        return _error(401, f"Token rejected: {exc}")

    expires = int(time.time()) + SESSION_TTL
    email = claims.get("email") or claims.get("cognito:username") or "unknown"
    session = [
        _cookie("gallery_user", identity_cookie(email, expires), SESSION_TTL),
    ] + [
        _cookie(name, value, SESSION_TTL)
        for name, value in _signed_cookies(expires).items()
    ]
    session.append(_cookie("gallery_exp", str(expires), SESSION_TTL))
    for stale in ("pkce", "state", "next"):
        session.append(_cookie(stale, "", 0))

    return _redirect(urllib.parse.unquote(cookies.get("next", "/")), session)


def _logout(_event):
    cleared = [
        _cookie(name, "", 0)
        for name in (
            "CloudFront-Policy",
            "CloudFront-Signature",
            "CloudFront-Key-Pair-Id",
            "gallery_exp",
            "gallery_user",
        )
    ]
    params = urllib.parse.urlencode(
        {"client_id": CLIENT_ID, "logout_uri": f"{SITE_URL}/"}
    )
    return _redirect(f"https://{COGNITO_DOMAIN}/logout?{params}", cleared)


ROUTES = {"/auth/login": _login, "/auth/callback": _callback, "/auth/logout": _logout}


def lambda_handler(event, _context):
    path = event.get("requestContext", {}).get("http", {}).get("path", "")
    handler = ROUTES.get(path.rstrip("/") or path)
    if handler is None:
        return _error(404, "Unknown auth route.")
    return handler(event)
