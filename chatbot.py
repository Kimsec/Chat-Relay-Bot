# chatbot.py
import os, asyncio, time, html, json, pathlib, re
from dotenv import load_dotenv
import aiohttp

load_dotenv()

# ====== TWITCH (med auto-refresh) ======
TWITCH_CLIENT_ID        = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET    = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_BROADCASTER_ID   = os.getenv("TWITCH_BROADCASTER_ID")
TWITCH_SENDER_ID        = os.getenv("TWITCH_SENDER_ID")
TWITCH_POST_URL         = "https://api.twitch.tv/helix/chat/messages"
TOKENS_FILE             = os.getenv("TWITCH_TOKENS_FILE", "twitch_tokens.json")
INIT_ACCESS_TOKEN       = os.getenv("TWITCH_BOT_TOKEN")
INIT_REFRESH_TOKEN      = os.getenv("TWITCH_REFRESH_TOKEN")
INIT_EXPIRES_AT         = os.getenv("TWITCH_TOKEN_EXPIRES_AT")
MIN_POLL_MS             = int(os.getenv("YT_MIN_POLL_MS"))

# ====== Moderation (banned words/phrases) ======
# One phrase per line in banned_words.txt. Lines starting with '#' are comments.
BANNED_WORDS_PATH   = os.getenv("BANNED_WORDS_FILE")
BAN_MODE            = os.getenv("BAN_MODE", "censor").strip().lower()   # "censor" or "drop"
BAN_CHAR            = os.getenv("BAN_CHAR", "*")                         # masking character
BAN_CASE_SENSITIVE  = os.getenv("BAN_CASE_SENSITIVE", "false").lower() in ("1","true","yes","on")
BAN_WATCH_INTERVAL  = float(os.getenv("BAN_WATCH_INTERVAL", "600"))

# Internal state for moderation
_BANNED_RE: re.Pattern | None = None
_BANNED_MTIME: float | None = None

def _build_banned_regex(words: list[str]) -> re.Pattern | None:
    """Compile a single regex that matches any banned word/phrase as a whole word.
    Uses \b on both sides to avoid substring hits (e.g., 'ass' won't match 'pass')."""
    parts: list[str] = []
    for raw in words:
        w = raw.strip()
        if not w or w.startswith("#"):  # allow comments / blank lines
            continue
        # Wrap each literal in word boundaries and escape it
        parts.append(r"\b" + re.escape(w) + r"\b")
    if not parts:
        return None
    flags = 0 if BAN_CASE_SENSITIVE else re.IGNORECASE
    return re.compile("|".join(parts), flags)

def _load_banned_words_file(path: str) -> tuple[list[str], re.Pattern | None]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            words = [ln.rstrip("\n") for ln in f]
        return words, _build_banned_regex(words)
    except FileNotFoundError:
        return [], None

async def _banned_words_watcher():
    """Tiny file-watcher: checks st_mtime periodically and reloads on change."""
    global _BANNED_RE, _BANNED_MTIME
    if not BANNED_WORDS_PATH:
        print("[Moderation] No BANNED_WORDS_FILE set; skipping banned words watcher.")
        return
    last_mtime = None
    # initial load (even if file not present yet)
    try:
        st = os.stat(BANNED_WORDS_PATH)
        last_mtime = st.st_mtime
        words, regex = _load_banned_words_file(BANNED_WORDS_PATH)
        _BANNED_RE = regex
        print(f"[Moderation] Loaded {len(words)} entries from {BANNED_WORDS_PATH}")
    except FileNotFoundError:
        _BANNED_RE = None
        last_mtime = None
        print(f"[Moderation] File not found ({BANNED_WORDS_PATH}); moderation list empty.")
    except Exception as e:
        print("[Moderation] initial load error:", e)

    while True:
        try:
            st = os.stat(BANNED_WORDS_PATH)
            if last_mtime != st.st_mtime:
                words, regex = _load_banned_words_file(BANNED_WORDS_PATH)
                _BANNED_RE = regex
                last_mtime = st.st_mtime
                print(f"[Moderation] Reloaded {len(words)} entries from {BANNED_WORDS_PATH}")
        except FileNotFoundError:
            if last_mtime is not None:
                # File removed; clear list
                _BANNED_RE = None
                last_mtime = None
                print("[Moderation] File removed; cleared banned list.")
        except Exception as e:
            # Log and keep going
            print("[Moderation] watcher error:", e)
        await asyncio.sleep(BAN_WATCH_INTERVAL)

def _moderate_text(text: str) -> str | None:
    """Return censored text, or None to drop message entirely."""
    regex = _BANNED_RE
    if not regex:
        return text
    if not regex.search(text):
        return text
    if BAN_MODE == "drop":
        return None
    # Censor: replace each matched span with BAN_CHAR * length
    return regex.sub(lambda m: BAN_CHAR * len(m.group(0)), text)

# ====== Auth ======
class TwitchAuth:
    TOKEN_URL = "https://id.twitch.tv/oauth2/token"

    def __init__(self, client_id, client_secret, tokens_file, init_access, init_refresh, init_expires_at):
        self.client_id = client_id
        self.client_secret = client_secret
        self.tokens_file = pathlib.Path(tokens_file)
        self.access_token = init_access
        self.refresh_token = init_refresh
        self.expires_at = int(init_expires_at) if init_expires_at else 0

        # Load from file if present (overrides init)
        if self.tokens_file.exists():
            try:
                data = json.loads(self.tokens_file.read_text(encoding="utf-8"))
                self.access_token = data.get("access_token") or self.access_token
                self.refresh_token = data.get("refresh_token") or self.refresh_token
                self.expires_at = int(data.get("expires_at") or self.expires_at or 0)
            except Exception:
                pass

    def _persist(self):
        data = {"access_token": self.access_token, "refresh_token": self.refresh_token, "expires_at": self.expires_at}
        self.tokens_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # Optionally mirror into .env for restarts
        def upd(k, v):
            try:
                p = pathlib.Path(".env")
                lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
                out, found = [], False
                for line in lines:
                    if line.startswith(f"{k}="):
                        out.append(f"{k}={v}"); found = True
                    else:
                        out.append(line)
                if not found: out.append(f"{k}={v}")
                p.write_text("\n".join(out) + "\n", encoding="utf-8")
            except Exception:
                pass
        upd("TWITCH_BOT_TOKEN", self.access_token)
        upd("TWITCH_REFRESH_TOKEN", self.refresh_token or "")
        upd("TWITCH_TOKEN_EXPIRES_AT", str(self.expires_at or 0))

    async def _refresh(self, session: aiohttp.ClientSession):
        if not (self.refresh_token and self.client_id and self.client_secret):
            raise RuntimeError("[TwitchAuth] Missing refresh/client_secret – run auth_server.py again.")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        async with session.post(self.TOKEN_URL, data=data) as r:
            js = await r.json()
            if r.status != 200:
                raise RuntimeError(f"[TwitchAuth] Refresh failed: {r.status} {js}")
        self.access_token = js["access_token"]
        self.refresh_token = js.get("refresh_token", self.refresh_token)
        self.expires_at = int(time.time()) + int(js.get("expires_in", 3600))
        self._persist()
        return self.access_token

    async def get_token(self, session: aiohttp.ClientSession):
        now = int(time.time())
        # if we can't refresh but have an access token, use it
        if not self.refresh_token and self.access_token:
            return self.access_token
        if not self.access_token or now >= (self.expires_at - 60):
            return await self._refresh(session)
        return self.access_token

AUTH = TwitchAuth(
    TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TOKENS_FILE,
    INIT_ACCESS_TOKEN, INIT_REFRESH_TOKEN, INIT_EXPIRES_AT
)

# ====== YOUTUBE ======
YOUTUBE_API_KEY        = os.getenv("YOUTUBE_API_KEY")
YOUTUBE_LIVE_CHAT_ID   = os.getenv("YOUTUBE_LIVE_CHAT_ID")     # "AUTO" => resolve automatically
YOUTUBE_CHANNEL_ID     = os.getenv("YOUTUBE_CHANNEL_ID")       # optional (UC...)
YOUTUBE_CHANNEL_HANDLE = os.getenv("YOUTUBE_CHANNEL_HANDLE")   # e.g. @kimsec
YOUTUBE_VIDEO_ID       = os.getenv("YOUTUBE_VIDEO_ID")         # optional for test/unlisted

# ====== KICK ======
KICK_CHANNEL           = os.getenv("KICK_CHANNEL")

# ====== Toggles / Prefix ======
def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None: return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

ENABLE_YT   = _env_bool("ENABLE_YT", True)
ENABLE_KICK = _env_bool("ENABLE_KICK", True)
PREFIX_YT   = os.getenv("PREFIX_YT",   "🔴[YT] ")
PREFIX_KICK = os.getenv("PREFIX_KICK", "🟢[KICK] ")

# ------------------ YouTube helpers ------------------ #
async def _fetch_json(session: aiohttp.ClientSession, url: str, params: dict):
    async with session.get(url, params=params) as r:
        if r.status != 200:
            body = await r.text()
            raise RuntimeError(f"HTTP {r.status} GET {r.url}: {body}")
        return await r.json()

async def resolve_youtube_channel_id(session: aiohttp.ClientSession, api_key: str, channel_id: str | None, handle: str | None) -> str:
    if channel_id:
        return channel_id
    handle = (handle or "").lstrip("@").strip()
    if not handle:
        raise RuntimeError("Set YOUTUBE_CHANNEL_ID or YOUTUBE_CHANNEL_HANDLE in .env")
    data = await _fetch_json(session, "https://www.googleapis.com/youtube/v3/search", {
        "part": "id", "type": "channel", "q": handle, "maxResults": 1, "key": api_key
    })
    items = data.get("items", [])
    if not items:
        raise RuntimeError(f"Could not find YouTube channel by handle '{handle}'.")
    return items[0]["id"]["channelId"]

async def resolve_live_chat_id_public_once(session: aiohttp.ClientSession, api_key: str, channel_id: str) -> str:
    srch = await _fetch_json(session, "https://www.googleapis.com/youtube/v3/search", {
        "part": "id", "channelId": channel_id, "eventType": "live",
        "type": "video", "maxResults": 1, "key": api_key
    })
    items = srch.get("items", [])
    if not items:
        raise RuntimeError("No public live right now.")
    video_id = items[0]["id"]["videoId"]
    vids = await _fetch_json(session, "https://www.googleapis.com/youtube/v3/videos", {
        "part": "liveStreamingDetails", "id": video_id, "key": api_key
    })
    vitems = vids.get("items", [])
    if not vitems:
        raise RuntimeError("No live video details found.")
    live_chat_id = (vitems[0].get("liveStreamingDetails") or {}).get("activeLiveChatId")
    if not live_chat_id:
        raise RuntimeError("No activeLiveChatId (not live yet or chat disabled).")
    print(f"[YouTube] Found liveChatId {live_chat_id} (video {video_id})")
    return live_chat_id

async def resolve_live_chat_id_from_video_id(session: aiohttp.ClientSession, api_key: str, video_id: str) -> str:
    vids = await _fetch_json(session, "https://www.googleapis.com/youtube/v3/videos", {
        "part": "liveStreamingDetails", "id": video_id, "key": api_key
    })
    vitems = vids.get("items", [])
    if not vitems:
        raise RuntimeError(f"Video {video_id} not found.")
    live_chat_id = (vitems[0].get("liveStreamingDetails") or {}).get("activeLiveChatId")
    if not live_chat_id:
        raise RuntimeError("No activeLiveChatId yet (not live or chat disabled).")
    print(f"[YouTube] Found liveChatId {live_chat_id} from video {video_id}")
    return live_chat_id

# ------------------ Twitch sender ------------------ #
class TwitchSender:
    def __init__(self):
        self.last_sent = 0.0

    async def send(self, session: aiohttp.ClientSession, text: str):
        # Needs broadcaster/sender + client-id
        if not (TWITCH_CLIENT_ID and TWITCH_BROADCASTER_ID and TWITCH_SENDER_ID):
            return
        # Get (or refresh) access token
        try:
            token = await AUTH.get_token(session)
        except Exception:
            return  # quiet in prod

        text = text[:500]  # Twitch max 500 chars
        wait = max(0.0, 1.0 - (time.time() - self.last_sent))  # 1 msg/s per channel
        if wait > 0:
            await asyncio.sleep(wait)

        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": TWITCH_CLIENT_ID,
            "Content-Type": "application/json"
        }
        payload = {
            "broadcaster_id": TWITCH_BROADCASTER_ID,
            "sender_id": TWITCH_SENDER_ID,
            "message": text
        }
        async with session.post(TWITCH_POST_URL, headers=headers, json=payload) as r:
            if r.status not in (200, 204):
                _ = await r.text()  # keep quiet in prod
        self.last_sent = time.time()

# ------------------ Workers ------------------ #
async def youtube_worker(session: aiohttp.ClientSession, twitch: TwitchSender):
    if not ENABLE_YT or not YOUTUBE_API_KEY:
        return

    global YOUTUBE_LIVE_CHAT_ID

    # Auto-resolve if not set or "AUTO"
    if not YOUTUBE_LIVE_CHAT_ID or YOUTUBE_LIVE_CHAT_ID.strip().upper() == "AUTO":
        if YOUTUBE_VIDEO_ID:
            printed = False
            while True:
                try:
                    YOUTUBE_LIVE_CHAT_ID = await resolve_live_chat_id_from_video_id(session, YOUTUBE_API_KEY, YOUTUBE_VIDEO_ID)
                    break
                except Exception:
                    if not printed:
                        print("[YouTube] Waiting for the unlisted video to actually go live…")
                        printed = True
                    await asyncio.sleep(30)
        else:
            channel_id = await resolve_youtube_channel_id(session, YOUTUBE_API_KEY, YOUTUBE_CHANNEL_ID, YOUTUBE_CHANNEL_HANDLE)
            printed = False
            while True:
                try:
                    YOUTUBE_LIVE_CHAT_ID = await resolve_live_chat_id_public_once(session, YOUTUBE_API_KEY, channel_id)
                    break
                except Exception:
                    if not printed:
                        print("[YouTube] Waiting for you to go live (public)…")
                        printed = True
                    await asyncio.sleep(30)

    page_token = None
    while True:
        params = {
            "part": "snippet,authorDetails",
            "liveChatId": YOUTUBE_LIVE_CHAT_ID,
            "key": YOUTUBE_API_KEY,
            "maxResults": 2000
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            async with session.get("https://www.googleapis.com/youtube/v3/liveChat/messages", params=params) as r:
                if r.status != 200:
                    # Quietly re-resolve on 403/404 (new stream)
                    if r.status in (403, 404):
                        if YOUTUBE_VIDEO_ID:
                            while True:
                                try:
                                    YOUTUBE_LIVE_CHAT_ID = await resolve_live_chat_id_from_video_id(session, YOUTUBE_API_KEY, YOUTUBE_VIDEO_ID)
                                    page_token = None
                                    break
                                except Exception:
                                    await asyncio.sleep(30)
                        else:
                            channel_id = await resolve_youtube_channel_id(session, YOUTUBE_API_KEY, YOUTUBE_CHANNEL_ID, YOUTUBE_CHANNEL_HANDLE)
                            while True:
                                try:
                                    YOUTUBE_LIVE_CHAT_ID = await resolve_live_chat_id_public_once(session, YOUTUBE_API_KEY, channel_id)
                                    page_token = None
                                    break
                                except Exception:
                                    await asyncio.sleep(30)
                        continue
                    await asyncio.sleep(10)
                    continue
                data = await r.json()
        except Exception:
            await asyncio.sleep(5)
            continue

        for item in data.get("items", []):
            author = item["authorDetails"].get("displayName", "?")
            msg = item["snippet"].get("displayMessage") or ""
            if msg:
                clean = html.unescape(msg)
                moderated = _moderate_text(clean)
                if moderated is None:
                    continue  # drop
                out = f"{PREFIX_YT}{author}: {moderated}"
                await twitch.send(session, out)

        page_token = data.get("nextPageToken")
        delay_ms = max(data.get("pollingIntervalMillis", 0), MIN_POLL_MS)
        await asyncio.sleep(delay_ms / 1000)

async def kick_worker(session: aiohttp.ClientSession, twitch: TwitchSender):
    if not ENABLE_KICK:
        return
    try:
        from kickpython import KickAPI
    except ImportError:
        return  # quiet in prod

    api = KickAPI()

    async def on_message(message):
        user = message.get("sender_username") or "?"
        content = message.get("content") or ""
        if content:
            moderated = _moderate_text(content)
            if moderated is None:
                return
            out = f"{PREFIX_KICK}{user}: {moderated}"
            await twitch.send(session, out)

    api.add_message_handler(on_message)

    while True:
        try:
            await api.connect_to_chatroom(KICK_CHANNEL)
            while True:
                await asyncio.sleep(60)
        except Exception:
            await asyncio.sleep(5)

async def main():
    twitch = TwitchSender()
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(
            _banned_words_watcher(),   # hot-reload banned words
            youtube_worker(session, twitch),
            kick_worker(session, twitch),
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass