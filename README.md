# بوت الدليل الذكي (dalil-bot)

بوت تلغرام يفهم اللهجة والأخطاء الإملائية ويبحث في دليل عناوين وأرقام.
Python + aiogram 3 + Postgres (Neon) + Gemini (تدوير مفاتيح) + Render.

## التشغيل
1. أنشئ البوت من @BotFather وخذ التوكن.
2. أنشئ قاعدة Postgres مجانية على neon.tech وانسخ Connection string.
3. خذ مفتاح Gemini أو أكثر من aistudio.google.com/apikey.
4. ارفع المشروع إلى GitHub، ثم في Render: New → Blueprint واختر المستودع.
5. أدخل المتغيرات في Render → Environment (الجدول أدناه).
6. افتح البوت، أرسل /start ثم /admin.

## متغيرات Render
| المتغير | ملاحظة |
|---|---|
| BOT_TOKEN | من BotFather |
| DATABASE_URL | رابط Neon |
| ADMIN_IDS | معرّفك الرقمي (من /myid) — هؤلاء "المالكون" |
| GEMINI_API_KEYS | مفاتيح مفصولة بفاصلة (ويمكن إدارتها لاحقاً من الدردشة) |
| WEBHOOK_SECRET | يُولَّد تلقائياً من render.yaml |
| GITHUB_TOKEN / GITHUB_REPO | اختياري: للتحكم بالكود من الدردشة |

الجداول تُنشأ تلقائياً عند أول تشغيل.
