import pytest

from app import ai, coding_agent, db
from app import github as gh
from app import handlers


# ---------- db.norm ----------
def test_norm_unifies_arabic_letters():
    assert db.norm("أَحْمَد") == db.norm("احمد")
    assert db.norm("مدرسة") == db.norm("مدرسه")
    assert db.norm("  مطعم   الرحمة ") == "مطعم الرحمه"


# ---------- ai.extract_json ----------
def test_extract_json_plain_fenced_and_buried():
    assert ai.extract_json('{"a": 1}') == {"a": 1}
    assert ai.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert ai.extract_json('هذه النتيجة: {"a": 1} شكراً') == {"a": 1}


def test_extract_json_rejects_garbage():
    assert ai.extract_json("") is None
    assert ai.extract_json("no json here") is None
    assert ai.extract_json("{}") is None
    assert ai.extract_json("42") is None
    assert ai.extract_json(None) is None


# ---------- github.check_path ----------
@pytest.mark.parametrize("bad", ["", "../x", "a/../b", ".env", "app/.env.local", ".git/config", "a//b", "/"])
def test_check_path_rejects(bad):
    with pytest.raises(gh.GHError):
        gh.check_path(bad)


def test_check_path_accepts():
    assert gh.check_path("app/db.py") == "app/db.py"
    assert gh.check_path("/app/db.py") == "app/db.py"


# ---------- handlers helpers ----------
def test_wa_link():
    assert handlers.wa_link("0912345678") == "https://wa.me/963912345678"
    assert handlers.wa_link("00963912345678") == "https://wa.me/963912345678"
    assert handlers.wa_link("+963 912 345 678") == "https://wa.me/963912345678"
    assert handlers.wa_link("abc") is None
    assert handlers.wa_link("123") is None
    assert handlers.wa_link(None) is None


def test_quick_admin_action():
    assert handlers.quick_admin_action("إحصائيات") == "stats"
    assert handlers.quick_admin_action("الطلبات المعلقة") == "pending"
    assert handlers.quick_admin_action("احذف كل شيء") is None


def test_rate_limit(monkeypatch):
    monkeypatch.setattr(handlers, "_RATE_MAX", 3)
    uid = 987654321
    handlers._hits.pop(uid, None)
    assert [handlers.rate_ok(uid) for _ in range(5)] == [True, True, True, False, False]


# ---------- coding agent validation ----------
SRC = {"app/x.py": "def f():\n    return 1\n\n\ndef g():\n    return 2\n" + "# pad\n" * 1000}


def _ok(changes):
    return coding_agent._validate_changes({"summary": "s", "changes": changes}, SRC)


def test_edits_are_applied_and_return_full_content():
    _, res, _ = _ok([{"path": "app/x.py", "edits": [{"search": "return 1", "replace": "return 10"}]}])
    assert "return 10" in res["app/x.py"] and "return 2" in res["app/x.py"]


def test_edit_search_must_match_exactly_once():
    with pytest.raises(coding_agent.CodingTaskError):
        _ok([{"path": "app/x.py", "edits": [{"search": "return 99", "replace": "x"}]}])
    with pytest.raises(coding_agent.CodingTaskError):
        _ok([{"path": "app/x.py", "edits": [{"search": "return", "replace": "x"}]}])  # appears twice


def test_new_file_ok_and_syntax_checked():
    _, res, _ = _ok([{"path": "tests/test_new.py", "content": "def test_a():\n    assert True\n"}])
    assert "tests/test_new.py" in res
    with pytest.raises(coding_agent.CodingTaskError):
        _ok([{"path": "app/new.py", "content": "def broken(:\n"}])


def test_big_existing_file_cannot_be_rewritten_whole():
    with pytest.raises(coding_agent.CodingTaskError):
        _ok([{"path": "app/x.py", "content": "print(1)\n"}])


@pytest.mark.parametrize("bad", [".github/workflows/code-checks.yml", "render.yaml", ".env", "app/.env.local",
                                 ".git/config", "../evil.py", "logo.png"])
def test_protected_or_unsafe_paths_rejected(bad):
    with pytest.raises(coding_agent.CodingTaskError):
        _ok([{"path": bad, "content": "x: 1\n"}])


def test_needs_exactly_one_of_edits_or_content():
    with pytest.raises(coding_agent.CodingTaskError):
        _ok([{"path": "tests/a.py"}])
    with pytest.raises(coding_agent.CodingTaskError):
        _ok([{"path": "tests/a.py", "content": "x=1\n", "edits": []}])


def test_key_regex_matches_old_and_new_formats():
    old = "AIza" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R"[:35]
    new = "AQ.Ab8RN6" + "x" * 40
    assert handlers.KEY_RE.findall(f"key: {old} ok") == [old]
    assert handlers.KEY_RE.findall(f"{new}.") == [new + "."]  # النقطة الأخيرة تُزال في handle_keys_in_text
    assert handlers.KEY_RE.findall("مطعم الرحمة بدرعا") == []
    assert handlers.KEY_RE.findall("AQ.short") == []


# ---------- قاموس الأوامر (app/lexicon.json) ----------
import json  # noqa: E402

from app import lexicon  # noqa: E402


def _m(text, admin=False):
    return lexicon.match(text, admin=admin)


def test_shipped_lexicon_is_valid():
    lexicon.load()


@pytest.mark.parametrize("bad", [
    {"intents": {"add": ["اضف"], "search": ["بدي"]}},                                  # help مفقودة
    {"intents": {"add": ["اضف"], "search": ["اضف"], "help": ["مساعدة"]}},               # عبارة مكررة بين نيتين
    {"intents": {"add": ["اضف"], "search": ["بدي"], "help": ["مساعدة"], "hack": ["x"]}},  # نية غير معروفة
    {"intents": {"add": ["اضف"], "search": ["بدي"], "help": [""]}},                      # عبارة فارغة
    {"intents": {"add": ["اضف"], "search": ["بدي"], "help": ["مساعدة"]}, "evil": 1},      # مفتاح غير معروف
    {"intents": {"add": ["اضف"], "search": ["بدي"], "help": ["مساعدة"]}, "settings": {"direct_add_min_words": 99}},
])
def test_invalid_lexicons_are_rejected(bad):
    with pytest.raises(lexicon.LexiconError):
        lexicon.validate(bad)


@pytest.mark.parametrize("text,intent", [
    ("اضف", "add"), ("ضف مكان", "add"), ("بدي اضيف محل", "add"), ("ودي اضيف مطعم", "add"),
    ("ممكن تضيف", "add"), ("لو سمحت ضيفلي مكان", "add"),
    ("بدي رقم مطعم الرحمة", "search"), ("ودي صيدلية بدرعا", "search"), ("وين مطعم الشام", "search"),
    ("شو في مطاعم بحمص", "search"), ("ابغى دكتور اسنان", "search"), ("دلني على مدرسة", "search"),
    ("الغاء", "cancel"), ("كنسل", "cancel"), ("مساعدة", "help"), ("مرحبا", "help"),
    ("السلام عليكم", "help"), ("شكرا", "thanks"), ("ايديي", "myid"),
])
def test_user_dialect_intents(text, intent):
    r = _m(text)
    assert r and r.intent == intent and r.deterministic, (text, r)


def test_search_verb_is_stripped_and_desire_does_not_hide_commands():
    assert _m("بدي رقم مطعم الرحمة").rest == "رقم مطعم الرحمة"
    assert _m("بدي اضيف مطعم الرحمة درعا").rest == "مطعم الرحمة درعا"
    assert _m("وين مطعم الشام").rest == "مطعم الشام"


def test_ordinary_searches_are_not_hijacked():
    for t in ["سجل مدني درعا", "مطعم الرحمة", "زيد المحامي", "رقم صيدلية الشفاء"]:
        r = _m(t)
        assert r is None or r.intent == "search", (t, r)
    assert _m("سجل مدني درعا") is None


def test_admin_intents_need_admin_and_ids():
    assert _m("احصائيات") is None or _m("احصائيات").intent != "stats"      # مستخدم عادي: بحث
    assert _m("احذف 12") is None
    for text, intent, rest in [("احصائيات", "stats", ""), ("وافق 12", "approve", "12"),
                               ("وافق على 12", "approve", "12"), ("ارفض #7", "reject", "7"),
                               ("احذف ١٢", "delete", "12"), ("وريني 5", "show", "5"),
                               ("الطلبات المعلقة", "pending", ""), ("الاعلانات", "ads_list", "")]:
        r = _m(text, admin=True)
        assert r and r.intent == intent and r.rest == rest and r.deterministic, (text, r)
        assert lexicon.admin_payload(r) == {"action": intent, "ref": rest}


def test_admin_commands_with_free_text_defer_to_ai():
    for t in ["احذف مطعم الرحمة", "عدل 15 الهاتف 0912345678", "بدي اعدل مطعم الشام", "وافق على الكل",
              "اضف اعلان مطعم | عرض", "انشر التحديث", "ضف ميزة جديدة للبوت"]:
        r = _m(t, admin=True)
        assert r and not r.deterministic, (t, r)
        assert lexicon.admin_payload(r) is None


def test_ai_hints_contain_dialect_words():
    h = lexicon.ai_hints()
    assert "add_entry" in h and "edit" in h and "ودي" in h


def test_coding_agent_rejects_bad_lexicon_edits():
    src = {"app/lexicon.json": open(lexicon.LEX_PATH, encoding="utf-8").read()}
    broken = [{"path": "app/lexicon.json", "edits": [{"search": '"version": 1', "replace": '"version": 1, "evil": 1'}]}]
    with pytest.raises(coding_agent.CodingTaskError):
        coding_agent._validate_changes({"summary": "s", "changes": broken}, src)
    good = [{"path": "app/lexicon.json", "edits": [{"search": '"ابدأ"', "replace": '"ابدأ", "ابدا يا بوت"'}]}]
    _, res, _ = coding_agent._validate_changes({"summary": "s", "changes": good}, src)
    lexicon.validate(json.loads(res["app/lexicon.json"]))


# ---------- موجّهات الفهم (اللهجة/السياق/الأخطاء الإملائية) ----------
def test_prompts_carry_understanding_blocks_and_keep_schemas():
    for p in (ai.SEARCH_SYS, ai.ADD_SYS, ai.ADMIN_SYS):
        assert "dialect" in p and "typos" in p and "NEVER invent" in p
    assert "Command conversion" in ai.ADMIN_SYS and '"action"' in ai.ADMIN_SYS and "user_search" in ai.ADMIN_SYS
    assert '"changes"' in ai.CODING_SYS and '"edits"' in ai.CODING_SYS and "Arabic" in ai.CODING_SYS
    assert '"content"' in ai.EDIT_SYS and "unchanged" in ai.EDIT_SYS
    assert '"name"' in ai.SEARCH_SYS and '"city"' in ai.SEARCH_SYS


# ---------- أرقام الهاتف القابلة للضغط ----------
@pytest.mark.parametrize("raw,expected", [
    ("0912345678", "+963912345678"),
    ("0912 345 678", "+963912345678"),
    ("+963 912 345 678", "+963912345678"),
    ("00963912345678", "+963912345678"),
    ("963912345678", "+963912345678"),
    ("912345678", "+963912345678"),
    ("٠٩١٢٣٤٥٦٧٨", "+963912345678"),
    ("011-1234567", "+963111234567"),
    ("0912345678 - 0933111222", "+963912345678 · +963933111222"),
    ("0912345678 / 0933111222", "+963912345678 · +963933111222"),
    ("0912345678 و 0933111222", "+963912345678 · +963933111222"),
    ("جوال: 0912345678", "جوال: +963912345678"),
    ("+905551112233", "+905551112233"),
    ("123", "123"),
    ("", ""),
])
def test_fmt_phones(raw, expected):
    assert handlers.fmt_phones(raw) == expected


def test_card_shows_clickable_international_phone():
    e = {"name": "مطعم", "category": "مطعم", "city": "درعا", "address": "", "phone": "0912345678",
         "description": "", "views": 0, "id": 1, "status": "approved", "boost": 0, "added_by": 1}
    assert "📞 <b>الهاتف:</b> +963912345678" in handlers.card(e)


# ---------- ai._clean_keywords (مرادفات البحث الاحتياطي) ----------
def test_clean_keywords_normalizes_dedups_and_filters():
    parsed = {"name": "", "category": "دكتور", "city": "درعا"}
    out = ai._clean_keywords(["طبيب", "دكتور", "عيادة", "طبيب", "%", "_x_", 5, None, "مركز طبي"], parsed)
    assert out == ["طبيب", "عياده", "مركز طبي"]
    assert "دكتور" not in out and "%" not in out
    assert ai._clean_keywords("not a list", parsed) == []
