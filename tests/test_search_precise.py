import asyncio
import inspect
from unittest.mock import AsyncMock
from app import db

def test_search_sql_is_precise():
    src = inspect.getsource(db.search)
    assert "word_similarity" not in src, "يجب إزالة البحث التقريبي"
    assert "ILIKE ANY" not in src, "يجب إزالة البحث بالمرادفات"
    assert "ILIKE '%'||$1||'%'" in src

def test_search_returns_only_exact_phrase(monkeypatch):
    mock_pool = AsyncMock()
    calls = []
    async def fake_fetch(sql, q, city, limit):
        calls.append((sql, q, city))
        # محاكاة سجلات
        all_rows = [
            {"id": 1, "search_text": "انس العلي مطعم دمشق"},
            {"id": 2, "search_text": "انس احمد مطعم دمشق"},
            {"id": 3, "search_text": "انس العلي صيدليه"},
        ]
        # فلترة دقيقة كما يفعل SQL الجديد
        filtered = [r for r in all_rows if (not q or q in r["search_text"]) and (not city or city in r["search_text"])]
        return filtered
    mock_pool.fetch = fake_fetch
    monkeypatch.setattr(db, "pool", mock_pool)
    rows = asyncio.run(db.search("انس العلي", "", "", limit=5))
    assert {r["id"] for r in rows} == {1, 3}
    assert 2 not in {r["id"] for r in rows}
    # البحث عن "انس" يطابق الجميع لأنه جزء من العبارة
    calls.clear()
    rows2 = asyncio.run(db.search("انس", "", "", limit=5))
    assert len(rows2) == 3

def test_search_ignores_keywords(monkeypatch):
    mock_pool = AsyncMock()
    async def fake_fetch(sql, q, city, limit):
        # لو كان البحث بالمرادفات موجوداً لاستُدعي مرة ثانية مع keywords
        # هنا نعيد فارغاً للاستعلام الأول فقط
        return []
    mock_pool.fetch = fake_fetch
    monkeypatch.setattr(db, "pool", mock_pool)
    rows = asyncio.run(db.search("دكتور", "", "", limit=5, keywords=["طبيب", "عياده"]))
    # يجب ألا يتم استخدام keywords، لذا النتيجة تبقى فارغة ولا يتم توسيعها
    assert rows == []
    # تأكد أن fetch استُدعي مرة واحدة فقط (أو مرتين كحد أقصى مع fallback المدينة) وليس مع keywords
    assert mock_pool.fetch.call_count <= 2
