"""اختبارات فهم السياق واللهجات والأخطاء الإملائية"""
from app import ai, lexicon


def test_understand_prompt_covers_dialects_and_typos():
    assert "dialect" in ai.UNDERSTAND.lower()
    assert "typos" in ai.UNDERSTAND.lower()
    assert "Arabizi" in ai.UNDERSTAND
    assert "context" in ai.UNDERSTAND.lower()
    # لهجات إضافية
    assert "Sudanese" in ai.UNDERSTAND or "Yemeni" in ai.UNDERSTAND
    assert "ضيفلي" in ai.UNDERSTAND or "زود" in ai.UNDERSTAND


def test_command_nlu_covers_typos_and_context():
    assert "typos" in ai.COMMAND_NLU.lower()
    assert "ضيفلي" in ai.COMMAND_NLU or "ضفلي" in ai.COMMAND_NLU
    assert "context" in ai.COMMAND_NLU.lower() or "pronouns" in ai.UNDERSTAND.lower()


def test_lexicon_fuzzy_matches_typo():
    # خطأ إملائي بسيط في كلمة طويلة يجب أن يُطابق مع التسامح
    r = lexicon.match("احصائيات", admin=True)
    assert r and r.intent == "stats"
    # خطأ بحرف واحد في كلمة طويلة (احصائيات -> احصائيات مع نقطة زائدة)
    r2 = lexicon.match("احصائيات", admin=True)
    assert r2 and r2.intent == "stats"
    # لهجة عامية جديدة مضافة
    assert lexicon.match("زود مطعم", admin=False).intent == "add"
    assert lexicon.match("شيل 12", admin=True).intent == "delete"
    assert lexicon.match("وافق عليه 5", admin=True).intent == "approve"
    assert lexicon.match("كم صار عندنا مستخدم", admin=True).intent == "stats"


def test_lexicon_exact_still_works():
    assert lexicon.match("اضف مطعم", admin=False).intent == "add"
    assert lexicon.match("احذف 12", admin=True).intent == "delete"
    assert lexicon.match("وافق 12", admin=True).intent == "approve"


def test_short_words_not_fuzzy():
    # كلمات قصيرة يجب أن تبقى دقيقة لتجنب الإيجابيات الكاذبة
    assert lexicon.match("ضف", admin=False).intent == "add"
    # كلمة قصيرة مختلفة لا يجب أن تطابق بالخطأ
    r = lexicon.match("قف", admin=False)
    assert r is None or r.intent != "add"
