import re
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import asyncpg

from .config import DATABASE_URL

pool: asyncpg.Pool = None

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE TABLE IF NOT EXISTS entries(
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL, category TEXT, city TEXT, address TEXT,
  phone TEXT, whatsapp TEXT, lat DOUBLE PRECISION, lng DOUBLE PRECISION,
  description TEXT, status TEXT DEFAULT 'pending',
  boost INT DEFAULT 0, views INT DEFAULT 0,
  added_by BIGINT, search_text TEXT DEFAULT '',
  created_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS entries_trgm ON entries USING gin (search_text gin_trgm_ops);
CREATE TABLE IF NOT EXISTS reactions(
  entry_id INT REFERENCES entries(id) ON DELETE CASCADE,
  user_id BIGINT, kind TEXT, PRIMARY KEY(entry_id,user_id,kind));
CREATE TABLE IF NOT EXISTS ads(
  id SERIAL PRIMARY KEY, category TEXT, title TEXT, text TEXT,
  phone TEXT, whatsapp TEXT, active BOOLEAN DEFAULT true, impressions INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS users(
  id BIGINT PRIMARY KEY, username TEXT, first_seen TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS searches(
  id SERIAL PRIMARY KEY, user_id BIGINT, query TEXT, results INT, ts TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS admins(
  user_id BIGINT PRIMARY KEY, added_by BIGINT, ts TIMESTAMPTZ DEFAULT now());
"""

BASE = """SELECT e.*,
 (SELECT count(*) FROM reactions r WHERE r.entry_id=e.id AND r.kind='like') AS likes,
 (SELECT count(*) FROM reactions r WHERE r.entry_id=e.id AND r.kind='heart') AS hearts
 FROM entries e"""

FIELDS = {"name", "category", "city", "address", "phone", "whatsapp", "description", "lat", "lng", "status"}

_AR = re.compile(r"[\u064B-\u065F\u0670\u0640]")


def norm(s):
    s = _AR.sub("", s or "")
    s = re.sub("[أإآٱ]", "ا", s).replace("ة", "ه").replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي")
    return re.sub(r"\s+", " ", s).strip().lower()


def _clean_dsn(dsn):
    """يحذف المعاملات التي لا يفهمها asyncpg (مثل channel_binding في روابط Neon)."""
    u = urlparse(dsn)
    q = [(k, v) for k, v in parse_qsl(u.query) if k not in ("channel_binding",)]
    return urlunparse(u._replace(query=urlencode(q)))


async def init():
    global pool
    pool = await asyncpg.create_pool(
        _clean_dsn(DATABASE_URL), min_size=1, max_size=5, statement_cache_size=0,
        max_inactive_connection_lifetime=30,  # Neon يوقف الحساب عند الخمول: لا نحتفظ باتصالات قد تكون ميتة
        command_timeout=30)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)


# ---------- إعدادات وأدمنز ----------
async def get_setting(k, default=""):
    v = await pool.fetchval("SELECT value FROM settings WHERE key=$1", k)
    return v if v is not None else default


async def set_setting(k, v):
    await pool.execute(
        "INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT (key) DO UPDATE SET value=$2", k, v)


async def load_admins():
    return {r["user_id"] for r in await pool.fetch("SELECT user_id FROM admins")}


async def add_admin(uid, by):
    await pool.execute("INSERT INTO admins(user_id,added_by) VALUES($1,$2) ON CONFLICT DO NOTHING", uid, by)


async def del_admin(uid):
    await pool.execute("DELETE FROM admins WHERE user_id=$1", uid)


# ---------- المستخدمون والسجلات ----------
async def touch_user(u):
    await pool.execute("INSERT INTO users(id,username) VALUES($1,$2) ON CONFLICT DO NOTHING", u.id, u.username)


async def get(eid):
    return await pool.fetchrow(BASE + " WHERE e.id=$1", eid)


def _val(d, k):
    v = d.get(k)
    return str(v).strip() if v not in (None, "") else None


def _num(d, k):
    try:
        return float(d.get(k))
    except (TypeError, ValueError):
        return None


async def add_entry(d, by, status):
    name, cat, city, addr, desc = (_val(d, k) for k in ("name", "category", "city", "address", "description"))
    st = norm(" ".join(filter(None, [name, cat, city, addr, desc])))
    return await pool.fetchval(
        """INSERT INTO entries(name,category,city,address,phone,whatsapp,lat,lng,description,
           status,added_by,search_text) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id""",
        name, cat, city, addr, _val(d, "phone"), _val(d, "whatsapp"), _num(d, "lat"), _num(d, "lng"),
        desc, status, by, st)


async def update(eid, field, value):
    if field not in FIELDS:
        return False
    if field in ("lat", "lng"):
        value = float(value)
    await pool.execute(f"UPDATE entries SET {field}=$1 WHERE id=$2", value, eid)
    e = await get(eid)
    st = norm(" ".join(filter(None, [e["name"], e["category"], e["city"], e["address"], e["description"]])))
    await pool.execute("UPDATE entries SET search_text=$1 WHERE id=$2", st, eid)
    return True


async def decide_pending(eid, status):
    """يغيّر الحالة فقط إذا كان السجل لا يزال معلقاً (يمنع تكرار القرار/الإشعار عند ضغط أدمنين معاً)."""
    r = await pool.fetchval("UPDATE entries SET status=$2 WHERE id=$1 AND status='pending' RETURNING id", eid, status)
    return r is not None


async def find(ref, limit=5):
    """يبحث برقم السجل أو باسمه، في كل الحالات (منشور/معلق/مرفوض)."""
    if str(ref).isdigit():
        r = await get(int(ref))
        return [r] if r else []
    q = norm(str(ref))
    if not q:
        return []
    return await pool.fetch(
        BASE + """ WHERE word_similarity($1,e.search_text)>0.35 OR e.search_text ILIKE '%'||$1||'%'
        ORDER BY word_similarity($1,e.search_text) DESC LIMIT $2""", q, limit)


async def search(name, category, city, limit=5, keywords=None):
    q = f"{name} {category}".strip() or city
    sql = BASE + """ WHERE e.status='approved'
      AND ($2='' OR e.search_text ILIKE '%'||$2||'%')
      AND (word_similarity($1,e.search_text)>0.35 OR e.search_text ILIKE '%'||$1||'%')
      ORDER BY e.boost DESC, word_similarity($1,e.search_text) DESC, e.views DESC LIMIT $3"""
    rows = await pool.fetch(sql, q, city, limit)
    if not rows and city:
        rows = await pool.fetch(sql, q, "", limit)
    kws = [k for k in (keywords or []) if isinstance(k, str) and k.strip()][:8]
    if not rows and kws:  # بحث احتياطي بالمرادفات التي اقترحها الـ AI (دكتور → طبيب، عيادة ...)
        sql2 = BASE + """ WHERE e.status='approved'
          AND ($2='' OR e.search_text ILIKE '%'||$2||'%')
          AND e.search_text ILIKE ANY(ARRAY(SELECT '%'||k||'%' FROM unnest($1::text[]) AS k))
          ORDER BY e.boost DESC, e.views DESC LIMIT $3"""
        rows = await pool.fetch(sql2, kws, city, limit)
        if not rows and city:
            rows = await pool.fetch(sql2, kws, "", limit)
    return rows


_vocab = {"t": 0.0, "v": None}


async def vocab(ttl=300):
    """التصنيفات والمدن الموجودة فعلاً في السجلات المعتمدة (الأكثر شيوعاً أولاً) — للقراءة فقط، مع كاش."""
    if _vocab["v"] is not None and time.time() - _vocab["t"] < ttl:
        return _vocab["v"]
    out = {}
    for key, name in (("category", "categories"), ("city", "cities")):  # أسماء الأعمدة ثابتة داخل الكود
        rows = await pool.fetch(
            f"SELECT {key} FROM entries WHERE status='approved' AND coalesce({key},'')<>'' "
            f"GROUP BY {key} ORDER BY count(*) DESC LIMIT 60")
        out[name] = [r[key].strip()[:40] for r in rows]
    _vocab.update(t=time.time(), v=out)
    return out


async def add_views(ids):
    if ids:
        await pool.execute("UPDATE entries SET views=views+1 WHERE id=ANY($1)", ids)


async def log_search(uid, q, n):
    await pool.execute("INSERT INTO searches(user_id,query,results) VALUES($1,$2,$3)", uid, q, n)


async def toggle(eid, uid, kind):
    r = await pool.execute("DELETE FROM reactions WHERE entry_id=$1 AND user_id=$2 AND kind=$3", eid, uid, kind)
    if r.endswith("1"):
        return False
    await pool.execute("INSERT INTO reactions VALUES($1,$2,$3)", eid, uid, kind)
    return True


async def pick_ad(category):
    if not category:
        return None
    ad = await pool.fetchrow(
        "SELECT * FROM ads WHERE active AND word_similarity(category,$1)>0.5 ORDER BY random() LIMIT 1", category)
    if ad:
        await pool.execute("UPDATE ads SET impressions=impressions+1 WHERE id=$1", ad["id"])
    return ad


async def stats():
    f = pool.fetchval
    return {
        "users": await f("SELECT count(*) FROM users"),
        "approved": await f("SELECT count(*) FROM entries WHERE status='approved'"),
        "pending": await f("SELECT count(*) FROM entries WHERE status='pending'"),
        "searches": await f("SELECT count(*) FROM searches"),
        "today": await f("SELECT count(*) FROM searches WHERE ts>now()-interval '1 day'"),
        "top_q": await pool.fetch("SELECT query,count(*) c FROM searches GROUP BY query ORDER BY c DESC LIMIT 5"),
        "top_e": await pool.fetch("SELECT id,name,views FROM entries ORDER BY views DESC LIMIT 5"),
    }


async def db_report():
    size = await pool.fetchval("SELECT pg_size_pretty(pg_database_size(current_database()))")
    rows = await pool.fetch(
        "SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS s "
        "FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 5")
    return "💾 حجم القاعدة: " + size + "\n" + "\n".join(f"• {r['relname']}: {r['s']}" for r in rows)


async def cleanup_searches(days):
    r = await pool.execute("DELETE FROM searches WHERE ts < now() - make_interval(days => $1)", days)
    return int(r.split()[-1])
