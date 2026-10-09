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
