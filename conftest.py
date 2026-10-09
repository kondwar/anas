import os

# قيم وهمية لتمكين استيراد التطبيق في الاختبارات (config.py يتطلبها)؛ لا اتصال حقيقي يحدث.
os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
