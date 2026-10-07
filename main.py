import asyncio

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from app import db
from app.config import ADMIN_IDS, BASE_URL, BOT_TOKEN, PORT, WEBHOOK_SECRET
from app.handlers import admin, user

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
dp.include_router(admin)
dp.include_router(user)


_bg = []


async def _daily_cleanup():
    while True:
        try:
            await db.cleanup_searches(90)
        except Exception:
            pass
        await asyncio.sleep(86400)


async def on_startup(bot: Bot):
    if not BASE_URL:
        raise RuntimeError("WEBHOOK_BASE_URL أو RENDER_EXTERNAL_URL غير متوفر")
    await db.init()
    ADMIN_IDS.update(await db.load_admins())
    _bg.append(asyncio.create_task(_daily_cleanup()))
    await bot.set_webhook(f"{BASE_URL}/webhook", secret_token=WEBHOOK_SECRET, drop_pending_updates=True)


dp.startup.register(on_startup)

app = web.Application()
app.router.add_get("/", lambda r: web.Response(text="ok"))
SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path="/webhook")
setup_application(app, dp, bot=bot)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
    
