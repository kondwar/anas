import base64

import httpx

from .config import GH_MAIN, GH_STAGE, GITHUB_REPO, GITHUB_TOKEN

API = "https://api.github.com"


class GHError(Exception):
    pass


def check_path(path):
    p = (path or "").strip().lstrip("/")
    segs = p.split("/")
    if not p or any(s in ("", ".", "..", ".git") or s.startswith(".env") for s in segs):
        raise GHError("مسار غير مسموح")
    return p


async def _req(method, url, **kw):
    if not (GITHUB_TOKEN and GITHUB_REPO):
        raise GHError("GITHUB_TOKEN / GITHUB_REPO غير مضبوطين في Render")
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    async with httpx.AsyncClient(timeout=30) as cl:
        return await cl.request(method, f"{API}/repos/{GITHUB_REPO}{url}", headers=headers, **kw)


async def ensure_stage():
    r = await _req("GET", f"/git/ref/heads/{GH_STAGE}")
    if r.status_code == 200:
        return
    m = await _req("GET", f"/git/ref/heads/{GH_MAIN}")
    if m.status_code != 200:
        raise GHError(f"تعذر قراءة الفرع {GH_MAIN}: {m.status_code}")
    c = await _req("POST", "/git/refs", json={"ref": f"refs/heads/{GH_STAGE}", "sha": m.json()["object"]["sha"]})
    if c.status_code not in (200, 201):
        raise GHError(f"تعذر إنشاء فرع {GH_STAGE}: {c.status_code}")


async def get_file(path):
    """يقرأ الملف من فرع التجربة إن وُجد وإلا من الرئيسي. يعيد (نص, sha) أو None."""
    path = check_path(path)
    for b in (GH_STAGE, GH_MAIN):
        r = await _req("GET", f"/contents/{path}", params={"ref": b})
        if r.status_code == 200 and isinstance(r.json(), dict):
            j = r.json()
            return base64.b64decode(j["content"]).decode("utf-8", "replace"), j["sha"]
    return None


async def put_file(path, data: bytes, message):
    path = check_path(path)
    await ensure_stage()
    r = await _req("GET", f"/contents/{path}", params={"ref": GH_STAGE})
    body = {"message": message, "content": base64.b64encode(data).decode(), "branch": GH_STAGE}
    if r.status_code == 200:
        body["sha"] = r.json()["sha"]
    w = await _req("PUT", f"/contents/{path}", json=body)
    if w.status_code not in (200, 201):
        raise GHError(f"فشل الرفع: {w.status_code}")


async def delete_file(path, message):
    path = check_path(path)
    await ensure_stage()
    r = await _req("GET", f"/contents/{path}", params={"ref": GH_STAGE})
    if r.status_code != 200:
        raise GHError("الملف غير موجود في فرع التجربة")
    d = await _req("DELETE", f"/contents/{path}",
                   json={"message": message, "sha": r.json()["sha"], "branch": GH_STAGE})
    if d.status_code != 200:
        raise GHError(f"فشل الحذف: {d.status_code}")


async def list_dir(path=""):
    path = path.strip("/")
    if path:
        path = check_path(path)
    for b in (GH_STAGE, GH_MAIN):
        r = await _req("GET", f"/contents/{path}", params={"ref": b})
        if r.status_code == 200 and isinstance(r.json(), list):
            return [("📁 " if i["type"] == "dir" else "📄 ") + i["path"] for i in r.json()]
    raise GHError("المسار غير موجود")


async def compare():
    r = await _req("GET", f"/compare/{GH_MAIN}...{GH_STAGE}")
    if r.status_code != 200:
        return None
    j = r.json()
    return j["ahead_by"], [f["filename"] for f in j["files"]]


async def deploy():
    r = await _req("POST", "/merges",
                   json={"base": GH_MAIN, "head": GH_STAGE, "commit_message": "deploy from chat"})
    if r.status_code == 201:
        return "✅ تم الدمج في main، وسيبدأ Render النشر تلقائياً."
    if r.status_code == 204:
        return "لا توجد تغييرات للنشر."
    if r.status_code == 409:
        raise GHError("تعارض في الدمج، حلّه يدوياً على GitHub.")
    raise GHError(f"فشل النشر: {r.status_code}")


async def reset_stage():
    m = await _req("GET", f"/git/ref/heads/{GH_MAIN}")
    if m.status_code != 200:
        raise GHError("تعذر قراءة الفرع الرئيسي")
    await ensure_stage()
    r = await _req("PATCH", f"/git/refs/heads/{GH_STAGE}", json={"sha": m.json()["object"]["sha"], "force": True})
    if r.status_code != 200:
        raise GHError(f"فشل إعادة الضبط: {r.status_code}")
      
