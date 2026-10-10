"""قالب بطاقة الدليل (المعاينة، النشر، المشاركة) + اكتشاف الإيموجي المناسب للمهنة والمكان.

دوال نقية بلا اتصال بتلغرام أو قاعدة البيانات، ليسهل اختبارها:
  render_html   نص البطاقة (HTML) كما يراها الزبون
  render_plain  نص عادي للمشاركة
  clean_draft   تنظيف ناتج الذكاء الاصطناعي إلى حقول البطاقة
  apply_edit    تطبيق تعديل حقل واحد (للمستخدم في المعاينة أو للأدمن)
"""
from __future__ import annotations

import html
import re

from . import db

esc = html.escape

MISSING = "غير متوفر"
HOURS_MISSING = "غير محدد"
DEFAULT_EMOJI = "🏷"

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# الحقول القابلة للتعديل: (المفتاح، الاسم الظاهر). «location» يعدّل lat/lng معاً.
FIELDS = [
    ("name", "الاسم"), ("category", "التخصص"), ("city", "المدينة"), ("address", "العنوان"),
    ("phone", "الهاتف"), ("whatsapp", "واتساب"), ("hours", "الدوام"), ("description", "الوصف"),
    ("location", "الموقع على الخريطة"),
]
LABEL = dict(FIELDS)
LIMITS = {"name": 100, "category": 60, "city": 40, "address": 160, "phone": 80, "whatsapp": 40,
          "hours": 160, "description": 500}
# الحقول الأساسية التي تظهر دائماً في البطاقة (وتُنبَّه إن كانت ناقصة)
CORE = ("category", "city", "address", "phone", "hours")

# ---------------------------------------------------------------- الإيموجي
# (كلمات مفتاحية بلا «ال»، الإيموجي) — الأخص أولاً. الكلمة ≥4 أحرف تطابق بداية الكلمة، وأقصر منها تطابق تاماً.
_RULES = [
    (("مشفى", "مستشفى", "اسعاف"), "🏥"),
    (("اسنان", "سنان"), "🦷"),
    (("عيون", "بصريات", "نظارات"), "👁"),
    (("صيدل",), "💊"),
    (("مخبر", "مختبر", "تحاليل", "اشعه"), "🔬"),
    (("بيطري", "حيوانات"), "🐾"),
    (("طبيب", "دكتور", "عياده", "جراح", "طب", "طبي", "نسائيه", "فيزيائي", "فيزيو"), "🩺"),
    (("محامي", "محاماه", "قانون", "قانوني"), "⚖️"),
    (("مهندس", "هندسي", "هندسه"), "📐"),
    (("مقاول", "مقاولات", "بناء", "تعهدات"), "🏗"),
    (("كهربائي", "كهرباء"), "⚡"),
    (("سباك", "سباكه", "نجار", "نجاره", "حداد", "حداده", "المنيوم"), "🔨"),
    (("دهان", "دهانات", "طلاء"), "🎨"),
    (("ميكانيكي", "ميكانيك", "كراج", "سيارات", "سياره", "بنشر", "كاوتشوك"), "🚗"),
    (("مطعم", "مطاعم", "شاورما", "فلافل", "مشاوي", "وجبات"), "🍽"),
    (("مقهى", "كافيه", "كافي", "قهوه", "كوفي"), "☕"),
    (("حلويات", "حلواني", "كيك", "بوظه"), "🍰"),
    (("فرن", "افران", "مخبز", "معجنات"), "🥖"),
    (("سوبرماركت", "بقاله", "ماركت", "تموينيه"), "🛒"),
    (("ملحمه", "لحام", "لحوم"), "🥩"),
    (("خضار", "فواكه", "خضروات"), "🥬"),
    (("مدرسه", "ثانويه", "اعداديه", "ابتدائيه"), "🏫"),
    (("جامعه", "معهد", "كليه", "اكاديميه"), "🎓"),
    (("روضه", "حضانه"), "🧸"),
    (("مدرس", "دروس", "تدريس", "مدرب", "تعليم"), "📚"),
    (("حلاق", "حلاقه"), "💈"),
    (("صالون", "تجميل", "كوافير", "كوافيره", "مشغل"), "💇"),
    (("فندق", "نزل", "موتيل"), "🏨"),
    (("سياحه", "سفر", "سفريات", "طيران"), "✈️"),
    (("تكسي", "اجره"), "🚕"),
    (("نقل", "شحن", "توصيل", "ترحيل"), "🚚"),
    (("بنك", "مصرف"), "🏦"),
    (("صرافه", "حوالات", "تحويل"), "💱"),
    (("محاسب", "محاسبه", "تدقيق"), "🧮"),
    (("عقار", "عقارات", "سمسار"), "🏠"),
    (("مصور", "تصوير", "استوديو"), "📷"),
    (("برمجه", "مبرمج", "حاسوب", "كمبيوتر", "تقنيه", "شبكات"), "💻"),
    (("موبايل", "موبايلات", "جوال", "هواتف"), "📱"),
    (("كترونيات", "كترونيه"), "🔌"),
    (("ملابس", "بوتيك", "خياط", "خياطه", "اقمشه", "احذيه", "حقائب"), "👗"),
    (("مجوهرات", "ذهب", "صاغه", "ساعات"), "💍"),
    (("مكتبه",), "📚"),
    (("قرطاسيه", "طباعه", "مطبعه"), "📝"),
    (("ورد", "زهور", "ازهار"), "💐"),
    (("جيم", "رياضه", "نادي", "لياقه"), "🏋"),
    (("مسجد", "جامع"), "🕌"),
    (("كنيسه",), "⛪"),
    (("جمعيه", "منظمه", "خيري"), "🤝"),
    (("اعلان", "دعايه", "تسويق"), "📢"),
    (("حفلات", "فعاليه", "مناسبات", "قاعه", "افراح"), "🎉"),
    (("اثاث", "مفروشات", "ديكور"), "🛋"),
    (("تنظيف",), "🧹"),
    (("مكيفات", "تبريد", "تكييف"), "❄️"),
    (("مولدات", "طاقه", "شمسيه", "بطاريات", "انفرتر"), "🔋"),
    (("زراعه", "مشتل", "اسمده"), "🌱"),
]

# إيموجي المكان (رأس البطاقة) عندما يختلف عن إيموجي المهنة، مثل: طبيب 🩺 ← عيادة 🏥
PLACE = {"🩺": "🏥", "🦷": "🏥", "👁": "🏥", "🔬": "🏥", "⚖️": "🏛", "📐": "🏢", "🧮": "🏢",
         "⚡": "🏪", "🔨": "🏪", "💈": "💈", "💇": "💇"}


def _strip_al(w: str) -> str:
    return w[2:] if w.startswith("ال") and len(w) > 3 else w


_COMPILED = [(tuple(_strip_al(db.norm(k)) for k in kws), em) for kws, em in _RULES]


def _tokens(*texts) -> list:
    return [_strip_al(t) for t in re.findall(r"\w+", db.norm(" ".join(str(x) for x in texts if x)))]


def _hit(tok: str, kw: str) -> bool:
    return tok == kw or (len(kw) >= 4 and tok.startswith(kw))


def detect_emoji(*texts) -> str:
    """إيموجي المهنة/النشاط من النصوص (التخصص أولاً ثم الاسم...) أو '' إن لم يُعرف."""
    toks = _tokens(*texts)
    for kws, em in _COMPILED:
        if any(_hit(t, k) for t in toks for k in kws):
            return em
    return ""


def valid_emoji(s) -> str:
    """يقبل إيموجي واحداً فقط (بلا حروف أو أرقام أو مسافات)، وإلا ''."""
    s = str(s or "").strip()
    return s if 1 <= len(s) <= 8 and not re.search(r"[\w\s]", s) else ""


def profession_emoji(e) -> str:
    g = e.get
    return (detect_emoji(g("category"))            # التخصص أدق دليل
            or detect_emoji(g("name"), g("description"))
            or valid_emoji(g("emoji"))              # اقتراح الذكاء الاصطناعي
            or DEFAULT_EMOJI)


def place_emoji(prof: str) -> str:
    return PLACE.get(prof, prof)


# ---------------------------------------------------------------- القيم والعرض
def _s(v) -> str:
    return str(v).strip() if v not in (None, "") else ""


def address_line(e) -> str:
    city, addr = _s(e.get("city")), _s(e.get("address"))
    if city and addr and db.norm(city) in db.norm(addr):
        return addr
    return " – ".join(x for x in (city, addr) if x)


def values(e, phones) -> dict:
    """قيم العرض: لا حقل أساسي يبقى فارغاً (تظهر «غير متوفر»/«غير محدد»)."""
    return {
        "name": _s(e.get("name")) or "بدون اسم",
        "category": _s(e.get("category")) or MISSING,
        "address": address_line(e) or MISSING,
        "phone": phones(_s(e.get("phone"))) or MISSING,
        "hours": _s(e.get("hours")) or HOURS_MISSING,
        "description": _s(e.get("description")),
    }


def missing(d) -> list:
    """أسماء الحقول الأساسية الناقصة (للتنبيه في المعاينة)."""
    return [LABEL[k] for k in CORE if not _s(d.get(k))]


def filled(d, key) -> bool:
    if key == "location":
        return d.get("lat") is not None and d.get("lng") is not None
    return bool(_s(d.get(key)))


def _ph_sep(phone: str) -> str:
    """رقم واحد بجانب العنوان، وعدة أرقام كل رقم في سطر تحت «الهاتف»."""
    return "\n" if "\n" in phone else " "


def render_html(e, phones, adm=False) -> str:
    v = values(e, phones)
    prof = profession_emoji(e)
    rows = [f"{prof} <b>التخصص:</b> {esc(v['category'])}",
            f"📍 <b>العنوان:</b> {esc(v['address'])}",
            f"📞 <b>الهاتف:</b>{_ph_sep(v['phone'])}{esc(v['phone'])}",
            f"⏰ <b>الدوام:</b> {esc(v['hours'])}"]
    if v["description"]:
        rows.append(f"ℹ️ {esc(v['description'])}")
    stats = [f"👁 {int(e.get('views') or 0)} مشاهدة"]
    if e.get("likes"):
        stats.append(f"👍 {e.get('likes')}")
    t = (f"{place_emoji(prof)} <b>{esc(v['name'])}</b>\n"
         f"<blockquote>{chr(10).join(rows)}</blockquote>\n" + "  ·  ".join(stats))
    if e.get("status") == "approved":
        t += "\n✅ <b>معتمد</b>"
    if adm:
        t += f"\n🆔 {e.get('id')} | {e.get('status')} | boost {e.get('boost')} | by {e.get('added_by')}"
    return t


def render_plain(e, phones) -> str:
    """نص المشاركة (بلا HTML)."""
    v = values(e, phones)
    prof = profession_emoji(e)
    lines = [f"{place_emoji(prof)} {v['name']}", f"{prof} التخصص: {v['category']}",
             f"📍 العنوان: {v['address']}", f"📞 الهاتف:{_ph_sep(v['phone'])}{v['phone']}", f"⏰ الدوام: {v['hours']}"]
    if v["description"]:
        lines.append(f"ℹ️ {v['description'][:200]}")
    return "\n".join(lines)


# ---------------------------------------------------------------- تنظيف وتعديل
def _coord(a, b):
    try:
        lat, lng = float(a), float(b)
    except (TypeError, ValueError):
        return None
    return (lat, lng) if -90 <= lat <= 90 and -180 <= lng <= 180 else None


_COORD_PATS = (r"@(-?\d+\.\d+),(-?\d+\.\d+)", r"[?&](?:q|ll)=(-?\d+\.\d+),(-?\d+\.\d+)",
               r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", r"(-?\d{1,3}\.\d+)\s*[,،;\s]\s*(-?\d{1,3}\.\d+)")


def parse_coords(s):
    """(lat, lng) من إحداثيات نصية أو رابط خرائط جوجل الكامل، وإلا None."""
    s = (s or "").translate(_AR_DIGITS)
    for p in _COORD_PATS:
        m = re.search(p, s)
        if m:
            c = _coord(m.group(1), m.group(2))
            if c:
                return c
    return None


def clean_draft(d) -> dict:
    """يحوّل ناتج الذكاء الاصطناعي إلى حقول بطاقة نظيفة (نصوص مقصوصة، أرقام لاتينية، إيموجي صالح)."""
    d = d or {}
    out = {}
    for k, n in LIMITS.items():
        v = _s(d.get(k))
        if k in ("phone", "whatsapp", "hours"):
            v = v.translate(_AR_DIGITS)
        out[k] = v[:n]
    c = _coord(d.get("lat"), d.get("lng"))
    out["lat"], out["lng"] = c if c else (None, None)
    out["emoji"] = valid_emoji(d.get("emoji"))
    return out


_CLEAR = {"-", "—", "حذف", "مسح", "بدون"}


# ترويسة إشعار «طلب تحديث بيانات» المرسل للأدمن؛ إن لُصقت مع القيمة عن طريق النسخ نحذفها
_NOTICE = re.compile(r"^[ \t]*(?:🔄\s*)?طلب تحديث بيانات(?:\s+من)?[ \t]*\d*[ \t]*:?[ \t]*\n?", re.M)


_HOURS_KW = ("دوام", "ساعات", "اوقات", "أوقات", "مواعيد", "ايام", "أيام", "صباحا", "صباحاً", "مساء", "مساءً")
_PHONE_KW = ("هاتف", "جوال", "موبايل", "تلفون", "واتس", "اتصال")
_ADDR_KW = ("عنوان", "شارع", "مقابل", "بجانب", "حي ")


def guess_field(text: str):
    """يخمّن الحقل الذي يقصده طلب التحديث من كلماته (hours/phone/address) أو None إن لم يتضح."""
    t = (text or "").translate(_AR_DIGITS)
    if any(k in t for k in _HOURS_KW):
        return "hours"
    if any(k in t for k in _PHONE_KW) or re.search(r"\d{7,}", re.sub(r"[ ().\-]", "", t)):
        return "phone"
    if any(k in t for k in _ADDR_KW):
        return "address"
    return None


def strip_notice(raw: str) -> str:
    return _NOTICE.sub("", raw or "").strip()


def apply_edit(d, field, raw):
    """يطبّق تعديل حقل على القاموس d. يعيد None عند النجاح أو رسالة خطأ بالعربية."""
    raw = strip_notice(raw)
    clear = raw in _CLEAR
    if field == "location":
        if clear:
            d["lat"] = d["lng"] = None
            return None
        c = parse_coords(raw)
        if not c:
            return ("ما قدرت أقرأ الموقع. أرسل الموقع من 📎 ← الموقع، أو الإحداثيات مثل: "
                    "33.5138, 36.2765 أو رابط خرائط جوجل الكامل.")
        d["lat"], d["lng"] = c
        return None
    if field not in LIMITS:
        return "حقل غير معروف."
    if clear:
        if field == "name":
            return "الاسم مطلوب ولا يمكن حذفه."
        d[field] = None
        return None
    if not raw:
        return "القيمة فارغة."
    if field == "name" and len(raw) < 2:
        return "الاسم قصير جداً."
    if field in ("phone", "whatsapp", "hours"):
        raw = raw.translate(_AR_DIGITS)
    if field in ("phone", "whatsapp") and not re.search(r"\d{7,}", re.sub(r"[ ().\-]", "", raw)):
        return "الرقم غير واضح، اكتبه بالأرقام (مثال: 0957603557)."
    d[field] = raw[:LIMITS[field]]
    return None
