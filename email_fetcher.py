"""
Fetch resumes from Indeed application emails and file them in WorkDrive.

Flow (start_fetch() runs it in a background thread; the UI polls status()):
  1. IMAP into the hiring inbox, find recent "New application for <Position>, <City>" mails.
  2. Skip mails already handled (storage.fetched_emails, keyed by Message-ID).
  3. In each mail find the "View resume" link, download the resume (parallel Chrome tabs).
  4. File it as ROOT / <Position> / <date> / <Candidate>.pdf, creating the position
     and date folders only when they don't exist yet. Uploads overlap with downloads.

A mail that fails (link expired, login wall...) is NOT marked as fetched, so the
next click retries it. Each resume is saved the moment it is ready, so a crash or
restart mid-run loses nothing already done.
"""
import asyncio
import email
import imaplib
import os
import re
import sys
import time
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Thread
from urllib.parse import urljoin, urlparse

import requests

# On Render the default browser cache (~/.cache) is not kept between build and runtime.
# Keep Playwright's browsers inside the project folder instead (same as PLAYWRIGHT_BROWSERS_PATH=0).
if os.getenv("RENDER") and not os.getenv("PLAYWRIGHT_BROWSERS_PATH"):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"

try:
    from playwright.async_api import Error as PWError, async_playwright
except ImportError:  # only needed for Indeed links
    PWError, async_playwright = Exception, None

import config
import storage
import zoho_workdrive as wd


class EmailFetchError(Exception):
    pass


_BROWSER_TABS = 4      # parallel Chrome tabs
_CHROME_BATCH = 10     # restart Chrome after this many resumes (frees leaked memory)
_UPLOAD_WORKERS = 4    # parallel WorkDrive uploads
_IMAP_BATCH = 20       # mail bodies fetched per IMAP call
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_SUBJECT_RE = re.compile(r"New application for\s+(.+)", re.I)


# ---------------------------------------------------------------- parsing helpers

class _Links(HTMLParser):
    """Collects (href, visible text) for every <a>."""

    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def _decode(value) -> str:
    # " ".join(split()) also unfolds headers wrapped over several lines.
    return " ".join(str(make_header(decode_header(value or ""))).split())


def _html_body(msg) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return ""


def _text_body(msg) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return ""


def parse_position(subject: str) -> str:
    """'[Action required] New application for Digital Marketing Internship, Mohali, Punjab'
    -> 'Digital Marketing Internship' (everything before the first comma = location)."""
    m = _SUBJECT_RE.search(subject)
    if not m:
        raise EmailFetchError(f"Can't read the position from subject: {subject!r}")
    return wd.clean_folder_name(m.group(1).split(",")[0])


def _candidate_name(msg, html: str, text: str) -> str:
    """Prefers the 'X applied' heading in the body (works for forwarded mails too,
    where From is the forwarder); falls back to the From name."""
    m = (re.search(r">\s*([^<>]+?)\s*</a>\s*applied", html)
         or re.search(r"^\s*([^\n\[\]]+?)\s+applied\s*$", text, re.I | re.M))
    name = m.group(1) if m else email.utils.parseaddr(_decode(msg.get("From")))[0]
    return re.sub(r"[^\w .'-]", "", name).strip().title() or "Candidate"


def _find_resume_link(html: str, text: str = "") -> str | None:
    p = _Links()
    p.feed(html)
    for href, label in p.links:
        if label.lower().startswith("view resume") and href:
            return href
    # Plain-text mail: "View resume [4]" ... "Links:" list, "[4] https://..." (URL may be on the next line)
    m = re.search(r"view resume\s*\[(\d+)\]", text, re.I)
    if m:
        u = re.search(rf"^\s*\[{m.group(1)}\]\s*(\S+)", text, re.M)
        if u:
            return u.group(1)
    return None


# ---------------------------------------------------------------- resume download

def _kind(content: bytes) -> str | None:
    if content[:5] == b"%PDF-":
        return ".pdf"
    if content[:2] == b"PK":
        return ".docx"
    return None


def _is_indeed(link: str) -> bool:
    host = urlparse(link).netloc.lower()
    return host == "indeed.com" or host.endswith(".indeed.com")


def download_resume(link: str) -> tuple[bytes, str]:
    """Plain-HTTP download for non-Indeed links. Returns (bytes, extension).
    The link normally lands on a viewer page; its 'Download' link / file URL is followed."""
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    try:
        resp = s.get(link, timeout=30, allow_redirects=True)
    except requests.exceptions.RequestException as e:
        raise EmailFetchError(f"Could not open the resume link: {e}")

    for _ in range(4):  # tracking redirect -> viewer page -> download link
        ext = _kind(resp.content)
        if ext:
            return resp.content, ext
        if re.search(r"<title>\s*(security check|blocked)", resp.text, re.I):
            raise EmailFetchError("The site blocked the automated download (Security Check).")
        refresh = re.search(r"http-equiv=[\"']?refresh[\"']?[^>]*URL=([^\"'>]+)", resp.text, re.I)
        if refresh:  # tracking links redirect via <meta refresh>
            try:
                resp = s.get(urljoin(resp.url, unescape(refresh.group(1))), timeout=30, allow_redirects=True)
            except requests.exceptions.RequestException as e:
                raise EmailFetchError(f"Could not open the resume link: {e}")
            continue
        if resp.status_code in (401, 403):
            raise EmailFetchError(f"Resume link refused access (HTTP {resp.status_code}) - it may need a login or have expired.")
        p = _Links()
        p.feed(resp.text)
        candidates = [h for h, t in p.links if h and (t.lower() == "download" or "download" in h.lower())]
        candidates += re.findall(r'["\'](https?://[^"\']+?\.(?:pdf|docx)[^"\']*)["\']', resp.text, re.I)
        nxt = next((urljoin(resp.url, c.replace("&amp;", "&")) for c in candidates), None)
        if not nxt:
            break
        try:
            resp = s.get(nxt, timeout=30, allow_redirects=True, headers={"Referer": resp.url})
        except requests.exceptions.RequestException as e:
            raise EmailFetchError(f"Could not download the resume file: {e}")
    raise EmailFetchError("Resume page opened but no downloadable PDF/DOCX was found (the page may need a login or JavaScript).")


# Indeed blocks default headless Chrome. These make it look like a normal desktop
# browser (no 'webdriver' flag, real user agent, plugins, languages) so it is let in.
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""
_CHROME_ARGS = ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage",
                # trim Chromium's memory: a 512 MB Render instance is easily exhausted
                "--disable-gpu", "--disable-extensions", "--mute-audio", "--disable-background-networking",
                "--js-flags=--max-old-space-size=128"]
_SKIP_RESOURCES = {"image", "media", "font"}  # the resume page doesn't need them to offer the Download button


async def _install_chromium() -> None:
    """Downloads Playwright's Chromium (~150 MB, once per deploy) when it is missing."""
    storage.log("info", "Email fetch: Chromium not found - downloading it (first run only, ~1-2 min).")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "playwright", "install", "chromium",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        raise EmailFetchError("Downloading Chromium timed out.")
    if proc.returncode != 0:
        raise EmailFetchError(f"Could not download Chromium: {out.decode(errors='replace')[-300:]}")


async def _launch_browser(pw):
    """Headless, stealth-patched browser -> (browser, context). Uses installed Google
    Chrome when there is one (a dev PC), else Playwright's bundled Chromium (a server),
    downloading that first if the build step didn't."""
    headless = os.getenv("FETCH_HEADLESS", "1").strip().lower() in ("1", "true", "yes", "on")
    try:
        browser = await pw.chromium.launch(channel="chrome", headless=headless, args=_CHROME_ARGS)
    except Exception:
        try:
            browser = await pw.chromium.launch(headless=headless, args=_CHROME_ARGS)
        except Exception as e:
            if "Executable doesn't exist" not in str(e):
                raise
            await _install_chromium()
            browser = await pw.chromium.launch(headless=headless, args=_CHROME_ARGS)
    ctx = await browser.new_context(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{browser.version} Safari/537.36"),
        viewport={"width": 1366, "height": 768}, locale="en-US", accept_downloads=True)
    await ctx.add_init_script(_STEALTH_JS)

    async def _lean(route):
        await (route.abort() if route.request.resource_type in _SKIP_RESOURCES else route.continue_())
    await ctx.route("**/*", _lean)
    return browser, ctx


async def _download_tab(browser, link: str):
    """One Indeed resume through its own tab, with one retry (a rate-limit 'Blocked'
    page or a slow load). Returns (bytes, ext) or an EmailFetchError."""
    err = EmailFetchError("Download failed.")
    for attempt in range(2):
        page = await browser.new_page()
        try:
            await page.goto(link, wait_until="domcontentloaded", timeout=60000)
            if "blocked" in (await page.title()).lower():
                err = EmailFetchError("Indeed temporarily blocked the request (too many at once).")
                await asyncio.sleep(5 * (attempt + 1))
                continue
            btn = page.get_by_text("Download", exact=True).first
            await btn.wait_for(timeout=30000)
            async with page.expect_download(timeout=60000) as dl:
                await btn.click()
            with open(await (await dl.value).path(), "rb") as f:
                content = f.read()
            ext = _kind(content)
            if ext:
                return content, ext
            err = EmailFetchError("Downloaded file is not a PDF/DOCX.")
        except PWError as e:
            err = EmailFetchError(f"Browser download failed: {str(e).splitlines()[0]}")
        finally:
            try:
                await page.close()
            except Exception:
                pass
    return err


# ---------------------------------------------------------------- WorkDrive filing

_folder_lock = Lock()


def _find_or_create(parent_id: str, name: str, cache: dict) -> tuple[str, bool]:
    """Case-insensitive match on an existing child folder; create it if missing.
    `cache` lives for one fetch run, so each folder is listed/created once."""
    key = (parent_id, name.lower())
    with _folder_lock:
        if key not in cache:
            for f in wd.list_children(parent_id)["folders"]:
                if f["name"].strip().lower() == name.lower():
                    cache[key] = (f["id"], False)
                    break
            else:
                cache[key] = (wd.create_folder(parent_id, name)["id"], True)
        return cache[key]


def date_label(dt: datetime) -> str:
    return f"{dt.day} {dt.strftime('%b')}"  # "21 Oct" - same as the UI's date folders


def file_resume(position: str, when: datetime, filename: str, content: bytes, cache: dict) -> dict:
    role_id, role_new = _find_or_create(config.WORKDRIVE_ROOT_FOLDER_ID, position, cache)
    date_id, date_new = _find_or_create(role_id, date_label(when), cache)
    up = None
    for attempt in range(3):  # network blips / WorkDrive rate limit are retried
        try:
            up = wd.upload_resume(date_id, filename, content)
            break
        except wd.WorkDriveError as e:
            transient = any(t in str(e) for t in ("Could not reach", "HTTP 429", "HTTP 5"))
            if not transient or attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    return {"file_id": up["id"], "file_name": up["name"], "folder": f"{position} / {date_label(when)}",
            "new_position_folder": role_new, "new_date_folder": date_new}


# ---------------------------------------------------------------- progress state

_state_lock = Lock()
_state: dict = {}


def _reset_state() -> None:
    _state.clear()
    _state.update(running=False, phase="idle", total=0, done=0, saved=0, failed=0,
                  results=[], error=None, started_at=None)


_reset_state()


def status() -> dict:
    with _state_lock:
        return {**_state, "results": list(_state["results"])}


def _record(item: dict) -> None:
    with _state_lock:
        _state["results"].append(item)
        _state["done"] += 1
        _state["saved" if item["ok"] else "failed"] += 1


def start_fetch() -> dict:
    """Starts a background fetch (or reports the one already running)."""
    if not config.EMAIL_APP_PASSWORD:
        raise EmailFetchError("EMAIL_APP_PASSWORD is not set in .env (Gmail App Password for "
                              f"{config.EMAIL_ADDRESS}).")
    with _state_lock:
        if _state["running"]:
            return {**_state, "results": list(_state["results"]), "started": False}
        _reset_state()
        _state.update(running=True, phase="scanning", started_at=time.time())
    Thread(target=_run, daemon=True).start()
    return {**status(), "started": True}


# ---------------------------------------------------------------- main job

def _message_ids(imap, ids: list) -> dict:
    """Message-ID per IMAP number in ONE round trip (headers only, no bodies)."""
    typ, raw = imap.fetch(b",".join(ids).decode(), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
    out = {}
    for part in raw if typ == "OK" else []:
        if isinstance(part, tuple):
            mid = email.message_from_bytes(part[1]).get("Message-ID")
            out[part[0].split()[0]] = (mid or "").strip()
    return out


def _scan() -> list[dict]:
    """Logs in, finds mails not handled before and parses each into a job."""
    try:
        imap = imaplib.IMAP4_SSL(config.EMAIL_IMAP_HOST)
        imap.login(config.EMAIL_ADDRESS, config.EMAIL_APP_PASSWORD)
    except (imaplib.IMAP4.error, OSError) as e:
        raise EmailFetchError(f"Could not log in to {config.EMAIL_ADDRESS}: {e}. "
                              "Check the App Password and that IMAP is enabled in Gmail.")
    jobs = []
    try:
        imap.select("INBOX", readonly=True)  # never changes read/unread state
        since = (datetime.now() - timedelta(days=config.EMAIL_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        typ, data = imap.search(None, "SINCE", since, "SUBJECT", f'"{config.EMAIL_SUBJECT_FILTER}"')
        ids = data[0].split() if typ == "OK" and data and data[0] else []
        mids = _message_ids(imap, ids) if ids else {}
        new = [n for n in ids if not (mids.get(n) and storage.email_already_fetched(mids[n]))]

        for i in range(0, len(new), _IMAP_BATCH):  # bodies in batches, not one round trip each
            batch = new[i:i + _IMAP_BATCH]
            typ, raw = imap.fetch(b",".join(batch).decode(), "(BODY.PEEK[])")
            for part in raw if typ == "OK" else []:
                if not isinstance(part, tuple):
                    continue
                msg = email.message_from_bytes(part[1])
                mid = mids.get(part[0].split()[0]) or f"{msg.get('Date')}|{msg.get('Subject')}"
                subject = _decode(msg.get("Subject"))
                try:
                    html, text = _html_body(msg), _text_body(msg)
                    link = _find_resume_link(html, text)
                    if not link:
                        raise EmailFetchError("No 'View resume' link in this email.")
                    try:
                        when = parsedate_to_datetime(msg.get("Date"))
                    except (TypeError, ValueError):
                        when = datetime.now()
                    jobs.append({"mid": mid, "subject": subject, "position": parse_position(subject),
                                 "link": link, "name": _candidate_name(msg, html, text), "when": when})
                except EmailFetchError as e:
                    storage.log("warn", f"Email fetch failed for '{subject}': {e}")
                    _record({"subject": subject, "ok": False, "error": str(e)})
                    with _state_lock:
                        _state["total"] += 1
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return jobs


async def _pipeline(jobs: list[dict], finish) -> None:
    """Downloads (Indeed: parallel Chrome tabs) and files resumes at the same time:
    as soon as one download completes, its upload runs on a worker thread while
    other tabs keep downloading. Chrome restarts every _CHROME_BATCH resumes so a
    crash or memory leak can't sink a big run."""
    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=_UPLOAD_WORKERS)
    sem = asyncio.Semaphore(_BROWSER_TABS)
    try:
        for i in range(0, len(jobs), _CHROME_BATCH):
            chunk = jobs[i:i + _CHROME_BATCH]
            browser, ctx, launch_err, pw = None, None, None, None
            if any(_is_indeed(j["link"]) for j in chunk):
                if async_playwright is None:
                    launch_err = EmailFetchError("Indeed needs a browser: run 'pip install playwright' and 'playwright install chromium'.")
                else:
                    try:
                        pw = await async_playwright().start()
                        browser, ctx = await _launch_browser(pw)
                    except Exception as e:
                        launch_err = EmailFetchError(f"Could not start the browser: {str(e).splitlines()[0]}")

            async def one(job):
                if not _is_indeed(job["link"]):
                    try:
                        out = await loop.run_in_executor(pool, download_resume, job["link"])
                    except EmailFetchError as e:
                        out = e
                elif ctx is None:
                    out = launch_err
                else:
                    async with sem:
                        out = await _download_tab(ctx, job["link"])
                await loop.run_in_executor(pool, finish, job, out)

            try:
                await asyncio.gather(*(one(j) for j in chunk))
            finally:
                if browser:
                    await browser.close()
                if pw:
                    await pw.stop()
    finally:
        pool.shutdown(wait=True)


def _run() -> None:
    try:
        jobs = _scan()
        with _state_lock:
            _state["total"] += len(jobs)
            _state["phase"] = "downloading"
        cache: dict = {}

        def finish(job, out):
            item = {"subject": job["subject"], "candidate": job["name"], "position": job["position"]}
            try:
                if isinstance(out, Exception):
                    raise out
                content, ext = out
                filed = file_resume(job["position"], job["when"], f"{job['name']}{ext}", content, cache)
                storage.mark_email_fetched(job["mid"], job["position"], job["name"], filed["file_name"], filed["file_id"])
                storage.log("info", f"Email fetch: {job['name']} -> {filed['folder']}")
                item.update(ok=True, **filed)
            except (EmailFetchError, wd.WorkDriveError) as e:
                storage.log("warn", f"Email fetch failed for {job['name']} ('{job['subject']}'): {e}")
                item.update(ok=False, error=str(e))
            except Exception as e:  # never let one resume stop the other 79
                storage.log("error", f"Email fetch crashed for {job['name']}: {e}")
                item.update(ok=False, error=f"Unexpected error: {e}")
            _record(item)

        if jobs:
            asyncio.run(_pipeline(jobs, finish))
        with _state_lock:
            s, f = _state["saved"], _state["failed"]
        storage.log("info", f"Email fetch finished: {s} saved, {f} failed.")
    except Exception as e:
        storage.log("error", f"Email fetch aborted: {e}")
        with _state_lock:
            _state["error"] = str(e)
    finally:
        with _state_lock:
            _state.update(running=False, phase="done")
