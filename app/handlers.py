import asyncio
import csv
import difflib
import html
import io
import logging
import os
import re
import secrets
import time
from collections import deque
from urllib.parse import quote, urlencode

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup as M

from . import ai, bulk, db
from . import phones as ph
from . import cardfmt as cf
from . import coding_agent
from . import lexicon as lex
from . import github as gh
from .config import ADMIN_IDS, BASE_URL, DEFAULT_CC, OWNER_IDS

esc = html.escape
log = logging.getLogger("dalil.handlers")
admin, user = Router(), Router()


def is_admin(e):
    return bool(e.from_user and e.from_user.id in ADMIN_IDS)


admin.message.filter(is_admin)
admin.callback_query.filter(is_admin)


class S(StatesGroup):
    add = State()      # المستخدم يكتب بيانات البطاقة
    preview = State()  # معاينة بطاقة المستخدم قبل الإرسال للإدارة
    dedit = State()    # المستخدم يعدّل حقلاً في المعاينة
    aedit = State()    # الأدمن يعدّل حقلاً في بطاقة منشورة/معلقة
    upd = State()      # طلب تحديث بيانات بطاقة
    bulk = State()     # الأدمن سيلصق قائمة عناوين كثيرة دفعة واحدة


def wa_link(n):
    """رابط واتساب، أو None إذا لم يكن الرقم صالحاً (بدل رابط معطوب يُفشل إرسال البطاقة)."""
    d = "".join(c for c in (n or "") if c.isdigit())
    if d.startswith("00"):
        d = d[2:]
    elif d.startswith("0"):
        d = DEFAULT_CC + d[1:]
    return f"https://wa.me/{d}" if 7 <= len(d) <= 15 else None


_PH_SPLIT = re.compile(r"\s*[,،/|;\n]\s*|\s+[-–—]\s+|\s+و\s+")
_PH_NUM = re.compile(r"\+?\d[\d ().\-]{5,}\d")
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _intl(m):
    """رقم بصيغة دولية +963...؛ يبقى النص كما هو إن لم يبدُ رقم هاتف."""
    raw = m.group(0)
    known = ph.classify(raw)["intl"]   # أرقام سوريا المعروفة تُصحَّح كاملة (مثل +963 0933 ← +963933)
    if known:
        return known
    d = "".join(c for c in raw if c.isdigit())
    if not 7 <= len(d) <= 15:
        return raw
    if raw.lstrip().startswith("+"):
        return "+" + d
    if d.startswith("00"):
        return "+" + d[2:]
    if d.startswith(DEFAULT_CC) and len(d) >= 11:
        return "+" + d
    if d.startswith("0"):
        return "+" + DEFAULT_CC + d[1:]
    if len(d) == 9:
        return "+" + DEFAULT_CC + d
    return "+" + d if len(d) >= 10 else raw


def _badge(m):
    """الرقم بالصيغة الدولية مسبوقاً بإيموجي نوعه (☎️ أرضي، 🔴 سيرياتل، 🟡 MTN، 🟣 زين، 📱 جوال آخر)."""
    num = _intl(m)
    em = ph.emoji_for(num)
    return f"{em} {num}" if em else num


def fmt_phones(s) -> str:
    """يحوّل كل رقم في الحقل إلى الصيغة الدولية ليتعرف عليه تلغرام ويجعله قابلاً للضغط (اتصال)،
    ويضع قبله إيموجي يدل على نوعه (أرضي أو مشغّل الجوال)."""
    parts = [x for x in _PH_SPLIT.split((s or "").translate(_AR_DIGITS)) if x.strip()]
    return " · ".join(_PH_NUM.sub(_badge, x.strip()) for x in parts)


def wa_number(e):
    """رقم واتساب البطاقة: الحقل المحدد يدوياً أولاً، وإلا أول رقم جوال في حقل الهاتف (تلقائياً)."""
    return e.get("whatsapp") or ph.auto_whatsapp(e.get("phone")) or None


def _int_env(name, default):
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


_RATE_WINDOW = 60.0
_RATE_MAX = _int_env("SEARCH_RATE_PER_MIN", 8)  # أقصى عدد بحوث للمستخدم العادي في الدقيقة
_hits: dict = {}


def rate_ok(uid) -> bool:
    """حد معدل بسيط في الذاكرة: يحمي حصص مفاتيح الـ AI المجانية من الإغراق."""
    now = time.monotonic()
    q = _hits.setdefault(uid, deque())
    while q and now - q[0] > _RATE_WINDOW:
        q.popleft()
    if len(q) >= _RATE_MAX:
        return False
    q.append(now)
    if len(_hits) > 5000:  # تنظيف دوري حتى لا تكبر الذاكرة
        for k in [k for k, v in _hits.items() if not v or now - v[-1] > _RATE_WINDOW]:
            _hits.pop(k, None)
    return True


BOT_USERNAME = ""  # يُضبط عند التشغيل (main.py) لبناء رابط مشاركة البطاقة


def card(e, adm=False):
    """بطاقة الدليل بالقالب المعتمد (انظر app/cardfmt.py)."""
    return cf.render_html(e, fmt_phones, adm)


def share_url(e):
    """رابط «مشاركة»: يفتح نافذة اختيار المحادثة في تلغرام بنص البطاقة ورابط يفتحها في البوت."""
    if not BOT_USERNAME or e.get("status") != "approved" or not e.get("id"):
        return None
    link = f"https://t.me/{BOT_USERNAME}?start=e{e['id']}"
    return "https://t.me/share/url?" + urlencode({"url": link, "text": cf.render_plain(e, fmt_phones)},
                                                 quote_via=quote)


def share_links(e):
    """روابط المشاركة لكل وسيلة: تلغرام، واتساب، فيسبوك، X. نص البطاقة + رابط يفتحها في البوت."""
    tg = share_url(e)
    if not tg:
        return {}
    link = f"https://t.me/{BOT_USERNAME}?start=e{e['id']}"
    text = cf.render_plain(e, fmt_phones)
    return {
        "tg": tg,
        "wa": "https://wa.me/?text=" + quote(f"{text}\n{link}", safe=""),
        "fb": "https://www.facebook.com/sharer/sharer.php?u=" + quote(link, safe=""),
        "x": "https://twitter.com/intent/tweet?" + urlencode({"text": text[:200], "url": link}, quote_via=quote),
    }


def share_menu_kb(e):
    ln = share_links(e)
    rows = []
    if ln:
        rows += [[B(text="✈️ تلغرام", url=ln["tg"]), B(text="🟢 واتساب", url=ln["wa"])],
                 [B(text="📘 فيسبوك", url=ln["fb"]), B(text="🐦 X (تويتر)", url=ln["x"])]]
    rows.append([B(text="📋 نص للنسخ (أي تطبيق آخر)", callback_data=f"sh:{e['id']}:c")])
    rows.append([B(text="⬅️ رجوع", callback_data=f"sh:{e['id']}:b")])
    return M(inline_keyboard=rows)


def map_url(e):
    """رابط خرائط جوجل الرسمي الشامل: يفتح تطبيق الخرائط مباشرة إن وُجد."""
    return f"https://www.google.com/maps/search/?api=1&query={e['lat']},{e['lng']}"


def go_url(kind, e, direct):
    """رابط يمر عبر صفحة التحويل في السيرفر (app/redirect.py) لتفتح التطبيق فوراً بدل صفحة وسيطة.
    للبطاقات المنشورة فقط، ويرجع للرابط المباشر إن لم يتوفر عنوان السيرفر."""
    if BASE_URL and e.get("id") and e.get("status") == "approved":
        return f"{BASE_URL}/go/{kind}/{e['id']}"
    return direct


def kb(e, adm=False):
    """أزرار البطاقة: سطر الاتصال (واتساب/خريطة) ثم سطر الإجراءات. بحد أقصى سطران للمستخدم."""
    contact = []
    published = bool(BASE_URL and e.get("id") and e.get("status") == "approved")
    # تلغرام لا يقبل روابط tel: في الأزرار، فيمر الاتصال عبر صفحة التحويل (للبطاقات المنشورة فقط)
    if published and ph.call_numbers(e.get("phone")):
        contact.append(B(text="📞 اتصال", url=f"{BASE_URL}/go/tel/{e['id']}"))
    wa = wa_link(wa_number(e))
    if wa:
        contact.append(B(text="💬 واتساب", url=go_url("wa", e, wa)))
    if e["lat"] is not None and e["lng"] is not None:
        contact.append(B(text="🗺 فتح الخريطة", url=go_url("map", e, map_url(e))))
    actions = [B(text=f"👍 {e['likes']}", callback_data=f"r:like:{e['id']}")]
    if share_url(e):
        actions.append(B(text="📤 مشاركة", callback_data=f"sh:{e['id']}:m"))
    rows = [contact] if contact else []
    if adm:
        rows.append(actions)
        rows.append([B(text="✏️ تعديل", callback_data=f"ae:{e['id']}:m"),
                     B(text="🗑 حذف", callback_data=f"ae:{e['id']}:x")])
    else:
        actions.append(B(text="📝 طلب تحديث", callback_data=f"u:{e['id']}"))
        rows.append(actions)
    return M(inline_keyboard=rows)


def decision_kb(eid):
    return M(inline_keyboard=[[B(text="✅ قبول", callback_data=f"a:ok:{eid}"),
                               B(text="❌ رفض", callback_data=f"a:no:{eid}")],
                              [B(text="✏️ تعديل", callback_data=f"ae:{eid}:m"),
                               B(text="🗑 حذف", callback_data=f"ae:{eid}:x")]])


def admin_kb(e):
    """لوحة الأدمن لأي بطاقة: المعلقة (قبول/رفض/تعديل/حذف) وغيرها (كل أزرار البطاقة + تعديل/حذف)."""
    return decision_kb(e["id"]) if e.get("status") == "pending" else kb(e, True)


def _pairs(btns):
    return [btns[i:i + 2] for i in range(0, len(btns), 2)]


def edit_menu_kb(eid):
    rows = _pairs([B(text="✏️ " + lbl, callback_data=f"ae:{eid}:{k}") for k, lbl in cf.FIELDS])
    rows.append([B(text="⬅️ رجوع", callback_data=f"ae:{eid}:b")])
    return M(inline_keyboard=rows)


def draft_kb(d):
    rows = _pairs([B(text=("✏️ " if cf.filled(d, k) else "➕ ") + lbl, callback_data=f"d:e:{k}")
                   for k, lbl in cf.FIELDS])
    rows.append([B(text="✅ إرسال للإدارة", callback_data="d:send"), B(text="❌ إلغاء", callback_data="d:cancel")])
    return M(inline_keyboard=rows)


def preview_text(d):
    t = ("🧾 <b>معاينة بطاقتك</b>\nهكذا ستظهر بعد موافقة الإدارة. عدّل أي حقل بالأزرار، "
         "ثم اضغط «إرسال للإدارة».\n\n" + card(d))
    miss = cf.missing(d)
    if miss:
        t += "\n\n⚠️ <b>معلومات ناقصة:</b> " + "، ".join(miss) + "\n(الأزرار ➕ أدناه لإضافتها)"
    return t


async def show_preview(m: Message, d):
    await m.answer(preview_text(d), reply_markup=draft_kb(d), disable_web_page_preview=True)


def _is_cancel(m: Message, admin=False) -> bool:
    r = lex.match(m.text or "", admin=admin)
    return bool(r and r.deterministic and r.intent == "cancel")


# =============================== المستخدمون ===============================
@user.message(CommandStart())
async def start(m: Message, command: CommandObject = None):
    await db.touch_user(m.from_user)
    arg = (command.args if command else None) or ""
    if arg[:1] == "e" and arg[1:].isdigit():  # رابط مشاركة بطاقة: /start e12
        e = await db.get(int(arg[1:]))
        adm = m.from_user.id in ADMIN_IDS
        if e and (e["status"] == "approved" or adm):
            await db.add_views([e["id"]])
            e = dict(e)
            e["views"] += 1
            return await m.answer(card(e, adm), reply_markup=kb(e, adm), disable_web_page_preview=True)
    await m.answer("أهلاً بك في دليل سوريا الذكي، ابحث عن أي شيء تريده بسهولة.\n"
                   "كما يمكنك إضافة بطاقات للأشخاص والأماكن والفعاليات والتعديل عليها من خلال التحدث مع البوت.")


@user.message(Command("myid"))
async def myid(m: Message):
    await m.answer(f"معرّفك: <code>{m.from_user.id}</code>")


@user.message(Command("add"))
async def add_cmd(m: Message, state: FSMContext):
    await state.set_state(S.add)
    await m.answer("اكتب معلومات المكان بأي صيغة (الاسم، النوع، المدينة، العنوان، الهاتف، الإحداثيات...)\n/cancel للإلغاء")


@user.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("تم الإلغاء")


@user.message(S.add, F.text, ~F.text.startswith("/"))
async def add_got(m: Message, state: FSMContext, bot: Bot):
    r = lex.match(m.text, admin=m.from_user.id in ADMIN_IDS)
    if r and r.deterministic and r.intent == "cancel":  # «الغاء / كنسل» داخل خطوة الإضافة
        return await cancel(m, state)
    await save_entry(m, state, bot, m.text)


async def save_entry(m: Message, state: FSMContext, bot: Bot, text: str):
    adm = m.from_user.id in ADMIN_IDS
    if not adm and not rate_ok(m.from_user.id):
        return await m.answer("⏳ أرسلت طلبات كثيرة بسرعة، انتظر دقيقة ثم حاول مرة ثانية.")
    d = await ai.parse_entry(text)
    if not d or not d.get("name"):
        return await m.answer("ما قدرت أفهم الاسم، أعد الكتابة بوضوح أكثر أو جرّب بعد قليل.")
    d = cf.clean_draft(d)
    if adm:  # الأدمن: نشر مباشر
        eid = await db.add_entry(d, m.from_user.id, "approved")
        await state.clear()
        e = await db.get(eid)
        return await m.answer("✅ أُضيف ونُشر:\n" + card(e, True), reply_markup=admin_kb(e),
                              disable_web_page_preview=True)
    # المستخدم العادي: معاينة قابلة للتعديل ثم إرسال للإدارة للتأكيد
    await state.set_state(S.preview)
    await state.update_data(draft=d)
    await show_preview(m, d)


@user.callback_query(F.data.startswith("d:"))
async def draft_cb(cb: CallbackQuery, state: FSMContext, bot: Bot):
    parts = cb.data.split(":")
    d = (await state.get_data()).get("draft")
    if not d:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await cb.answer("انتهت الجلسة، أرسل طلب الإضافة من جديد.", show_alert=True)
    act = parts[1] if len(parts) > 1 else ""
    if act == "e" and len(parts) > 2 and parts[2] in cf.LABEL:
        f = parts[2]
        await state.set_state(S.dedit)
        await state.update_data(field=f)
        hint = "\nأو أرسل 📎 الموقع مباشرة." if f == "location" else ""
        await cb.message.answer(f"✏️ أرسل <b>{cf.LABEL[f]}</b> الجديد.{hint}\nأرسل «-» لحذف القيمة.",
                                reply_markup=M(inline_keyboard=[[B(text="↩️ رجوع للمعاينة", callback_data="d:back")]]))
    elif act == "back":
        await state.set_state(S.preview)
        await show_preview(cb.message, d)
    elif act == "cancel":
        await state.clear()
        try:
            await cb.message.edit_text("تم إلغاء الطلب.")
        except Exception:
            pass
    elif act == "send":
        if not d.get("name"):
            return await cb.answer("الاسم مطلوب.", show_alert=True)
        eid = await db.add_entry(d, cb.from_user.id, "pending")
        await state.clear()  # يمنع الإرسال المزدوج
        e = await db.get(eid)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.message.answer("✅ أُرسلت بطاقتك للإدارة، وستظهر في الدليل بعد الموافقة. شكراً لك 🌹")
        for a in list(ADMIN_IDS):
            try:
                await bot.send_message(a, "🆕 طلب إضافة:\n" + card(e, True), reply_markup=admin_kb(e),
                                       disable_web_page_preview=True)
            except Exception:
                pass
    await cb.answer()


async def _draft_set(m: Message, state: FSMContext, d: dict):
    await state.update_data(draft=d)
    await state.set_state(S.preview)
    await show_preview(m, d)


@user.message(S.dedit, F.text, ~F.text.startswith("/"))
async def draft_value(m: Message, state: FSMContext):
    if _is_cancel(m):
        return await cancel(m, state)
    data = await state.get_data()
    d, field = data.get("draft"), data.get("field")
    if not d or not field:
        await state.clear()
        return await m.answer("انتهت الجلسة، أرسل طلب الإضافة من جديد.")
    err = cf.apply_edit(d, field, m.text)
    if err:
        return await m.answer("⚠️ " + err)
    await _draft_set(m, state, d)


@user.message(S.dedit, F.location)
async def draft_location(m: Message, state: FSMContext):
    data = await state.get_data()
    d = data.get("draft")
    if not d or data.get("field") != "location":
        return await m.answer("اختر زر «الموقع على الخريطة» أولاً.")
    d["lat"], d["lng"] = m.location.latitude, m.location.longitude
    await _draft_set(m, state, d)


@user.message(S.preview, F.text, ~F.text.startswith("/"))
async def preview_hint(m: Message, state: FSMContext):
    if _is_cancel(m):
        return await cancel(m, state)
    d = (await state.get_data()).get("draft")
    if d:
        await m.answer("استخدم الأزرار أسفل المعاينة لتعديل بطاقتك ثم «إرسال للإدارة» (/cancel للإلغاء).")
        await show_preview(m, d)


# ---- طلب تحديث بيانات بطاقة منشورة ----
@user.callback_query(F.data.startswith("u:"))
async def upd_request(cb: CallbackQuery, state: FSMContext):
    try:
        eid = int(cb.data.split(":")[1])
    except (IndexError, ValueError):
        return await cb.answer()
    e = await db.get(eid)
    if not e or e["status"] != "approved":
        return await cb.answer("البطاقة غير متاحة.", show_alert=True)
    await state.set_state(S.upd)
    await state.update_data(eid=eid)
    await cb.message.answer(f"📝 اكتب البيانات التي تريد تحديثها في بطاقة <b>{esc(e['name'])}</b> "
                            "(مثال: الدوام الجديد، رقم جديد...)\n/cancel للإلغاء")
    await cb.answer()


@user.message(S.upd, F.text, ~F.text.startswith("/"))
async def upd_text(m: Message, state: FSMContext, bot: Bot):
    if _is_cancel(m):
        return await cancel(m, state)
    if not rate_ok(m.from_user.id):
        return await m.answer("⏳ أرسلت طلبات كثيرة بسرعة، انتظر دقيقة ثم حاول مرة ثانية.")
    eid = (await state.get_data()).get("eid")
    await state.clear()
    e = await db.get(eid) if eid else None
    if not e:
        return await m.answer("البطاقة لم تعد موجودة.")
    note = f"🔄 <b>طلب تحديث بيانات</b> من <code>{m.from_user.id}</code>:\n{esc(m.text[:500])}\n\n"
    for a in list(ADMIN_IDS):
        try:
            await bot.send_message(a, note + card(e, True), reply_markup=admin_kb(e), disable_web_page_preview=True)
        except Exception:
            pass
    await m.answer("✅ وصل طلبك للإدارة وسنراجعه قريباً.")


@user.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def on_text(m: Message, state: FSMContext, bot: Bot):
    r = lex.match(m.text, admin=False)
    if r and r.deterministic and await run_common(m, state, bot, r):
        return
    await do_search(m, m.text)


async def run_common(m: Message, state: FSMContext, bot: Bot, r) -> bool:
    """ينفّذ النوايا المشتركة التي يحددها القاموس (بدل /add و/start و/myid و/cancel والبحث)."""
    it = r.intent
    if it == "thanks":
        await m.answer("العفو 🌹 أنا بالخدمة.")
    elif it == "help":
        if m.from_user.id in ADMIN_IDS:
            await admin_help(m)
        else:
            await start(m)
    elif it == "admin_help":
        await admin_help(m)
    elif it == "myid":
        await myid(m)
    elif it == "cancel":
        await cancel(m, state)
    elif it == "add":
        words = r.rest.split()
        if len(words) >= lex.min_words() or any(c.isdigit() for c in r.rest):
            await save_entry(m, state, bot, r.rest)  # أضاف التفاصيل في نفس الرسالة
        else:
            await add_cmd(m, state)
    elif it == "search":
        await do_search(m, r.rest or m.text)
    else:
        return False
    return True


async def do_search(m: Message, text: str):
    if m.from_user.id not in ADMIN_IDS and not rate_ok(m.from_user.id):
        return await m.answer("⏳ أرسلت طلبات كثيرة بسرعة، انتظر دقيقة ثم حاول مرة ثانية.")
    text = (text or "").strip()[:300]
    await db.touch_user(m.from_user)
    try:
        vocab = await db.vocab()
    except Exception:  # القوائم تحسين فقط: فشلها لا يمنع البحث
        log.warning("search: vocab unavailable")
        vocab = None
    p = await ai.parse_search(text, vocab)
    rows = await db.search(p["name"], p["category"], p["city"], keywords=p.get("keywords"))
    await db.log_search(m.from_user.id, text, len(rows))
    if not rows:
        return await m.answer("ما لقيت نتائج. جرّب صياغة ثانية، أو أضف المكان بـ /add")
    await db.add_views([r["id"] for r in rows])
    if p["city"] and not any(p["city"] in (r["search_text"] or "") for r in rows):
        # db.search وسّع البحث بدون المدينة: لا نُوهم المستخدم أن النتائج من مدينته
        await m.answer(f"ما لقيت نتائج في {esc(p['city'])}، هذه أقرب النتائج من أماكن أخرى:")
    adm = m.from_user.id in ADMIN_IDS
    for r in rows:
        r = dict(r)
        r["views"] += 1
        try:  # فشل بطاقة واحدة لا يوقف باقي النتائج
            await m.answer(card(r, adm), reply_markup=kb(r, adm), disable_web_page_preview=True)
        except Exception:
            log.warning("search: failed to send entry %s", r.get("id"), exc_info=True)
    try:
        ad = await db.pick_ad(p["category"])
        if ad:
            t = f"📢 <b>إعلان</b>\n<b>{esc(ad['title'] or '')}</b>\n{esc(ad['text'] or '')}"
            if ad["phone"]:
                t += f"\n📞 {esc(fmt_phones(ad['phone']))}"
            link = wa_link(ad["whatsapp"]) if ad["whatsapp"] else None
            btn = [[B(text="💬 واتساب", url=link)]] if link else []
            await m.answer(t, reply_markup=M(inline_keyboard=btn) if btn else None)
    except Exception:  # الإعلان تحسين فقط
        log.warning("search: ad failed", exc_info=True)


@user.callback_query(F.data.startswith("sh:"))
async def share_cb(cb: CallbackQuery):
    try:
        _, eid, act = cb.data.split(":")
        e = await db.get(int(eid))
    except ValueError:
        return await cb.answer()
    adm = cb.from_user.id in ADMIN_IDS
    if not e or (e["status"] != "approved" and not adm):
        return await cb.answer("البطاقة غير متاحة.", show_alert=True)
    e = dict(e)
    if act == "c":  # نص جاهز للنسخ/إعادة التوجيه إلى أي تطبيق
        await cb.message.answer(cf.render_plain(e, fmt_phones) + f"\n\nhttps://t.me/{BOT_USERNAME}?start=e{e['id']}",
                                disable_web_page_preview=True, parse_mode=None)
        return await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=share_menu_kb(e) if act == "m" else kb(e, adm))
    except Exception:
        pass
    await cb.answer()


@user.callback_query(F.data.startswith("r:"))
async def react(cb: CallbackQuery):
    _, kind, eid = cb.data.split(":")
    if kind != "like":  # زر القلب أُلغي (أزرار الرسائل القديمة قد تحمله)
        return await cb.answer("هذا الزر لم يعد متاحاً.")
    on = await db.toggle(int(eid), cb.from_user.id, kind)
    e = await db.get(int(eid))
    try:
        await cb.message.edit_reply_markup(reply_markup=kb(e, cb.from_user.id in ADMIN_IDS))
    except Exception:
        pass
    await cb.answer("تم ✔️" if on else "أُلغي")


# ================================ الإدارة ================================
@admin.message(Command("admin"))
async def admin_help(m: Message):
    t = ("<b>الأدمن</b> — اكتب طلبك بالعربي مباشرة (مثل: احصائيات، وافق 12، ارفض 12، احذف 12، اعرض 12)، أو استخدم الأوامر:\n"
         "✏️ كل بطاقة تظهر لك فيها أزرار «تعديل» (أي حقل) و«حذف».\n"
         "/stats — الإحصائيات\n/pending — الطلبات المعلقة\n"
         "/edit id حقل قيمة — الحقول: name category city address phone whatsapp description hours lat lng status\n"
         "/boost id رقم — ترتيب النتيجة (الأعلى أولاً)\n/del id — حذف\n"
         "/ad تصنيف | عنوان | نص | هاتف | واتساب(اختياري)\n/ads — الإعلانات\n/adoff id — إيقاف إعلان\n"
         "📥 /bulk — الصق قائمة عناوين كثيرة (أو الصقها مباشرة) فتتحول لبطاقات منفردة وتُحفظ بزر واحد\n"
         "ملف CSV مع التعليق /import — استيراد جماعي\n"
         "أعمدة CSV: name,category,city,address,phone,whatsapp,lat,lng,description\n/add — إضافة مباشرة")
    if m.from_user.id in OWNER_IDS:
        t += ("\n\n<b>المالك فقط</b>\n/addadmin id · /deladmin id · /admins\n"
              "/keys · /delkey رقم · /models اسم1,اسم2 (والصق مفتاح Gemini هنا لإضافته)\n"
              "/ghls [مجلد] · /ghshow مسار · /ghedit مسار | تعليمات · /ghcode وصف التعديل · /ghdel مسار\n"
              "ملف مع التعليق /push مسار — رفع ملف\n/deploy — نشر · /ghreset — تجاهل التغييرات المعلقة")
    await m.answer(t)


@admin.message(Command("stats"))
async def stats(m: Message):
    # أرقام حقيقية من القاعدة فقط — لا يمر هذا الأمر على الذكاء الاصطناعي إطلاقاً
    try:
        s = await db.stats()
    except Exception:
        log.exception("stats: db error")
        return await m.answer("⚠️ تعذّر قراءة الإحصائيات من قاعدة البيانات الآن. حاول بعد قليل.")
    t = (f"👥 المستخدمون: {s['users']}\n📦 المنشور: {s['approved']} | ⏳ المعلق: {s['pending']}\n"
         f"🔎 البحوث: {s['searches']} (آخر 24 ساعة: {s['today']})\n\n<b>أكثر البحوث:</b>\n")
    t += "\n".join(f"• {esc((r['query'] or '')[:60])} ({r['c']})" for r in s["top_q"]) or "—"
    t += "\n\n<b>الأكثر مشاهدة:</b>\n" + ("\n".join(
        f"• #{r['id']} {esc(r['name'])} ({r['views']})" for r in s["top_e"]) or "—")
    await m.answer(t)


@admin.message(Command("pending"))
async def pending(m: Message):
    # مباشرة من القاعدة — لا يعتمد على الذكاء الاصطناعي
    try:
        rows = await db.pool.fetch(db.BASE + " WHERE e.status='pending' ORDER BY e.id LIMIT 10")
    except Exception:
        log.exception("pending: db error")
        return await m.answer("⚠️ تعذّر قراءة الطلبات المعلقة من قاعدة البيانات الآن. حاول بعد قليل.")
    if not rows:
        return await m.answer("لا توجد طلبات معلقة")
    for e in rows:
        try:
            await m.answer(card(e, True), reply_markup=admin_kb(e), disable_web_page_preview=True)
        except Exception:  # فشل إرسال بطاقة واحدة لا يوقف باقي الطلبات
            log.exception("pending: failed to send entry %s", e["id"])


async def notify_decision(bot: Bot, uid, ok: bool):
    if not uid:
        return
    try:
        await bot.send_message(uid, "✅ تمت الموافقة على إضافتك ونُشرت." if ok else "❌ لم تتم الموافقة على إضافتك.")
    except Exception:
        pass


async def set_status(bot: Bot, eid: int, ok: bool):
    await db.update(eid, "status", "approved" if ok else "rejected")
    e = await db.get(eid)
    if e:
        await notify_decision(bot, e["added_by"], ok)


@admin.callback_query(F.data.startswith("a:"))
async def decide(cb: CallbackQuery, bot: Bot):
    try:
        _, act, eid = cb.data.split(":")
        eid = int(eid)
    except ValueError:
        return await cb.answer()
    ok = act == "ok"
    # تحديث ذري: ينجح مرة واحدة فقط حتى لو ضغط أدمنان الزر معاً
    if not await db.decide_pending(eid, "approved" if ok else "rejected"):
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await cb.answer("سبق البتّ في هذا الطلب (أو حُذف).", show_alert=True)
    e = await db.get(eid)
    if e:
        await notify_decision(bot, e["added_by"], ok)
    try:
        await cb.message.edit_text(cb.message.html_text + ("\n\n✅ قُبل" if ok else "\n\n❌ رُفض"))
    except Exception:
        log.warning("decide: could not edit message", exc_info=True)
    await cb.answer()


@admin.message(Command("edit"))
async def edit(m: Message, command: CommandObject):
    try:
        i, f, v = command.args.split(maxsplit=2)
        ok = await db.update(int(i), f, v)
    except Exception:
        return await m.answer("الصيغة: /edit id حقل قيمة")
    await m.answer("تم ✅" if ok else "حقل غير صالح")


@admin.message(Command("boost"))
async def boost(m: Message, command: CommandObject):
    try:
        i, n = command.args.split()
        await db.pool.execute("UPDATE entries SET boost=$1 WHERE id=$2", int(n), int(i))
    except Exception:
        return await m.answer("الصيغة: /boost id رقم")
    await m.answer("تم ✅")


@admin.message(Command("del"))
async def delete(m: Message, command: CommandObject):
    try:
        await db.pool.execute("DELETE FROM entries WHERE id=$1", int(command.args))
    except Exception:
        return await m.answer("الصيغة: /del id")
    await m.answer("تم الحذف")


@admin.message(Command("ad"))
async def ad_add(m: Message, command: CommandObject):
    p = [x.strip() for x in (command.args or "").split("|")]
    if len(p) < 4:
        return await m.answer("الصيغة: /ad تصنيف | عنوان | نص | هاتف | واتساب(اختياري)")
    wa = p[4] if len(p) > 4 and p[4] else None
    i = await db.pool.fetchval(
        "INSERT INTO ads(category,title,text,phone,whatsapp) VALUES($1,$2,$3,$4,$5) RETURNING id",
        db.norm(p[0]), p[1], p[2], p[3], wa)
    await m.answer(f"تم إنشاء الإعلان #{i}")


@admin.message(Command("ads"))
async def ads_list(m: Message):
    rows = await db.pool.fetch("SELECT * FROM ads ORDER BY id DESC LIMIT 20")
    await m.answer("\n".join(f"#{r['id']} [{esc(r['category'] or '')}] {esc(r['title'] or '')} — "
                             f"{'فعال' if r['active'] else 'متوقف'} — ظهر {r['impressions']}" for r in rows) or "لا إعلانات")


@admin.message(Command("adoff"))
async def ad_off(m: Message, command: CommandObject):
    try:
        await db.pool.execute("UPDATE ads SET active=false WHERE id=$1", int(command.args))
    except Exception:
        return await m.answer("الصيغة: /adoff id")
    await m.answer("تم الإيقاف")


@admin.message(F.document, F.caption.startswith("/import"))
async def import_csv(m: Message, bot: Bot):
    if m.document.file_size and m.document.file_size > 2_000_000:
        return await m.answer("الحد الأقصى لملف الاستيراد 2MB.")
    buf = io.BytesIO()
    await bot.download(m.document, destination=buf)
    try:
        text = buf.getvalue().decode("utf-8-sig")
    except UnicodeDecodeError:
        return await m.answer("❌ يجب أن يكون الملف CSV بترميز UTF-8.")
    n = bad = 0
    try:
        for i, r in enumerate(csv.DictReader(io.StringIO(text))):
            if i >= 5000:
                await m.answer("⚠️ توقف الاستيراد عند الحد الأقصى (5000 سجل).")
                break
            if not r.get("name"):
                continue
            try:
                await db.add_entry(r, m.from_user.id, "approved")
                n += 1
            except Exception:
                bad += 1
                log.warning("import: bad row %d", i + 2)
    except csv.Error:
        return await m.answer(f"❌ ملف CSV غير صالح. استُورد {n} سجل قبل الخطأ.")
    await m.answer(f"✅ تم استيراد {n} سجل" + (f" (تعذّر {bad})" if bad else ""))


# ===================== الأدمن: إضافة بطاقات كثيرة دفعة واحدة =====================
BULK_WAIT = 2.5       # ثوانٍ ننتظرها لتجميع أجزاء الرسالة الطويلة (تلغرام يقسم النص فوق 4096 حرفاً)
BULK_BUF: dict = {}   # uid -> {"parts": [(message_id, text)], "task": Task}
BULK_PENDING: dict = {}   # token -> {"uid", "drafts", "dups"}  (في الذاكرة؛ تنتهي مع إعادة التشغيل)
_BULK_TASKS: set = set()  # مراجع قوية حتى لا يجمعها GC أثناء التنفيذ


async def _bulk_fire(m: Message, uid: int):
    await asyncio.sleep(BULK_WAIT)  # رسالة جديدة تلغي هذا الانتظار وتبدأ انتظاراً جديداً
    b = BULK_BUF.pop(uid, None)
    if b:
        text = "\n".join(t for _, t in sorted(b["parts"], key=lambda x: x[0]))
        t = asyncio.create_task(bulk_process(m, text))
        _BULK_TASKS.add(t)
        t.add_done_callback(_BULK_TASKS.discard)


async def bulk_collect(m: Message, text: str):
    uid = m.from_user.id
    b = BULK_BUF.setdefault(uid, {"parts": [], "task": None})
    b["parts"].append((m.message_id, text))
    if b["task"]:
        b["task"].cancel()
    b["task"] = asyncio.create_task(_bulk_fire(m, uid))


async def bulk_process(m: Message, text: str):
    if len(text) > bulk.MAX_TEXT:
        await m.answer(f"⚠️ النص كبير جداً؛ سأحلل أول {bulk.MAX_TEXT // 1000} ألف حرف فقط. أرسل الباقي بعد الانتهاء.")
    wait = await m.answer("⏳ جاري قراءة النص واستخراج البطاقات…")

    async def progress(i, n):
        if n > 1:
            await wait.edit_text(f"⏳ جاري التحليل… الجزء {i} من {n}")

    try:
        raw, failed, total = await ai.parse_bulk(text, progress)
    except Exception:
        log.exception("bulk: unexpected failure")
        return await wait.edit_text("⚠️ حدث خطأ غير متوقع أثناء التحليل. لم يُحفظ شيء.")
    drafts, same = bulk.dedupe(bulk.clean_all(raw))
    if not drafts:
        msg = "ما قدرت أستخرج أي بطاقة من النص."
        if failed:
            msg = "⚠️ الذكاء الاصطناعي غير متاح الآن، لم أستطع تحليل النص. أعد المحاولة بعد قليل."
        return await wait.edit_text(msg)
    try:
        dups = bulk.find_dups(drafts, await db.existing_names())
    except Exception:  # كشف التكرار تحسين فقط
        log.warning("bulk: duplicate check failed", exc_info=True)
        dups = set()
    for k in [k for k, v in BULK_PENDING.items() if v["uid"] == m.from_user.id]:
        BULK_PENDING.pop(k, None)  # دفعة واحدة معلقة لكل أدمن
    while len(BULK_PENDING) >= 20:
        BULK_PENDING.pop(next(iter(BULK_PENDING)), None)
    tok = secrets.token_hex(4)
    BULK_PENDING[tok] = {"uid": m.from_user.id, "drafts": drafts, "dups": dups}

    new_n = len(drafts) - len(dups)
    head = f"✅ استخرجت <b>{len(drafts)}</b> بطاقة"
    notes = []
    if dups:
        notes.append(f"🔁 {len(dups)} منها موجودة مسبقاً في الدليل")
    if same:
        notes.append(f"تم دمج {same} مكرر داخل النص")
    nophone = sum(1 for d in drafts if not d.get("phone"))
    if nophone:
        notes.append(f"⚠️ {nophone} بلا رقم هاتف")
    if failed:
        notes.append(f"⚠️ تعذّر تحليل {failed} من {total} أجزاء (الذكاء الاصطناعي غير متاح)، "
                     "فقد تكون بطاقات ناقصة؛ أعد إرسال الجزء المفقود بعد الحفظ")
    await wait.edit_text(head + (("\n" + "\n".join(notes)) if notes else ""))
    for page in bulk.summary_pages(drafts, dups):
        await m.answer(page, disable_web_page_preview=True)
    rows = []
    if dups and new_n:
        rows.append([B(text=f"✅ حفظ الجديدة فقط ({new_n})", callback_data=f"bk:new:{tok}")])
        rows.append([B(text=f"💾 حفظ الكل مع المكررة ({len(drafts)})", callback_data=f"bk:all:{tok}")])
    elif dups:
        rows.append([B(text=f"💾 حفظ مع ذلك ({len(drafts)})", callback_data=f"bk:all:{tok}")])
    else:
        rows.append([B(text=f"✅ حفظ الكل ({len(drafts)})", callback_data=f"bk:all:{tok}")])
    rows.append([B(text="❌ إلغاء", callback_data=f"bk:no:{tok}")])
    await m.answer("اضغط للحفظ في قاعدة البيانات (تُنشر مباشرة كبطاقات معتمدة):", reply_markup=M(inline_keyboard=rows))


@admin.callback_query(F.data.startswith("bk:"))
async def bulk_cb(cb: CallbackQuery):
    try:
        _, act, tok = cb.data.split(":")
    except ValueError:
        return await cb.answer()
    item = BULK_PENDING.get(tok)
    if not item or item["uid"] != cb.from_user.id:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await cb.answer("انتهت صلاحية هذه الدفعة (أعد لصق النص).", show_alert=True)
    if act == "no":
        BULK_PENDING.pop(tok, None)
        await cb.message.edit_text("أُلغيت الدفعة ولم يُحفظ شيء.")
        return await cb.answer()
    if act not in ("all", "new"):
        return await cb.answer()
    BULK_PENDING.pop(tok, None)  # يمنع الحفظ المزدوج عند الضغط مرتين
    drafts = [d for i, d in enumerate(item["drafts"]) if act == "all" or i not in item["dups"]]
    try:
        ids = await db.add_entries(drafts, cb.from_user.id, "approved")
    except Exception:
        log.exception("bulk: save failed")
        BULK_PENDING[tok] = item  # تبقى الدفعة لإعادة المحاولة (المعاملة تراجعت: لم يُحفظ شيء)
        return await cb.answer("⚠️ فشل الحفظ ولم يُحفظ شيء. حاول مرة ثانية.", show_alert=True)
    skipped = len(item["drafts"]) - len(drafts)
    await cb.message.edit_text(f"✅ حُفظت <b>{len(ids)}</b> بطاقة ونُشرت في الدليل."
                               + (f"\n(تخطيت {skipped} مكررة)" if skipped else ""))
    await cb.answer("تم الحفظ")


@admin.message(Command("bulk"))
async def bulk_cmd(m: Message, state: FSMContext):
    await state.set_state(S.bulk)
    await m.answer("📥 الصق قائمة العناوين كلها هنا (حتى لو بأكثر من رسالة، وبأي ترتيب)، "
                   "أو أرسل ملف .txt مع التعليق /bulk\n/cancel للإلغاء")


@admin.message(S.bulk, F.text, ~F.text.startswith("/"))
async def bulk_text(m: Message, state: FSMContext):
    if _is_cancel(m, True):
        return await cancel(m, state)
    await state.clear()
    await bulk_collect(m, m.text)


@admin.message(F.document, F.caption.startswith("/bulk"))
async def bulk_doc(m: Message, bot: Bot, state: FSMContext):
    if m.document.file_size and m.document.file_size > 1_000_000:
        return await m.answer("الحد الأقصى للملف 1MB.")
    buf = io.BytesIO()
    await bot.download(m.document, destination=buf)
    try:
        text = buf.getvalue().decode("utf-8-sig")
    except UnicodeDecodeError:
        return await m.answer("❌ يجب أن يكون الملف نصاً بترميز UTF-8.")
    await state.clear()
    await bulk_process(m, text)


# ===================== الأدمن: تعديل/حذف أي جزء في أي بطاقة =====================
def _current(e, field):
    if field == "location":
        return f"{e['lat']}, {e['lng']}" if e["lat"] is not None and e["lng"] is not None else ""
    return str(e[field] or "")


@admin.callback_query(F.data.startswith("ae:"))
async def admin_edit_cb(cb: CallbackQuery, state: FSMContext):
    try:
        _, eid, act = cb.data.split(":")
        eid = int(eid)
    except ValueError:
        return await cb.answer()
    e = await db.get(eid)
    if not e:
        return await cb.answer("البطاقة غير موجودة (ربما حُذفت).", show_alert=True)
    try:
        if act == "m":
            await cb.message.edit_reply_markup(reply_markup=edit_menu_kb(eid))
        elif act == "b":
            await cb.message.edit_reply_markup(reply_markup=admin_kb(e))
    except Exception:
        pass
    if act == "x":
        await cb.message.answer(f"⚠️ حذف نهائي للبطاقة #{eid} {esc(e['name'])}؟", reply_markup=M(inline_keyboard=[[
            B(text="🗑 نعم، احذف", callback_data=f"c:del:{eid}"), B(text="إلغاء", callback_data="c:no")]]))
    elif act in cf.LABEL:
        await state.set_state(S.aedit)
        await state.update_data(eid=eid, field=act)
        cur = _current(e, act)
        hint = "\nأو أرسل 📎 الموقع مباشرة." if act == "location" else ""
        await cb.message.answer(
            f"✏️ تعديل <b>{cf.LABEL[act]}</b> في #{eid} {esc(e['name'])}\n"
            + (f"الحالي: <code>{esc(cur)}</code>\n" if cur else "الحالي: —\n")
            + f"أرسل القيمة الجديدة، أو «-» للحذف.{hint}\n/cancel للإلغاء")
    await cb.answer()


async def _admin_save(m: Message, state: FSMContext, eid: int, d: dict):
    await state.clear()
    if not await db.get(eid):
        return await m.answer("البطاقة لم تعد موجودة.")
    for k, v in d.items():
        await db.update(eid, k, v)
    e = await db.get(eid)
    await m.answer("✅ تم التعديل:\n" + card(e, True), reply_markup=admin_kb(e), disable_web_page_preview=True)


@admin.message(S.aedit, F.text, ~F.text.startswith("/"))
async def admin_edit_value(m: Message, state: FSMContext):
    if _is_cancel(m, True):
        return await cancel(m, state)
    data = await state.get_data()
    eid, field = data.get("eid"), data.get("field")
    if not eid or not field:
        await state.clear()
        return await m.answer("انتهت الجلسة.")
    d = {}
    err = cf.apply_edit(d, field, m.text)
    if err:
        return await m.answer("⚠️ " + err)
    await _admin_save(m, state, eid, d)


@admin.message(S.aedit, F.location)
async def admin_edit_location(m: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("eid") or data.get("field") != "location":
        return await m.answer("اختر زر «الموقع على الخريطة» أولاً.")
    await _admin_save(m, state, data["eid"], {"lat": m.location.latitude, "lng": m.location.longitude})


# ======================== المالك: الأدمنز والمفاتيح ========================
async def owner_only(m: Message) -> bool:
    if m.from_user.id in OWNER_IDS:
        return True
    await m.answer("⛔ هذا الأمر للمالك فقط.")
    return False


async def add_admin_flow(m: Message, bot: Bot, uid: int, by=None):
    if uid in ADMIN_IDS:
        return await m.answer("هو أدمن بالفعل.")
    await db.add_admin(uid, by or m.from_user.id)
    ADMIN_IDS.add(uid)
    await m.answer(f"✅ أُضيف الأدمن {uid}")
    try:
        await bot.send_message(uid, "✅ تم تعيينك أدمن. اكتب /admin لعرض الأوامر.")
    except Exception:
        await m.answer("(لم أستطع مراسلته، يجب أن يبدأ البوت أولاً بـ /start)")


async def del_admin_flow(m: Message, uid: int):
    if uid in OWNER_IDS:
        return await m.answer("لا يمكن حذف مالك.")
    if uid not in ADMIN_IDS:
        return await m.answer("ليس أدمن.")
    await db.del_admin(uid)
    ADMIN_IDS.discard(uid)
    await m.answer("✅ أُزيل")


@admin.message(Command("addadmin"))
async def cmd_addadmin(m: Message, command: CommandObject, bot: Bot):
    if not await owner_only(m):
        return
    try:
        uid = int(command.args)
    except (TypeError, ValueError):
        return await m.answer("الصيغة: /addadmin معرّف_رقمي (يحصل عليه بكتابة /myid للبوت)")
    await add_admin_flow(m, bot, uid)


@admin.message(Command("deladmin"))
async def cmd_deladmin(m: Message, command: CommandObject):
    if not await owner_only(m):
        return
    try:
        uid = int(command.args)
    except (TypeError, ValueError):
        return await m.answer("الصيغة: /deladmin معرّف_رقمي")
    await del_admin_flow(m, uid)


@admin.message(Command("admins"))
async def cmd_admins(m: Message):
    others = ", ".join(str(i) for i in ADMIN_IDS - OWNER_IDS) or "—"
    await m.answer("👑 المالكون: " + ", ".join(map(str, OWNER_IDS)) + "\n🛡 الأدمنز: " + others)


# AIza... = المفاتيح القديمة، AQ. = مفاتيح AI Studio الجديدة (منذ مايو 2026؛ تحتوي نقاطاً)
KEY_RE = re.compile(r"AIza[\w-]{35}|AQ\.[\w.-]{20,}")


def mask(k):
    return k[:6] + "…" + k[-4:]


@admin.message(Command("keys"))
async def cmd_keys(m: Message):
    if not await owner_only(m):
        return
    ks, ms = await ai.get_keys(), await ai.get_models()
    t = "🔑 مفاتيح Gemini:\n" + ("\n".join(f"{i}. {mask(k)}" for i, k in enumerate(ks, 1)) or "—")
    provs = await ai.provider_summary()
    await m.answer(t + "\n\n🤖 نماذج Gemini: " + ", ".join(ms) +
                   "\n\n🔌 مزودو الذكاء الاصطناعي (بالأولوية):\n" + (esc("\n".join(provs)) or "—") +
                   "\n(مزودو AI_PROVIDER_* تُضبط من Environment في Render)" +
                   "\n\nأضف مفتاح Gemini بلصقه هنا مباشرة · /delkey رقم · /models اسم1,اسم2")


@admin.message(Command("delkey"))
async def cmd_delkey(m: Message, command: CommandObject):
    if not await owner_only(m):
        return
    ks = await ai.get_keys()
    try:
        i = int(command.args)
        if not 1 <= i <= len(ks):
            raise ValueError
    except (TypeError, ValueError):
        return await m.answer("الصيغة: /delkey رقم (انظر /keys)")
    k = ks.pop(i - 1)
    await ai.set_keys(ks)
    await m.answer(f"🗑 حُذف {mask(k)}. المتبقي: {len(ks)}")


@admin.message(Command("models"))
async def cmd_models(m: Message, command: CommandObject):
    if not await owner_only(m):
        return
    names = [x.strip() for x in (command.args or "").split(",") if x.strip()]
    if not names:
        return await m.answer("الصيغة: /models gemini-2.5-flash,gemini-2.5-flash-lite")
    await ai.set_models(names)
    await m.answer("✅ تم تحديث النماذج: " + ", ".join(names))


async def handle_keys_in_text(m: Message) -> bool:
    found = [k.rstrip(".") for k in KEY_RE.findall(m.text)]
    if not found:
        return False
    try:
        await m.delete()  # حذف الرسالة التي تحوي المفتاح من المحادثة
    except Exception:
        pass
    if m.from_user.id not in OWNER_IDS:
        await m.answer("⛔ إدارة المفاتيح للمالك فقط.")
        return True
    ks = await ai.get_keys()
    new = [k for k in dict.fromkeys(found) if k not in ks]
    await ai.set_keys(ks + new)
    await m.answer(f"✅ أُضيف {len(new)} مفتاح. الإجمالي: {len(ks) + len(new)}")
    return True


# ===================== المالك: التحكم بالكود عبر GitHub =====================
PENDING: dict = {}  # token -> (path, new_content | None للحذف, وصف)
CODE_PENDING: dict = {}  # token -> (complete file map, summary, tests)


def _gh_err(e):
    return f"⚠️ {esc(str(e))}"


async def do_gh_list(m: Message, path: str):
    try:
        items = await gh.list_dir(path)
    except gh.GHError as e:
        return await m.answer(_gh_err(e))
    await m.answer(esc("\n".join(items)) or "فارغ")


async def do_gh_show(m: Message, path: str):
    try:
        r = await gh.get_file(path)
    except gh.GHError as e:
        return await m.answer(_gh_err(e))
    if not r:
        return await m.answer("الملف غير موجود")
    await m.answer_document(BufferedInputFile(r[0].encode(), filename=path.split("/")[-1]))


async def do_gh_edit(m: Message, path: str, instruction: str):
    try:
        cur = await gh.get_file(path)
    except gh.GHError as e:
        return await m.answer(_gh_err(e))
    old = cur[0] if cur else ""
    wait = await m.answer("⏳ جاري التعديل بالذكاء الاصطناعي…")
    new = await ai.edit_code(path, old, instruction)
    if not new:
        return await wait.edit_text("⚠️ نموذج البرمجة المخصص غير مضبوط أو غير متاح؛ لم نستخدم أي نموذج عام ولم يُرفع شيء.")
    if path.endswith(".py"):
        try:
            compile(new, path, "exec")
        except SyntaxError as e:
            return await wait.edit_text(f"❌ الكود الناتج فيه خطأ صياغة (سطر {e.lineno}) ولم يُرفع شيء. أعد صياغة الطلب.")
    diff = "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{path}", f"b/{path}", lineterm="", n=2))
    if not diff:
        return await wait.edit_text("لا تغيير مطلوب.")
    tok = secrets.token_hex(4)
    PENDING[tok] = (path, new, instruction)
    kbd = M(inline_keyboard=[[B(text="✅ ارفع إلى فرع التجربة", callback_data=f"g:ok:{tok}"),
                              B(text="❌ إلغاء", callback_data=f"g:no:{tok}")]])
    head = f"📝 <b>{esc(path)}</b>{' (جديد)' if not cur else ''}"
    if len(diff) <= 2000:
        await wait.edit_text(f"{head}\n<pre>{esc(diff)}</pre>", reply_markup=kbd)
    else:
        await wait.edit_text(f"{head} — التغييرات طويلة، أرسلتها كملف أدناه.", reply_markup=kbd)
        await m.answer_document(BufferedInputFile(diff.encode(), "changes.diff"))


async def do_gh_code(m: Message, instruction: str):
    """Repository-wide change proposal using the dedicated coding model only."""
    if not instruction or len(instruction.strip()) < 8:
        return await m.answer("الصيغة: /ghcode وصف التعديل المطلوب على المشروع")
    wait = await m.answer("🔎 يجري فحص ملفات المستودع وتحليل العلاقات بينها باستخدام نموذج البرمجة المخصص…")
    try:
        proposal = await coding_agent.propose(instruction)
    except coding_agent.CodingTaskError as e:
        return await wait.edit_text("⚠️ " + esc(str(e)))
    except Exception:
        log.exception("Unexpected repository coding failure")
        return await wait.edit_text("⚠️ فشل غير متوقع أثناء تحليل المستودع. لم تُرفع أي تغييرات.")

    changes = proposal["changes"]
    token = secrets.token_hex(5)
    CODE_PENDING[token] = (changes, proposal["summary"], proposal["tests"])
    names = list(changes)
    summary = esc(proposal["summary"] or "تم تجهيز التغييرات المقترحة.")
    file_lines = "\n".join("• " + esc(x) for x in names)
    tests = proposal["tests"]
    test_text = "\n".join("• " + esc(x) for x in tests) if tests else "لم يحدد النموذج اختبارات إضافية؛ ستُجرى فحوص الصياغة قبل الرفع."
    keyboard = M(inline_keyboard=[[
        B(text="✅ ارفع المجموعة إلى فرع التجربة", callback_data=f"g:batchok:{token}"),
        B(text="❌ إلغاء", callback_data=f"g:batchno:{token}"),
    ]])
    message = (f"🧠 <b>اقتراح نموذج البرمجة المخصص</b>\n{summary}\n\n"
               f"<b>الملفات ({len(names)}):</b>\n{file_lines}\n\n"
               f"<b>الاختبارات المقترحة:</b>\n{test_text}\n\n"
               "تم فحص صياغة ملفات Python المقترحة. لم يُرفع شيء بعد؛ راجع التغييرات قبل الموافقة.")
    diff_parts = []
    for path, content in changes.items():
        try:
            old_result = await gh.get_file(path)
            old = old_result[0] if old_result else ""
        except gh.GHError:
            old = ""
        diff_parts.extend(difflib.unified_diff(old.splitlines(), content.splitlines(), f"a/{path}", f"b/{path}", lineterm=""))
        diff_parts.append("")
    diff_text = "\n".join(diff_parts).strip() or "لا توجد فروقات قابلة للعرض."
    await wait.edit_text(message, reply_markup=keyboard)
    await m.answer_document(BufferedInputFile(diff_text.encode("utf-8"), filename="coding-proposal.diff"),
                            caption="راجع هذا الملف لمعاينة الفروقات المقترحة قبل الموافقة على رفعها.")


async def do_gh_del(m: Message, path: str):
    tok = secrets.token_hex(4)
    PENDING[tok] = (path, None, f"delete {path}")
    await m.answer(f"⚠️ حذف <b>{esc(path)}</b> من فرع التجربة؟", reply_markup=M(inline_keyboard=[[
        B(text="🗑 نعم، احذف", callback_data=f"g:ok:{tok}"), B(text="إلغاء", callback_data=f"g:no:{tok}")]]))


async def do_deploy(m: Message):
    try:
        cmp = await gh.compare()
    except gh.GHError as e:
        return await m.answer(_gh_err(e))
    if not cmp or cmp[0] == 0:
        return await m.answer("لا توجد تغييرات بانتظار النشر.")
    ahead, files = cmp
    await m.answer(f"🚀 تغييرات بانتظار النشر ({ahead} commit):\n" + "\n".join(f"• {esc(f)}" for f in files) +
                   "\n\nالنشر سيعيد تشغيل البوت.",
                   reply_markup=M(inline_keyboard=[[B(text="🚀 انشر الآن", callback_data="g:dep"),
                                                    B(text="إلغاء", callback_data="g:x")]]))


@admin.message(Command("ghls"))
async def cmd_ghls(m: Message, command: CommandObject):
    if await owner_only(m):
        await do_gh_list(m, command.args or "")


@admin.message(Command("ghshow"))
async def cmd_ghshow(m: Message, command: CommandObject):
    if await owner_only(m):
        await do_gh_show(m, command.args or "")


@admin.message(Command("ghedit"))
async def cmd_ghedit(m: Message, command: CommandObject):
    if not await owner_only(m):
        return
    if not command.args or "|" not in command.args:
        return await m.answer("الصيغة: /ghedit المسار | التعليمات")
    path, ins = (x.strip() for x in command.args.split("|", 1))
    await do_gh_edit(m, path, ins)


@admin.message(Command("ghcode"))
async def cmd_ghcode(m: Message, command: CommandObject):
    if not await owner_only(m):
        return
    await do_gh_code(m, command.args or "")


@admin.message(Command("ghdel"))
async def cmd_ghdel(m: Message, command: CommandObject):
    if await owner_only(m):
        if not command.args:
            return await m.answer("الصيغة: /ghdel المسار")
        await do_gh_del(m, command.args.strip())


@admin.message(Command("deploy"))
async def cmd_deploy(m: Message):
    if await owner_only(m):
        await do_deploy(m)


@admin.message(Command("ghreset"))
async def cmd_ghreset(m: Message):
    if not await owner_only(m):
        return
    try:
        await gh.reset_stage()
    except gh.GHError as e:
        return await m.answer(_gh_err(e))
    await m.answer("✅ أُعيد فرع التجربة ليطابق الرئيسي (أُلغيت التغييرات المعلقة).")


@admin.message(F.document, F.caption.startswith("/push"))
async def push_doc(m: Message, bot: Bot):
    if not await owner_only(m):
        return
    parts = m.caption.split(maxsplit=2)
    if len(parts) < 2:
        return await m.answer("أرسل الملف مع تعليق: /push المسار/في/المستودع")
    path = parts[1]
    if m.document.file_size and m.document.file_size > 500_000:
        return await m.answer("الحد الأقصى 500KB")
    buf = io.BytesIO()
    await bot.download(m.document, destination=buf)
    data = buf.getvalue()
    if path.endswith(".py"):
        try:
            compile(data.decode("utf-8"), path, "exec")
        except (SyntaxError, UnicodeDecodeError):
            return await m.answer("❌ الملف فيه خطأ صياغة ولم يُرفع.")
    try:
        await gh.put_file(path, data, f"chat: push {path}")
    except gh.GHError as e:
        return await m.answer(_gh_err(e))
    await m.answer(f"✅ رُفع {esc(path)} إلى فرع التجربة. اكتب /deploy للنشر.")


@admin.callback_query(F.data.startswith("g:"))
async def gh_cb(cb: CallbackQuery):
    if cb.from_user.id not in OWNER_IDS:
        return await cb.answer("للمالك فقط", show_alert=True)
    parts = cb.data.split(":")
    act = parts[1]
    if act == "batchno":
        CODE_PENDING.pop(parts[2], None)
        await cb.message.edit_text("أُلغي اقتراح التعديل ولم يُرفع شيء.")
    elif act == "batchok":
        item = CODE_PENDING.get(parts[2])
        if not item:
            return await cb.answer("انتهت صلاحية الطلب", show_alert=True)
        changes, summary, tests = item
        try:
            paths = await gh.put_files(changes, "chat: repository-aware coding change")
            CODE_PENDING.pop(parts[2], None)
            msg = ("✅ رُفعت مجموعة التغييرات في commit واحد إلى فرع التجربة.\n\n"
                   + "الملفات: " + ", ".join(paths)
                   + "\n\nانتظر نجاح GitHub Actions، ثم استخدم /deploy.")
        except gh.GHError as e:
            msg = _gh_err(e)
        except Exception:
            log.exception("Failed to stage repository-wide coding proposal")
            msg = "⚠️ تعذر رفع التغييرات بسبب خطأ اتصال أو GitHub. بقي الاقتراح متاحًا لإعادة المحاولة."
        await cb.message.edit_text(msg)
    elif act == "x":
        await cb.message.edit_text("أُلغي")
    elif act == "no":
        PENDING.pop(parts[2], None)
        await cb.message.edit_text("أُلغي")
    elif act == "ok":
        item = PENDING.pop(parts[2], None)
        if not item:
            return await cb.answer("انتهت صلاحية الطلب", show_alert=True)
        path, new, ins = item
        try:
            if new is None:
                await gh.delete_file(path, f"chat: {ins[:60]}")
                msg = f"🗑 حُذف <b>{esc(path)}</b> من فرع التجربة. اكتب /deploy للنشر."
            else:
                await gh.put_file(path, new.encode(), f"chat: {ins[:60]}")
                msg = f"✅ رُفع <b>{esc(path)}</b> إلى فرع التجربة. اكتب /deploy للنشر."
        except gh.GHError as e:
            msg = _gh_err(e)
        await cb.message.edit_text(msg)
    elif act == "dep":
        try:
            msg = esc(await gh.deploy())
        except gh.GHError as e:
            msg = _gh_err(e)
        await cb.message.edit_text(msg)
    await cb.answer()


# =================== التحكم بالدردشة الطبيعية (للأدمن) ===================
async def resolve(m: Message, ref):
    rows = await db.find(ref)
    if not rows:
        await m.answer("ما لقيت سجلاً بهذا الاسم/الرقم.")
        return None
    if len(rows) > 1 and not str(ref).isdigit():
        await m.answer("وجدت أكثر من نتيجة، أعد الأمر مع الرقم:\n" + "\n".join(
            f"#{r['id']} {esc(r['name'])} — {esc(r['city'] or '')} ({r['status']})" for r in rows))
        return None
    return rows[0]


OWNER_ACTIONS = ("add_admin", "remove_admin", "list_admins", "gh_list", "gh_show", "gh_edit", "gh_code", "gh_delete", "deploy")


async def owner_actions(m: Message, bot: Bot, p: dict, act: str) -> bool:
    if act not in OWNER_ACTIONS:
        return False
    if not await owner_only(m):
        return True
    if act == "list_admins":
        await cmd_admins(m)
        return True
    if act in ("add_admin", "remove_admin"):
        try:
            uid = int(p.get("user_id"))
        except (TypeError, ValueError):
            await m.answer("أرسل المعرّف الرقمي للمستخدم (يحصل عليه بكتابة /myid للبوت).")
            return True
        # تنفيذ فوري من نص حر ممنوع: قد يخطئ الـ AI في المعرّف أو التصنيف، فنطلب تأكيداً
        if act == "add_admin":
            text, data = f"⚠️ تعيين <code>{uid}</code> أدمن؟", f"c:addadm:{uid}"
        else:
            text, data = f"⚠️ إزالة <code>{uid}</code> من الأدمنز؟", f"c:rmadm:{uid}"
        await m.answer(text, reply_markup=M(inline_keyboard=[[
            B(text="✅ نعم، نفّذ", callback_data=data), B(text="إلغاء", callback_data="c:no")]]))
        return True
    if act == "deploy":
        await do_deploy(m)
        return True
    if act == "gh_code":
        await do_gh_code(m, p.get("instruction") or m.text)
        return True
    path = (p.get("path") or "").strip()
    if act == "gh_list":
        await do_gh_list(m, path)
        return True
    if not path:
        await m.answer("حدد مسار الملف.")
        return True
    if act == "gh_show":
        await do_gh_show(m, path)
    elif act == "gh_delete":
        await do_gh_del(m, path)
    elif act == "gh_edit":
        await do_gh_edit(m, path, p.get("instruction") or m.text)
    return True


# عند تعطل الـ AI فقط: عبارات قراءة فقط تُطابَق مطابقة تامة (بعد التطبيع) لتبقى الإحصائيات والمعلقات متاحة
_QUICK_STATS = {db.norm(x) for x in ("stats", "إحصائيات", "الإحصائيات", "احصاءات", "الاحصاءات")}
_QUICK_PENDING = {db.norm(x) for x in ("pending", "المعلقة", "الطلبات", "الطلبات المعلقة", "طلبات معلقة")}


def quick_admin_action(text):
    t = db.norm(text)
    if t in _QUICK_STATS:
        return "stats"
    if t in _QUICK_PENDING:
        return "pending"
    return None


@admin.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def admin_nl(m: Message, bot: Bot, state: FSMContext):
    if await handle_keys_in_text(m):  # المفاتيح لا تمر على الذكاء الاصطناعي أبداً
        return
    if m.from_user.id in BULK_BUF or bulk.looks_bulk(m.text):  # قائمة عناوين طويلة (أو تكملتها)
        return await bulk_collect(m, m.text)
    r = lex.match(m.text, admin=True)  # القاموس أولاً: فوري وبلا ذكاء اصطناعي
    p = None
    if r and r.deterministic:
        if await run_common(m, state, bot, r):
            return
        p = lex.admin_payload(r)
    if p is None:
        p = await ai.parse_admin(m.text)
    if p is None:  # فشل التحليل: الذكاء الاصطناعي غير متاح أو أعاد JSON غير صالح
        quick = quick_admin_action(m.text)  # عبارتان للقراءة فقط، مطابقة تامة للنص
        if quick == "stats":
            return await stats(m)
        if quick == "pending":
            return await pending(m)
        log.warning("admin NL: ai parse failed; no action executed")
        return await m.answer("⚠️ تعذّر تحليل طلبك بالذكاء الاصطناعي الآن، ولم يُنفَّذ أي شيء.\n"
                              "أعد المحاولة بعد قليل، أو استخدم الأوامر اليدوية من /admin (مثل /stats و /pending).")
    await dispatch_admin(m, bot, p)


async def dispatch_admin(m: Message, bot: Bot, p: dict):
    act, ref = p.get("action", "unknown"), p.get("ref") or ""

    if act == "user_search":
        return await do_search(m, m.text)
    if act == "stats":
        return await stats(m)
    if act == "pending":
        return await pending(m)
    if act == "ads_list":
        return await ads_list(m)

    if act == "approve_all":
        n = await db.pool.fetchval("SELECT count(*) FROM entries WHERE status='pending'")
        if not n:
            return await m.answer("لا توجد طلبات معلقة.")
        return await m.answer(f"⚠️ الموافقة على كل الطلبات المعلقة ({n})؟", reply_markup=M(inline_keyboard=[[
            B(text="✅ نعم، وافق على الكل", callback_data="c:apall"), B(text="إلغاء", callback_data="c:no")]]))

    if act in ("show", "approve", "reject", "edit", "boost", "delete"):
        e = await resolve(m, ref)
        if not e:
            return
        eid = e["id"]
        if act == "show":
            return await m.answer(card(e, True), reply_markup=admin_kb(e), disable_web_page_preview=True)
        if act in ("approve", "reject"):
            await set_status(bot, eid, act == "approve")
            return await m.answer(("✅ قُبل" if act == "approve" else "❌ رُفض") + f": #{eid} {esc(e['name'])}")
        if act == "boost":
            try:
                v = int(p.get("value") or 0)
            except (TypeError, ValueError):
                return await m.answer("قيمة الترتيب غير واضحة.")
            await db.pool.execute("UPDATE entries SET boost=$1 WHERE id=$2", v, eid)
            return await m.answer(f"✅ ترتيب #{eid} {esc(e['name'])} = {v}")
        if act == "edit":
            ch = {k: v for k, v in (p.get("changes") or {}).items()
                  if v not in ("", None) and k in db.FIELDS - {"status"}}
            if not ch:
                return await m.answer("ما حددت أي تعديل.")
            try:
                for k, v in ch.items():
                    await db.update(eid, k, v)
            except ValueError:
                return await m.answer("قيمة غير صالحة (الإحداثيات يجب أن تكون أرقاماً).")
            e2 = await db.get(eid)
            return await m.answer("✅ تم التعديل:\n" + card(e2, True), reply_markup=admin_kb(e2), disable_web_page_preview=True)
        if act == "delete":  # يحتاج تأكيداً
            return await m.answer(f"⚠️ حذف نهائي لـ #{eid} {esc(e['name'])}؟", reply_markup=M(inline_keyboard=[[
                B(text="🗑 نعم، احذف", callback_data=f"c:del:{eid}"), B(text="إلغاء", callback_data="c:no")]]))

    if act == "add_entry":
        d = await ai.parse_entry(m.text)
        if not d or not d.get("name"):
            return await m.answer("ما قدرت أستخرج الاسم، أعد الكتابة.")
        eid = await db.add_entry(cf.clean_draft(d), m.from_user.id, "approved")
        e = await db.get(eid)
        return await m.answer("✅ أُضيف ونُشر:\n" + card(e, True), reply_markup=admin_kb(e),
                              disable_web_page_preview=True)

    if act == "ad_add":
        a = p.get("ad") or {}
        if not (a.get("category") and a.get("title") and a.get("text")):
            return await m.answer("الإعلان يحتاج: التصنيف والعنوان والنص (والهاتف اختياري).")
        i = await db.pool.fetchval(
            "INSERT INTO ads(category,title,text,phone,whatsapp) VALUES($1,$2,$3,$4,$5) RETURNING id",
            db.norm(a["category"]), a["title"], a["text"], a.get("phone") or None, a.get("whatsapp") or None)
        return await m.answer(f"✅ تم إنشاء الإعلان #{i}")

    if act == "ad_off":
        try:
            await db.pool.execute("UPDATE ads SET active=false WHERE id=$1", int(p.get("ad_id")))
        except (TypeError, ValueError):
            return await m.answer("حدد رقم الإعلان.")
        return await m.answer("✅ تم إيقاف الإعلان")

    if act == "db_size":
        return await m.answer(await db.db_report())

    if act == "cleanup":
        try:
            days = max(7, int(p.get("value") or 90))
        except (TypeError, ValueError):
            days = 90
        return await m.answer(f"⚠️ حذف سجلات البحث الأقدم من {days} يوماً؟", reply_markup=M(inline_keyboard=[[
            B(text="🧹 نعم، احذف", callback_data=f"c:clean:{days}"), B(text="إلغاء", callback_data="c:no")]]))

    if await owner_actions(m, bot, p, act):
        return

    await m.answer("ما فهمت الأمر. اكتبه بشكل أوضح أو استخدم /admin لعرض الأوامر.")


@admin.callback_query(F.data.startswith("c:"))
async def confirm(cb: CallbackQuery, bot: Bot):
    parts = cb.data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    try:
        if act == "del":
            await db.pool.execute("DELETE FROM entries WHERE id=$1", int(parts[2]))
            msg = "🗑 تم الحذف"
        elif act == "apall":
            ids = [r["id"] for r in await db.pool.fetch("SELECT id FROM entries WHERE status='pending'")]
            done = 0
            for i in ids:
                try:
                    await set_status(bot, i, True)
                    done += 1
                except Exception:
                    log.warning("approve_all: failed for entry %s", i)
            msg = f"✅ تمت الموافقة على {done} سجل"
        elif act == "clean":
            n = await db.cleanup_searches(max(7, int(parts[2])))
            msg = f"🧹 حُذف {n} سجل بحث."
        elif act in ("addadm", "rmadm"):
            if cb.from_user.id not in OWNER_IDS:
                return await cb.answer("للمالك فقط", show_alert=True)
            uid = int(parts[2])
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            if act == "addadm":
                await add_admin_flow(cb.message, bot, uid, by=cb.from_user.id)
            else:
                await del_admin_flow(cb.message, uid)
            return await cb.answer()
        else:
            msg = "أُلغي"
    except (IndexError, ValueError):
        msg = "طلب غير صالح"
    try:
        await cb.message.edit_text(msg)
    except Exception:
        pass
    await cb.answer()
