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
    # Fail closed: never merge staged code before the repository CI checks pass.
    ref = await _req("GET", f"/git/ref/heads/{GH_STAGE}")
    if ref.status_code != 200:
        raise GHError(f"تعذر قراءة فرع التجربة: {ref.status_code}")
    sha = ref.json()["object"]["sha"]
    checks = await _req("GET", f"/commits/{sha}/check-runs", params={"per_page": "100"})
    if checks.status_code != 200:
        raise GHError("تعذر قراءة نتائج اختبارات GitHub. تحقق من صلاحيات GITHUB_TOKEN (Checks: read)؛ لم يُنشر شيء.")
    runs = checks.json().get("check_runs", [])
    if not runs:
        raise GHError("لا توجد نتائج اختبارات لهذا التغيير بعد. انتظر تشغيل GitHub Actions وانتهاء الفحوص ثم أعد /deploy.")
    failed = [x.get("name", "check") for x in runs if x.get("status") != "completed" or x.get("conclusion") != "success"]
    if failed:
        raise GHError("لم يُنشر التغيير لأن الاختبارات لم تنجح أو لم تنتهِ: " + ", ".join(failed[:8]))
    r = await _req("POST", "/merges",
                   json={"base": GH_MAIN, "head": GH_STAGE, "commit_message": "deploy from chat"})
    if r.status_code == 201:
        return "✅ نجحت فحوص GitHub Actions وتم الدمج في main؛ سيبدأ Render النشر تلقائياً."
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
      


async def list_repo_files(branch=None):
    """Return repository file paths from Git Trees API; reject truncated inventories."""
    await ensure_stage()
    branch = branch or GH_STAGE
    r = await _req("GET", f"/git/trees/{branch}", params={"recursive": "1"})
    if r.status_code != 200:
        raise GHError(f"تعذر استعراض المستودع: {r.status_code}")
    data = r.json()
    if data.get("truncated"):
        raise GHError("قائمة ملفات المستودع أكبر من الحد الذي أعاده GitHub؛ أوقفنا المراجعة لتجنب تعديل غير مكتمل.")
    return [x["path"] for x in data.get("tree", []) if x.get("type") == "blob"]


async def get_files(paths):
    """Read multiple repository files from staging/main."""
    out = {}
    for path in paths:
        result = await get_file(path)
        if result is not None:
            out[path] = result[0]
    return out


async def put_files(files, message):
    """Stage multiple files atomically in one commit using Git Data API."""
    if not files:
        raise GHError("لا توجد ملفات لرفعها")
    clean = {check_path(p): (c if isinstance(c, bytes) else c.encode("utf-8")) for p, c in files.items()}
    await ensure_stage()
    ref = await _req("GET", f"/git/ref/heads/{GH_STAGE}")
    if ref.status_code != 200:
        raise GHError(f"تعذر قراءة فرع التجربة: {ref.status_code}")
    parent_sha = ref.json()["object"]["sha"]
    commit_resp = await _req("GET", f"/git/commits/{parent_sha}")
    if commit_resp.status_code != 200:
        raise GHError(f"تعذر قراءة commit فرع التجربة: {commit_resp.status_code}")
    base_tree = commit_resp.json()["tree"]["sha"]
    entries = []
    for path, content in clean.items():
        blob = await _req("POST", "/git/blobs", json={
            "content": base64.b64encode(content).decode("ascii"), "encoding": "base64"
        })
        if blob.status_code not in (200, 201):
            raise GHError(f"تعذر تجهيز الملف {path}: {blob.status_code}")
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]})
    tree = await _req("POST", "/git/trees", json={"base_tree": base_tree, "tree": entries})
    if tree.status_code not in (200, 201):
        raise GHError(f"تعذر إنشاء شجرة التغييرات: {tree.status_code}")
    commit = await _req("POST", "/git/commits", json={
        "message": message[:200], "tree": tree.json()["sha"], "parents": [parent_sha]
    })
    if commit.status_code not in (200, 201):
        raise GHError(f"تعذر إنشاء commit: {commit.status_code}")
    update = await _req("PATCH", f"/git/refs/heads/{GH_STAGE}", json={"sha": commit.json()["sha"], "force": False})
    if update.status_code != 200:
        raise GHError(f"تعذر تحديث فرع التجربة: {update.status_code}")
    return list(clean)
