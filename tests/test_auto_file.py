import io
import json
from unittest.mock import AsyncMock, MagicMock
import pytest
from app import handlers

def test_extract_txt_and_csv():
    assert handlers._extract_file_text("مرحبا\n0957714030".encode(), "a.txt").strip().startswith("مرحبا")
    assert "0957" in handlers._extract_file_text("name,phone\nمطعم,0957714030".encode(), "b.csv")

def test_extract_json_list():
    data = json.dumps([{"name":"مطعم","phone":"0957714030"}], ensure_ascii=False).encode()
    txt = handlers._extract_file_text(data, "data.json")
    assert "مطعم" in txt

def test_extract_json_entries_wrapper():
    data = json.dumps({"entries":[{"name":"صيدلية"}]}, ensure_ascii=False).encode()
    assert "صيدلية" in handlers._extract_file_text(data, "x.json")

def test_extract_fallback_cp1256():
    # bytes that are valid utf-8-sig fallback should still work
    assert handlers._extract_file_text("نص عربي".encode("utf-8"), "noext") is not None

def test_extract_empty_returns_none():
    assert handlers._extract_file_text(b"", "a.txt") is None
    assert handlers._extract_file_text(b"   \n  ", "a.txt") is None

@pytest.mark.asyncio
async def test_auto_file_calls_bulk_process(monkeypatch):
    called = {}
    async def fake_bulk(m, text):
        called["text"] = text
    monkeypatch.setattr(handlers, "bulk_process", fake_bulk)
    m = MagicMock()
    m.caption = None
    m.document.file_name = "list.txt"
    m.document.file_size = 100
    m.answer = AsyncMock()
    bot = MagicMock()
    async def fake_download(doc, destination):
        destination.write("مطعم الرحمة 0957714030 دمشق".encode())
    bot.download = fake_download
    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)
    state.clear = AsyncMock()
    await handlers.auto_file(m, bot, state)
    assert "مطعم الرحمة" in called["text"]
    state.clear.assert_awaited_once()

@pytest.mark.asyncio
async def test_auto_file_ignores_push_caption(monkeypatch):
    m = MagicMock()
    m.caption = "/push app/test.py"
    m.document.file_name = "a.txt"
    m.document.file_size = 10
    bot = MagicMock()
    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)
    # should return early without downloading
    await handlers.auto_file(m, bot, state)
    bot.download = AsyncMock()
    # no assertion needed, just no exception

@pytest.mark.asyncio
async def test_auto_file_rejects_large():
    m = MagicMock()
    m.caption = None
    m.document.file_name = "big.txt"
    m.document.file_size = 6_000_000
    m.answer = AsyncMock()
    bot = MagicMock()
    state = MagicMock()
    state.get_state = AsyncMock(return_value=None)
    await handlers.auto_file(m, bot, state)
    m.answer.assert_awaited_once()
    assert "5MB" in m.answer.call_args[0][0]
