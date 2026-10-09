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
def _limit(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


MAX_FILES = _limit("CODING_MAX_REPO_FILES", 80, 1, 300)
MAX_CONTEXT_CHARS = _limit("CODING_MAX_CONTEXT_CHARS", 90000, 10000, 300000)
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


def _validate_changes(obj: Any, known_paths: set[str]) -> tuple[str, dict[str, str], list[str]]:
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
    for item in changes:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("content"), str):
            raise CodingTaskError("أحد تغييرات الملفات غير صالح.")
        path = item["path"].strip()
        # Reuse the same path safety rules as GitHub writes.
        try:
            safe_path = gh.check_path(path)
        except gh.GHError as exc:
            raise CodingTaskError(f"مسار غير مسموح في التغيير المقترح: {path}") from exc
        if len(path) > 240 or safe_path != path or not _allowed_write_path(path):
            raise CodingTaskError(f"رفضنا مسارًا حساسًا أو غير مدعوم: {path}")
        if path in result:
            raise CodingTaskError(f"تكرر المسار في الاستجابة: {path}")
        content = item["content"]
        if len(content) > MAX_FILE_CHARS:
            raise CodingTaskError(f"الملف المقترح كبير جدًا: {path}")
        total += len(content)
        if total > MAX_CONTEXT_CHARS * 2:
            raise CodingTaskError("الحجم الإجمالي للتغييرات تجاوز الحد الآمن.")
        if path.endswith(".py"):
            try:
                compile(content, path, "exec")
            except (SyntaxError, ValueError) as exc:
                line = getattr(exc, "lineno", None)
                raise CodingTaskError(f"فشل فحص صياغة Python في {path}" + (f"، السطر {line}" if line else "") + ". لم يُرفع أي ملف.") from exc
        result[path] = content.rstrip() + "\n"
    normalized_tests = [x.strip()[:180] for x in tests if isinstance(x, str) and x.strip()][:8]
    return summary.strip()[:800], result, normalized_tests[:8]


def _allowed_write_path(path: str) -> bool:
    p = PurePosixPath(path)
    if p.name.lower().startswith(".env") or p.name.lower() in EXCLUDED_NAMES:
        return False
    if any(part in EXCLUDED_PARTS for part in p.parts):
        return False
    # Do not allow generated/binary assets or changes to Git metadata.
    if p.suffix.lower() not in TEXT_SUFFIXES:
        return False
    return True


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

    summary, changes, tests = _validate_changes(raw, set(candidates))
    if not changes:
        raise CodingTaskError("لم يقترح النموذج ملفات للتعديل. الملخص: " + (summary or "لم يوضح السبب."))
    # New files are allowed only under repository-safe textual paths; all proposed
    # contents are complete-file replacements, not partial patches.
    return {"summary": summary, "changes": changes, "tests": tests}
