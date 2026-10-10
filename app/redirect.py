"""صفحات تحويل صغيرة تفتح واتساب أو خرائط جوجل مباشرة من زر البطاقة.

أزرار تلغرام لا تقبل إلا روابط http(s)، فنمرّر الضغطة على السيرفر نفسه:
  /go/wa/<id>   ← يفتح محادثة واتساب مع صاحب البطاقة (تطبيق واتساب على الجوال)
  /go/map/<id>  ← يفتح موقع البطاقة في تطبيق خرائط جوجل

الصفحة تحاول فتح التطبيق فوراً، وإن لم ينجح (التطبيق غير مثبّت/سطح المكتب)
تنتقل بعد لحظة إلى الرابط العادي. للبطاقات المنشورة فقط.
"""
from __future__ import annotations

import html
import json
from urllib.parse import quote

from aiohttp import web

from . import db


def _ua(request) -> str:
    return (request.headers.get("User-Agent") or "").lower()


def page(app_url, fallback) -> str:
    """HTML يحاول app_url فوراً ثم fallback إن بقيت الصفحة ظاهرة (أي لم يُفتح التطبيق)."""
    a, f = json.dumps(app_url or ""), json.dumps(fallback)
    a, f = a.replace("</", "<\\/"), f.replace("</", "<\\/")
    href = html.escape(fallback, quote=True)
    return (
        '<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>جارٍ الفتح…</title></head>"
        '<body style="font-family:sans-serif;text-align:center;padding:48px 16px">'
        f'<p>جارٍ الفتح…</p><p><a href="{href}">اضغط هنا إن لم يفتح تلقائياً</a></p>'
        f"<script>var app={a},fb={f};"
        "if(app){location.href=app;"
        "setTimeout(function(){if(document.visibilityState==='visible')location.replace(fb)},1800);}"
        "else{location.replace(fb)}</script></body></html>"
    )


def wa_target(digits: str, ua: str):
    """(رابط التطبيق، الرابط الاحتياطي) لواتساب."""
    fallback = f"https://wa.me/{digits}"
    mobile = any(x in ua for x in ("android", "iphone", "ipad"))
    return (f"whatsapp://send?phone={digits}" if mobile else ""), fallback


def map_target(lat, lng, ua: str):
    """(رابط التطبيق، الرابط الاحتياطي) لخرائط جوجل."""
    q = f"{lat},{lng}"
    fallback = f"https://www.google.com/maps/search/?api=1&query={quote(q, safe=',')}"
    if "android" in ua:
        return (f"intent://www.google.com/maps/search/?api=1&query={q}"
                f"#Intent;scheme=https;package=com.google.android.apps.maps;end"), fallback
    if "iphone" in ua or "ipad" in ua:
        return f"comgooglemaps://?q={q}", fallback
    return "", fallback


def _html(body: str, status=200):
    return web.Response(text=body, status=status, content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-store"})


async def _entry(request):
    try:
        e = await db.get(int(request.match_info["id"]))
    except (ValueError, KeyError):
        return None
    return e if e and e["status"] == "approved" else None


async def go_wa(request):
    from .handlers import wa_link  # استيراد متأخر: لا اعتماد دائري عند التحميل
    e = await _entry(request)
    link = wa_link(e["whatsapp"]) if e and e["whatsapp"] else None
    if not link:
        return _html("<p dir=rtl>الرابط غير متاح.</p>", 404)
    app, fb = wa_target(link.rsplit("/", 1)[1], _ua(request))
    return _html(page(app, fb))


async def go_map(request):
    e = await _entry(request)
    if not e or e["lat"] is None or e["lng"] is None:
        return _html("<p dir=rtl>الرابط غير متاح.</p>", 404)
    app, fb = map_target(e["lat"], e["lng"], _ua(request))
    return _html(page(app, fb))


def register(app: web.Application):
    app.router.add_get("/go/wa/{id}", go_wa)
    app.router.add_get("/go/map/{id}", go_map)
