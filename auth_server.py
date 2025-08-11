import os, json, secrets, time, pathlib
from urllib.parse import urlencode
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
SCOPES        = os.getenv("TWITCH_SCOPES", "user:write:chat user:bot")
TOKENS_FILE   = os.getenv("TWITCH_TOKENS_FILE", "twitch_tokens.json")

AUTH_BIND_HOST   = os.getenv("AUTH_BIND_HOST", "0.0.0.0")
AUTH_PORT        = int(os.getenv("AUTH_PORT", "3750"))
AUTH_PUBLIC_BASE = os.getenv("AUTH_PUBLIC_BASE", f"http://localhost:{AUTH_PORT}")

REDIRECT_URI = AUTH_PUBLIC_BASE.rstrip("/") + "/callback"

if not CLIENT_ID or not CLIENT_SECRET:
    raise SystemExit("Sett TWITCH_CLIENT_ID og TWITCH_CLIENT_SECRET i .env")

ENV_FILE = pathlib.Path(".env")
_state = None

def _update_env(k, v):
    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    out, found = [], False
    for line in lines:
        if line.startswith(f"{k}="):
            out.append(f"{k}={v}"); found=True
        else:
            out.append(line)
    if not found:
        out.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")

def _save_tokens(access_token, refresh_token, expires_in):
    expires_at = int(time.time()) + int(expires_in)
    pathlib.Path(TOKENS_FILE).write_text(json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at
    }, indent=2), encoding="utf-8")
    _update_env("TWITCH_BOT_TOKEN", access_token)
    _update_env("TWITCH_REFRESH_TOKEN", refresh_token)
    _update_env("TWITCH_TOKEN_EXPIRES_AT", str(expires_at))
    print("✅ Lagret tokens til .env og", TOKENS_FILE)

async def index(req):  return web.Response(text="Gå til /login for å autorisere botten.")

async def login(req):
    global _state
    _state = secrets.token_urlsafe(24)
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": _state,
        "force_verify": "true"
    }
    raise web.HTTPFound("https://id.twitch.tv/oauth2/authorize?" + urlencode(params))

async def callback(req):
    global _state
    code  = req.rel_url.query.get("code")
    state = req.rel_url.query.get("state")
    if not code or state != _state:
        return web.Response(status=400, text="State mismatch eller mangler code.")
    _state = None

    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }
    async with req.app["session"].post("https://id.twitch.tv/oauth2/token", data=data) as r:
        js = await r.json()
        if r.status != 200:
            return web.Response(status=r.status, text=str(js))

    access  = js["access_token"]
    refresh = js["refresh_token"]
    expires = js.get("expires_in", 3600)
    _save_tokens(access, refresh, expires)

    async with req.app["session"].get("https://id.twitch.tv/oauth2/validate",
                                      headers={"Authorization": f"OAuth {access}"}) as vr:
        info = await vr.json()

    return web.Response(content_type="text/html", text=f"""
        <h1>Bot autorisert ✅</h1>
        <p>Bruker: {info.get('login')} (user_id: {info.get('user_id')})</p>
        <p>Du kan lukke dette vinduet.</p>
    """)

async def make_app():
    import aiohttp
    app = web.Application()
    app["session"] = aiohttp.ClientSession()
    app.add_routes([ web.get("/", index), web.get("/login", login), web.get("/callback", callback) ])
    return app

if __name__ == "__main__":
    web.run_app(make_app(), host=AUTH_BIND_HOST, port=AUTH_PORT)