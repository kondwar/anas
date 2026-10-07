import json
import time

import httpx

from . import db
from .config import GEMINI_KEYS as ENV_KEYS
from .config import GEMINI_MODELS as ENV_MODELS
from .db import norm

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_idx = 0
_cool: dict = {}

SEARCH_SYS = """You normalize search queries for an Arabic local directory (Syria/Levant).
The text may contain spelling mistakes or colloquial dialect. Return JSON only:
{"name":"business/person name or ''","category":"standard Arabic singular noun like مطعم، صيدلية، طبيب، مدرسة or ''","city":"city/area in standard spelling or ''"}
Drop filler words (رقم، عنوان، وين، بدي، شو)."""

ADD_SYS = """Extract a directory entry from Arabic text (may be colloquial or have typos). Return JSON only:
{"name":"","category":"","city":"","address":"","phone":"","whatsapp":"","lat":null,"lng":null,"description":""}
Use western digits for phones. Put whatsapp only if the text says the number is on WhatsApp.
lat/lng only if coordinates or a Google Maps link are given. Use null/'' for missing fields."""

ADMIN_SYS = """You convert an admin's Arabic message (may be colloquial or have typos) for a directory bot into ONE JSON command. Return JSON only:
{"action":"stats|pending|show|approve|approve_all|reject|edit|boost|delete|add_entry|ad_add|ads_list|ad_off|add_admin|remove_admin|list_admins|gh_list|gh_show|gh_edit|gh_delete|deploy|db_size|cleanup|user_search|unknown",
 "ref":"entry id or entry name, or ''",
 "changes":{"name":"","category":"","city":"","address":"","phone":"","whatsapp":"","description":"","lat":"","lng":""},
 "value":0,
 "ad":{"category":"","title":"","text":"","phone":"","whatsapp":""},
 "ad_id":0,
 "user_id":0,
 "path":"",
 "instruction":""}
Rules:
- In "changes" fill only the fields to modify.
- "boost" value is an integer priority (higher shows first; 'first/top' = 100; remove boost = 0).
- add_entry: the admin gives details of a NEW place to add.
- add_admin/remove_admin: user_id is the numeric Telegram id.
- gh_*: path is a repo-relative file path (gh_list: a directory or ''). gh_edit: instruction describes the change, or the content of a new file; copy any content the admin supplied verbatim.
- deploy: publish staged code changes.
- db_size: show database size. cleanup: delete old search logs; value = days to keep (default 90).
- If the message is just a normal search for a place or phone number, use user_search."""

EDIT_SYS = """You are a careful senior Python engineer editing one file of an aiogram 3 Telegram bot project.
Apply the user's instruction to the file. Return ONLY the complete new file content, no explanations, no markdown fences.
Keep everything else unchanged. If the file is empty, create it from the instruction."""


# ---------- المفاتيح والنماذج (تُخزَّن في القاعدة وتتغير من الدردشة) ----------
async def get_keys():
    v = await db.get_setting("gemini_keys", "")
    return json.loads(v) if v else list(ENV_KEYS)


async def set_keys(ks):
    await db.set_setting("gemini_keys", json.dumps(ks))


async def get_models():
    v = await db.get_setting("gemini_models", "")
    return json.loads(v) if v else list(ENV_MODELS)


async def set_models(ms):
    await db.set_setting("gemini_models", json.dumps(ms))


# ---------- الاتصال بـ Gemini مع التدوير ----------
async def _gen(system, text, as_json):
    """يدوّر بين المفاتيح والنماذج ويتخطى ما تجاوز الحصة مؤقتاً."""
    global _idx
    keys, models = await get_keys(), await get_models()
    n = len(keys)
    if not n:
        return None
    cfg = {"temperature": 0.1 if as_json else 0.2, "maxOutputTokens": 8192}
    if as_json:
        cfg["responseMimeType"] = "application/json"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": cfg,
    }
    async with httpx.AsyncClient(timeout=60) as cl:
        for model in models:
            for _ in range(n):
                key = keys[_idx % n]
                _idx += 1
                ck = key + model
                if _cool.get(ck, 0) > time.time():
                    continue
                try:
                    r = await cl.post(URL.format(model=model), headers={"x-goog-api-key": key}, json=body)
                    if r.status_code == 200:
                        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    _cool[ck] = time.time() + (60 if r.status_code in (429, 500, 503) else 600)
                except Exception:
                    _cool[ck] = time.time() + 30
    return None


async def gemini_json(system, text):
    t = await _gen(system, text, True)
    try:
        return json.loads(t) if t else None
    except ValueError:
        return None


async def gemini_text(system, text):
    return await _gen(system, text, False)


# ---------- الوظائف ----------
async def parse_search(text):
    d = await gemini_json(SEARCH_SYS, text) or {}
    if isinstance(d, list):
        d = d[0] if d else {}
    if not isinstance(d, dict):
        d = {}
    out = {k: norm(str(d.get(k) or "")) for k in ("name", "category", "city")}
    if not any(out.values()):
        out["name"] = norm(text)
    return out


async def parse_entry(text):
    d = await gemini_json(ADD_SYS, text)
    if isinstance(d, list):
        d = d[0] if d else None
    return d if isinstance(d, dict) else None


async def parse_admin(text):
    d = await gemini_json(ADMIN_SYS, text)
    if isinstance(d, list):
        d = d[0] if d else None
    return d if isinstance(d, dict) else None


async def edit_code(path, source, instruction):
    t = await gemini_text(EDIT_SYS, f"File: {path}\n\nInstruction: {instruction}\n\n--- CURRENT CONTENT ---\n{source}")
    if not t:
        return None
    t = t.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.rstrip() + "\n"
 
