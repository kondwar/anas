from app import phones as ph


def test_landline_with_area_code():
    c = ph.classify("011 2345678")
    assert c["kind"] == "landline" and c["badge"] == "☎️" and c["area"] == "دمشق وريفها"
    assert c["intl"] == "+963112345678"
    assert ph.classify("+963 21 1234567")["area"] == "حلب"
    assert ph.classify("0152123456")["area"] == "درعا"          # 6 خانات محلية


def test_landline_local_without_area_code():
    c = ph.classify("2345678")
    assert c["kind"] == "landline" and c["badge"] == "☎️" and c["intl"] == ""


def test_syriatel_and_mtn_prefixes():
    for n in ("0933123456", "933123456", "+963933123456", "00963933123456"):
        c = ph.classify(n)
        assert c["operator"] == "syriatel" and c["badge"] == "Syriatel" and c["intl"] == "+963933123456", n
    for n in ("0983123456", "0993123456"):
        assert ph.classify(n)["operator"] == "syriatel", n
    for n in ("0943123456", "0953123456", "0963123456"):
        c = ph.classify(n)
        assert c["operator"] == "mtn" and c["badge"] == "MTN", n


def test_unknown_mobile_prefix_and_garbage():
    assert ph.classify("0913123456")["badge"] == "📱"           # بادئة غير مسجّلة عندنا: جوال عام
    assert ph.classify("12345")["kind"] == "other" and ph.classify("12345")["badge"] == ""
    assert ph.classify("")["kind"] == "other"


def test_arabic_digits():
    assert ph.classify("٠٩٣٣١٢٣٤٥٦")["operator"] == "syriatel"


def test_zain_rebrand(monkeypatch):
    monkeypatch.setattr(ph, "_MTN_KEY", "zain")
    c = ph.classify("0953123456")
    assert c["operator"] == "zain" and c["badge"] == "Zain" and c["operator_name"] == "Zain"
    assert ph.classify("0933123456")["operator"] == "syriatel"   # سيرياتل لا تتأثر


def test_extract_and_whatsapp_pick():
    f = "0112345678 - 0933123456 / 0953111222"
    assert [c["kind"] for c in ph.extract(f)] == ["landline", "mobile", "mobile"]
    assert ph.auto_whatsapp(f) == "+963933123456"
    assert ph.auto_whatsapp("0112345678") is None
    assert ph.auto_whatsapp("") is None and ph.auto_whatsapp(None) is None


def test_auto_whatsapp_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AUTO_WHATSAPP", "0")
    assert ph.auto_whatsapp("0933123456") is None


def test_call_numbers_mobile_first_and_skip_local():
    assert ph.call_numbers("0112345678, 0933123456") == ["+963933123456", "+963112345678"]
    assert ph.call_numbers("2345678") == []                      # بلا رمز منطقة لا يصلح للاتصال


def test_zero_after_country_code():
    for n in ("+963 0933 123 456", "+9630933123456", "00963 0933123456", "+963 (0) 933123456"):
        c = ph.classify(n)
        assert c["operator"] == "syriatel" and c["intl"] == "+963933123456", n
    c = ph.classify("+963 011 2345678")
    assert c["kind"] == "landline" and c["intl"] == "+963112345678"
    assert ph.classify("+963 0944123456")["operator"] == "mtn"
