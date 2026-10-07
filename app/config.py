import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

# المالكون (من البيئة) — لا يمكن حذفهم من الدردشة
OWNER_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
# كل الأدمنز: المالكون + من أُضيفوا من الدردشة (يُحدَّث في مكانه عند التشغيل)
ADMIN_IDS = set(OWNER_IDS)

GEMINI_KEYS = [k.strip() for k in os.getenv("GEMINI_API_KEYS", "").split(",") if k.strip()]
GEMINI_MODELS = [m.strip() for m in os.getenv("GEMINI_MODELS", "gemini-2.5-flash,gemini-2.5-flash-lite").split(",") if m.strip()]

BASE_URL = (os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me")
DEFAULT_CC = os.getenv("DEFAULT_CC", "963")
PORT = int(os.getenv("PORT", "10000"))

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GH_MAIN = os.getenv("GITHUB_MAIN", "main")
GH_STAGE = os.getenv("GITHUB_STAGE", "staging")
