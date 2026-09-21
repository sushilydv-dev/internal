"""
Maps the basic information extracted by ai_scorer onto Zoho Recruit
Candidates api_names. zoho_recruit.prepare_payload then converts each value
to the field's real type and drops anything Recruit would reject.

  basic_information        Recruit field(s)
  salutation               Salutation (dropdown)
  first_name               First_Name
  last_name                Last_Name (mandatory - falls back to "Unknown")
  gender                   Gender (dropdown)
  age_years                Age_Yrs (dropdown - matched to the option covering the age;
                           computed from date_of_birth when the resume gives one)
  marital_status           Martial_Status (dropdown, sic)
  contact                  Contact, Mobile (+91 added to Indian mobiles)
  phone                    Phone (falls back to contact)
  email                    Email
  secondary_email          Secondary_Email
  street/city/state        Street, City, State
  zip_code/country         Zip_Code, Country
  location                 Current_Location
  hometown_location        Hometown_Location
  linkedin_url             Linkedin_URL
  portfolio/project_urls   Github_URL (first github.com link)
  portfolio_url            Website, Portfolio_Link_Video_Editors ("Portfolio /Project URL")
  project_urls             Portfolio_Link_Video_Editors
  experience_years         Work_Exp ("Experience", nearest dropdown option) - summed in code
                           from work_history dates, internships and training included
  current_job_title        Current_Job_Title (dropdown)
  highest_qualification    Highest_Qualification_Held (dropdown)
  graduation_year          Year_of_Clearing_B_Tech_BCA_MCA (year of tech_degree only)
  education_board          Education_Board (dropdown)
  skills                   Skill_Set
  department               Departments (dropdown)
  position_applied         Current_Job_Role ("Position Applied" - the role folder)

Not taken from the resume - Recruit or the hiring team fill these:
  assessment / call / interview / HR / technical / final-result fields,
  Source, Origin, Candidate_Status, Candidate_Owner, Internal_Employee__s,
  Referred_by_Employee__s, and system fields (Candidate_ID, Created_*,
  Updated_On, Last_Activity_Time, Mobile_Number formula, ZOHO_RECORD_ID).
"""
import re
from datetime import date

import config


def _url(url: str) -> str:
    url = (url or "").strip()
    if not url or url.lower().startswith("mailto:"):
        return ""
    return url if re.match(r"^https?://", url, re.IGNORECASE) else "https://" + url


def _text(value) -> str:
    return str(value or "").strip()


def phone_number(value) -> str:
    """Indian mobile numbers get a +91 prefix. Without a country code, Recruit
    formats a bare 10-digit number US-style: 8528323584 -> (852) 832-3584."""
    raw = _text(value)
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    elif raw.startswith("+"):
        return raw  # already has another country code
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91 {digits}"
    return raw  # landline or unrecognised - send as written


# Recruit's Work_Exp options as (years, label), lowest first.
WORK_EXP_OPTIONS = ((0, "Fresher"), (0.25, "3 M"), (0.5, "6 M"), (0.75, "9 M"), (1, "1 Yr"),
                    (1.5, "1.5 Yr"), (2, "2 Yr"), (3, "3 Yr"), (4, "4 Yr"), (5, "5 Yr"))
# Half a month, so "5 months and 3 weeks" still reaches the 6 M option.
_ROUNDING_SLACK = 0.5 / 12


def work_exp_option(years) -> str:
    """Recruit Work_Exp option for a number of years, rounded DOWN: someone with
    8 months of experience shows as '6 M', never '9 M'. Overstating a candidate's
    experience is worse than understating it."""
    if years is None:
        return ""
    if float(years) > 5 + _ROUNDING_SLACK:
        return "> 5 Yrs"
    y = float(years) + _ROUNDING_SLACK
    label = "Fresher"
    for limit, option in WORK_EXP_OPTIONS:
        if y >= limit:
            label = option
    return label


def build_candidate_payload(result: dict, *, role_name: str, folder_path: str) -> dict:
    b = result.get("basic_information") or {}
    ev = result.get("evaluation") or {}

    contact = phone_number(b.get("contact"))
    portfolio = _url(b.get("portfolio_url"))
    project_links = [u for u in dict.fromkeys(_url(u) for u in (b.get("project_urls") or [])) if u]
    portfolio_and_projects = "\n".join([u for u in [portfolio] if u] + [u for u in project_links if u != portfolio])
    github = next((u for u in [portfolio, *project_links] if re.search(r"//(www\.)?github\.com/", u, re.IGNORECASE)), "")
    year = re.search(r"\b(19|20)\d{2}\b", _text(b.get("graduation_year")))

    payload = {
        "Salutation": _text(b.get("salutation")),
        "First_Name": _text(b.get("first_name")),
        "Last_Name": _text(b.get("last_name")) or "Unknown",
        "Gender": _text(b.get("gender")),
        "Age_Yrs": b.get("age_years"),
        "Martial_Status": _text(b.get("marital_status")),
        "Contact": contact,
        "Mobile": contact,
        "Phone": phone_number(b.get("phone")) or contact,
        "Email": _text(b.get("email")),
        "Secondary_Email": _text(b.get("secondary_email")),
        "Street": _text(b.get("street")),
        "City": _text(b.get("city")),
        "State": _text(b.get("state")),
        "Zip_Code": _text(b.get("zip_code")),
        "Country": _text(b.get("country")),
        "Current_Location": _text(b.get("location")),
        "Hometown_Location": _text(b.get("hometown_location")),
        "Linkedin_URL": _url(b.get("linkedin_url")),
        "Github_URL": github,
        "Website": portfolio,
        "Portfolio_Link_Video_Editors": portfolio_and_projects,
        "Work_Exp": work_exp_option(b.get("experience_years")),
        "Current_Job_Title": _text(b.get("current_job_title")),
        "Highest_Qualification_Held": _text(b.get("highest_qualification")),
        "Year_of_Clearing_B_Tech_BCA_MCA": year.group(0) if year else "",
        "Education_Board": _text(b.get("education_board")),
        "Skill_Set": ", ".join(b.get("skills") or []),
        "Departments": _text(b.get("department")),
        "Current_Job_Role": _text(b.get("position_applied")) or role_name,
    }

    score_values = {
        "overall_score": ev.get("overall_score"),
        "ai_score": ev.get("ai_score"),
        "skill_score": ev.get("skill_score"),
        "summary": ev.get("summary"),
    }
    for key, api in config.RECRUIT_SCORE_FIELDS.items():
        if api:
            payload[api] = score_values.get(key)

    return {k: v for k, v in payload.items() if v not in ("", None, [], ())}


AI_NOTE_TITLE = "AI Screening"


def ai_note_content(result: dict, *, role_name: str, pass_threshold: int, filename: str) -> str:
    """Plain-text body of the AI Screening note written to the candidate."""
    ev = result.get("evaluation") or {}
    b = result.get("basic_information") or {}
    lines = [
        f"Overall score: {ev.get('overall_score')}/100 - {ev.get('recommendation', '')} (pass mark {pass_threshold})",
        f"Experience: {_text(b.get('experience')) or 'not stated'}",
        f"AI score: {ev.get('ai_score')}  |  Skill score: {ev.get('skill_score')}  |  "
        f"Experience score: {ev.get('experience_score')}",
        "",
        f"Summary: {_text(ev.get('summary'))}",
    ]
    if ev.get("matched_skills"):
        lines.append(f"Matches the job: {', '.join(ev['matched_skills'])}")
    if ev.get("missing_skills"):
        lines.append(f"Missing: {', '.join(ev['missing_skills'])}")
    lines += ["", f"Position: {role_name}  |  Resume: {filename}  |  Screened {date.today().isoformat()} by Resume Screener"]
    return "\n".join(lines)
