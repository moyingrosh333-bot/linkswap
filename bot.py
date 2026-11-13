"""
Link Changer Bot
----------------
Tap "Change link" in Telegram, send a new URL, and the bot re-deploys your
Netlify landing page with that URL inside the "Continue to Website" button.

Visitors only ever load the page from Netlify, so it stays fast even if this
bot is asleep (Render free) or restarting.

Run modes (picked automatically):
  - Railway / your PC  -> long polling (no domain needed)
  - Render web service -> webhook (uses RENDER_EXTERNAL_URL)
"""

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ─────────────────────────── Settings ───────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.strip().isdigit()}
NETLIFY_TOKEN = os.getenv("NETLIFY_TOKEN", "").strip()
NETLIFY_SITE_ID = os.getenv("NETLIFY_SITE_ID", "").strip()  # e.g. mysite.netlify.app or the API ID
NETLIFY_API = os.getenv("NETLIFY_API", "https://api.netlify.com/api/v1").rstrip("/")
TEMPLATE_PATH = Path(os.getenv("TEMPLATE_PATH", str(Path(__file__).with_name("template.html"))))
WEBHOOK_BASE = (os.getenv("WEBHOOK_BASE") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))

PLACEHOLDER = "{{LINK}}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("linkbot")


# ─────────────────────────── Link helpers ───────────────────────────

def normalize_link(text: str) -> str | None:
    """Turn what the admin sent into a clean http(s) URL, or None if it is not a link."""
    t = (text or "").strip().strip("<>")
    if not t or len(t) > 2000 or any(c.isspace() for c in t):
        return None
    if not re.match(r"^[a-z][a-z0-9+.\-]*://", t, re.I):
        t = "https://" + t
    p = urlparse(t)
    if p.scheme.lower() not in ("http", "https"):
        return None
    host = p.hostname or ""
    if "." not in host or host.startswith(".") or host.endswith("."):
        return None
    return t


def link_from_message(message: Message) -> str | None:
    """Prefer a real link entity (works for hyperlinked text too), else the raw text."""
    text = message.text or message.caption or ""
    for ent in (message.entities or message.caption_entities or []):
        if ent.type == "text_link" and ent.url:
            return normalize_link(ent.url)
        if ent.type == "url":
            return normalize_link(ent.extract_from(text))
    return normalize_link(text)


HEADERS_FILE = (
    "/\n"
    "  Content-Type: text/html; charset=UTF-8\n"
    "  Cache-Control: public, max-age=0, must-revalidate\n"
    "/index.html\n"
    "  Content-Type: text/html; charset=UTF-8\n"
    "  Cache-Control: public, max-age=0, must-revalidate\n"
)


def build_site_files(link: str) -> dict[str, bytes]:
    """Fill the template with the link. Returns the files to upload to Netlify."""
    tpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    if PLACEHOLDER not in tpl:
        raise RuntimeError(f"template.html must contain {PLACEHOLDER}")
    page = tpl.replace(PLACEHOLDER, html.escape(link, quote=True))
    return {
        "index.html": page.encode("utf-8"),
        # Forces Netlify to serve the page as a web page, never as plain text.
        "_headers": HEADERS_FILE.encode("utf-8"),
    }


META_RE = re.compile(
    r"""<meta\s+name=["']link-target["']\s+content=["']([^"']*)["']""", re.IGNORECASE
)


def read_link_from_html(page: str) -> str | None:
    m = META_RE.search(page or "")
    if not m:
        return None
    value = html.unescape(m.group(1)).strip()
    return value if value.startswith(("http://", "https://")) else None


# ─────────────────────────── Netlify ───────────────────────────

class NetlifyError(Exception):
    pass


class Netlify:
    def __init__(self, token: str, site_id: str, api: str = NETLIFY_API):
        self.token = token
        self.site_id = site_id
        self.api = api
        self._session: aiohttp.ClientSession | None = None
        self._site_url: str | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _api(self, method: str, path: str, **kw) -> dict:
        s = await self.session()
        headers = {"Authorization": f"Bearer {self.token}", **kw.pop("headers", {})}
        try:
            async with s.request(method, f"{self.api}{path}", headers=headers, **kw) as r:
                body = await r.text()
                if r.status == 401:
                    raise NetlifyError("Netlify token is wrong or expired (NETLIFY_TOKEN).")
                if r.status == 404:
                    raise NetlifyError("Netlify site not found. Check NETLIFY_SITE_ID.")
                if r.status == 429:
                    raise NetlifyError("Netlify says too many deploys. Wait a minute and try again.")
                if r.status >= 400:
                    raise NetlifyError(f"Netlify error {r.status}: {body[:200]}")
                return json.loads(body) if body.strip() else {}
        except aiohttp.ClientError as e:
            raise NetlifyError(f"Could not reach Netlify: {e}") from e

    async def site(self) -> dict:
        data = await self._api("GET", f"/sites/{self.site_id}")
        self._site_url = (data.get("ssl_url") or data.get("url") or "").rstrip("/") or None
        return data

    async def site_url(self) -> str:
        if not self._site_url:
            await self.site()
        if not self._site_url:
            raise NetlifyError("Netlify did not return a URL for this site.")
        return self._site_url

    async def deploy_files(self, files: dict[str, bytes], title: str) -> dict:
        """Netlify's recommended 'file digest' deploy: send SHA1s, then upload what it asks for."""
        digests = {f"/{path}": hashlib.sha1(data).hexdigest() for path, data in files.items()}
        deploy = await self._api(
            "POST",
            f"/sites/{self.site_id}/deploys",
            params={"title": title[:200]},
            json={"files": digests},
        )
        deploy_id = deploy.get("id")
        if not deploy_id:
            raise NetlifyError("Netlify did not return a deploy ID.")
        required = set(deploy.get("required") or [])
        for path, data in files.items():
            if digests[f"/{path}"] in required:
                await self._api(
                    "PUT",
                    f"/deploys/{deploy_id}/files/{path}",
                    data=data,
                    headers={"Content-Type": "application/octet-stream"},
                )
        return deploy

    async def wait_ready(self, deploy_id: str, timeout: float = 120) -> dict:
        end = time.monotonic() + timeout
        while True:
            d = await self._api("GET", f"/deploys/{deploy_id}")
            state = d.get("state")
            if state == "ready":
                return d
            if state == "error":
                raise NetlifyError(f"Netlify deploy failed: {d.get('error_message') or 'unknown error'}")
            if time.monotonic() > end:
                raise NetlifyError(f"Netlify deploy is taking too long (state: {state}). Check Netlify.")
            await asyncio.sleep(1.5)

    async def restore(self, deploy_id: str) -> dict:
        return await self._api("POST", f"/sites/{self.site_id}/deploys/{deploy_id}/restore")

    async def live_link(self) -> str | None:
        """Read the link from the page visitors actually get right now."""
        url = await self.site_url()
        s = await self.session()
        try:
            async with s.get(
                f"{url}/?v={int(time.time() * 1000)}",
                headers={"Cache-Control": "no-cache"},
                allow_redirects=True,
            ) as r:
                if r.status >= 400:
                    raise NetlifyError(f"The live page returned error {r.status}.")
                return read_link_from_html(await r.text())
        except aiohttp.ClientError as e:
            raise NetlifyError(f"Could not open the live page: {e}") from e

    async def wait_live(self, expected: str, tries: int = 8) -> str | None:
        """Netlify switches almost at once, but give the CDN a few seconds."""
        live = None
        for _ in range(tries):
            live = await self.live_link()
            if live == expected:
                return live
            await asyncio.sleep(1.5)
        return live


netlify = Netlify(NETLIFY_TOKEN, NETLIFY_SITE_ID)
deploy_lock = asyncio.Lock()


# ─────────────────────────── Bot UI ───────────────────────────

class ChangeLink(StatesGroup):
    waiting_link = State()


def e(text: str) -> str:
    return html.escape(text or "", quote=False)


def menu_kb(site_url: str | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🔗 Change link", callback_data="change")]]
    second = [InlineKeyboardButton(text="👀 Current link", callback_data="current")]
    if site_url:
        second.append(InlineKeyboardButton(text="🌐 Open page", url=site_url))
    rows.append(second)
    return InlineKeyboardMarkup(inline_keyboard=rows)


CANCEL_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="✖️ Cancel", callback_data="cancel")]]
)

DEPLOY_ID_RE = re.compile(r"^[a-f0-9]{24}$")

router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


async def panel_text() -> tuple[str, str | None]:
    try:
        site_url = await netlify.site_url()
        live = await netlify.live_link()
    except NetlifyError as err:
        return f"🔗 <b>Link Changer</b>\n\n⚠️ {e(str(err))}", None
    now = e(live) if live else "<i>not set yet — the page still has the old template</i>"
    return (
        "🔗 <b>Link Changer</b>\n\n"
        f"<b>Page:</b> {e(site_url)}\n"
        f"<b>Button goes to:</b> {now}\n\n"
        "Tap <b>Change link</b> and send the new link."
    ), site_url


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text, site_url = await panel_text()
    await message.answer(text, reply_markup=menu_kb(site_url))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.", reply_markup=menu_kb(netlify._site_url))


@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Cancelled")
    await call.message.edit_text("Cancelled.", reply_markup=menu_kb(netlify._site_url))


@router.callback_query(F.data == "change")
async def cb_change(call: CallbackQuery, state: FSMContext):
    await state.set_state(ChangeLink.waiting_link)
    await call.answer()
    await call.message.answer(
        "Send me the new link.\n\nExample: <code>https://yourshop.com</code>",
        reply_markup=CANCEL_KB,
    )


@router.callback_query(F.data == "current")
async def cb_current(call: CallbackQuery):
    await call.answer("Checking…")
    text, site_url = await panel_text()
    await call.message.answer(text, reply_markup=menu_kb(site_url))


@router.message(StateFilter(ChangeLink.waiting_link))
async def got_link(message: Message, state: FSMContext):
    link = link_from_message(message)
    if not link:
        await message.answer(
            "That doesn't look like a link. Send something like <code>https://yourshop.com</code>",
            reply_markup=CANCEL_KB,
        )
        return
    await state.clear()
    await apply_link(message, link)


async def apply_link(message: Message, link: str):
    if deploy_lock.locked():
        await message.answer("⏳ Another change is still running. Wait a few seconds.")
        return
    async with deploy_lock:
        status = await message.answer(f"⏳ Putting the new link live…\n{e(link)}")
        try:
            site = await netlify.site()
            prev_deploy = (site.get("published_deploy") or {}).get("id") or ""
            try:
                old = await netlify.live_link()
            except NetlifyError:
                old = None
            deploy = await netlify.deploy_files(build_site_files(link), title=f"Link: {link}")
            await netlify.wait_ready(deploy["id"])
            live = await netlify.wait_live(link)
        except NetlifyError as err:
            log.warning("deploy failed: %s", err)
            await status.edit_text(f"❌ Link not changed.\n\n{e(str(err))}", reply_markup=menu_kb(netlify._site_url))
            return
        except (RuntimeError, OSError) as err:
            log.exception("deploy failed")
            await status.edit_text(f"❌ Link not changed.\n\n{e(str(err))}", reply_markup=menu_kb(netlify._site_url))
            return

        site_url = netlify._site_url
        if live == link:
            text = (
                "✅ <b>Link changed.</b>\n\n"
                f"<b>New:</b> {e(link)}\n"
                f"<b>Old:</b> {e(old) if old else '—'}\n\n"
                "Anyone who taps the button now goes to the new link."
            )
        else:
            text = (
                "⚠️ Netlify finished the deploy, but the live page still shows:\n"
                f"{e(live) if live else '(no link found)'}\n\n"
                "Check that <b>auto publishing</b> is not locked in Netlify → Deploys."
            )
        rows = []
        if DEPLOY_ID_RE.match(prev_deploy):
            rows.append([InlineKeyboardButton(text="↩️ Undo", callback_data=f"undo:{prev_deploy}")])
        rows += menu_kb(site_url).inline_keyboard
        await status.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("undo:"))
async def cb_undo(call: CallbackQuery):
    deploy_id = call.data.split(":", 1)[1]
    if not DEPLOY_ID_RE.match(deploy_id):
        await call.answer("Bad undo data", show_alert=True)
        return
    if deploy_lock.locked():
        await call.answer("Another change is running. Try again in a few seconds.", show_alert=True)
        return
    await call.answer("Undoing…")
    async with deploy_lock:
        try:
            await netlify.restore(deploy_id)
            await asyncio.sleep(1.5)
            live = await netlify.live_link()
        except NetlifyError as err:
            await call.message.answer(f"❌ Undo failed.\n\n{e(str(err))}", reply_markup=menu_kb(netlify._site_url))
            return
    shown = e(live) if live else "the old page (from before the bot)"
    await call.message.answer(
        f"↩️ <b>Undone.</b> The button now goes to:\n{shown}",
        reply_markup=menu_kb(netlify._site_url),
    )


@router.message()
async def fallback(message: Message):
    text, site_url = await panel_text()
    await message.answer(text, reply_markup=menu_kb(site_url))


# ─────────────────────────── Startup ───────────────────────────

def check_config():
    missing = [
        name
        for name, val in [
            ("BOT_TOKEN", BOT_TOKEN),
            ("ADMIN_IDS", ADMIN_IDS),
            ("NETLIFY_TOKEN", NETLIFY_TOKEN),
            ("NETLIFY_SITE_ID", NETLIFY_SITE_ID),
        ]
        if not val
    ]
    if missing:
        log.error("Missing settings: %s. Add them as environment variables.", ", ".join(missing))
        sys.exit(1)
    if not TEMPLATE_PATH.exists():
        log.error("template.html not found at %s", TEMPLATE_PATH)
        sys.exit(1)


def make_bot_and_dp() -> tuple[Bot, Dispatcher]:
    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = Dispatcher()
    dp.include_router(router)

    async def on_startup(bot: Bot):
        await bot.set_my_commands([
            BotCommand(command="start", description="Open the link panel"),
            BotCommand(command="cancel", description="Cancel"),
        ])

    async def on_shutdown():
        await netlify.close()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    return bot, dp


async def run_polling():
    bot, dp = make_bot_and_dp()
    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Starting in polling mode")
    await dp.start_polling(bot)


def build_webhook_app(bot: Bot, dp: Dispatcher) -> web.Application:
    url = f"{WEBHOOK_BASE}/webhook"
    secret = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]

    async def set_hook(bot: Bot):
        info = await bot.get_webhook_info()
        if info.url != url:
            await bot.set_webhook(url, secret_token=secret)
            log.info("Webhook set to %s", url)

    dp.startup.register(set_hook)

    app = web.Application()

    async def health(_):
        return web.Response(text="ok")

    app.router.add_get("/", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    return app


def run_webhook():
    bot, dp = make_bot_and_dp()
    app = build_webhook_app(bot, dp)
    log.info("Starting in webhook mode on port %s", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    check_config()
    if WEBHOOK_BASE:
        run_webhook()
    else:
        asyncio.run(run_polling())
