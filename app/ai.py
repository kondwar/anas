import os
import json
import logging
import google.generativeai as genai
from app.config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

# ==================== [إعداد ملفات العامية والـ System Prompt] ====================
BASE_DIR = os.path.dirname(__file__)

# 1. تحميل قاموس العامية
DIALECTS_MAP = {}
dict_path = os.path.join(BASE_DIR, "dialects_mapping.json")
if os.path.exists(dict_path):
    try:
        with open(dict_path, "r", encoding="utf-8") as f:
            DIALECTS_MAP = json.load(f)
    except Exception as e:
        logger.error(f"خطأ في قراءة ملف القاموس العامي: {e}")

# 2. تحميل الـ System Prompt
SYSTEM_PROMPT = ""
prompt_path = os.path.join(BASE_DIR, "system_prompt.txt")
if os.path.exists(prompt_path):
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            SYSTEM_PROMPT = f.read().strip()
    except Exception as e:
        logger.error(f"خطأ في قراءة ملف System Prompt: {e}")
# ===================================================================================

# تهيئة المفتاح للذكاء الاصطناعي
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# إنشاء نموذج الذكاء الاصطناعي مع تضمين الـ System Prompt
model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction=SYSTEM_PROMPT if SYSTEM_PROMPT else None
)


def normalize_dialect_command(text: str) -> str:
    """
    دالة مطابقة الفهم العامي واستخراج الأمر القياسي المقابل قبل إرساله للبوت.
    """
    if not text:
        return text

    cleaned = text.strip().lower()
    for intent, aliases in DIALECTS_MAP.items():
        for alias in aliases:
            if alias.lower() in cleaned:
                return intent

    return text


async def generate_ai_response(prompt: str) -> str:
    """
    توليد استجابة من نموذج Gemini بناءً على مدخلات المستخدم.
    """
    try:
        if not GEMINI_API_KEY:
            return "مفتاح API الخاص بالذكاء الاصطناعي غير متوفر."

        # مطابقة النص بالعامية أولاً
        processed_prompt = normalize_dialect_command(prompt)

        response = model.generate_content(processed_prompt)
        if response and hasattr(response, "text"):
            return response.text
        return "لم أتمكن من الحصول على إجابة، يرجى المحاولة لاحقاً."
    except Exception as e:
        logger.error(f"Error generating AI response: {e}")
        return "حدث خطأ أثناء الاتصال بمزود الذكاء الاصطناعي."
      
