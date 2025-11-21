
# ABIC Manpower Payroll - Full System (Render ready)

This repo contains a full-featured payroll starter app for ABIC Manpower with:
- Admin dashboard (add/remove employees, timecards, generate payroll)
- Employee login (auto-created credentials shown to admin)
- Payroll engine for Mali Lending Corp rules
- Modern responsive layout (Tailwind CDN)
- SQLite database for persistence
- Ready to deploy on Render.com (render.yaml included)

## How to deploy on Render
1. Push this repo to GitHub.
2. Create a new Web Service on Render, connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`

Admin seeded credentials will be created after first run. Change SECRET_KEY env var in Render for security.
