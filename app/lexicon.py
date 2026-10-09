"""قاموس الأوامر: يحوّل العبارات العامية إلى «نوايا» بدل الأوامر المائلة (/add و/admin...).

المصدر هو الملف app/lexicon.json (يُحمَّل عند التشغيل ويُفحص في CI). لإضافة كلمة جديدة
يكفي إضافتها إلى قائمة النية المناسبة في الملف، دون تعديل أي كود.

أنماط المطابقة (يحددها الكود لكل نية، لا الملف، حتى لا يفتح تعديل الملف باباً خطراً):
  exact      الرسالة كلها تساوي العبارة (مثل: احصائيات)
  prefix     الرسالة تبدأ بالعبارة وما بعدها وسيط (مثل: اضف مطعم الرحمة درعا)
  prefix_id  العبارة ثم رقم سجل فقط (مثل: وافق 12 / احذف #12)
  hint       لا تُنفَّذ مباشرة؛ تُمرَّر للذكاء الاصطناعي كتلميح وتمنع الخلط مع البحث
إذا تعذّر تحميل الملف يُعطَّل القاموس ويعود البوت لسلوكه السابق (بحث / ذكاء اصطناعي).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import db

log = logging.getLogger("dalil.lexicon")
LEX_PATH = Path(__file__).with_name("lexicon.json")

# النية -> (النطاق، نمط المطابقة)
INTENTS: dict[str, tuple[str, str]] = {
    "thanks": ("user", "exact"), "help": ("user", "exact"), "myid": ("user", "exact"),
    "cancel": ("user", "exact"), "add": ("user", "prefix"), "search": ("user", "prefix"),
    "admin_help": ("admin", "exact"), "stats": ("admin", "exact"), "pending": ("admin", "exact"),
    "ads_list": ("admin", "exact"), "list_admins": ("admin", "exact"), "db_size": ("admin", "exact"),
    "show": ("admin", "prefix_id"), "approve": ("admin", "prefix_id"),
    "reject": ("admin", "prefix_id"), "delete": ("admin", "prefix_id"),
    "edit": ("admin", "hint"), "boost": ("admin", "hint"), "approve_all": ("admin", "hint"),
    "ad_add": ("admin", "hint"), "ad_off": ("admin", "hint"), "add_admin": ("admin", "hint"),
    "remove_admin": ("admin", "hint"), "cleanup": ("admin", "hint"), "deploy": ("admin", "hint"),
    "gh_list": ("admin", "hint"), "gh_show": ("admin", "hint"), "gh_edit": ("admin", "hint"),
    "gh_code": ("admin", "hint"), "gh_delete": ("admin", "hint"),
}
REQUIRED = ("add", "search", "help")
# النوايا التي تتحول مباشرة إلى أمر إداري جاهز (نفس مفاتيح الذكاء الاصطناعي)
ADMIN_PAYLOAD = {"stats", "pending", "ads_list", "list_admins", "db_size", "show", "approve", "reject", "delete"}
_AI_NAME = {"add": "add_entry", "search": "user_search"}
_TOP_KEYS = {"version", "fillers", "desire", "settings", "intents"}
_ID = re.compile(r"^#?\d{1,9}$")
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_PUNCT = ".,!?؟،:;\"'()[]{}«»…-_*~"
MAX_PHRASES, MAX_WORDS, MAX_CHARS = 200, 6, 40


class LexiconError(ValueError):
    pass


@dataclass(frozen=True)
class Match:
    intent: str
    rest: str
    deterministic: bool


@dataclass(frozen=True)
class Compiled:
    fillers: tuple
    desire: frozenset
    min_words: int
    phrases: tuple   # ((tokens, intent), ...) الأطول أولاً
    hints: str


def _words(p: str) -> tuple:
    return tuple(w for w in (db.norm(x.strip(_PUNCT)) for x in p.split()) if w)


def _strlist(v, where, single_word=False):
    if not isinstance(v, list):
        raise LexiconError(f"{where}: يجب أن تكون قائمة")
    out = []
    for x in v:
        if not isinstance(x, str) or not x.strip():
            raise LexiconError(f"{where}: عنصر غير صالح {x!r}")
        if len(x) > MAX_CHARS:
            raise LexiconError(f"{where}: عبارة أطول من {MAX_CHARS} حرفاً")
        w = _words(x)
        if not w or len(w) > MAX_WORDS or (single_word and len(w) != 1):
            raise LexiconError(f"{where}: عبارة غير صالحة {x!r}")
        out.append((x.strip(), w))
    return out


def validate(data) -> Compiled:
    """يفحص بنية القاموس ويبنيه؛ يرمي LexiconError عند أي خلل (يُستخدم عند التحميل وفي CI)."""
    if not isinstance(data, dict):
        raise LexiconError("الجذر يجب أن يكون كائناً")
    extra = {k for k in data if k not in _TOP_KEYS and not str(k).startswith("_")}
    if extra:
        raise LexiconError(f"مفاتيح غير معروفة: {sorted(extra)}")
    fillers = _strlist(data.get("fillers", []), "fillers")
    desire = _strlist(data.get("desire", []), "desire", single_word=True)
    settings = data.get("settings", {})
    if not isinstance(settings, dict):
        raise LexiconError("settings يجب أن تكون كائناً")
    mw = settings.get("direct_add_min_words", 3)
    if not isinstance(mw, int) or isinstance(mw, bool) or not 2 <= mw <= 10:
        raise LexiconError("direct_add_min_words يجب أن يكون عدداً بين 2 و10")
    intents = data.get("intents")
    if not isinstance(intents, dict):
        raise LexiconError("intents مفقودة")
    unknown = [k for k in intents if k not in INTENTS]
    if unknown:
        raise LexiconError(f"نوايا غير معروفة: {unknown}")
    for r in REQUIRED:
        if not intents.get(r):
            raise LexiconError(f"النية المطلوبة فارغة أو مفقودة: {r}")
    seen: dict[tuple, str] = {}
    phrases, shown = [], {}
    filler_set = {w for _, w in fillers}
    for name, lst in intents.items():
        items = _strlist(lst, f"intents.{name}")
        if len(items) > MAX_PHRASES:
            raise LexiconError(f"intents.{name}: أكثر من {MAX_PHRASES} عبارة")
        shown[name] = [o for o, _ in items]
        for orig, w in items:
            if w in seen and seen[w] != name:
                raise LexiconError(f"العبارة {orig!r} مكررة في «{name}» و«{seen[w]}»")
            if w in filler_set:
                raise LexiconError(f"العبارة {orig!r} موجودة أيضاً في fillers")
            seen[w] = name
            phrases.append((w, name))
    phrases = sorted(set(phrases), key=lambda x: -len(x[0]))
    order = [k for k in INTENTS if INTENTS[k][0] == "admin"] + ["add", "search"]
    lines = [f"- {_AI_NAME.get(k, k)}: " + " | ".join(shown[k][:10]) for k in order if shown.get(k)]
    hints = ""
    if lines:
        hints = ("Dialect glossary: these Arabic words/phrases (any dialect) signal the action:\n" + "\n".join(lines)
                 + "\nDesire words (mean 'I want'; not an action by themselves): "
                 + " | ".join(o for o, _ in desire[:20]))
    return Compiled(tuple(sorted({w for _, w in fillers}, key=lambda x: -len(x))),
                    frozenset(w[0] for _, w in desire), mw, tuple(phrases), hints)


def load(path: Path | None = None) -> Compiled:
    try:
        data = json.loads(Path(path or LEX_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LexiconError(f"تعذّر قراءة القاموس: {exc}") from exc
    return validate(data)


_state: dict = {"lex": None, "tried": False}


def get() -> Compiled | None:
    if not _state["tried"]:
        _state["tried"] = True
        try:
            _state["lex"] = load()
        except Exception:
            log.exception("القاموس معطّل (ملف غير صالح أو مفقود): %s", LEX_PATH)
    return _state["lex"]


def ai_hints() -> str:
    lex = get()
    return lex.hints if lex else ""


def min_words() -> int:
    lex = get()
    return lex.min_words if lex else 3


def _tokenize(text):
    out = []
    for raw in (text or "").split():
        o = raw.strip(_PUNCT)
        n = db.norm(o)
        if n:
            out.append((o, n))
    return out


def _best(lex, toks, start, admin):
    rest_n = len(toks) - start
    for ph, intent in lex.phrases:
        scope, mode = INTENTS[intent]
        k = len(ph)
        if (scope == "admin" and not admin) or k > rest_n:
            continue
        if tuple(t[1] for t in toks[start:start + k]) != ph:
            continue
        rest_toks = toks[start + k:]
        rest = " ".join(t[0] for t in rest_toks)
        if mode == "exact":
            if rest_toks:
                continue
            det = True
        elif mode == "prefix":
            det = True
        elif mode == "prefix_id":
            n = rest_toks[0][1].translate(_DIGITS) if len(rest_toks) == 1 else ""
            det = bool(_ID.match(n))
            if det:
                rest = n.lstrip("#")
        else:
            det = False
        return Match(intent, rest, det)
    return None


def match(text, admin=False) -> Match | None:
    """أفضل نية مطابقة لبداية الرسالة، أو None. admin=True يفعّل نوايا الأدمن."""
    lex = get()
    if lex is None:
        return None
    toks = _tokenize(text)
    if not toks:
        return None
    i = 0
    while i < len(toks):  # عبارات المجاملة/التحية في البداية (ممكن، لو سمحت، مرحبا ...)
        for f in lex.fillers:
            k = len(f)
            if tuple(t[1] for t in toks[i:i + k]) == f:
                i += k
                break
        else:
            break
    if i >= len(toks):
        return Match("help", "", True)  # تحية فقط
    best = _best(lex, toks, i, admin)
    j = i
    while j < len(toks) and toks[j][1] in lex.desire:
        j += 1
    if j > i:  # «بدي اضيف / ودي اعدل»: الأمر بعد كلمة الرغبة يتغلب على البحث
        alt = _best(lex, toks, j, admin)
        if alt and alt.intent != "search":
            return alt
    return best


def admin_payload(m: Match):
    """أمر إداري جاهز (بنفس شكل ناتج الذكاء الاصطناعي) للنوايا القطعية، وإلا None."""
    if m and m.deterministic and m.intent in ADMIN_PAYLOAD:
        return {"action": m.intent, "ref": m.rest}
    return None
