"""
One OpenAI call per resume: extract the basic information the hiring team
needs, and score the resume against the job description.

Extracted fields (basic_information):
  salutation, first_name, last_name, gender, date_of_birth, age_years, marital_status,
  contact, phone, email, secondary_email,
  street, city, state, zip_code, country, location, hometown_location,
  linkedin_url, portfolio_url, project_urls,
  work_history, current_job_title,
  experience_years + experience (computed in code from work_history dates),
  highest_qualification, degrees, education_board, skills, department,
  tech_degree + graduation_year (derived from degrees in code),
  position_applied (set from the role folder, not by the AI)

Kept deliberately small so token use stays low:
  * compact resume text (whitespace, repeated header/footer lines) with a cap
  * static instructions first and the JD second, so OpenAI's automatic prompt
    caching reuses that prefix for every resume in a run
  * short output - no work history / education lists

Dropdown fields are constrained to Recruit's real options with enums, so the
values always save. The options come from Recruit field metadata (see
PICKLIST_FIELDS); a dropdown whose metadata is unavailable falls back to the
defaults below, or to free text that zoho_recruit matches at save time.

Structured Outputs (strict json_schema) guarantee every key is present and
typed - overall_score drives an automatic delete, so it must never be missing.
"""
import json
import re
from datetime import date

from openai import OpenAI

import config

_client = None

# Bump whenever basic_information gains fields, so older analyses aren't reused.
EXTRACTION_VERSION = "basic-v4"  # v3: every score on 0-100; v4: experience from work_history dates
EDUCATION_BOARDS = ["ICSE", "CBSE", "State Education Board", "Other"]
RECOMMENDATIONS = ["Strong Fit", "Good Fit", "Average Fit", "Not a Fit"]
WORK_KINDS = ["job", "internship", "training", "freelance"]
# Degrees whose completion year goes to Year_of_Clearing_B_Tech_BCA_MCA, highest first.
TECH_DEGREE_PATTERNS = [
    ("MCA", r"\bm\.?\s?c\.?\s?a\b|master\S* (of|in) computer applications?"),
    ("B.Tech / B.E.", r"\bb\.?\s?tech\b|\bb\.?\s?e\b\.?|bachelor\S* (of|in) (engineering|technology)"),
    ("BCA", r"\bb\.?\s?c\.?\s?a\b|bachelor\S* (of|in) computer applications?"),
]

# basic_information key -> Recruit Candidates picklist api_name. The pipeline
# reads these fields' options from Recruit and passes them to build_schema.
PICKLIST_FIELDS = {
    "salutation": "Salutation",
    "gender": "Gender",
    "marital_status": "Martial_Status",   # sic - Recruit's api_name
    "current_job_title": "Current_Job_Title",
    "highest_qualification": "Highest_Qualification_Held",
    "education_board": "Education_Board",
    "department": "Departments",
}
DEFAULT_OPTIONS = {
    "salutation": ["Mr.", "Mrs.", "Ms."],
    "gender": ["Male", "Female", "Other"],
    "marital_status": ["Single", "Married"],
    "education_board": EDUCATION_BOARDS,
    "department": ["Technical", "BDE / Marketing", "HR", "Management"],
}


class AIError(Exception):
    pass


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not config.OPENAI_API_KEY:
            raise AIError("OPENAI_API_KEY is not set in your .env file.")
        _client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _client


def _s(desc=None):
    return {"type": "string", **({"description": desc} if desc else {})}


def _enum(options, desc=None):
    # "" = not stated / no option fits.
    return {"type": "string", "enum": list(options) + [""], **({"description": desc} if desc else {})}


def build_schema(picklists: dict[str, list[str]] | None = None) -> dict:
    """picklists: {basic_information key: Recruit options}, from PICKLIST_FIELDS."""
    picklists = picklists or {}

    def pick(key, desc):
        options = picklists.get(key) or DEFAULT_OPTIONS.get(key)
        return _enum(options, desc) if options else _s(desc)

    basic = {
        "salutation": pick("salutation", "ONLY if the resume states a title or the candidate's gender"),
        "first_name": _s(),
        "last_name": _s(),
        "gender": pick("gender", "ONLY if the resume states it"),
        "date_of_birth": _s("YYYY-MM-DD if the resume states a date of birth, else \"\""),
        "age_years": {"type": ["integer", "null"], "description": "age ONLY if the resume states it as a number; else null"},
        "marital_status": pick("marital_status", "ONLY if the resume states it"),
        "contact": _s("the candidate's primary PHONE number (digits only field, never an email)"),
        "phone": _s("a SECOND, different phone number if the resume lists one, else \"\""),
        "email": _s("primary email"),
        "secondary_email": _s("a SECOND, different email if the resume lists one, else \"\""),
        "street": _s("street / house / locality line of the current address"),
        "city": _s("current city"),
        "state": _s("current state / province"),
        "zip_code": _s("PIN / postal code"),
        "country": _s(),
        "location": _s("current location as 'City, State'"),
        "hometown_location": _s("permanent / native / hometown address if listed separately from the current one"),
        "linkedin_url": _s("full LinkedIn profile URL"),
        "portfolio_url": _s("personal portfolio or website URL"),
        "project_urls": {"type": "array", "items": {"type": "string"}, "description": "project, GitHub or demo URLs"},
        "work_history": {"type": "array", "description": "every job, internship, traineeship or freelance role "
                         "that has its own entry with a title, newest first, max 8 - under ANY heading (Experience, "
                         "Internships, Professional Training, Trainee, etc.). NOT degrees or academic projects",
                         "items": {"type": "object", "additionalProperties": False,
                                   "required": ["title", "company", "start", "end", "kind"],
                                   "properties": {
                                       "title": _s("the role's title exactly as written in that entry"),
                                       "company": _s(),
                                       "start": _s("YYYY-MM exactly as printed on this entry (YYYY-01 if only a year is given), "
                                                   "\"\" if no start is printed"),
                                       "end": _s("YYYY-MM, \"present\" if ongoing, \"\" if not printed"),
                                       "kind": {"type": "string", "enum": WORK_KINDS}}}},
        "current_job_title": pick("current_job_title", "title of the NEWEST work_history entry; \"\" if work_history is "
                                  "empty. Never the resume headline or summary"),
        "highest_qualification": pick("highest_qualification", "highest degree held, e.g. 'B.Tech', 'MBA'"),
        "degrees": {"type": "array", "description": "every college degree / diploma listed, max 5",
                    "items": {"type": "object", "additionalProperties": False, "required": ["degree", "year"],
                              "properties": {"degree": _s("as written, e.g. 'BCA', 'MBA Marketing'"),
                                             "year": _s("4-digit completion year printed next to THIS degree, else \"\"")}}},
        "education_board": pick("education_board",
            "the school board for class 10/12, ONLY when the resume names the board or its exam. "
            "Work out which option it belongs to: CBSE / AISSE / AISSCE -> CBSE; ICSE / ISC / CISCE -> ICSE; "
            "ANY state board, whatever its name or abbreviation (PSEB, HBSE, JKBOSE, UP Board, BSEB, "
            "Maharashtra/Karnataka/Tamil Nadu State Board, SSC, HSC, Matriculation, Intermediate Board, ...) "
            "-> State Education Board; a named board or curriculum that is none of the above (IB / International "
            "Baccalaureate, IGCSE, Cambridge, NIOS, an overseas board) -> Other. "
            "If the resume names only a school, a college, a university or a degree and no board -> \"\""),
        "skills": {"type": "array", "items": {"type": "string"},
                   "description": "skills and tools listed on the resume, max 25, short names"},
        "department": pick("department", "department the ROLE BEING HIRED belongs to"),
    }
    evaluation = {
        "matched_skills": {"type": "array", "items": {"type": "string"}, "description": "max 8"},
        "missing_skills": {"type": "array", "items": {"type": "string"}, "description": "max 8"},
        "skill_score": {"type": "integer", "description": "0-100"},
        "experience_score": {"type": "integer", "description": "0-100"},
        "ai_score": {"type": "integer", "description": "0-100"},
        "overall_score": {"type": "integer", "description": "0-100"},
        "recommendation": {"type": "string", "enum": RECOMMENDATIONS},
        "summary": _s("max 2 sentences"),
    }

    def obj(props):
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}

    return obj({"basic_information": obj(basic), "evaluation": obj(evaluation)})


PROMPT = """You screen resumes against a job description (JD). Return JSON only.

BASIC INFORMATION: copy values exactly as written in the resume. Never invent data:
use "" for missing text and [] for none. Take work_history ONLY from dated role entries -
the headline and summary ("Results-oriented Business Development Executive...") describe
what the candidate wants to be, not roles they held. Do not guess a
dropdown value - leave it "" unless the resume clearly supports it (department is the
exception: pick it from the ROLE BEING HIRED, given with the JD). For a dropdown whose
options don't include the exact value, pick the closest equivalent (e.g. "B.E." for
"Bachelor of Engineering"), or "" if nothing is equivalent. The resume ends with a
HYPERLINKS section of URLs hidden behind clickable text - use it for LinkedIn,
portfolio and project URLs.

EVALUATION - score only on evidence in the resume. EVERY score is 0-100 (never 0-10), using these bands:
  90-100 exceeds nearly every requirement | 75-89 meets all core requirements
  60-74 meets most, notable gaps | 40-59 partial fit | 0-39 largely unrelated
A candidate lacking the JD's core function cannot score above 59 overall.
skill_score = hard-skill overlap; experience_score = level/domain fit;
ai_score = holistic fit; overall_score = final combined score.
recommendation must match overall_score: Strong Fit 85+, Good Fit 70-84, Average Fit 50-69, Not a Fit <50."""


def compact_resume_text(text: str) -> str:
    """Shrinks resume text without losing content: normalises whitespace,
    drops blank runs and repeated short lines (page headers/footers), and caps length."""
    lines, seen_short = [], set()
    for raw in (text or "").replace("\r", "\n").split("\n"):
        line = re.sub(r"[ \t ]+", " ", raw).strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if len(line) <= 60:
            key = line.lower()
            if key in seen_short:
                continue
            seen_short.add(key)
        lines.append(line)
    return "\n".join(lines).strip()[: config.MAX_RESUME_CHARS]


def _usage(response) -> dict:
    u = getattr(response, "usage", None)
    if not u:
        return {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
    details = getattr(u, "prompt_tokens_details", None)
    return {
        "prompt_tokens": u.prompt_tokens or 0,
        "completion_tokens": u.completion_tokens or 0,
        "cached_tokens": (getattr(details, "cached_tokens", 0) or 0) if details else 0,
    }


def screen_resume(resume_text: str, job_description: str, role_name: str,
                  picklists: dict[str, list[str]] | None = None) -> tuple[dict, dict]:
    """Returns (result, usage). result = {"extraction", "basic_information", "evaluation"}."""
    response = _get_client().chat.completions.create(
        model=config.OPENAI_MODEL,
        temperature=0,
        max_completion_tokens=config.QUICK_MAX_OUTPUT_TOKENS,
        response_format={"type": "json_schema", "json_schema": {
            "name": "resume_screening", "strict": True, "schema": build_schema(picklists)}},
        messages=[
            # Order matters for prompt caching: identical prefix across a run.
            {"role": "system", "content": PROMPT},
            {"role": "system", "content": f"TODAY: {date.today().isoformat()} (use it for 'present' / ongoing roles)\n"
                                          f"ROLE BEING HIRED: {role_name}\n\nJOB DESCRIPTION:\n{job_description.strip()}"},
            {"role": "user", "content": f"RESUME:\n{resume_text}"},
        ],
    )
    choice = response.choices[0]
    if getattr(choice.message, "refusal", None):
        raise AIError(f"The AI model refused to process this resume: {choice.message.refusal}")
    if choice.finish_reason == "length":
        raise AIError("The AI response was cut off (token limit).")
    try:
        data = json.loads(choice.message.content)
    except (TypeError, ValueError):
        raise AIError("The AI returned invalid JSON.")

    ev = data["evaluation"]
    for key in ("skill_score", "experience_score", "ai_score", "overall_score"):
        if not isinstance(ev.get(key), (int, float)):
            raise AIError(f"AI returned a non-numeric {key}: {ev.get(key)!r}")
        ev[key] = max(0, min(100, int(round(ev[key]))))

    basic = data["basic_information"]
    basic["project_urls"] = [u for u in dict.fromkeys(basic.get("project_urls") or []) if u]
    basic["skills"] = [s for s in dict.fromkeys((s or "").strip() for s in basic.get("skills") or []) if s]
    # The candidate applied for the role whose folder the resume sits in.
    basic["position_applied"] = role_name
    _apply_work_history(basic)
    basic["tech_degree"], basic["graduation_year"] = _tech_degree(basic.get("degrees") or [])
    dob_age = _age_from_dob(basic.get("date_of_birth"))
    if dob_age is not None:
        basic["age_years"] = dob_age
    _fix_phone_fields(basic)
    _fix_email_fields(basic)
    return {"extraction": EXTRACTION_VERSION, **data}, _usage(response)


def _looks_like_phone(value: str) -> bool:
    return "@" not in value and len(re.sub(r"\D", "", value)) >= 7


def _fix_phone_fields(basic: dict) -> None:
    """Deterministic guard: contact and phone must be phone numbers. The model
    occasionally puts an email in 'contact' when a resume labels it that way."""
    contact = (basic.get("contact") or "").strip()
    phone = (basic.get("phone") or "").strip()
    if not _looks_like_phone(contact):
        contact = ""
    if not _looks_like_phone(phone):
        phone = ""
    if not contact and phone:
        contact, phone = phone, ""
    if re.sub(r"\D", "", phone) and re.sub(r"\D", "", phone) == re.sub(r"\D", "", contact):
        phone = ""
    basic["contact"], basic["phone"] = contact, phone


def _month(value, today: date):
    """'2025-01' -> months since year 0; 'present' -> this month; else None."""
    s = str(value or "").strip().lower()
    if s in ("present", "current", "ongoing", "till date", "now"):
        return today.year * 12 + today.month - 1
    m = re.match(r"^((?:19|20)\d{2})(?:-(\d{1,2}))?", s)
    if not m:
        return None
    month = int(m.group(2) or 1)
    return int(m.group(1)) * 12 + min(max(month, 1), 12) - 1


def _apply_work_history(basic: dict) -> None:
    """experience_years and experience come from the dated work_history
    entries - the model's own judgement of seniority is not used, because it
    reads the resume headline as a job and inflates freshers."""
    today = date.today()
    history = [w for w in basic.get("work_history") or [] if (w.get("title") or "").strip()]
    spans, undated = [], 0
    for w in history:
        start, end = _month(w.get("start"), today), _month(w.get("end"), today)
        if start is None or end is None or end < start:
            undated += 1
            continue
        spans.append((start, max(end, start + 1)))  # a role within one month counts as 1 month
    months = 0
    for start, end in _merge(spans):
        months += end - start

    if not history:
        basic["experience_years"] = 0  # fresher
    elif not spans:
        basic["experience_years"] = None  # roles listed without dates - unknown
    else:
        basic["experience_years"] = round(months / 12, 2)
    basic["experience_months"] = months if spans else None

    latest = history[0] if history else None
    if not latest:
        basic["current_job_title"] = ""
        basic["experience"] = "Fresher - no jobs, internships or training listed"
        return
    parts = []
    for w in history[:3]:
        kind = "" if w.get("kind") == "job" else f" ({w.get('kind')})"
        dates = " - ".join(x for x in (w.get("start"), w.get("end")) if x)
        parts.append(f"{w['title'].strip()}{kind}" + (f" at {w['company'].strip()}" if (w.get("company") or "").strip() else "")
                     + (f", {dates}" if dates else ""))
    total = human_experience(months) if spans else ""
    basic["experience"] = (f"{total} total: " if total else "") + "; ".join(parts) + (
        f" (+{undated} undated)" if undated and spans else "")


def human_experience(months) -> str:
    """20 -> '1 yr 8 mo'. Exact, never rounded to a neat number."""
    if months is None:
        return ""
    years, rest = divmod(int(months), 12)
    parts = ([f"{years} yr" + ("s" if years > 1 else "")] if years else []) + ([f"{rest} mo"] if rest else [])
    return " ".join(parts) or "0 mo"


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _tech_degree(degrees: list[dict]) -> tuple[str, str]:
    """(degree, year) for the highest B.Tech/B.E., BCA or MCA listed, else ("", "")."""
    for label, pattern in TECH_DEGREE_PATTERNS:
        for d in degrees:
            if re.search(pattern, str(d.get("degree") or ""), re.IGNORECASE):
                year = re.search(r"\b(19|20)\d{2}\b", str(d.get("year") or ""))
                return label, year.group(0) if year else ""
    return "", ""


def _age_from_dob(dob):
    """Age in whole years from a YYYY-MM-DD date of birth; None if unusable."""
    try:
        born = date.fromisoformat(str(dob or "").strip())
    except ValueError:
        return None
    today = date.today()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return age if 14 <= age <= 80 else None


def _fix_email_fields(basic: dict) -> None:
    """secondary_email must be a different, valid-looking address."""
    email = (basic.get("email") or "").strip()
    second = (basic.get("secondary_email") or "").strip()
    if "@" not in second or second.lower() == email.lower():
        second = ""
    if "@" not in email and second:
        email, second = second, ""
    basic["email"], basic["secondary_email"] = email, second


def full_name(result: dict) -> str:
    b = result.get("basic_information") or {}
    return " ".join(p for p in (b.get("first_name", "").strip(), b.get("last_name", "").strip()) if p)


def estimate_cost(prompt_tokens: int, completion_tokens: int, cached_tokens: int) -> float:
    uncached = max(0, prompt_tokens - cached_tokens)
    return (uncached * config.PRICE_INPUT_PER_M
            + cached_tokens * config.PRICE_CACHED_INPUT_PER_M
            + completion_tokens * config.PRICE_OUTPUT_PER_M) / 1_000_000
