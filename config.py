"""
Central place where all environment variables and Zoho URL patterns live.
Everything else in the app imports from here instead of touching os.environ
directly - so if a domain/URL changes, you only fix it in one place.
"""
import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


ZOHO_DOMAIN = os.getenv("ZOHO_DOMAIN", "in").strip().lstrip(".")

ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID", "").strip()
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET", "").strip()
ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# ---- Token budget ----
# Resume text sent to the AI is compacted then capped at this many characters
# (~4 chars per token; 14000 chars covers a dense 3-4 page resume).
MAX_RESUME_CHARS = int(os.getenv("MAX_RESUME_CHARS", "14000"))
QUICK_MAX_OUTPUT_TOKENS = 1400
FULL_MAX_OUTPUT_TOKENS = 3500

# USD per 1M tokens, used only for the estimated cost shown in the dashboard.
# Defaults are gpt-4o-mini list prices - update if you change OPENAI_MODEL.
PRICE_INPUT_PER_M = float(os.getenv("PRICE_INPUT_PER_M", "0.15"))
PRICE_CACHED_INPUT_PER_M = float(os.getenv("PRICE_CACHED_INPUT_PER_M", "0.075"))
PRICE_OUTPUT_PER_M = float(os.getenv("PRICE_OUTPUT_PER_M", "0.60"))

FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

DATABASE_PATH = os.getenv("DATABASE_PATH", os.path.join(BASE_DIR, "screener.db"))

# ---- Derived Zoho endpoints (based on ZOHO_DOMAIN) ----
ACCOUNTS_BASE = f"https://accounts.zoho.{ZOHO_DOMAIN}"
WORKDRIVE_API_BASE = f"https://www.zohoapis.{ZOHO_DOMAIN}/workdrive/api/v1"
RECRUIT_API_BASE = f"https://recruit.zoho.{ZOHO_DOMAIN}/recruit/v2"

TOKEN_URL = f"{ACCOUNTS_BASE}/oauth/v2/token"
AUTH_URL = f"{ACCOUNTS_BASE}/oauth/v2/auth"

# Combined scope string used when generating the refresh token.
#   WorkDrive.files.READ      list folders, download resumes, verify file ancestry
#   WorkDrive.files.CREATE    create role/date folders and upload resumes from the app
#   WorkDrive.files.UPDATE    move a passed resume to trash (first half of delete)
#   WorkDrive.files.DELETE    permanently remove it from trash
#   ZohoRecruit.modules.ALL   create/update Candidates and attach resumes
#   ZohoRecruit.settings.READ read Candidates field types + picklist options, so
#                             values are coerced correctly instead of rejected
OAUTH_SCOPES = (
    "WorkDrive.files.READ,WorkDrive.files.CREATE,WorkDrive.files.UPDATE,WorkDrive.files.DELETE,"
    "ZohoRecruit.modules.ALL,ZohoRecruit.settings.READ"
)

# ---- WorkDrive folder layout ----
# ROOT / <role folder, e.g. "Full Stack"> / <date folder, e.g. "16 Sept"> / resumes
# The role folder (first level under ROOT) owns the job description. Screening
# can run on any folder at or below role level that holds resumes.
WORKDRIVE_ROOT_FOLDER_ID = os.getenv(
    "WORKDRIVE_ROOT_FOLDER_ID", "2zlwtb06b69f6279d461c9aee79190bdf257e"
).strip()

SUPPORTED_RESUME_EXTENSIONS = {".pdf", ".docx"}

# ---- Screening ----
# A candidate "passes" when overall_score >= this. Each role can override it
# from the UI; this is the starting value for a role with no saved JD yet.
DEFAULT_PASS_THRESHOLD = int(os.getenv("DEFAULT_PASS_THRESHOLD", "70"))

# Kill switch for the automatic permanent delete of passed resumes. With it
# off, passed candidates still go to Recruit but every file stays in WorkDrive.
WORKDRIVE_DELETE_ENABLED = _bool("WORKDRIVE_DELETE_ENABLED", True)

# Safety cap on how far up the parent chain the delete guard walks looking for
# ROOT. Real layout is 2-3 levels; hitting this means the file is not ours.
MAX_FOLDER_DEPTH = 10

# ---- Zoho Recruit ----
RECRUIT_DUPLICATE_FIELD = os.getenv("RECRUIT_DUPLICATE_FIELD", "Email")

# Recruit api_names for the AI score fields. Create them in Recruit (Setup ->
# Modules and Fields -> Candidates) and the app writes to them automatically.
# Any that don't exist yet are skipped with a logged warning - never a failure.
RECRUIT_SCORE_FIELDS = {
    "overall_score": "Overall_score",   # exists (Single Line text)
    "ai_score": "AI_Score",             # create as Number
    "skill_score": "Skill_Score",       # create as Number
    "summary": "AI_Summary",            # create as Multi Line
}

# Attachment categories tried in order when uploading a resume to a Candidate.
# "Resume" is the correct one but accepts only a single file per candidate,
# so "Others" catches re-uploads for a candidate who already has one.
RECRUIT_ATTACHMENT_CATEGORIES = ("Resume", "Others")
