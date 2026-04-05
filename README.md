# APK Cloud Launchpad for Heroku

Ye folder ab proper standalone Heroku app package hai. Isko alag GitHub repo ke root me rakh do, phir `app.json` + Deploy Button ke through one-click deploy flow use ho jayega.

<p align="center"><a href="https://dashboard.heroku.com/new?template=https://github.com/pagal4206/webtoapk"> <img src="https://img.shields.io/badge/Deploy%20On%20Heroku-bringle?style=for-the-badge&logo=heroku" width="220" height="38.45"/></a></p>

## Deploy Button use karne se pehle

- Ye folder GitHub repo ke root me hona chahiye.
- Repo me valid `app.json` root par hona chahiye.
- Repo me Git submodules nahi hone chahiye.
- Heroku Button Cedar-generation app create karta hai; Fir ke liye nahi.

Agar aap button ko GitHub README ke bahar use karna chahte ho, to explicit template URL use karo:

## Is package me kya ready hai

- `app.py` Flask web app aur API proxy
- `requirements.txt` Python dependencies
- `.python-version` Heroku Python runtime selector
- `Procfile` dyno start command
- `app.json` Heroku manifest for one-click deploy
- `.env.example` sample config vars
- `src/main/resources/public/` static frontend assets

## Heroku one-click deploy

Deploy ke time Heroku `app.json` ke basis par config vars prompt karega.

Required:

```text
REMOTE_BUILDER_BASE_URL=https://your-codespace-builder-url
```

Optional:

```text
REMOTE_BUILDER_TOKEN=match-builder-shared-secret
GITHUB_ACCESS_TOKEN=ghp_xxx
GITHUB_CODESPACE_NAME=your-codespace-name
GITHUB_API_BASE_URL=https://api.github.com
GITHUB_API_VERSION=2022-11-28
REMOTE_BUILDER_HEALTH_PATH=/health
BUILDER_REQUEST_TIMEOUT_SECONDS=900
CODESPACE_START_TIMEOUT_SECONDS=180
CODESPACE_WAKE_COOLDOWN_SECONDS=15
API_RATE_LIMIT_MAX_REQUESTS=60
API_RATE_LIMIT_WINDOW_SECONDS=60
```

## Local run

Quick start on Linux:

```bash
bash start-web.sh https://your-codespace-builder-url
```

Quick start on Windows:

```powershell
.\start-web.bat https://your-codespace-builder-url
```

Manual run on Linux:

```bash
export REMOTE_BUILDER_BASE_URL="https://your-codespace-builder-url"
python3 -m pip install -r requirements.txt
export PORT=8090
python3 app.py
```

Manual run on Windows:

```powershell
$env:REMOTE_BUILDER_BASE_URL='https://your-codespace-builder-url'
python -m pip install -r requirements.txt
$env:PORT='8090'
python app.py
```

`.env` file rakhoge to app local run ke time usko auto-load kar lega.

## Heroku notes

- `Procfile` gunicorn ko Heroku `$PORT` par bind karta hai aur request logs stdout/stderr me bhejta hai.
- `.python-version` currently `3.14` use karta hai, jo Heroku ke current recommended major runtime flow ke saath align hai.
- `requirements.txt` aur `.python-version` root par hone ki wajah se Heroku Python buildpack app ko detect kar leta hai.
- `PORT` Heroku khud set karta hai, isliye usko Heroku config var ke roop me manually set karne ki zarurat nahi hoti.
- `REMOTE_BUILDER_TOKEN` ko builder service ke `BUILDER_SHARED_SECRET` ke saath same rakho agar builder API private rakhni hai.
- Public-facing API defaults ke saath rate limited hai, isliye burst abuse par `429` response milega.
