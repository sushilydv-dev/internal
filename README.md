# Resume Screener — WorkDrive → AI Score → Zoho Recruit

check repo

Ek Flask web app jo:

1. Zoho **WorkDrive** ke ek folder se resumes (.pdf/.docx) one-by-one uthaata hai
2. Har resume ko **OpenAI** se, aapki Job Description ke against, analyze karke
   `ai_score`, `skill_score`, `overall_score` + full JSON verdict deta hai
3. Ek click me candidate ko Zoho **Recruit → Candidates module** me save kar deta hai
   (resume file attachment ke saath)

---

## 0. Pehle yeh decide karo: Zoho account ka region

Aapka Zoho account jis data-center pe signup hua hai (India users usually `in`),
wahi `.env` me `ZOHO_DOMAIN` set karna hoga. Options: `com`, `in`, `eu`, `com.au`, `jp`.
Agar pata nahi, apne Zoho account ke URL me dekho — `recruit.zoho.in` hai ya `recruit.zoho.com`.

---

## 1. Zoho API credentials banao (Client ID / Secret)

1. Browser me kholo: **https://api-console.zoho.<in/com/...>/**
2. **GET STARTED** → **Self Client** pe hover karke **CREATE NOW** dabao.
3. **CREATE** → **OK** — ab **Client Secret** tab me `Client ID` aur `Client Secret` dikhega, copy kar lo.

## 2. Refresh token generate karo (ek baar ka kaam)

```bash
cd zoho_resume_screener
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# .env kholo, ZOHO_DOMAIN, ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET fill karo
```

Ab wapas API Console me, apne Self Client ke andar **Generate Code** tab kholo:

- **Scope** field me yeh exact string paste karo (WorkDrive + Recruit dono ek saath):
  ```
  WorkDrive.files.READ,WorkDrive.files.CREATE,WorkDrive.files.UPDATE,WorkDrive.files.DELETE,ZohoRecruit.modules.ALL,ZohoRecruit.settings.READ
  ```
- **Time Duration** — 10 minutes rakho (bas itni der me exchange karna hai)
- **Scope Description** — kuch bhi likh do, e.g. "resume screener"
- **CREATE** dabao. Agar prompt aaye ki konsa app/portal chahiye, WorkDrive aur Recruit dono select kar dena.
- Ek **grant token** (authorization code) screen pe dikhega — copy kar lo (yeh sirf kuch minute valid rehta hai, jaldi karo).

Ab terminal me:

```bash
python generate_refresh_token.py
```

Jab pooche, wahi grant token paste kar do. Script `ZOHO_REFRESH_TOKEN` print karega — isse `.env` me daal do.

Yeh ek hi refresh token WorkDrive aur Recruit dono ke liye kaam karega, kyunki scope me dono the.

## 3. OpenAI API key

`.env` me `OPENAI_API_KEY` daalo (https://platform.openai.com/api-keys se milega).
`OPENAI_MODEL` default `gpt-4o-mini` hai — accuracy zyada chahiye to `gpt-4o` try karo.

## 4. Zoho Recruit me custom fields banao (sirf ek baar)

AI Score / Skill Score / Overall Score / Summary — yeh Candidates module ke
standard fields nahi hain, inhe khud banana padega:

**Zoho Recruit → Setup → Modules and Fields → Candidates → New Field**

| Field label      | API name (exactly yeh rakhna) | Type            |
| ---------------- | ----------------------------- | --------------- |
| AI Score         | `AI_Score`                    | Number          |
| Skill Score      | `Skill_Score`                 | Number          |
| Overall Score    | `Overall_Score`               | Number          |
| JD Match Summary | `JD_Match_Summary`            | Multi-line text |

Agar tum different API names use karna chahte ho, `config.py` me
`RECRUIT_CUSTOM_SCORE_FIELDS` dict edit kar dena — bas ek jagah change karna hoga.

## 5. WorkDrive folder ID dhoondo

WorkDrive me jis folder me resumes hain usko kholo — URL ke end me jo ID dikhega
(e.g. `.../workdrive/folders/jk3x9f2a8b1c4`), wahi Folder ID hai jo app me daalna hai.

## 6. App run karo

```bash
PORT=5001 python3 app.py
```

Browser me kholo → **http://127.0.0.1:5001** (port 5000 macOS AirPlay use karta hai)

### Flow

WorkDrive layout: `ROOT / <role, e.g. Full Stack> / <date, e.g. 16 Sept> / resumes`

1. **Folders** tab → role folder kholo → right side **Job description** + passing score save karo
2. Date folder kholo → **Screen resumes**
3. Har resume: download → text + hidden hyperlinks extract → AI full extraction + score
   - score < threshold → **Not passed**, file WorkDrive me hi rehti hai
   - score ≥ threshold → Recruit me candidate upsert + resume attach → Recruit se
     read-back verify → tab WorkDrive se **permanently delete**
4. **Activity log** tab → har run, Recruit push aur deletion ka audit (`screener.db`)

### Delete safety

File delete sirf tab hoti hai jab ye sab live re-check me true ho:
file hai (folder nahi), .pdf/.docx hai, naam + byte size same hai, usi folder me hai
jahan score hui, parent chain ROOT tak pahunchti hai, aur Recruit me same naam + size
ka attachment confirm hai. Koi bhi check fail → file rehti hai, reason log hota hai.
Emergency: `.env` me `WORKDRIVE_DELETE_ENABLED=false`.

### Token scopes

Delete aur exact Recruit field mapping ke liye token me ye scopes chahiye
(`python3 generate_refresh_token.py` yahi string dikhata hai):

```
WorkDrive.files.READ,WorkDrive.files.CREATE,WorkDrive.files.UPDATE,WorkDrive.files.DELETE,ZohoRecruit.modules.ALL,ZohoRecruit.settings.READ
```

Recruit me score fields banao (Candidates): `AI_Score` (Number), `Skill_Score` (Number),
`AI_Summary` (Multi Line). Naam alag ho to `config.py` → `RECRUIT_SCORE_FIELDS`.

---

## Notes / limitations

- Runs, results, JDs aur audit log `screener.db` (SQLite) me save hote hain - restart pe safe.
- Server restart mid-run ho to wo run "failed (interrupted)" mark hota hai; dobara
  **Screen resumes** dabao - Recruit upsert aur attachment dono idempotent hain.
- Jo resumes same JD pe pehle reject ho chuke hain wo skip hote hain (OpenAI cost bachane
  ke liye); **Re-screen all** tick karke dobara score karo.
- Scanned/image-only PDFs se text nahi nikalta (OCR nahi hai) - wo "Failed" dikhenge.
- Single-user local tool hai: login/auth nahi hai, team ke liye deploy karne se pehle
  auth lagana zaroori hai.

# internal
