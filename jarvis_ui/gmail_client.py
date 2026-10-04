"""
Gmail access through Google OAuth 2.0 (installed-app flow with PKCE). Standard library only.

Your Google password never reaches Aurora: sign-in happens on Google's page in your browser, Aurora
receives a token limited to the permissions you approve, and stores it locally in gmail_token.json.

Setup (once): Google Cloud Console -> create project -> enable Gmail API -> OAuth consent screen ->
Credentials -> "OAuth client ID" of type Desktop app -> download JSON as google_client.json (next to api_key.txt).

Permissions are separate and incremental:
  read  -> gmail.readonly   (read unread mail; Aurora cannot modify or mark anything read)
  send  -> gmail.send       (send only; drafts stay local in email_drafts.json, and the router still asks "yes")

Voice: "connect gmail" / "connect gmail with sending" / "disconnect gmail".
Place at: jarvis_ui/gmail_client.py
"""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CLIENT_FILE = os.path.join(BASE, "google_client.json")
TOKEN_FILE = os.path.join(BASE, "gmail_token.json")

SCOPES = {"read": "https://www.googleapis.com/auth/gmail.readonly",
          "send": "https://www.googleapis.com/auth/gmail.send"}
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailError(Exception):
    pass


# ---- storage ---------------------------------------------------------------
def _load_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_token(tok):
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(tok, f)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def _client():
    try:
        with open(CLIENT_FILE, encoding="utf-8") as f:
            c = json.load(f).get("installed")
        return c["client_id"], c["client_secret"]
    except Exception:
        raise GmailError("Gmail isn't set up. Put the Desktop-app OAuth file from Google Cloud next to api_key.txt "
                         "as google_client.json.")


def connected():
    return bool(_load_token())


def has(permission):
    """True if the user has granted 'read' or 'send'."""
    tok = _load_token()
    return bool(tok) and SCOPES[permission] in tok.get("scopes", [])


# ---- HTTP -----------------------------------------------------------------
def _request(url, data=None, headers=None, as_json=False):
    body = None
    headers = dict(headers or {})
    if data is not None:
        body = json.dumps(data).encode() if as_json else urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/json" if as_json else "application/x-www-form-urlencoded"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, body, headers), timeout=20) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode())
            msg = msg.get("error_description") or msg.get("error", {}).get("message") or msg.get("error")
        except Exception:
            msg = e.reason
        raise GmailError(f"Google refused the request ({e.code}): {str(msg)[:100]}")
    except (urllib.error.URLError, OSError) as e:
        raise GmailError(f"I couldn't reach Google ({type(e).__name__}).")


def _access_token():
    tok = _load_token()
    if not tok:
        raise GmailError("Gmail isn't connected. Say connect Gmail.")
    if tok["expires_at"] - 60 > time.time():
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise GmailError("Gmail sign-in expired. Say connect Gmail again.")
    cid, secret = _client()
    r = _request(TOKEN_URL, {"client_id": cid, "client_secret": secret, "refresh_token": tok["refresh_token"],
                             "grant_type": "refresh_token"})
    tok.update(access_token=r["access_token"], expires_at=time.time() + int(r.get("expires_in", 3600)))
    _save_token(tok)
    return tok["access_token"]


def _api(path, data=None):
    return _request(f"{API}/{path}", data, {"Authorization": f"Bearer {_access_token()}"}, as_json=True)


# ---- sign-in -----------------------------------------------------------------
def authorize(permissions, timeout=180):
    """Opens Google's sign-in page and waits for approval. Keeps any permission granted earlier."""
    cid, secret = _client()
    old = (_load_token() or {}).get("scopes", [])
    scopes = sorted(set(old) | {SCOPES[p] for p in permissions})
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, result = secrets.token_urlsafe(16), {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            if "code" in q or "error" in q:
                result.update(q)
            page = b"<h3>Aurora is connected to Gmail. You can close this tab.</h3>" if "code" in q else \
                   b"<h3>Nothing to do here.</h3>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    redirect = f"http://127.0.0.1:{server.server_port}"
    webbrowser.open(AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(scopes),
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"}))
    end = time.time() + timeout
    try:
        while not result and time.time() < end:
            server.handle_request()
    finally:
        server.server_close()
    if not result:
        raise GmailError("Google sign-in timed out.")
    if "code" not in result or result.get("state") != state:
        raise GmailError("Google sign-in was cancelled.")
    r = _request(TOKEN_URL, {"client_id": cid, "client_secret": secret, "code": result["code"], "code_verifier": verifier,
                             "redirect_uri": redirect, "grant_type": "authorization_code"})
    prev = _load_token() or {}
    _save_token({"access_token": r["access_token"], "expires_at": time.time() + int(r.get("expires_in", 3600)),
                 "refresh_token": r.get("refresh_token") or prev.get("refresh_token"),
                 "scopes": r.get("scope", " ".join(scopes)).split()})


def connect_async(permissions, on_done):
    """Runs authorize() off the voice thread; on_done(error_message_or_None)."""
    def work():
        try:
            authorize(permissions)
            on_done(None)
        except GmailError as e:
            on_done(str(e))
    threading.Thread(target=work, daemon=True).start()


def disconnect():
    tok = _load_token()
    if not tok:
        return False
    try:
        _request(REVOKE_URL, {"token": tok.get("refresh_token") or tok["access_token"]})
    except GmailError:
        pass                                   # token is deleted locally either way
    try:
        os.remove(TOKEN_FILE)
    except OSError:
        pass
    return True


# ---- mail ------------------------------------------------------------------------
def _h(value):
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _text(part):
    data = part.get("body", {}).get("data")
    if part.get("mimeType") == "text/plain" and data:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", "ignore")
    for p in part.get("parts") or []:
        t = _text(p)
        if t:
            return t
    return ""


def fetch_unread(limit=5):
    """Newest unread inbox messages (read-only; nothing is marked read). Same shape as the IMAP reader."""
    if not has("read"):
        raise GmailError("Reading Gmail isn't allowed yet. Say connect Gmail.")
    q = urllib.parse.urlencode({"q": "is:unread in:inbox", "maxResults": limit})
    out = []
    for m in _api(f"messages?{q}").get("messages", []):
        msg = _api(f"messages/{m['id']}?format=full")
        hdr = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        name, addr = parseaddr(hdr.get("from", ""))
        body = " ".join(_text(msg.get("payload", {})).split())[:4000]
        out.append({"from": _h(name) or addr, "address": addr, "subject": _h(hdr.get("subject")),
                    "date": hdr.get("date", ""), "body": body})
    return out


def send(draft, confirmed=False):
    """Sends a draft dict {to, subject, body}. Refuses unless the user confirmed this exact draft."""
    if confirmed is not True:
        raise PermissionError("Sending email requires explicit confirmation.")
    if not has("send"):
        raise GmailError("Sending isn't allowed yet. Say connect Gmail with sending.")
    if not (draft and draft.get("to") and draft.get("body")):
        raise GmailError("The draft is incomplete.")
    msg = EmailMessage()
    msg["To"], msg["Subject"] = draft["to"], draft.get("subject") or "(no subject)"
    msg.set_content(draft["body"])
    _api("messages/send", {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()})
    return True
