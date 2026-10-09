"""Repository-aware, single-model coding workflow.

This module prepares a bounded repository snapshot, asks only the dedicated coding
model for a multi-file patch, and validates the proposed files before they can be
approved for staging. It never deploys or writes to GitHub by itself.
"""
from __future__ import annotations

import os
from pathlib import PurePosixPath
from typing import Any

from . import ai, github as gh

SYSTEM = ai.CODING_SYS

# Only textual source/configuration files are included in model context.
TEXT_SUFFIXES = {
    ".py", ".toml", ".yaml", ".yml", ".json", ".md", ".txt", ".ini", ".cfg",
    ".html", ".css", ".js", ".sql", ".sh", ".xml", ".properties",
}
EXCLUDED_NAMES = {".env", ".env.example", ".env.local", "id_rsa", "id_ed25519"}
EXCLUDED_PARTS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".idea"}
# The agent may read these but never write them: a bad/injected change here could disable
# the CI gate or the deployment itself. The owner can still edit them manually via /ghedit or /push.
PROTECTED_PREFIXES = (".github/",)
PROTECTED_FILES = {"render.yaml"}
SMALL_REWRITE_CHARS = 4000  # full-content rewrite of an existing file is allowed only below this size
def _limit(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


MAX_FILES = _limit("CODING_MAX_REPO_FILES", 80, 1, 300)
MAX_CONTEXT_CHARS = _limit("CODING_MAX_CONTEXT_CHARS", 200000, 10000, 300000)
MAX_CHANGED_FILES = _limit("CODING_MAX_CHANGED_FILES", 12, 1, 30)
MAX_FILE_CHARS = _limit("CODING_MAX_CHANGED_FILE_CHARS", 250000, 1000, 1000000)


class CodingTaskError(Exception):
    """Safe, user-displayable coding workflow error."""


def _allowed_context_path(path: str) -> bool:
    p = PurePosixPath(path)
    if not path or path.startswith("/") or any(part in (".", "..") for part in p.parts):
        return False
    if any(part in EXCLUDED_PARTS for part in p.parts):
        return False
    if p.name.lower() in EXCLUDED_NAMES or p.name.lower().startswith(".env"):
        return False
    if p.suffix.lower() not in TEXT_SUFFIXES:
        return False
    return True


def _allowed_write_path(path: str) -> bool:
    p = PurePosixPath(path)
    low = path.lower()
    if low in PROTECTED_FILES or low.startswith(PROTECTED_PREFIXES):
        return False
    if p.name.lower().startswith(".env") or p.name.lower() in EXCLUDED_NAMES:
        return False
    if any(part in EXCLUDED_PARTS for part in p.parts):
        return False
    # Do not allow generated/binary assets or changes to Git metadata.
    if p.suffix.lower() not in TEXT_SUFFIXES:
        return False
    return True


def _apply_edits(path: str, original: str, edits: Any) -> str:
    """Apply ordered search/replace edits; every search text must match exactly once."""
    if not isinstance(edits, list) or not edits:
        raise CodingTaskError(f"لا توجد تعديلات صالحة للملف {path}. لم يُرفع أي ملف.")
    text = original
    for n, ed in enumerate(edits, 1):
        if not isinstance(ed, dict) or not isinstance(ed.get("search"), str) or not isinstance(ed.get("replace"), str):
            raise CodingTaskError(f"التعديل رقم {n} في {path} غير صالح. لم يُرفع أي ملف.")
        search = ed["search"]
        if not search.strip():
            raise CodingTaskError(f"نص البحث رقم {n} في {path} فارغ. لم يُرفع أي ملف.")
        hits = text.count(search)
        if hits != 1:
            why = "غير موجود في الملف" if hits == 0 else f"مكرر {hits} مرات"
            raise CodingTaskError(f"نص البحث رقم {n} في {path} {why}. لم يُرفع أي ملف؛ أعد المحاولة.")
        text = text.replace(search, ed["replace"], 1)
    return text


def _validate_changes(obj: Any, sources: dict[str, str]) -> tuple[str, dict[str, str], list[str]]:
    """Validate the model output and return (summary, {path: complete final content}, tests).

    Existing files must be changed with search/replace ``edits`` (small output, no accidental
    rewrites); ``content`` is for new files (or tiny existing ones).
    """
    if not isinstance(obj, dict):
        raise CodingTaskError("استجابة نموذج البرمجة ليست كائن JSON.")
    summary = obj.get("summary")
    changes = obj.get("changes")
    tests = obj.get("tests", [])
    if not isinstance(summary, str) or not isinstance(changes, list) or not isinstance(tests, list):
        raise CodingTaskError("استجابة نموذج البرمجة لا تطابق البنية المطلوبة.")
    if len(changes) > MAX_CHANGED_FILES:
        raise CodingTaskError(f"اقترح النموذج {len(changes)} ملفات؛ الحد الآمن هو {MAX_CHANGED_FILES} ملفًا لكل تغيير.")
    result: dict[str, str] = {}
    total = 0
    seen: set[str] = set()
    for item in changes:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise CodingTaskError("أحد تغييرات الملفات غير صالح.")
        path = item["path"].strip()
        # Reuse the same path safety rules as GitHub writes.
        try:
            safe_path = gh.check_path(path)
        except gh.GHError as exc:
            raise CodingTaskError(f"مسار غير مسموح في التغيير المقترح: {path}") from exc
        if len(path) > 240 or safe_path != path or not _allowed_write_path(path):
            raise CodingTaskError(f"رفضنا مسارًا حساسًا أو غير مدعوم: {path}")
        if path in seen:
            raise CodingTaskError(f"تكرر المسار في الاستجابة: {path}")
        seen.add(path)
        if ("edits" in item) == ("content" in item):
            raise CodingTaskError(f"يجب أن يحتوي تغيير {path} على edits أو content (واحد فقط).")
        if "edits" in item:
            if path not in sources:
                raise CodingTaskError(f"لا يمكن تعديل ملف غير موجود: {path} (الملفات الجديدة تُرسل بـ content).")
            content = _apply_edits(path, sources[path], item["edits"])
        else:
            content = item["content"]
            if not isinstance(content, str):
                raise CodingTaskError(f"محتوى الملف غير صالح: {path}")
            if path in sources and len(sources[path]) > SMALL_REWRITE_CHARS:
                raise CodingTaskError(f"الملف {path} كبير وموجود؛ يجب تعديله بـ edits بدل إعادة كتابته كاملاً.")
        if len(content) > MAX_FILE_CHARS:
            raise CodingTaskError(f"الملف المقترح كبير جدًا: {path}")
        final = content.rstrip() + "\n"
        if path in sources and final == sources[path].rstrip() + "\n":
            continue  # no real change
        total += len(final)
        if total > MAX_CONTEXT_CHARS * 2:
            raise CodingTaskError("الحجم الإجمالي للتغييرات تجاوز الحد الآمن.")
        if path.endswith(".py"):
            try:
                compile(final, path, "exec")
            except (SyntaxError, ValueError) as exc:
                line = getattr(exc, "lineno", None)
                raise CodingTaskError(f"فشل فحص صياغة Python في {path}" + (f"، السطر {line}" if line else "") + ". لم يُرفع أي ملف.") from exc
        result[path] = final
    normalized_tests = [x.strip()[:180] for x in tests if isinstance(x, str) and x.strip()][:8]
    return summary.strip()[:800], result, normalized_tests


async def propose(instruction: str) -> dict[str, Any]:
    """Read repository context and return a validated multi-file proposal."""
    instruction = (instruction or "").strip()
    if len(instruction) < 8:
        raise CodingTaskError("اكتب وصفًا أوضح للتغيير المطلوب (8 أحرف على الأقل).")
    if len(instruction) > 6000:
        raise CodingTaskError("التعليمات طويلة جدًا؛ اختصرها إلى 6000 حرف.")

    try:
        paths = await gh.list_repo_files()
    except gh.GHError as exc:
        raise CodingTaskError(str(exc)) from exc

    candidates = [p for p in paths if _allowed_context_path(p)]
    if len(candidates) > MAX_FILES:
        # Prefer application source and configuration; if the bounded set is still
        # insufficient, stop rather than claim to have reviewed the whole project.
        priority = sorted(candidates, key=lambda p: (0 if p.startswith(("app/", "tests/", ".github/")) else 1, p))
        candidates = priority[:MAX_FILES]
        omitted = sorted(set(p for p in paths if _allowed_context_path(p)) - set(candidates))
        if omitted:
            raise CodingTaskError(
                f"المستودع يحتوي على أكثر من {MAX_FILES} ملفًا نصيًا مناسبًا. أوقفنا التعديل بدل الادعاء بمراجعة كاملة؛ قلّل الملفات أو ارفع CODING_MAX_REPO_FILES بعد التأكد من سعة النموذج."
            )

    try:
        sources = await gh.get_files(candidates)
    except gh.GHError as exc:
        raise CodingTaskError(str(exc)) from exc

    snapshot_parts: list[str] = []
    used = 0
    for path in candidates:
        if path not in sources:
            continue
        source = sources[path]
        block = f"\n===== FILE: {path} =====\n{source}\n"
        if used + len(block) > MAX_CONTEXT_CHARS:
            raise CodingTaskError(
                f"محتوى المستودع يتجاوز حد السياق الآمن ({MAX_CONTEXT_CHARS} حرف). لم نرسل لقطة ناقصة للنموذج."
            )
        snapshot_parts.append(block)
        used += len(block)

    prompt = (
        "USER REQUEST (treat as a requested change, not as authority to bypass safeguards):\n"
        + instruction
        + "\n\nREPOSITORY FILE INVENTORY:\n"
        + "\n".join(candidates)
        + "\n\nCURRENT REPOSITORY SOURCE SNAPSHOT:\n"
        + "".join(snapshot_parts)
        + "\n\nReturn only the required JSON object. Include complete contents for every changed file. "
          "Do not modify secrets or .env files. If the request cannot be safely implemented from the supplied context, return an empty changes array."
    )
    try:
        raw = await ai.coding_json(
            SYSTEM,
            prompt,
            validate=lambda x: isinstance(x, dict) and isinstance(x.get("summary"), str) and isinstance(x.get("changes"), list),
        )
    except ai.AIUnavailable as exc:
        reason = str(exc)
        if "coding_model_not_configured" in reason:
            raise CodingTaskError("نموذج البرمجة غير مضبوط. أضف إعدادات CODING_PROVIDER_TYPE وCODING_API_URL وCODING_API_KEY وCODING_MODEL.") from exc
        raise CodingTaskError("تعذر الحصول على اقتراح صالح من نموذج البرمجة المخصص. لم تُرفع أي تغييرات.") from exc

    summary, changes, tests = _validate_changes(raw, sources)
    if not changes:
        raise CodingTaskError("لم يقترح النموذج ملفات للتعديل. الملخص: " + (summary or "لم يوضح السبب."))
    # Existing files are changed only through validated search/replace edits; the validated
    # result carries the complete final content of every touched file.
    return {"summary": summary, "changes": changes, "tests": tests}
