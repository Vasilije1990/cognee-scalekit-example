import html
import os
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from scalekit.frameworks.fastapi import ScalekitAuth

load_dotenv()

REQUIRED_ENV = (
    "SCALEKIT_ENVIRONMENT_URL",
    "SCALEKIT_CLIENT_ID",
    "SCALEKIT_CLIENT_SECRET",
    "COOKIE_ENCRYPTION_SECRET",
)

missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
if missing:
    raise RuntimeError("missing env: " + ", ".join(missing))

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


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    session = auth.get_session(request)
    user = (session or {}).get("user")
    if not user:
        return '<a href="/login">Log in</a>'

    sub = html.escape(str(user.get("sub") or ""))
    email = html.escape(str(user.get("email") or ""))
    return (
        f"<p>sub: {sub}</p>"
        f"<p>email: {email}</p>"
        '<form method="get" action="/">'
        '<input type="hidden" name="turn" value="alice" />'
        '<button type="submit">this turn is Alice</button>'
        "</form>"
        '<p><a href="/logout">Log out</a></p>'
    )


@app.get("/account")
async def account(user: dict = Depends(auth.requires_auth)):
    return {"sub": user["sub"], "email": user.get("email")}
