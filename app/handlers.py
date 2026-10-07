import csv
import difflib
import html
import io
import re
import secrets

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup as M

from . import ai, db
from . import github as gh
from .config import ADMIN_IDS, DEFAULT_CC, OWNER_IDS

esc = html.escape
admin, user = Router(), Router()


def is_admin(e):
    return bool(e.from_user and e.from_user.id in ADMIN_IDS)


admin.message.filter(is_admin)
admin.callback_query.filter(is_admin)


class S(StatesGroup):
    add = State()


def wa_link(n):
    d = "".join(c for c in n if c.isdigit())
    if d.startswith("00"):
        d = d[2:]
    elif d.startswith("0"):
        d = DEFAULT_CC + d[1:]
    return f"https://wa.me/{d}"


def card(e, adm=False):
    t = f"📍 <b>{esc(e['name'])}</b>\n🏷 {esc(e['category'] or '')}"
    if e["city"]:
        t += f" • {esc(e['city'])}"
    if e["address"]:
        t += f"\n🧭 {esc(e['address'])}"
    if e["phone"]:
        t += f"\n📞 {esc(e['phone'])}"  # تلغرام يجعل الرقم قابلاً للضغط للاتصال
    if e["description"]:
        t += f"\nℹ️ {esc(e['description'])}"
    t += f"\n👁 {e['views']}"
    if adm:
        t += f"\n🆔 {e['id']} | {e['status']} | boost {e['boost']} | by {e['added_by']}"
    return t


def kb(e):
    r1 = []
    if e["whatsapp"]:
        r1.append(B(text="💬 واتساب", url=wa_link(e["whatsapp"])))
    if e["lat"] is not None and e["lng"] is not None:
        r1.append(B(text="🗺 الخريطة", url=f"https://www.google.com/maps?q={e['lat']},{e['lng']}"))
    r2 = [B(text=f"❤️ {e['hearts']}", callback_data=f"r:heart:{e['id']}"),
          B(text=f"👍 {e['likes']}", callback_data=f"r:like:{e['id']}")]
    return M(inline_keyboard=[r1, r2] if r1 else [r2])


def decision_kb(eid):
    return M(inline_keyboard=[[B(text="✅ قبول", callback_data=f"a:ok:{eid}"),
                               B(text="❌ رفض", callback_data=f"a:no:{eid}")]])


# =============================== المستخدمون ===============================
@user.message(CommandStart())
async def start(m: Message):
    await db.touch_user(m.from_user)
    await m.answer("أهلاً! اكتب ما تبحث عنه بأي لهجة، مثل: رقم مطعم الرحمة بدرعا\nلإضافة مكان: /add")


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
    d = await ai.parse_entry(m.text)
    if not d or not d.get("name"):
        return await m.answer("ما قدرت أفهم الاسم، أعد الكتابة بوضوح أكثر أو جرّب بعد قليل.")
    adm = m.from_user.id in ADMIN_IDS
    eid = await db.add_entry(d, m.from_user.id, "approved" if adm else "pending")
    await state.clear()
    e = await db.get(eid)
    if adm:
        return await m.answer("✅ أُضيف ونُشر:\n" + card(e, True), reply_markup=kb(e))
    await m.answer("شكراً! استلمنا الإضافة وستظهر بعد موافقة الإدارة.")
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, "🆕 طلب إضافة:\n" + card(e, True), reply_markup=decision_kb(eid))
        except Exception:
            pass


@user.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def on_text(m: Message):
    await do_search(m, m.text)


async def do_search(m: Message, text: str):
    await db.touch_user(m.from_user)
    p = await ai.parse_search(text)
    rows = await db.search(p["name"], p["category"], p["city"])
    await db.log_search(m.from_user.id, text, len(rows))
    if not rows:
        return await m.answer("ما لقيت نتائج. جرّب صياغة ثانية، أو أضف المكان بـ /add")
    await db.add_views([r["id"] for r in rows])
    for r in rows:
        r = dict(r)
        r["views"] += 1
        await m.answer(card(r), reply_markup=kb(r), disable_web_page_preview=True)
    ad = await db.pick_ad(p["category"])
    if ad:
        t = f"📢 <b>إعلان</b>\n<b>{esc(ad['title'] or '')}</b>\n{esc(ad['text'] or '')}"
        if ad["phone"]:
            t += f"\n📞 {esc(ad['phone'])}"
        btn = [[B(text="💬 واتساب", url=wa_link(ad["whatsapp"]))]] if ad["whatsapp"] else []
        await m.answer(t, reply_markup=M(inline_keyboard=btn) if btn else None)


@user.callback_query(F.data.startswith("r:"))
async def react(cb: CallbackQuery):
    _, kind, eid = cb.data.split(":")
    on = await db.toggle(int(eid), cb.from_user.id, kind)
    e = await db.get(int(eid))
    try:
        await cb.message.edit_reply_markup(reply_markup=kb(e))
    except Exception:
        pass
    await cb.answer("تم ✔️" if on else "أُلغي")


# ================================ الإدارة ================================
@admin.message(Command("admin"))
async def admin_help(m: Message):
    t = ("<b>الأدمن</b> — اكتب طلبك بالعربي مباشرة، أو استخدم الأوامر:\n"
         "/stats — الإحصائيات\n/pending — الطلبات المعلقة\n"
         "/edit id حقل قيمة — الحقول: name category city address phone whatsapp description lat lng status\n"
         "/boost id رقم — ترتيب النتيجة (الأعلى أولاً)\n/del id — حذف\n"
         "/ad تصنيف | عنوان | نص | هاتف | واتساب(اختياري)\n/ads — الإعلانات\n/adoff id — إيقاف إعلان\n"
         "ملف CSV مع التعليق /import — استيراد جماعي\n"
         "أعمدة CSV: name,category,city,address,phone,whatsapp,lat,lng,description\n/add — إضافة مباشرة")
    if m.from_user.id in OWNER_IDS:
        t += ("\n\n<b>المالك فقط</b>\n/addadmin id · /deladmin id · /admins\n"
              "/keys · /delkey رقم · /models اسم1,اسم2 (والصق مفتاح Gemini هنا لإضافته)\n"
              "/ghls [مجلد] · /ghshow مسار · /ghedit مسار | تعليمات · /ghdel مسار\n"
              "ملف مع التعليق /push مسار — رفع ملف\n/deploy — نشر · /ghreset — تجاهل التغييرات المعلقة")
    await m.answer(t)


@admin.message(Command("stats"))
async def stats(m: Message):
    s = await db.stats()
    t = (f"👥 المستخدمون: {s['users']}\n📦 المنشور: {s['approved']} | ⏳ المعلق: {s['pending']}\n"
         f"🔎 البحوث: {s['searches']} (آخر 24 ساعة: {s['today']})\n\n<b>أكثر البحوث:</b>\n")
    t += "\n".join(f"• {esc(r['query'])} ({r['c']})" for r in s["top_q"])
    t += "\n\n<b>الأكثر مشاهدة:</b>\n" + "\n".join(f"• #{r['id']} {esc(r['name'])} ({r['views']})" for r in s["top_e"])
    await m.answer(t)


@admin.message(Command("pending"))
async def pending(m: Message):
    rows = await db.pool.fetch(db.BASE + " WHERE e.status='pending' ORDER BY e.id LIMIT 10")
    if not rows:
        return await m.answer("لا توجد طلبات معلقة")
    for e in rows:
        await m.answer(card(e, True), reply_markup=decision_kb(e["id"]))


async def set_status(bot: Bot, eid: int, ok: bool):
    await db.update(eid, "status", "approved" if ok else "rejected")
    e = await db.get(eid)
    if e["added_by"]:
        try:
            await bot.send_message(e["added_by"], "✅ تمت الموافقة على إضافتك ونُشرت." if ok else "❌ لم تتم الموافقة على إضافتك.")
        except Exception:
            pass


@admin.callback_query(F.data.startswith("a:"))
async def decide(cb: CallbackQuery, bot: Bot):
    _, act, eid = cb.data.split(":")
    await set_status(bot, int(eid), act == "ok")
    await cb.message.edit_text(cb.message.html_text + ("\n\n✅ قُبل" if act == "ok" else "\n\n❌ رُفض"))
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
    buf = io.BytesIO()
    await bot.download(m.document, destination=buf)
    n = 0
    for r in csv.DictReader(io.StringIO(buf.getvalue().decode("utf-8-sig"))):
        if r.get("name"):
            await db.add_entry(r, m.from_user.id, "approved")
            n += 1
    await m.answer(f"✅ تم استيراد {n} سجل")


# ======================== المالك: الأدمنز والمفاتيح ========================
async def owner_only(m: Message) -> bool:
    if m.from_user.id in OWNER_IDS:
        return True
    await m.answer("⛔ هذا الأمر للمالك فقط.")
    return False


async def add_admin_flow(m: Message, bot: Bot, uid: int):
    if uid in ADMIN_IDS:
        return await m.answer("هو أدمن بالفعل.")
    await db.add_admin(uid, m.from_user.id)
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


KEY_RE = re.compile(r"AIza[\w-]{35}")


def mask(k):
    return k[:6] + "…" + k[-4:]


@admin.message(Command("keys"))
async def cmd_keys(m: Message):
    if not await owner_only(m):
        return
    ks, ms = await ai.get_keys(), await ai.get_models()
    t = "🔑 المفاتيح:\n" + ("\n".join(f"{i}. {mask(k)}" for i, k in enumerate(ks, 1)) or "—")
    await m.answer(t + "\n\n🤖 النماذج: " + ", ".join(ms) +
                   "\n\nأضف مفتاحاً بلصقه هنا مباشرة · /delkey رقم · /models اسم1,اسم2")


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
    found = KEY_RE.findall(m.text)
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
        return await wait.edit_text("⚠️ الذكاء الاصطناعي غير متاح أو لم يُرجع نتيجة.")
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
    if act == "x":
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


OWNER_ACTIONS = ("add_admin", "remove_admin", "list_admins", "gh_list", "gh_show", "gh_edit", "gh_delete", "deploy")


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
        if act == "add_admin":
            await add_admin_flow(m, bot, uid)
        else:
            await del_admin_flow(m, uid)
        return True
    if act == "deploy":
        await do_deploy(m)
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


@admin.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def admin_nl(m: Message, bot: Bot):
    if await handle_keys_in_text(m):  # المفاتيح لا تمر على الذكاء الاصطناعي أبداً
        return
    p = await ai.parse_admin(m.text)
    if p is None:  # الذكاء الاصطناعي غير متاح
        await m.answer("⚠️ الذكاء الاصطناعي غير متاح الآن، نفّذت بحثاً عادياً. استخدم /admin للأوامر اليدوية.")
        return await do_search(m, m.text)
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
        ids = [r["id"] for r in await db.pool.fetch("SELECT id FROM entries WHERE status='pending'")]
        for i in ids:
            await set_status(bot, i, True)
        return await m.answer(f"✅ تمت الموافقة على {len(ids)} سجل")

    if act in ("show", "approve", "reject", "edit", "boost", "delete"):
        e = await resolve(m, ref)
        if not e:
            return
        eid = e["id"]
        if act == "show":
            return await m.answer(card(e, True), reply_markup=kb(e))
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
            return await m.answer("✅ تم التعديل:\n" + card(e2, True), reply_markup=kb(e2))
        if act == "delete":  # يحتاج تأكيداً
            return await m.answer(f"⚠️ حذف نهائي لـ #{eid} {esc(e['name'])}؟", reply_markup=M(inline_keyboard=[[
                B(text="🗑 نعم، احذف", callback_data=f"c:del:{eid}"), B(text="إلغاء", callback_data="c:no")]]))

    if act == "add_entry":
        d = await ai.parse_entry(m.text)
        if not d or not d.get("name"):
            return await m.answer("ما قدرت أستخرج الاسم، أعد الكتابة.")
        eid = await db.add_entry(d, m.from_user.id, "approved")
        e = await db.get(eid)
        return await m.answer("✅ أُضيف ونُشر:\n" + card(e, True), reply_markup=kb(e))

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
        n = await db.cleanup_searches(days)
        return await m.answer(f"🧹 حُذف {n} سجل بحث أقدم من {days} يوماً.")

    if await owner_actions(m, bot, p, act):
        return

    await m.answer("ما فهمت الأمر. اكتبه بشكل أوضح أو استخدم /admin لعرض الأوامر.")


@admin.callback_query(F.data.startswith("c:"))
async def confirm(cb: CallbackQuery):
    parts = cb.data.split(":")
    if parts[1] == "del":
        await db.pool.execute("DELETE FROM entries WHERE id=$1", int(parts[2]))
        await cb.message.edit_text("🗑 تم الحذف")
    else:
        await cb.message.edit_text("أُلغي")
    await cb.answer()
