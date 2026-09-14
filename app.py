import hashlib
import hmac
import html
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import cognee
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from scalekit import ScalekitClient
from scalekit.frameworks.fastapi import ScalekitAuth

from loop import USERS, is_conflict, recall_user, short_recall, user_spec

load_dotenv()

REQUIRED_SCALEKIT = (
    "SCALEKIT_ENVIRONMENT_URL",
    "SCALEKIT_CLIENT_ID",
    "SCALEKIT_CLIENT_SECRET",
    "COOKIE_ENCRYPTION_SECRET",
)

missing = [name for name in REQUIRED_SCALEKIT if not os.getenv(name)]
if missing:
    raise RuntimeError("missing env: " + ", ".join(missing))

DEMO_USER = "alice"
BIND_COOKIE = "desk_customer"
GUEST_COOKIE = "desk_guest"
ROOT = Path(__file__).resolve().parent
PAGE_PATH = ROOT / "page.html"

redirect_uri = os.getenv("SCALEKIT_REDIRECT_URI", "http://localhost:5001/callback")
callback_path = urlparse(redirect_uri).path or "/callback"

app = FastAPI()
auth_kwargs = {
    "env_url": os.environ["SCALEKIT_ENVIRONMENT_URL"],
    "client_id": os.environ["SCALEKIT_CLIENT_ID"],
    "client_secret": os.environ["SCALEKIT_CLIENT_SECRET"],
    "redirect_uri": redirect_uri,
    "cookie_encryption_secret": os.environ["COOKIE_ENCRYPTION_SECRET"],
    "cookie_secure": False,
}
if callback_path.endswith("/callback"):
    auth_kwargs["callback_path"] = callback_path

auth = ScalekitAuth(**auth_kwargs)
auth.install(app)


@asynccontextmanager
async def cognee_session():
    key = os.getenv("COGNEE_API_KEY")
    url = os.getenv("COGNEE_BASE_URL")
    if not key or not url:
        raise RuntimeError("missing env: COGNEE_API_KEY, COGNEE_BASE_URL")
    await cognee.serve(url=url, api_key=key)
    try:
        yield
    finally:
        await cognee.disconnect()


def _secret() -> bytes:
    return os.environ["COOKIE_ENCRYPTION_SECRET"].encode("utf-8")


def sign_customer(name: str) -> str:
    mac = hmac.new(_secret(), name.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{name}.{mac}"


def read_signed_cookie(request: Request, cookie: str) -> str | None:
    raw = request.cookies.get(cookie) or ""
    if "." not in raw:
        return None
    name, mac = raw.rsplit(".", 1)
    if name not in USERS:
        return None
    expect = hmac.new(_secret(), name.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expect):
        return None
    return name


def read_bind_cookie(request: Request) -> str | None:
    return read_signed_cookie(request, BIND_COOKIE)


def read_guest(request: Request) -> str | None:
    name = read_signed_cookie(request, GUEST_COOKIE)
    if name == "bob":
        return "bob"
    return None


def customer_from_claims(user: dict | None) -> str | None:
    if not user:
        return None
    sub = str(user.get("sub") or "")
    if sub and sub == os.getenv("SCALEKIT_ALICE_SUB"):
        return "alice"
    if sub and sub == os.getenv("SCALEKIT_BOB_SUB"):
        return "bob"
    blob = " ".join(
        str(user.get(key) or "")
        for key in ("email", "preferred_username", "username", "name")
    ).lower()
    if blob.startswith("alice") or "alice+sktest" in blob or blob.startswith("alice@"):
        return "alice"
    if "alice+" in blob or blob.split("@", 1)[0] == "alice":
        return "alice"
    if blob.startswith("bob") or "bob+sktest" in blob or blob.startswith("bob@"):
        return "bob"
    if "bob+" in blob or blob.split("@", 1)[0] == "bob":
        return "bob"
    return None


def bound_customer(request: Request, user: dict | None) -> str | None:
    if read_guest(request) == "bob":
        return "bob"
    return customer_from_claims(user) or read_bind_cookie(request)


def require_customer(request: Request):
    session = auth.get_session(request)
    user = (session or {}).get("user")
    name = bound_customer(request, user)
    if name == "bob" and read_guest(request) == "bob":
        return name, None
    if not user:
        return None, JSONResponse({"ok": False, "error": "sign in first"}, status_code=401)
    if not name:
        return None, JSONResponse(
            {"ok": False, "error": "bind alice or bob"},
            status_code=403,
        )
    return name, None


def role_payload(name: str) -> dict:
    spec = user_spec(name)
    return {
        "name": name,
        "summary": spec.get("summary") or "",
        "plan": "Pro" if name == "alice" else "not Pro",
        "questions": [
            {"id": item["id"], "text": item["text"]}
            for item in spec["questions"]
        ],
    }


def slack_actions():
    client = ScalekitClient(
        client_id=os.environ["SCALEKIT_CLIENT_ID"],
        client_secret=os.environ["SCALEKIT_CLIENT_SECRET"],
        env_url=os.environ["SCALEKIT_ENVIRONMENT_URL"],
    )
    return client.actions


def slack_status(identifier: str):
    connection_name = os.getenv("SLACK_CONNECTION_NAME") or "slack"
    actions = slack_actions()
    account = actions.get_or_create_connected_account(
        connection_name, identifier
    ).connected_account
    if account is not None and account.status == "ACTIVE":
        return True, None
    link = actions.get_authorization_link(
        connection_name=connection_name,
        identifier=identifier,
    ).link
    return False, link


@app.get("/tokens.css")
async def tokens_css():
    return FileResponse(ROOT / "tokens.css", media_type="text/css")


@app.get("/diagrams/issue-desk-sequence.html")
async def sequence_diagram():
    return FileResponse(
        ROOT / "diagrams" / "issue-desk-sequence.html",
        media_type="text/html",
    )


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    session = auth.get_session(request)
    user = (session or {}).get("user")
    logged_in = bool(user)
    sub = str((user or {}).get("sub") or "")
    email = str((user or {}).get("email") or "")
    guest = read_guest(request) == "bob"
    customer = bound_customer(request, user)
    if guest:
        who = "Viewing Bob as a guest. This is not a Scalekit login."
        nav = (
            '<a class="nav-cta" href="/logout">Log out</a>'
            if logged_in
            else '<a class="nav-cta" href="/login">Sign in as Alice</a>'
        )
    elif logged_in:
        label = html.escape(email or sub)
        who = f"Signed in as Alice. <code>{label}</code>."
        nav = '<a class="nav-cta" href="/logout">Log out</a>'
    else:
        who = "Sign in as Alice to open the desk."
        nav = '<a class="nav-cta" href="/login">Sign in</a>'
    slack_active, slack_link = False, None
    if customer:
        try:
            slack_active, slack_link = slack_status(customer)
        except Exception:
            slack_active, slack_link = False, None
    channel = os.getenv("SLACK_CHANNEL") or "general"
    boot = {
        "logged_in": logged_in,
        "sub": sub or None,
        "email": email or None,
        "customer": customer,
        "guest": guest,
        "role": role_payload(customer) if customer else None,
        "slack_active": slack_active,
        "slack_link": slack_link,
        "channel": channel,
    }
    page = (
        PAGE_PATH.read_text(encoding="utf-8")
        .replace("__WHO__", who)
        .replace("__NAV__", nav)
        .replace("__BOOT__", json.dumps(boot))
    )
    return page


@app.get("/account")
async def account(user: dict = Depends(auth.requires_auth)):
    return {"sub": user["sub"], "email": user.get("email")}


@app.get("/api/status")
async def api_status(request: Request):
    session = auth.get_session(request)
    user = (session or {}).get("user")
    customer = bound_customer(request, user)
    slack_active, slack_link = False, None
    if customer:
        try:
            slack_active, slack_link = slack_status(customer)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {
        "ok": True,
        "logged_in": bool(user),
        "sub": (user or {}).get("sub"),
        "email": (user or {}).get("email"),
        "customer": customer,
        "guest": read_guest(request) == "bob",
        "slack_active": slack_active,
        "slack_link": slack_link,
        "channel": os.getenv("SLACK_CHANNEL") or "general",
    }


@app.post("/api/bind")
async def api_bind(request: Request):
    session = auth.get_session(request)
    user = (session or {}).get("user")
    if not user:
        return JSONResponse({"ok": False, "error": "sign in first"}, status_code=401)
    mapped = customer_from_claims(user)
    body = await request.json()
    name = str(body.get("customer") or "")
    if mapped and name and name != mapped:
        return JSONResponse(
            {"ok": False, "error": "this login is already " + mapped},
            status_code=403,
        )
    chosen = mapped or name
    if chosen not in USERS:
        return JSONResponse({"ok": False, "error": "choose alice or bob"}, status_code=400)
    response = JSONResponse({"ok": True, "customer": chosen, "role": role_payload(chosen)})
    response.set_cookie(
        BIND_COOKIE,
        sign_customer(chosen),
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=60 * 60 * 8,
    )
    return response


@app.post("/api/guest")
async def api_guest():
    response = JSONResponse(
        {"ok": True, "customer": "bob", "guest": True, "role": role_payload("bob")}
    )
    response.set_cookie(
        GUEST_COOKIE,
        sign_customer("bob"),
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=60 * 60 * 8,
    )
    return response


@app.post("/api/guest/clear")
async def api_guest_clear():
    response = JSONResponse({"ok": True, "guest": False})
    response.delete_cookie(GUEST_COOKIE)
    return response


@app.post("/api/ask")
async def api_ask(request: Request):
    name, err = require_customer(request)
    if err:
        return err
    try:
        body = await request.json()
    except ValueError:
        body = {}
    qid = str((body or {}).get("question_id") or "")
    spec = user_spec(name)
    picked = next((item for item in spec["questions"] if item["id"] == qid), None)
    if picked is None:
        return JSONResponse({"ok": False, "error": "unknown question"}, status_code=400)
    try:
        async with cognee_session():
            try:
                results = await recall_user(name, picked["text"])
            except Exception as exc:
                if not is_conflict(exc):
                    raise
                results = await recall_user(name, picked["text"])
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    answer = short_recall(results)
    expect = picked["expect"]
    matched = all(bit.lower() in (answer + "").lower() for bit in expect)
    return {
        "ok": True,
        "product": "cognee",
        "question": picked["text"],
        "dataset": name,
        "answer": answer,
        "matched": matched,
    }


@app.post("/api/seed")
async def api_seed(request: Request):
    name, err = require_customer(request)
    if err:
        return err
    spec = user_spec(name)
    note = spec["note"].read_text(encoding="utf-8").strip()
    try:
        async with cognee_session():
            await cognee.remember(note, dataset_name=name)
            await cognee.improve(name)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "product": "cognee", "dataset": name, "seeded": True}


@app.post("/api/slack")
async def api_slack(request: Request):
    name, err = require_customer(request)
    if err:
        return err
    channel = os.getenv("SLACK_CHANNEL") or "general"
    connection_name = os.getenv("SLACK_CONNECTION_NAME") or "slack"
    spec = user_spec(name)
    text = (
        f"Issue desk status: {name} asked “{spec['question']}”. "
        "Memory is that customer’s Cognee dataset only."
    )
    actions = slack_actions()
    account = actions.get_or_create_connected_account(
        connection_name, name
    ).connected_account
    if account is None or account.status != "ACTIVE":
        link = actions.get_authorization_link(
            connection_name=connection_name,
            identifier=name,
        ).link
        return {"ok": False, "product": "scalekit", "link": link}
    response = actions.request(
        connection_name=connection_name,
        identifier=name,
        method="POST",
        path="/api/chat.postMessage",
        body={"channel": channel, "text": text},
    )
    try:
        payload = response.json()
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": f"http {response.status_code}"},
            status_code=400,
        )
    return {
        "ok": bool(payload.get("ok")),
        "product": "scalekit",
        "ts": payload.get("ts"),
        "error": payload.get("error"),
        "channel": channel,
    }
