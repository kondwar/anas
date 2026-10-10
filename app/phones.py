"""التعرف على نوع رقم الهاتف (أرضي / سيرياتل / MTN أو زين / جوال آخر) واختيار الإيموجي المناسب.

دوال نقية بلا اتصال بتلغرام أو قاعدة البيانات (مثل cardfmt.py) ليسهل اختبارها:
  classify        نوع الرقم + المشغّل + الإيموجي + الصيغة الدولية
  emoji_for       إيموجي الرقم فقط ('' إن لم يُعرف)
  extract         كل الأرقام الصالحة في حقل الهاتف (قائمة classify)
  auto_whatsapp   أول رقم جوال في الحقل (مرشّح لواتساب) أو None
  call_numbers    أرقام الاتصال بالترتيب (الجوال أولاً ثم الأرضي)

ملاحظة: لا توجد طريقة رسمية للتأكد أن رقماً عليه واتساب بلا إرسال رسالة له؛
لذلك نعتبر كل رقم جوال مرشّحاً لواتساب، ويمكن للأدمن تحديد رقم واتساب مختلف يدوياً.
"""
from __future__ import annotations

import os
import re

CC = os.getenv("DEFAULT_CC", "963")

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_PH_SPLIT = re.compile(r"\s*[,،/|;\n]\s*|\s+[-–—]\s+|\s+و\s+")   # نفس فاصل handlers.fmt_phones
_PH_NUM = re.compile(r"\+?\d[\d ().\-]{5,}\d")

LANDLINE_EMOJI = "☎️"
MOBILE_EMOJI = "📱"  # جوال بمشغّل غير معروف

# رمز المنطقة (بعد حذف الصفر) → المحافظة، للأرضي فقط
AREA_CODES = {
    "11": "دمشق وريفها", "14": "القنيطرة", "15": "درعا", "16": "السويداء",
    "21": "حلب", "22": "الرقة", "23": "إدلب", "31": "حمص", "33": "حماة",
    "41": "اللاذقية", "43": "طرطوس", "51": "دير الزور", "52": "الحسكة",
}

# بادئات الجوال الوطنية (3 خانات بالصفر) → مفتاح المشغّل.
# تنبيه: لا يوجد نقل أرقام في سوريا، فالبادئة تحدد المشغّل الأصلي.
_PREFIX_OP = {"093": "syriatel", "098": "syriatel", "099": "syriatel",
              "094": "mtn", "095": "mtn", "096": "mtn"}

# بعد انسحاب MTN فازت زين بالترخيص (إطلاق العلامة متوقع 2027). عند الإطلاق ضع MTN_BRAND=zain
# في متغيرات البيئة فتظهر بادئات 094/095/096 على أنها زين، دون تعديل أي كود.
_MTN_BRAND = os.getenv("MTN_BRAND", "mtn").strip().lower()
_MTN_KEY = "zain" if _MTN_BRAND == "zain" else "mtn"

# مفتاح المشغّل → (الاسم، الإيموجي). الألوان تشبه ألوان الشعارات (سيرياتل أحمر، MTN أصفر).
OPERATORS = {
    "syriatel": ("سيرياتل", "🔴"),
    "mtn": ("MTN", "🟡"),
    "zain": ("زين", "🟣"),
}


def _digits(s) -> str:
    return "".join(c for c in str(s or "").translate(_AR_DIGITS) if c.isdigit())


def _national(raw) -> str:
    """الرقم الوطني بلا الصفر الأول ولا رمز الدولة (مثل 933123456 أو 112345678)."""
    d = _digits(raw)
    if d.startswith("00" + CC):
        return d[2 + len(CC):]
    if d.startswith(CC) and len(d) >= 11:   # 963933123456
        return d[len(CC):]
    if d.startswith("0"):
        return d[1:]
    return d


def classify(raw) -> dict:
    """يصنّف رقماً واحداً. kind: mobile | landline | other.

    الحقول: kind, intl (+963...), national (0...), operator (مفتاح أو ''), operator_name,
    area (اسم المنطقة أو ''), emoji.
    """
    n = _national(raw)
    out = {"kind": "other", "intl": "", "national": "", "operator": "", "operator_name": "",
           "area": "", "emoji": ""}
    if re.fullmatch(r"9\d{8}", n):                       # جوال: 09X XXXXXXX
        out.update(kind="mobile", intl=f"+{CC}{n}", national="0" + n)
        key = _PREFIX_OP.get("0" + n[:2], "")
        key = _MTN_KEY if key == "mtn" else key
        if key:
            name, em = OPERATORS[key]
            out.update(operator=key, operator_name=name, emoji=em)
        else:
            out["emoji"] = MOBILE_EMOJI
    elif re.fullmatch(r"(?:%s)\d{6,7}" % "|".join(AREA_CODES), n):   # أرضي مع رمز المنطقة
        out.update(kind="landline", intl=f"+{CC}{n}", national="0" + n,
                   area=AREA_CODES[n[:2]], emoji=LANDLINE_EMOJI)
    elif re.fullmatch(r"[2-8]\d{5,6}", n):               # أرضي محلي بلا رمز المنطقة
        out.update(kind="landline", national=n, emoji=LANDLINE_EMOJI)
    return out


def emoji_for(raw) -> str:
    return classify(raw)["emoji"]


def extract(field) -> list:
    """كل الأرقام المعروفة في حقل الهاتف (بترتيب الكتابة). الأرقام غير المفهومة تُتجاهل."""
    parts = _PH_SPLIT.split(str(field or "").translate(_AR_DIGITS))
    found = [m for x in parts for m in _PH_NUM.findall(x)]
    return [c for c in (classify(x) for x in found) if c["kind"] != "other"]


def auto_whatsapp(field):
    """أول رقم جوال بصيغة دولية (+963...) كمرشّح لواتساب، أو None. يُعطَّل بـ AUTO_WHATSAPP=0."""
    if os.getenv("AUTO_WHATSAPP", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    for c in extract(field):
        if c["kind"] == "mobile":
            return c["intl"]
    return None


def call_numbers(field) -> list:
    """أرقام الاتصال الصالحة بصيغة دولية: الجوال أولاً ثم الأرضي (الأرضي المحلي بلا رمز منطقة يُستبعد)."""
    nums = [c for c in extract(field) if c["intl"]]
    nums.sort(key=lambda c: c["kind"] != "mobile")   # sort مستقر: يحفظ ترتيب الكتابة داخل كل نوع
    return [c["intl"] for c in nums]
