from urllib.parse import parse_qs, urlparse

from app import cardfmt as cf
from app import handlers


def _phones(s):
    return handlers.fmt_phones(s)


DOC = {"name": "د. يحيى كيوان", "category": "طب فيزيائي", "city": "دمشق", "address": "الميدان - مركز إدارة",
       "phone": "0957603557", "hours": "10:00 ص – 06:00 م", "views": 13, "status": "approved", "id": 7,
       "boost": 0, "added_by": 1, "likes": 0, "hearts": 0, "whatsapp": None, "lat": None, "lng": None}


# ---------- الإيموجي ----------
def test_profession_and_place_emoji():
    assert cf.detect_emoji("طب فيزيائي") == "🩺"
    assert cf.detect_emoji("طبيب أسنان") == "🦷"          # الأخص قبل الأعم
    assert cf.detect_emoji("صيدلية الشفاء") == "💊"
    assert cf.detect_emoji("مطعم الرحمة") == "🍽"
    assert cf.detect_emoji("محامي") == "⚖️"
    assert cf.detect_emoji("شي غير معروف") == ""
    assert cf.place_emoji("🩺") == "🏥"
    assert cf.place_emoji("🍽") == "🍽"


def test_emoji_fallbacks():
    assert cf.profession_emoji({"category": "", "name": "x", "emoji": "🎻"}) == "🎻"   # اقتراح الـ AI
    assert cf.profession_emoji({"category": "", "name": "x", "emoji": "abc"}) == cf.DEFAULT_EMOJI
    assert cf.profession_emoji({"category": "مطعم", "emoji": "🎻"}) == "🍽"            # القاموس أولاً
    assert cf.valid_emoji("🩺") == "🩺" and cf.valid_emoji("a b") == "" and cf.valid_emoji(None) == ""


# ---------- القالب ----------
def test_card_follows_template():
    t = cf.render_html(DOC, _phones)
    assert t.startswith("🏥 <b>د. يحيى كيوان</b>")
    for part in ("🩺 <b>التخصص:</b> طب فيزيائي", "📍 <b>العنوان:</b> دمشق – الميدان - مركز إدارة",
                 "📞 <b>الهاتف:</b> MTN: +963957603557", "⏰ <b>الدوام:</b> 10:00 ص – 06:00 م",
                 "👁 13 مشاهدة", "✅ <b>معتمد</b>"):
        assert part in t


def test_missing_fields_are_never_blank():
    t = cf.render_html({"name": "مطعم الرحمة"}, _phones)
    assert cf.MISSING in t and cf.HOURS_MISSING in t
    assert "<b>التخصص:</b> " + cf.MISSING in t
    assert cf.missing({"name": "x"}) == ["التخصص", "المدينة", "العنوان", "الهاتف", "الدوام"]
    assert cf.missing(DOC) == []


def test_address_does_not_repeat_city():
    assert cf.address_line({"city": "دمشق", "address": "دمشق - الميدان"}) == "دمشق - الميدان"
    assert cf.address_line({"city": "درعا", "address": ""}) == "درعا"
    assert cf.address_line({}) == ""


def test_plain_text_for_sharing_has_no_html():
    t = cf.render_plain(DOC, _phones)
    assert "<" not in t and "د. يحيى كيوان" in t and "+963957603557" in t


# ---------- التنظيف والتعديل ----------
def test_clean_draft_normalizes():
    d = cf.clean_draft({"name": " د. أحمد ", "phone": "٠٩١٢٣٤٥٦٧٨", "hours": "١٠:٠٠ ص", "lat": "33.5",
                        "lng": "36.3", "emoji": "💊", "city": None})
    assert d["name"] == "د. أحمد" and d["phone"] == "0912345678" and d["hours"] == "10:00 ص"
    assert (d["lat"], d["lng"]) == (33.5, 36.3) and d["emoji"] == "💊" and d["city"] == ""
    assert cf.clean_draft({"lat": 999, "lng": 1})["lat"] is None


def test_apply_edit():
    d = {"name": "x"}
    assert cf.apply_edit(d, "hours", "٩ ص - ٥ م") is None and d["hours"] == "9 ص - 5 م"
    assert cf.apply_edit(d, "phone", "abc") is not None
    assert cf.apply_edit(d, "phone", "٠٩٥٧٦٠٣٥٥٧") is None and d["phone"] == "0957603557"
    assert cf.apply_edit(d, "name", "-") is not None          # الاسم لا يُحذف
    assert cf.apply_edit(d, "city", "-") is None and d["city"] is None
    assert cf.apply_edit(d, "bogus", "x") is not None


def test_parse_coords_and_location_edit():
    assert cf.parse_coords("33.5138, 36.2765") == (33.5138, 36.2765)
    assert cf.parse_coords("https://www.google.com/maps/@33.5138,36.2765,17z") == (33.5138, 36.2765)
    assert cf.parse_coords("https://maps.google.com/?q=33.5,36.3") == (33.5, 36.3)
    assert cf.parse_coords("لا شيء") is None
    d = {}
    assert cf.apply_edit(d, "location", "33.5, 36.3") is None and (d["lat"], d["lng"]) == (33.5, 36.3)
    assert cf.apply_edit(d, "location", "-") is None and d["lat"] is None
    assert cf.apply_edit(d, "location", "xyz") is not None


# ---------- أزرار البطاقة ----------
def _urls(markup):
    return [b.url for row in markup.inline_keyboard for b in row if b.url]


def _datas(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def test_share_button(monkeypatch):
    monkeypatch.setattr(handlers, "BOT_USERNAME", "dalil_test_bot")
    e = dict(DOC)
    url = handlers.share_url(e)
    q = parse_qs(urlparse(url).query)
    assert q["url"] == ["https://t.me/dalil_test_bot?start=e7"]
    assert "د. يحيى كيوان" in q["text"][0]
    assert url in _urls(handlers.share_menu_kb(e))                     # تلغرام داخل قائمة المشاركة
    assert "sh:7:m" in _datas(handlers.kb(e)) and url not in _urls(handlers.kb(e))
    ln = handlers.share_links(e)
    assert ln["wa"].startswith("https://wa.me/?text=") and "facebook.com/sharer" in ln["fb"]
    assert "twitter.com/intent/tweet" in ln["x"] and "sh:7:c" in _datas(handlers.share_menu_kb(e))
    assert "sh:7:b" in _datas(handlers.share_menu_kb(e))
    assert handlers.share_url({**e, "status": "pending"}) is None        # غير المنشورة لا تُشارك
    monkeypatch.setattr(handlers, "BOT_USERNAME", "")
    assert handlers.share_url(e) is None


def test_user_vs_admin_keyboards():
    e = dict(DOC)
    assert "u:7" in _datas(handlers.kb(e)) and "ae:7:m" not in _datas(handlers.kb(e))
    adm = _datas(handlers.kb(e, True))
    assert "ae:7:m" in adm and "ae:7:x" in adm and "u:7" not in adm
    pend = _datas(handlers.admin_kb({**e, "status": "pending"}))
    assert {"a:ok:7", "a:no:7", "ae:7:m", "ae:7:x"} <= set(pend)


def test_draft_keyboard_marks_missing_and_all_fields_editable():
    kbd = handlers.draft_kb({"name": "x", "phone": "0912345678"})
    labels = [b.text for row in kbd.inline_keyboard for b in row]
    assert "✏️ الاسم" in labels and "➕ الدوام" in labels
    assert {f"d:e:{k}" for k, _ in cf.FIELDS} <= set(_datas(kbd))
    assert "d:send" in _datas(kbd) and "d:cancel" in _datas(kbd)
    menu = _datas(handlers.edit_menu_kb(5))
    assert {f"ae:5:{k}" for k, _ in cf.FIELDS} <= set(menu)


# ---------- تعديلات الأزرار ----------
def _rows(markup):
    return [[b.text for b in row] for row in markup.inline_keyboard]


def test_no_heart_button_and_max_two_rows_for_users(monkeypatch):
    monkeypatch.setattr(handlers, "BOT_USERNAME", "dalil_test_bot")
    monkeypatch.setattr(handlers, "BASE_URL", "")
    e = {**DOC, "whatsapp": "0957714030", "lat": 33.5, "lng": 36.2}
    kbd = handlers.kb(e)
    assert len(kbd.inline_keyboard) == 2
    assert not any("❤" in t for row in _rows(kbd) for t in row)
    assert not any(d.startswith("r:heart") for d in _datas(kbd))
    assert [b.text for b in kbd.inline_keyboard[1]] == ["👍 0", "📤 مشاركة", "📝 طلب تحديث"]
    assert len(handlers.share_menu_kb(e).inline_keyboard) == 4
    # بلا واتساب/خريطة: يبقى سطران أيضاً ولا ينهار الشكل
    plain = handlers.kb({**DOC, "phone": "0112345678"})      # أرضي فقط: لا واتساب تلقائي
    assert len(plain.inline_keyboard) == 1 and "u:7" in _datas(plain)


def test_heart_not_in_card_text():
    t = cf.render_html({**DOC, "hearts": 5, "likes": 2}, _phones)
    assert "❤" not in t and "👍 2" in t


def test_map_and_whatsapp_use_redirect_when_base_url_set(monkeypatch):
    monkeypatch.setattr(handlers, "BASE_URL", "https://bot.example")
    e = {**DOC, "whatsapp": "0957714030", "lat": 33.5, "lng": 36.2}
    urls = _urls(handlers.kb(e))
    assert "https://bot.example/go/wa/7" in urls and "https://bot.example/go/map/7" in urls
    # غير المنشورة: روابط مباشرة (صفحة التحويل للمنشورة فقط)
    urls2 = _urls(handlers.kb({**e, "status": "pending"}))
    assert "https://wa.me/963957714030" in urls2
    assert "https://www.google.com/maps/search/?api=1&query=33.5,36.2" in urls2
    monkeypatch.setattr(handlers, "BASE_URL", "")
    assert "https://wa.me/963957714030" in _urls(handlers.kb(e))


def test_admin_keyboard_keeps_edit_delete():
    e = {**DOC, "likes": 1}
    adm = handlers.kb(e, True)
    assert "ae:7:m" in _datas(adm) and not any(d.startswith("r:heart") for d in _datas(adm))


# ---------- الأرقام: إيموجي + واتساب تلقائي + زر اتصال ----------
def test_phone_badges_in_card():
    t = cf.render_html({**DOC, "phone": "0112345678 - 0933123456"}, _phones)
    assert "☎️ +96311" in t and "Syriatel: +963933123456" in t


def test_auto_whatsapp_for_mobile_only(monkeypatch):
    monkeypatch.setattr(handlers, "BASE_URL", "")
    mob = _urls(handlers.kb({**DOC, "phone": "0112345678, 0933123456"}))
    assert "https://wa.me/963933123456" in mob                 # أول جوال، لا الأرضي
    assert not any("wa.me" in u for u in _urls(handlers.kb({**DOC, "phone": "0112345678"})))
    manual = _urls(handlers.kb({**DOC, "whatsapp": "0957714030"}))
    assert "https://wa.me/963957714030" in manual              # التحديد اليدوي أولاً


def test_call_button_only_when_published_and_base_url(monkeypatch):
    monkeypatch.setattr(handlers, "BASE_URL", "https://bot.example")
    assert "https://bot.example/go/tel/7" in _urls(handlers.kb(DOC))
    assert "https://bot.example/go/tel/7" not in _urls(handlers.kb({**DOC, "status": "pending"}))
    assert "https://bot.example/go/tel/7" not in _urls(handlers.kb({**DOC, "phone": ""}))
    monkeypatch.setattr(handlers, "BASE_URL", "")
    assert not any(u.startswith("tel:") or "/go/tel/" in u for u in _urls(handlers.kb(DOC)))


def test_each_phone_on_its_own_line():
    t = cf.render_html({**DOC, "phone": "0112345678 - 0933123456"}, _phones)
    assert "📞 <b>الهاتف:</b>\n☎️ +963112345678\nSyriatel: +963933123456" in t
    one = cf.render_html(DOC, _phones)                       # رقم واحد يبقى بجانب العنوان
    assert "📞 <b>الهاتف:</b> MTN: +963957603557" in one
    plain = cf.render_plain({**DOC, "phone": "0112345678 - 0933123456"}, _phones)
    assert "📞 الهاتف:\n☎️ +963112345678\nSyriatel: +963933123456" in plain
