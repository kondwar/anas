import os
import psycopg2
import psycopg2.extras

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/app")

def get_connection():
    """إنشاء اتصال بقاعدة البيانات."""
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """تهيئة جداول قاعدة البيانات."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS items (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            conn.commit()
    finally:
        conn.close()

def search(query, limit=10, offset=0):
    """
    بحث دقيق بـ ILIKE فقط، بلا بحث تقريبي ولا مرادفات

    ينفذ بحثا دقيقا باستخدام ILIKE فقط على الحقول النصية.
    لا يستخدم بحثا تقريبيا ولا مرادفات.
    """
    # بحث دقيق بـ ILIKE فقط، بلا بحث تقريبي ولا مرادفات
    if not query:
        return []
    pattern = f"%{query}%"
    sql = """
        SELECT id, title, description, created_at
        FROM items
        WHERE title ILIKE %s OR description ILIKE %s
        ORDER BY id ASC
        LIMIT %s OFFSET %s
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (pattern, pattern, limit, offset))
            rows = cur.fetchall()
            return rows
    finally:
        conn.close()

def get_by_id(item_id):
    """جلب عنصر بواسطة المعرف."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM items WHERE id = %s", (item_id,))
            return cur.fetchone()
    finally:
        conn.close()

def insert_item(title, description=None):
    """إدراج عنصر جديد."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO items (title, description) VALUES (%s, %s) RETURNING id",
                (title, description)
            )
            new_id = cur.fetchone()[0]
            conn.commit()
            return new_id
    finally:
        conn.close()
