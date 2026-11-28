"""
7-DAY EMAIL SEQUENCER  (Build To Grow)
--------------------------------------
Give it a lead list + 7 email files. Every lead walks through the sequence
on its own calendar:  Day 1 email -> Day 2 email -> ... -> Day 7 email.

Each run (once a day, via GitHub Actions) it:
  1. checks your Gmail inbox: anyone who REPLIED or BOUNCED is stopped
  2. works out who is due today (follow-ups first, then brand-new leads)
  3. sends up to DAILY_LIMIT emails, spaced out with random delays
  4. saves progress to state.csv so nothing is ever sent twice
"""

import os
import re
import csv
import sys
import html
import time
import random
import imaplib
import smtplib
import email
from email.header import decode_header, make_header
from email.utils import formatdate, make_msgid, parseaddr
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


# ============================================================
# CONFIGURATION  (you normally never edit this file)
#   - lists   -> drop CSV files into the  leads/   folder
#   - emails  -> emails/day1.txt, day2.txt, ... (any number of days)
#   - options -> settings.json
# ============================================================

import json

LEADS_DIR = "leads"
STATE_FILE = "state.csv"            # created automatically - DO NOT delete
EMAILS_DIR = "emails"
SETTINGS_FILE = "settings.json"
LOGO_FILE = "btg_logo.png"          # optional, next to this script

SENDER_EMAIL = os.environ.get("MY_EMAIL")
SENDER_PASSWORD = os.environ.get("MY_PASSWORD")     # Gmail App Password

DEFAULT_SETTINGS = {
    "sender_name": "Sarah",
    "company_name": "Build To Grow",
    "company_tagline": "Digital Growth & Technology",
    "fallback_name": "your restaurant",   # used when a lead has no name
    "timezone": "Asia/Dubai",
    "start_time": "13:00",       # do not send before this time (24h clock, local time)
    "end_time": "20:00",         # stop sending at this time
    "send_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],   # remove days to switch them off
    "daily_limit": 500,          # max emails per day (Gmail personal ~500, Workspace ~2000)
    "day_offsets": None,         # None = one email per day. Or e.g. [0,2,4,6,9,12,16]
    "min_delay_seconds": 170,    # about 3 minutes between emails
    "max_delay_seconds": 190,
}


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    if not os.path.exists(SETTINGS_FILE):
        print(f"[!] {SETTINGS_FILE} not found - using the built-in defaults.", flush=True)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                user = json.load(f)
            settings.update({k: v for k, v in user.items() if k in DEFAULT_SETTINGS})
        except Exception as e:
            print(f"[!] Could not read {SETTINGS_FILE} ({e}) - using defaults.", flush=True)
    env_limit = os.environ.get("DAILY_LIMIT")       # optional GitHub variable
    if env_limit and env_limit.strip().isdigit():
        settings["daily_limit"] = int(env_limit)
    return settings


SETTINGS = load_settings()
SENDER_NAME = SETTINGS["sender_name"]
COMPANY_NAME = SETTINGS["company_name"]
COMPANY_TAGLINE = SETTINGS["company_tagline"]
FALLBACK_NAME = SETTINGS["fallback_name"]
TIMEZONE = SETTINGS["timezone"]


def parse_hhmm(value, default_minutes, name):
    """'13:30' -> minutes after midnight. Bad value -> default (with a warning)."""
    try:
        h, m = str(value).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 24 and 0 <= m < 60:
            return h * 60 + m
    except Exception:
        pass
    print(f"[!] settings.json: '{name}' must look like \"13:30\" - using default.", flush=True)
    return default_minutes


START_MIN = parse_hhmm(SETTINGS["start_time"], 13 * 60, "start_time")
END_MIN = parse_hhmm(SETTINGS["end_time"], 20 * 60, "end_time")
SEND_DAYS = {str(d).strip().lower()[:3] for d in (SETTINGS.get("send_days") or [])} \
            or {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def fmt_minutes(total):
    return f"{total // 60:02d}:{total % 60:02d}"


def sending_allowed_now(now):
    """True only on a chosen weekday and between start_time and end_time."""
    minutes = now.hour * 60 + now.minute
    return now.strftime("%a").lower()[:3] in SEND_DAYS and START_MIN <= minutes < END_MIN

DAILY_LIMIT = int(SETTINGS["daily_limit"])
DELAY_MIN = int(SETTINGS["min_delay_seconds"])
DELAY_MAX = int(SETTINGS["max_delay_seconds"])

DAY_OFFSETS = []            # filled in automatically from the number of day files

MAX_RUN_MINUTES = 330       # stop before GitHub's 6-hour job limit
MAX_CONSECUTIVE_FAILURES = 5

DRY_RUN = os.environ.get("DRY_RUN", "") == "1"      # DRY_RUN=1 -> preview only

STATE_FIELDS = ["email", "name", "status", "steps_sent",
                "first_sent_date", "last_sent_date", "note", "data"]
# status: active | done | replied | unsubscribed | bounced | skipped

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NAME_COLUMNS = ("restaurant name", "restaurant", "name", "business name", "business",
                "company name", "company", "venue", "cafe", "title")
EMAIL_COLUMNS = ("email", "e-mail", "email address", "emails", "mail", "email id")
STOP_WORDS = ("no thanks", "no thank you", "unsubscribe", "stop", "remove me",
              "not interested", "don't contact", "do not contact")
AUTO_REPLY_MARKERS = ("automatic reply", "auto-reply", "autoreply",
                      "out of office", "auto reply")


# ============================================================
# HTML EMAIL CARD (same look as your original)
# ============================================================

HTML_CARD = """
<html>
  <body style="margin:0; padding:0; background-color:#f5f4f1;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="background-color:#f5f4f1; padding:24px 12px;">
      <tr><td align="center">

        <table role="presentation" width="600" cellpadding="0" cellspacing="0"
               style="max-width:600px; width:100%; background-color:#ffffff; border-radius:6px;">
          <tr>
            <td bgcolor="#000000" align="left"
                style="background-color:#000000; padding:14px 28px; border-radius:6px 6px 0 0;">
              __LOGO__
            </td>
          </tr>
          <tr>
            <td height="3" style="height:3px; font-size:0; line-height:0;
                background-color:#e8203e; background-image:linear-gradient(90deg,#0a3dbd,#e8203e);">&nbsp;</td>
          </tr>
          <tr>
            <td style="padding:28px 36px 8px 36px;
                       font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;
                       font-size:15px; line-height:1.7; color:#2b2b2b;">
              __BODY__
            </td>
          </tr>
          <tr>
            <td style="padding:12px 36px 28px 36px; font-family:Arial,sans-serif;">
              <div style="border-top:1px solid #e6e2d9; padding-top:10px; font-size:13px; color:#111111;">
                <strong style="font-style:italic;">__COMPANY_LINE__</strong>
              </div>
            </td>
          </tr>
        </table>

        <table role="presentation" width="600" cellpadding="0" cellspacing="0"
               style="max-width:600px; width:100%;">
          <tr>
            <td style="padding:14px 36px; font-family:Arial,sans-serif; font-size:11px;
                       line-height:1.5; color:#999999; text-align:center;">
              Not relevant for __RESTAURANT__?
              Reply &quot;no thanks&quot; and we won't contact you again.
            </td>
          </tr>
        </table>

      </td></tr>
    </table>
  </body>
</html>
"""

LOGO_IMG_TAG = ('<img src="cid:btglogo" width="200" alt="{Alt}" '
                'style="display:block; border:0; outline:none; width:200px; '
                'max-width:100%; height:auto;">')
LOGO_TEXT_FALLBACK = ("<span style=\"font-family:Georgia,'Times New Roman',serif; "
                      'font-size:14px; letter-spacing:4px; color:#ffffff; '
                      'text-transform:uppercase;">{Company}</span>')


# ============================================================
# HELPERS
# ============================================================

def pick_delay(sends_left, elapsed_seconds):
    """Seconds to wait before the next email, based on how much work is left."""
    time_left = max(MAX_RUN_MINUTES * 60 - elapsed_seconds, 1)
    base = time_left / max(sends_left, 1)
    base = min(max(base, DELAY_MIN), DELAY_MAX)
    return min(DELAY_MAX, max(DELAY_MIN, int(random.uniform(0.85, 1.15) * base)))


def log(msg=""):
    print(msg, flush=True)


def get_today():
    override = os.environ.get("TODAY")          # for testing: TODAY=2026-12-01
    if override:
        return date.fromisoformat(override)
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def clean(value):
    text = str(value or "").strip()
    return "" if text.lower() in ("nan", "none") else text


def load_logo():
    try:
        with open(LOGO_FILE, "rb") as f:
            return f.read()
    except FileNotFoundError:
        log(f"[!] Logo '{LOGO_FILE}' not found - using text header.")
        return None


# ---------------- email templates (emails/day1.txt ...) ----------------

def load_templates():
    """emails/day1.txt, day2.txt, ... one file per day, as many days as you want.
    Each file: first line 'Subject: ...', blank line, then the body.
    Placeholders: {Restaurant_Name} {Sender_Name} and any column of your CSV,
    e.g. a column 'City' can be used as {City}."""
    global DAY_OFFSETS
    found = {}
    if os.path.isdir(EMAILS_DIR):
        for fn in os.listdir(EMAILS_DIR):
            m = re.fullmatch(r"day(\d+)\.txt", fn.lower())
            if m:
                found[int(m.group(1))] = os.path.join(EMAILS_DIR, fn)
    problems = []
    if not found:
        problems.append(f"no day files found in '{EMAILS_DIR}/' (need day1.txt, day2.txt, ...)")
    else:
        n = max(found)
        for i in range(1, n + 1):
            if i not in found:
                problems.append(f"missing file for day {i} (day{i}.txt)")
    templates = []
    if not problems:
        for i in range(1, n + 1):
            path = found[i]
            raw = open(path, encoding="utf-8").read().replace("\r\n", "\n").strip()
            if "EDIT ME" in raw:
                problems.append(f"{path} still contains the EDIT ME placeholder")
                continue
            first, _, rest = raw.partition("\n")
            if not first.lower().startswith("subject:"):
                problems.append(f"{path}: first line must start with 'Subject:'")
                continue
            subject = first.split(":", 1)[1].strip()
            body = rest.strip()
            if not subject or not body:
                problems.append(f"{path}: subject or body is empty")
                continue
            templates.append((subject, body))
        offsets = SETTINGS.get("day_offsets") or list(range(n))
        if len(offsets) != n:
            problems.append(f"settings.json: day_offsets has {len(offsets)} numbers "
                            f"but you have {n} email files")
        DAY_OFFSETS = [int(x) for x in offsets]
    if problems:
        log("[!] Email templates are not ready:")
        for p in problems:
            log("    - " + p)
        sys.exit(1)
    return templates


def lead_values(row):
    values = {"Restaurant_Name": row["name"], "Name": row["name"],
              "Sender_Name": SENDER_NAME, "Company_Name": COMPANY_NAME}
    try:
        extra = json.loads(row.get("data") or "{}")
    except Exception:
        extra = {}
    for k, v in extra.items():
        key = re.sub(r"\W+", "_", k.strip()).strip("_")
        if key and key not in values and clean(v):
            values[key] = clean(v)
    return values


def fill(text, values):
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    left = re.findall(r"\{[A-Za-z0-9_]+\}", text)
    if left:
        raise ValueError("missing value for " + ", ".join(sorted(set(left))))
    return text


def check_placeholders(templates, state):
    """Stop early (and skip nobody) if an email uses a {Placeholder} that exists nowhere,
    for example a typo like {Resturant_Name}."""
    known = {"Restaurant_Name", "Name", "Sender_Name", "Company_Name"}
    for row in state.values():
        try:
            extra = json.loads(row.get("data") or "{}")
        except Exception:
            extra = {}
        for k in extra:
            key = re.sub(r"\W+", "_", k.strip()).strip("_")
            if key:
                known.add(key)
    problems = []
    for i, (subject, body) in enumerate(templates, start=1):
        for ph in sorted(set(re.findall(r"\{([A-Za-z0-9_]+)\}", subject + "\n" + body))):
            if ph not in known:
                problems.append(f"day{i}.txt uses {{{ph}}} - not a known placeholder")
    if problems:
        log("[!] Placeholder problem - nothing was sent:")
        for p in problems:
            log("    - " + p)
        log("    Allowed: {Restaurant_Name} {Name} {Sender_Name} {Company_Name} and any column name of your CSV "
            "(spelling and capital letters must match).")
        sys.exit(1)


def build_message(row, template, logo_bytes):
    values = lead_values(row)
    to_addr = row["email"]
    restaurant_name = values["Restaurant_Name"]
    subject_t, body_t = template
    subject = fill(subject_t, values)
    body = fill(body_t, values)
    company_line = f"{COMPANY_NAME} | {COMPANY_TAGLINE}"

    plain = (body + "\n\n" + company_line + "\n\n"
             + f'Not relevant for {restaurant_name}? Reply "no thanks" '
               "and we won't contact you again.\n")

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    body_html = "".join(
        '<p style="margin:0 0 16px 0;">'
        + html.escape(p).replace("\n", "<br>") + "</p>"
        for p in paragraphs
    )
    logo_block = (LOGO_IMG_TAG.format(Alt=html.escape(COMPANY_NAME)) if logo_bytes
                  else LOGO_TEXT_FALLBACK.format(Company=html.escape(COMPANY_NAME)))
    html_body = (HTML_CARD.replace("__LOGO__", logo_block)
                          .replace("__BODY__", body_html)
                          .replace("__COMPANY_LINE__", html.escape(company_line))
                          .replace("__RESTAURANT__", html.escape(restaurant_name)))

    msg = MIMEMultipart("alternative")
    msg["From"] = SENDER_EMAIL
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(SENDER_EMAIL or "x@x").split("@")[-1])
    msg["List-Unsubscribe"] = f"<mailto:{SENDER_EMAIL}?subject=unsubscribe>"
    msg.attach(MIMEText(plain, "plain", "utf-8"))

    related = MIMEMultipart("related")
    related.attach(MIMEText(html_body, "html", "utf-8"))
    if logo_bytes:
        img = MIMEImage(logo_bytes, _subtype="png")
        img.add_header("Content-ID", "<btglogo>")
        img.add_header("Content-Disposition", "inline", filename="btg-logo.png")
        related.attach(img)
    msg.attach(related)
    return msg


# ============================================================
# STATE  (state.csv = the memory of the whole campaign)
# ============================================================

def load_state():
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                state[row["email"].lower()] = row
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=STATE_FIELDS)
        w.writeheader()
        for row in state.values():
            w.writerow({k: row.get(k, "") for k in STATE_FIELDS})
    os.replace(tmp, STATE_FILE)


def _find_column(fieldnames, candidates):
    norm = {re.sub(r"\s+", " ", (f or "").strip().lower()): f for f in fieldnames}
    for c in candidates:
        if c in norm:
            return norm[c]
    return None


def import_leads(state):
    """Read EVERY .csv in leads/ . New addresses are added to state; known ones are untouched."""
    files = []
    if os.path.isdir(LEADS_DIR):
        files = sorted(f for f in os.listdir(LEADS_DIR) if f.lower().endswith(".csv"))
    if not files:
        log(f"[*] No CSV files in '{LEADS_DIR}/' yet. Upload a list there.")
        return 0
    added = bad = dupes = 0
    for fn in files:
        with open(os.path.join(LEADS_DIR, fn), newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or []
            email_col = _find_column(fields, EMAIL_COLUMNS)
            name_col = _find_column(fields, NAME_COLUMNS)
            if not email_col:
                log(f"[!] {fn}: no email column found (columns: {', '.join(fields)}) - skipped.")
                continue
            for row in reader:
                addr = clean(row.get(email_col)).lower()
                if not EMAIL_RE.match(addr):
                    bad += 1
                    continue
                if addr in state:
                    dupes += 1
                    continue
                name = clean(row.get(name_col)) if name_col else ""
                extra = {k: clean(v) for k, v in row.items()
                         if k and k not in (email_col, name_col) and clean(v)}
                state[addr] = {"email": addr, "name": name or FALLBACK_NAME,
                               "status": "active", "steps_sent": "0",
                               "first_sent_date": "", "last_sent_date": "",
                               "note": "", "data": json.dumps(extra, ensure_ascii=False)}
                added += 1
    log(f"[*] Lead files ({len(files)}): {added} new, {dupes} already known/duplicate, {bad} invalid.")
    return added


def is_due(row, today):
    if row["status"] != "active":
        return False
    n = int(row["steps_sent"] or 0)
    if n >= len(DAY_OFFSETS):
        return False
    if n == 0:
        return True
    last = date.fromisoformat(row["last_sent_date"])
    if last >= today:                       # max one email per lead per day
        return False
    first = date.fromisoformat(row["first_sent_date"])
    return today >= first + timedelta(days=DAY_OFFSETS[n])


def build_queue(state, today, slots):
    due = [r for r in state.values() if is_due(r, today)]
    followups = sorted([r for r in due if int(r["steps_sent"]) > 0],
                       key=lambda r: -int(r["steps_sent"]))   # finish sequences first
    fresh = [r for r in due if int(r["steps_sent"]) == 0]      # CSV order
    return (followups + fresh)[:max(slots, 0)], len(followups), len(fresh)


# ============================================================
# INBOX CHECK  (stops sequence for replies + bounces)
# ============================================================

def _decode(value):
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _text_of(msg, limit=4000):
    parts = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "message/delivery-status"):
            payload = part.get_payload(decode=True)
            if payload:
                parts.append(payload.decode(part.get_content_charset() or "utf-8", "ignore"))
            else:
                parts.append(str(part.get_payload()))
    return "\n".join(parts)[:limit]


def check_inbox(state, today):
    active = {e: r for e, r in state.items()
              if r["status"] == "active" and int(r["steps_sent"] or 0) > 0}
    if not active:
        return
    since = min(date.fromisoformat(r["first_sent_date"]) for r in active.values())
    log("[*] Checking inbox for replies and bounces...")
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(SENDER_EMAIL, SENDER_PASSWORD)
        imap.select("INBOX", readonly=True)
        _, data = imap.search(None, "SINCE", since.strftime("%d-%b-%Y"))
        replied = bounced = 0
        for num in data[0].split():
            _, hdr = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            head = email.message_from_bytes(hdr[0][1])
            sender = parseaddr(head.get("From", ""))[1].lower()
            subject = _decode(head.get("Subject", "")).lower()

            if "mailer-daemon" in sender or "postmaster" in sender:
                _, full = imap.fetch(num, "(BODY.PEEK[])")
                text = _text_of(email.message_from_bytes(full[0][1])).lower()
                for addr, row in active.items():
                    if row["status"] == "active" and addr in text:
                        row["status"], row["note"] = "bounced", "bounce detected"
                        bounced += 1
            elif sender in active and active[sender]["status"] == "active":
                if any(m in subject for m in AUTO_REPLY_MARKERS):
                    continue
                _, full = imap.fetch(num, "(BODY.PEEK[])")
                text = (subject + " " + _text_of(email.message_from_bytes(full[0][1]), 600)).lower()
                row = active[sender]
                if any(w in text for w in STOP_WORDS):
                    row["status"], row["note"] = "unsubscribed", "asked to stop"
                else:
                    row["status"], row["note"] = "replied", "REPLIED - follow up manually"
                replied += 1
        imap.logout()
        log(f"[*] Inbox check done: {replied} replies, {bounced} bounces.")
    except Exception as e:
        log(f"[!] Inbox check failed ({e}). Replies were NOT checked this run.")
        log("    Make sure IMAP is enabled in Gmail settings and MY_PASSWORD is an App Password.")


# ============================================================
# SENDING
# ============================================================

def connect_smtp():
    server = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
    server.ehlo()
    server.starttls()
    server.ehlo()
    server.login(SENDER_EMAIL, SENDER_PASSWORD)
    return server


def main():
    forced = (os.environ.get("FORCE_RUN") == "true" or bool(os.environ.get("TODAY")) or DRY_RUN)
    if not forced:
        now = datetime.now(ZoneInfo(TIMEZONE))
        if not sending_allowed_now(now):
            log(f"[*] Outside the sending schedule ({fmt_minutes(START_MIN)}-{fmt_minutes(END_MIN)} "
                f"{TIMEZONE} on {','.join(d for d in DAY_ORDER if d in SEND_DAYS)}; now {now.strftime('%a %H:%M')}). Nothing to do.")
            return

    if not DRY_RUN and (not SENDER_EMAIL or not SENDER_PASSWORD):
        log("[!] Missing GitHub Secrets: MY_EMAIL and MY_PASSWORD")
        sys.exit(1)

    templates = load_templates()
    state = load_state()
    import_leads(state)
    check_placeholders(templates, state)
    today = get_today()
    log(f"[*] Today (Dubai): {today.isoformat()}   DRY_RUN={DRY_RUN}")
    log(f"[*] Settings in use: window {fmt_minutes(START_MIN)}-{fmt_minutes(END_MIN)}, "
        f"days {','.join(d for d in DAY_ORDER if d in SEND_DAYS)}, "
        f"gap {DELAY_MIN}-{DELAY_MAX}s, daily limit {DAILY_LIMIT}, sender '{SENDER_NAME}'")

    if not DRY_RUN:
        check_inbox(state, today)
        save_state(state)

    sent_today = sum(1 for r in state.values() if r["last_sent_date"] == today.isoformat())
    queue, n_follow, n_new = build_queue(state, today, DAILY_LIMIT - sent_today)
    log(f"[*] Already sent today: {sent_today} | limit {DAILY_LIMIT}")
    log(f"[*] Queue now: {len(queue)}  ({n_follow} follow-ups due, {n_new} new leads waiting)")

    if not queue:
        log("[*] Nothing to send. Done.")
        summary(state)
        return

    if DRY_RUN:
        log("[*] PREVIEW ONLY - nothing is sent and nothing is saved.")
        for n, r in enumerate(queue):
            step = int(r["steps_sent"])
            try:
                vals = lead_values(r)
                subject = fill(templates[step][0], vals)
                body = fill(templates[step][1], vals)
                log(f"  [dry] Day {step + 1} -> {r['name']} <{r['email']}>")
                if n < 3:                       # show the finished text for the first 3
                    log(f"        Subject: {subject}")
                    for line in body.splitlines()[:6]:
                        log(f"        | {line}")
            except ValueError as e:
                log(f"  [dry] Day {step + 1} -> {r['name']} <{r['email']}>  WOULD BE SKIPPED: {e}")
        summary(state)
        return

    logo_bytes = load_logo()
    started = time.time()
    server = None
    sent = failures = 0

    for i, row in enumerate(queue):
        if not forced and not sending_allowed_now(datetime.now(ZoneInfo(TIMEZONE))):
            log(f"[*] Reached end_time ({fmt_minutes(END_MIN)}) - stopping. Remaining leads continue next send day.")
            break
        if (time.time() - started) / 60 > MAX_RUN_MINUTES:
            log("[*] Time limit reached - stopping. Remaining leads continue next run.")
            break
        if failures >= MAX_CONSECUTIVE_FAILURES:
            log("[!] Too many failures in a row - stopping this run.")
            break

        step = int(row["steps_sent"])
        label = f"Day {step + 1} -> {row['name']} <{row['email']}>"
        try:
            msg = build_message(row, templates[step], logo_bytes)
        except ValueError as e:
            row["status"], row["note"] = "skipped", str(e)
            save_state(state)
            log(f"  [!] SKIPPED {label}: {e}")
            continue

        try:
            for attempt in (1, 2):
                try:
                    if server is None:
                        server = connect_smtp()
                    server.sendmail(SENDER_EMAIL, row["email"], msg.as_string())
                    break
                except smtplib.SMTPServerDisconnected:
                    server = None
                    if attempt == 2:
                        raise
            sent += 1
            failures = 0
            row["steps_sent"] = str(step + 1)
            row["last_sent_date"] = today.isoformat()
            if step == 0:
                row["first_sent_date"] = today.isoformat()
            if step + 1 >= len(DAY_OFFSETS):
                row["status"] = "done"
            save_state(state)
            log(f"  [{sent}] SENT  {label}")

        except smtplib.SMTPAuthenticationError as e:
            log(f"[!] Gmail login failed - check the App Password: {e}")
            break
        except smtplib.SMTPRecipientsRefused:
            row["status"], row["note"] = "bounced", "address refused by server"
            save_state(state)
            failures = 0
            log(f"  [!] REFUSED {label} (marked bounced)")
        except Exception as e:
            failures += 1
            server = None
            log(f"  [!] FAILED {label}: {e}")
            if "limit" in str(e).lower():
                log("[!] Gmail sending limit hit - stopping for today.")
                break

        if i < len(queue) - 1:
            wait = pick_delay(len(queue) - i - 1, time.time() - started)
            log(f"      waiting {wait}s...")
            time.sleep(wait)

    if server is not None:
        try:
            server.quit()
        except Exception:
            pass

    save_state(state)
    log(f"\n[*] Run finished. Sent {sent} emails this run.")
    summary(state)


def summary(state):
    counts = {}
    for r in state.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    log("=" * 50)
    log("CAMPAIGN STATUS: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    steps = {}
    for r in state.values():
        if r["status"] == "active":
            steps[r["steps_sent"]] = steps.get(r["steps_sent"], 0) + 1
    if steps:
        log("Active leads by emails received: " +
            ", ".join(f"{k}:{v}" for k, v in sorted(steps.items())))
    log("=" * 50)


if __name__ == "__main__":
    main()
