"""Sign-in for saved chats: Google (OpenID Connect) and one-time email links.

Signing in is optional: anyone can chat; an account only adds saved history.
Accounts are keyed by email, so both sign-in methods reach the same account.
The session is Flask's signed cookie (HttpOnly, Secure, SameSite=Lax).
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import requests
from authlib.integrations.flask_client import OAuth
from flask import Blueprint, current_app, jsonify, redirect, request, session, url_for

from api import db

bp = Blueprint("auth", __name__)
oauth = OAuth()

# Limits (approved 2026-11-20); env vars can override them.
LINK_MINUTES = int(os.getenv("PINGUIDE_LINK_MINUTES", 15))
EMAILS_PER_HOUR = int(os.getenv("PINGUIDE_EMAILS_PER_HOUR", 5))
SESSION_DAYS = int(os.getenv("PINGUIDE_SESSION_DAYS", 30))

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def init_app(app) -> None:
    secret = os.getenv("SECRET_KEY")
    if not secret:
        # Sessions then reset on every restart. Fine locally, wrong in production.
        app.logger.warning("SECRET_KEY is not set: using a temporary key")
        secret = secrets.token_hex(32)
    app.config.update(
        SECRET_KEY=secret,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,      # browsers also accept Secure cookies on http://localhost
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS),
    )
    oauth.init_app(app)
    if google_enabled():
        oauth.register(
            "google",
            client_id=os.getenv("GOOGLE_CLIENT_ID"),
            client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    app.register_blueprint(bp)


def google_enabled() -> bool:
    return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))


def current_user() -> dict | None:
    uid = session.get("uid")
    return db.get_user(uid) if isinstance(uid, int) else None


def _sign_in(email: str, name: str = "") -> None:
    uid = db.upsert_user(email, name)
    session.clear()  # a fresh session on sign-in (no fixation)
    session.permanent = True
    session["uid"] = uid


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _hour_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


# --------------------------------------------------------------------------
# Google
# --------------------------------------------------------------------------

@bp.get("/auth/google")
def google_start():
    if not google_enabled():
        return redirect("/#signin-error")
    return oauth.google.authorize_redirect(url_for("auth.google_callback", _external=True))


@bp.get("/auth/google/callback")
def google_callback():
    try:
        token = oauth.google.authorize_access_token()
    except Exception:
        current_app.logger.exception("Google sign-in failed")
        return redirect("/#signin-error")
    info = token.get("userinfo") or {}
    if not info.get("email") or not info.get("email_verified"):
        return redirect("/#signin-error")
    _sign_in(info["email"], info.get("name") or "")
    return redirect("/")


# --------------------------------------------------------------------------
# Email link
# --------------------------------------------------------------------------

def _send_email(to: str, link: str) -> None:
    text = (f"Sign in to Pinguide with this link (it expires in {LINK_MINUTES} minutes):\n\n"
            f"{link}\n\nIf you didn't ask for this, you can ignore this email.")
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        # Local development: no email service, print the link instead.
        print(f"[dev] sign-in link for {to}: {link}", flush=True)
        return
    safe = html.escape(link, quote=True)
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "from": os.getenv("MAIL_FROM", "Pinguide <signin@pinguide.info>"),
            "to": [to],
            "subject": "Your Pinguide sign-in link",
            "text": text,
            "html": (f"<p>Sign in to Pinguide with this link (it expires in {LINK_MINUTES} "
                     f"minutes):</p><p><a href=\"{safe}\">Sign in to Pinguide</a></p>"
                     "<p>If you didn't ask for this, you can ignore this email.</p>"),
        },
        timeout=20,
    )
    r.raise_for_status()


@bp.post("/auth/email")
def email_start():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return jsonify({"error": "Please enter a valid email address."}), 400
    # Throttle per address and per IP so the form can't flood an inbox.
    hour = _hour_key()
    keys = (f"mail:{email}", f"mail-ip:{request.remote_addr}")
    if any(db.usage_get(k, hour) >= EMAILS_PER_HOUR for k in keys):
        return jsonify({"error": "Too many sign-in emails. Please try again in an hour."}), 429
    for k in keys:
        db.usage_add(k, 1, hour)

    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=LINK_MINUTES)).isoformat(timespec="seconds")
    db.save_login_token(_hash(token), email, expires)
    try:
        _send_email(email, url_for("auth.email_verify", token=token, _external=True))
    except Exception:
        current_app.logger.exception("sending sign-in email failed")
        return jsonify({"error": "The sign-in email couldn't be sent. Please try again later."}), 502
    return jsonify({"ok": True})


_VERIFY_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>Sign in to Pinguide</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0f0f0f;
  color: #e6e6e6; display: grid; place-items: center; min-height: 100vh; margin: 0; }}
main {{ text-align: center; padding: 1rem; }}
button {{ font: inherit; font-weight: 700; padding: 0.7rem 1.4rem; border: none; border-radius: 10px;
  background: #e8a020; color: #1a1a1a; cursor: pointer; }}
button:focus-visible {{ outline: 2px solid #f0b030; outline-offset: 2px; }}
</style></head>
<body><main>
<h1>Sign in to Pinguide</h1>
<form method="post" action="{action}">
<input type="hidden" name="token" value="{token}">
<button type="submit" autofocus>Sign in</button>
</form>
</main></body></html>"""


@bp.get("/auth/email/verify")
def email_verify():
    # A button page rather than signing in on GET: email scanners open links
    # automatically, which would use up a single-use token.
    token = request.args.get("token", "")
    return _VERIFY_PAGE.format(action=html.escape(url_for("auth.email_verify_post")),
                               token=html.escape(token, quote=True))


@bp.post("/auth/email/verify")
def email_verify_post():
    email = db.use_login_token(_hash(request.form.get("token", "")))
    if not email:
        return redirect("/#signin-expired")
    _sign_in(email)
    return redirect("/")


# --------------------------------------------------------------------------
# Account
# --------------------------------------------------------------------------

@bp.get("/api/me")
def me():
    user = current_user()
    return jsonify({"user": {"email": user["email"], "name": user["name"]} if user else None})


@bp.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@bp.delete("/api/me")
def delete_me():
    user = current_user()
    if not user:
        return jsonify({"error": "not signed in"}), 401
    db.delete_user(user["id"])
    session.clear()
    return jsonify({"ok": True})
