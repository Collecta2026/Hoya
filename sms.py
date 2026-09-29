"""
SMS notifications via Twilio, with a log-only fallback when Twilio isn't
configured (so the app works fully offline / in dev without credentials).
Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER to enable.
"""
import os

TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER")

_client = None
if TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM:
    try:
        from twilio.rest import Client
        _client = Client(TWILIO_SID, TWILIO_TOKEN)
    except Exception as e:  # pragma: no cover
        print(f"[sms] Twilio client init failed, falling back to log-only: {e}")
        _client = None


def send_sms(to_number, message):
    if not to_number:
        return
    if _client:
        try:
            _client.messages.create(to=to_number, from_=TWILIO_FROM, body=message)
            return
        except Exception as e:  # pragma: no cover
            print(f"[sms] send failed, logging instead: {e}")
    print(f"[sms:log-only] to={to_number} message={message!r}")
