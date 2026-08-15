"""Scheduled backup — writes a timestamped JSON snapshot of all data and,
if S3/R2 credentials are present, uploads it offsite. Run on a schedule via
the Render cron job defined in render.yaml.

Environment (all optional except DATABASE_URL for Postgres):
  BACKUP_BUCKET, BACKUP_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
Without bucket creds it writes locally to ./backups (fine for testing; note
Render's disk is ephemeral, so use a bucket for durable offsite copies).
"""
import os, io, json
from datetime import datetime, date
from app import app
from models import (db, Customer, Driver, Vehicle, Order, Item, Route,
                    Stop, Scan, SmsLog)

TABLES = [("customers", Customer), ("drivers", Driver), ("vehicles", Vehicle),
          ("orders", Order), ("items", Item), ("routes", Route),
          ("stops", Stop), ("scans", Scan), ("sms_log", SmsLog)]


def snapshot():
    data = {"generated": datetime.utcnow().isoformat()}
    for name, model in TABLES:
        rows = []
        for r in model.query.all():
            d = {c.name: getattr(r, c.name) for c in r.__table__.columns
                 if c.name not in ("photo", "signature")}
            for k, v in d.items():
                if isinstance(v, (datetime, date)):
                    d[k] = v.isoformat()
            rows.append(d)
        data[name] = rows
    return data


def main():
    with app.app_context():
        payload = json.dumps(snapshot(), indent=2, default=str)
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    key = f"helioops_backup_{stamp}.json"

    bucket = os.environ.get("BACKUP_BUCKET")
    if bucket:
        try:
            import boto3
            s3 = boto3.client("s3", endpoint_url=os.environ.get("BACKUP_ENDPOINT"))
            s3.upload_fileobj(io.BytesIO(payload.encode()), bucket, key)
            print(f"Uploaded {key} to {bucket}")
            return
        except Exception as e:
            print("Offsite upload failed, writing locally:", e)

    os.makedirs("backups", exist_ok=True)
    with open(os.path.join("backups", key), "w") as f:
        f.write(payload)
    print(f"Wrote backups/{key}")


if __name__ == "__main__":
    main()
