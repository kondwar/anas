import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

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


log = logging.getLogger("dalil.main")
_bg = []


async def _daily_cleanup():
    while True:
        try:
            await db.cleanup_searches(90)
        except Exception:
            log.warning("daily cleanup failed", exc_info=True)
        await asyncio.sleep(86400)


async def on_startup(bot: Bot):
    if not BASE_URL:
        raise RuntimeError("WEBHOOK_BASE_URL أو RENDER_EXTERNAL_URL غير متوفر")
    await db.init()
    ADMIN_IDS.update(await db.load_admins())
    _bg.append(asyncio.create_task(_daily_cleanup()))
    # لا نُسقط الرسائل المعلقة: على الخطة المجانية قد تكون الرسالة التي أيقظت الخدمة بينها
    await bot.set_webhook(f"{BASE_URL}/webhook", secret_token=WEBHOOK_SECRET, drop_pending_updates=False)


async def on_shutdown(bot: Bot):
    for t in _bg:
        t.cancel()
    if db.pool is not None:
        try:
            await asyncio.wait_for(db.pool.close(), 10)
        except Exception:
            log.warning("db pool close failed", exc_info=True)


dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)

app = web.Application()
app.router.add_get("/", lambda r: web.Response(text="ok"))
SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path="/webhook")
setup_application(app, dp, bot=bot)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
    
