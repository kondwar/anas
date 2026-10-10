"""استيراد جماعي: نص طويل فيه عشرات العناوين ← بطاقات منفردة.

دوال نقية (بلا تلغرام ولا قاعدة بيانات) ليسهل اختبارها:
  looks_bulk      هل النص يبدو دفعة عناوين (3 أرقام هاتف فأكثر)؟
  split_chunks    تقسيم النص الطويل إلى أجزاء آمنة لطلبات الذكاء الاصطناعي
  entries_from    استخراج قائمة السجلات من استجابة النموذج
  dedupe          حذف المكرر داخل الدفعة نفسها
  find_dups       كشف المكرر مقابل السجلات الموجودة في القاعدة
  summary_pages   نص ملخص الدفعة (صفحات ≤ حد تلغرام)
"""
from __future__ import annotations

import html
import re

from . import cardfmt as cf
from . import db

esc = html.escape

MAX_TEXT = 200_000      # أقصى حجم نص يُحلَّل في دفعة واحدة
MAX_ENTRIES = 300       # أقصى عدد بطاقات في دفعة واحدة
CHUNK = 4000            # حجم الجزء الواحد المرسل للنموذج (حرف)

_PHONE = re.compile(r"(?:\+|00)?\d[\d ()\-]{7,}\d")


def _digits(s) -> str:
    return str(s or "").translate(cf._AR_DIGITS)


def numbers(text) -> list:
    """أرقام الهاتف في النص (أرقام فقط). الأرقام المتجاورة بمسافة تُفصل: «0957603557 0933111222»
    رقمان، بينما «0957 603 557» و«+963 944 555 666» رقم واحد مجزّأ."""
    out = []
    for cand in _PHONE.findall(_digits(text)):
        cur, cur_plus = "", False
        for tok in cand.split():
            d = "".join(c for c in tok if c.isdigit())
            if not d:
                continue
            need = 12 if cur_plus else 9   # الصيغة الدولية (+ أو 00) تُكمَل حتى 12 رقماً
            if cur and len(cur) < need:
                cur += d
            else:
                if cur:
                    out.append(cur)
                cur, cur_plus = d, tok.startswith("+") or d.startswith("00")
        if cur:
            out.append(cur)
    return [n for n in out if 7 <= len(n) <= 15]


def looks_bulk(text, min_phones=3) -> bool:
    """النص يحوي ≥ 3 أرقام هاتف ⇒ غالباً قائمة عناوين وليست أمراً عادياً."""
    return len(numbers(text)) >= min_phones


def split_chunks(text, size=CHUNK) -> list:
    """يقسّم النص على الفواصل الطبيعية (سطر فارغ ثم سطر) دون كسر بطاقة في المنتصف قدر الإمكان."""
    text = (text or "").strip()
    if not text:
        return []
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    units = []
    for b in blocks:  # كتلة أطول من الحد: نقسمها على الأسطر، وسطر أطول من الحد: على الحروف
        if len(b) <= size:
            units.append(b)
            continue
        for ln in b.split("\n"):
            while len(ln) > size:
                units.append(ln[:size])
                ln = ln[size:]
            if ln.strip():
                units.append(ln)
    chunks, cur = [], ""
    for u in units:
        if cur and len(cur) + len(u) + 2 > size:
            chunks.append(cur)
            cur = u
        else:
            cur = f"{cur}\n\n{u}" if cur else u
    if cur:
        chunks.append(cur)
    return chunks


def entries_from(obj) -> list:
    """قائمة القواميس من استجابة النموذج: {"entries":[...]} أو [...] مباشرة."""
    if isinstance(obj, dict):
        obj = obj.get("entries")
    if not isinstance(obj, list):
        return []
    return [x for x in obj if isinstance(x, dict)]


def phone_keys(p) -> set:
    """آخر 9 أرقام من كل رقم في الحقل (يتجاهل رمز الدولة والصفر الأول)."""
    return {n[-9:] for n in numbers(p)}


def _same(a: dict, name_key: str, keys: set) -> bool:
    return db.norm(a.get("name")) == name_key and (not keys or not phone_keys(a.get("phone")) or
                                                    bool(keys & phone_keys(a.get("phone"))))


def dedupe(drafts) -> tuple:
    """(قائمة فريدة، عدد المحذوف): مكرر = نفس الاسم وأرقام متقاطعة (أو أحدهما بلا رقم)."""
    out, removed = [], 0
    for d in drafts:
        nk, ks = db.norm(d.get("name")), phone_keys(d.get("phone"))
        if any(_same(o, nk, ks) for o in out):
            removed += 1
            continue
        out.append(d)
    return out, removed


def find_dups(drafts, existing_rows) -> set:
    """فهارس البطاقات التي لها نظير في القاعدة. existing_rows: عناصر فيها name و phone."""
    names = {}
    for r in existing_rows:
        names.setdefault(db.norm(r["name"]), []).append(phone_keys(r["phone"]))
    flags = set()
    for i, d in enumerate(drafts):
        group = names.get(db.norm(d.get("name")))
        if not group:
            continue
        mine = phone_keys(d.get("phone"))
        if not mine or any(not ks or ks & mine for ks in group):
            flags.add(i)
    return flags


def clean_all(raw_entries) -> list:
    """ينظّف كل سجل بقالب البطاقة ويُسقط ما بلا اسم صالح."""
    out = []
    for r in raw_entries:
        d = cf.clean_draft(r)
        if len(d.get("name") or "") >= 2:
            out.append(d)
    return out[:MAX_ENTRIES]


def summary_line(i, d, dup=False) -> str:
    prof = cf.profession_emoji(d)
    parts = [f"{i}. {prof} <b>{esc(d['name'])}</b>"]
    if d.get("category"):
        parts.append(esc(d["category"]))
    if d.get("city"):
        parts.append(esc(d["city"]))
    parts.append(esc(d["phone"]) if d.get("phone") else "⚠️ بلا هاتف")
    if dup:
        parts.append("🔁 موجودة")
    return " — ".join(parts)


def summary_pages(drafts, dups=(), limit=3800) -> list:
    """ملخص الدفعة مقسوماً إلى رسائل لا تتجاوز حد تلغرام (4096)."""
    pages, cur = [], ""
    for i, d in enumerate(drafts, 1):
        line = summary_line(i, d, (i - 1) in dups)
        if cur and len(cur) + len(line) + 1 > limit:
            pages.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        pages.append(cur)
    return pages
