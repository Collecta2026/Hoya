"""Customer SMS notifications.

Uses Twilio if credentials are set in the environment; otherwise it logs the
message to the database and console so the whole system runs end-to-end with
no account. To go live, set TWILIO_SID, TWILIO_TOKEN and TWILIO_FROM on Render.
"""
import os
import requests
from models import db, SmsLog


def _creds():
    return (
        os.environ.get("TWILIO_SID"),
        os.environ.get("TWILIO_TOKEN"),
        os.environ.get("TWILIO_FROM"),
    )


def send_sms(to, body, order_ref=None):
    sid, token, sender = _creds()
    status = "logged"
    if sid and token and sender and to:
        try:
            r = requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                data={"To": to, "From": sender, "Body": body},
                auth=(sid, token), timeout=15,
            )
            status = "sent" if r.status_code < 300 else "failed"
        except Exception:
            status = "failed"
    else:
        print(f"[SMS log-only] to={to} :: {body}")

    log = SmsLog(to=to, body=body, status=status, order_ref=order_ref)
    db.session.add(log)
    db.session.commit()
    return status
