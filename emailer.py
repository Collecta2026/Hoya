"""
Email sender for Heliolink Ops.

Uses SMTP when configured via environment variables, otherwise logs the email
to the console (log-only fallback) so the app works fully without credentials
- the same pattern the original app used for SMS.

Env vars:
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS,
  MAIL_FROM (default noreply@heliolink.co.uk),
  SALES_EMAIL (default sales@heliolink.co.uk)
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

SALES_EMAIL = os.environ.get("SALES_EMAIL", "sales@heliolink.co.uk")
MAIL_FROM = os.environ.get("MAIL_FROM", "noreply@heliolink.co.uk")


def send_email(to, subject, html_body, attachments=None):
    """attachments = list of (filename, bytes, mimetype_subtype). Returns True if
    actually sent, False if logged only."""
    host = os.environ.get("SMTP_HOST")
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html"))
    for fn, data, subtype in (attachments or []):
        part = MIMEApplication(data, _subtype=subtype)
        part.add_header("Content-Disposition", "attachment", filename=fn)
        msg.attach(part)

    if not host:
        print("\n[email] (log-only - SMTP not configured)")
        print("[email] To:", to, "| Subject:", subject)
        for fn, data, subtype in (attachments or []):
            print("[email] attachment:", fn, "(%d bytes)" % len(data))
        return False

    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
            if user:
                s.login(user, pw)
            s.send_message(msg)
        print("[email] sent to", to, ":", subject)
        return True
    except Exception as exc:          # pragma: no cover - defensive
        print("[email] send failed (%s) - logged only" % exc)
        return False
