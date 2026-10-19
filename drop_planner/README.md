# Drop Planner (in Hoya)

Multi-drop route planner, moved from the Claude artifact into the Hoya repo.

## Files

```
drop_planner/index.html   the planner page
drop_planner/pc/*.json    postcode lookup data (ONS Postcode Directory, Feb 2026, ~29 MB)
```

Lives in `drop_planner/` (not `static/`) so it sits behind the Hoya login.

## Opening it

Log in to Hoya as admin or dispatcher, then **Operations → Drop Planner**, or go to `/drop-planner/`.
Routes are in `app.py` (search for "Drop Planner").

## Differences from the Claude version

- **Fleet, fuel prices and allowances save per browser** (localStorage), not shared between users.
  In Claude they were shared. Each person/device will need to enter vehicles and fuel prices once.
  Sharing them would mean saving to Hoya's Postgres database via a small API — a later step.
- **"Download CSV" button is hidden**; "Copy CSV" still works.
- Everything else (postcode lookup, route optimisation, costing, Google Maps links, QR codes) works the same.

Postcode data: Contains OS data © Crown copyright and database right 2026. Contains Royal Mail data © Royal Mail
copyright and database right 2026. Source: Office for National Statistics, licensed under the Open Government Licence v3.0.
