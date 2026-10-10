import asyncio

from app import bulk, redirect

SAMPLE = """محامون
المحامي عزالدين العلي 0957714030 دمشق

د. يحيى كيوان طب فيزيائي
الميدان - مركز إدارة
٠٩٥٧٦٠٣٥٥٧
"""


def test_looks_bulk_needs_three_phones():
    assert bulk.looks_bulk(SAMPLE + "\n0933111222")
    assert not bulk.looks_bulk(SAMPLE)                         # رقمان فقط: أمر/رسالة عادية
    assert not bulk.looks_bulk("وافق 12")
    assert bulk.looks_bulk("٠٩٥٧٦٠٣٥٥٧ ٠٩٣٣١١١٢٢٢ +963 944 555 666")  # أرقام عربية وصيغ مختلفة


def test_split_chunks_keeps_blocks_whole_and_covers_everything():
    blocks = [f"اسم {i}\n0957{i:06d}\nدمشق" for i in range(100)]
    text = "\n\n".join(blocks)
    chunks = bulk.split_chunks(text, size=500)
    assert len(chunks) > 1 and all(len(c) <= 500 for c in chunks)
    joined = "\n\n".join(chunks)
    assert all(b in joined for b in blocks)                    # لا تُكسر بطاقة ولا يضيع شيء
    assert bulk.split_chunks("") == [] and bulk.split_chunks("  \n ") == []


def test_split_chunks_handles_one_giant_line():
    chunks = bulk.split_chunks("ا" * 9000, size=4000)
    assert [len(c) for c in chunks] == [4000, 4000, 1000]


def test_entries_from_accepts_both_shapes_and_garbage():
    assert bulk.entries_from({"entries": [{"name": "a"}, "x", 3]}) == [{"name": "a"}]
    assert bulk.entries_from([{"name": "a"}]) == [{"name": "a"}]
    assert bulk.entries_from({"entries": "no"}) == [] and bulk.entries_from(None) == []


def test_clean_all_drops_nameless_and_normalizes_digits():
    out = bulk.clean_all([{"name": "د. يحيى", "phone": "٠٩٥٧٦٠٣٥٥٧"}, {"name": ""}, {"phone": "0933"}, {"name": "x"}])
    assert [d["name"] for d in out] == ["د. يحيى"] and out[0]["phone"] == "0957603557"


def test_dedupe_inside_batch():
    ds = [{"name": "عزالدين العلي", "phone": "0957714030"},
          {"name": "عزالدين  العلي", "phone": "+963957714030"},     # نفس الرقم بصيغة دولية
          {"name": "عزالدين العلي", "phone": "0933000111"},         # نفس الاسم، رقم مختلف: شخص آخر
          {"name": "سامي", "phone": "0957714030"}]                  # نفس الرقم، اسم آخر: يبقى
    out, removed = bulk.dedupe(ds)
    assert removed == 1 and len(out) == 3


def test_find_dups_against_database():
    existing = [{"name": "عزالدين العلي", "phone": "+963957714030"}, {"name": "مطعم الرحمة", "phone": None}]
    drafts = [{"name": "عزالدين العلي", "phone": "0957714030"},     # 0
              {"name": "عزالدين العلي", "phone": "0911222333"},     # 1 رقم مختلف
              {"name": "مطعم الرحمة", "phone": "0944000111"},       # 2 القديم بلا رقم ⇒ نعتبره مكرراً
              {"name": "جديد", "phone": "0944000111"}]              # 3
    assert bulk.find_dups(drafts, existing) == {0, 2}


def test_summary_pages_respect_telegram_limit():
    ds = [{"name": f"اسم طويل جداً رقم {i}", "category": "محامي", "city": "دمشق", "phone": "0957714030"}
          for i in range(300)]
    pages = bulk.summary_pages(ds, {0}, limit=3800)
    assert len(pages) > 1 and all(len(p) <= 4096 for p in pages)
    assert "🔁 موجودة" in pages[0] and sum(p.count("\n") + 1 for p in pages) == 300
    assert "⚠️ بلا هاتف" in bulk.summary_pages([{"name": "ب"}])[0]


def test_summary_escapes_html():
    assert "&lt;b&gt;" in bulk.summary_pages([{"name": "<b>x</b>"}])[0]


# ---------- صفحات التحويل ----------
def test_wa_target_mobile_vs_desktop():
    app, fb = redirect.wa_target("963957714030", "mozilla/5.0 (linux; android 14)")
    assert app == "whatsapp://send?phone=963957714030" and fb == "https://wa.me/963957714030"
    assert redirect.wa_target("963957714030", "mozilla/5.0 (windows nt 10)")[0] == ""


def test_map_target_per_platform():
    app, fb = redirect.map_target(33.5, 36.2, "android")
    assert app.startswith("intent://") and "package=com.google.android.apps.maps" in app
    assert fb == "https://www.google.com/maps/search/?api=1&query=33.5,36.2"
    assert redirect.map_target(33.5, 36.2, "iphone")[0] == "comgooglemaps://?q=33.5,36.2"
    assert redirect.map_target(33.5, 36.2, "x11")[0] == ""


def test_page_is_safe_html():
    h = redirect.page("whatsapp://send?phone=1", 'https://x/"</script><b>')
    assert "</script><b>" not in h.replace("</script></body>", "")  # لا كسر للسكربت


class _Req:
    def __init__(self, i, ua=""):
        self.match_info, self.headers = {"id": i}, {"User-Agent": ua}


def test_handlers_404_for_unapproved_or_missing(monkeypatch):
    async def fake_get(eid):
        return {1: {"status": "approved", "whatsapp": "0957714030", "lat": 33.5, "lng": 36.2},
                2: {"status": "pending", "whatsapp": "0957714030", "lat": 1, "lng": 2}}.get(eid)
    monkeypatch.setattr(redirect.db, "get", fake_get)
    run = asyncio.run
    assert run(redirect.go_wa(_Req("1", "android"))).status == 200
    assert run(redirect.go_map(_Req("1", "android"))).status == 200
    assert run(redirect.go_wa(_Req("2"))).status == 404      # معلقة: لا تسريب رقم
    assert run(redirect.go_map(_Req("9"))).status == 404
    assert run(redirect.go_wa(_Req("abc"))).status == 404


def test_numbers_splits_adjacent_and_keeps_grouped():
    assert bulk.numbers("0957603557 0933111222") == ["0957603557", "0933111222"]
    assert bulk.numbers("0957 603 557") == ["0957603557"]
    assert bulk.numbers("+963 944 555 666 و 0911222333") == ["963944555666", "0911222333"]
    assert bulk.numbers("الميدان 12 شارع 5") == []
    assert bulk.phone_keys("0957603557 / +963957603557") == {"957603557"}
