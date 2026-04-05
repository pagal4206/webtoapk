# APK Cloud Launchpad for Heroku

Ye package ab standalone Flask + MongoDB Heroku app hai jisme:

- user registration page
- login page
- protected build dashboard
- MongoDB-based users and sessions
- remote builder proxy
- optional Codespace auto-wake and auto-stop

<p align="center"><a href="https://dashboard.heroku.com/new?template=https://github.com/pagal4206/webtoapk"> <img src="https://img.shields.io/badge/Deploy%20On%20Heroku-bringle?style=for-the-badge&logo=heroku" width="220" height="38.45"/></a></p>

## Heroku deploy button

Deploy button use karne ke liye is folder ko GitHub repo ke root me rakho. Agar aap manual template URL use karna chahte ho:

```text
https://www.heroku.com/deploy?template=https://github.com/<owner>/<repo>
```

## `app.json` me kya prompt hoga

Required:

```text
REMOTE_BUILDER_BASE_URL=https://your-builder-url
MONGODB_URL=mongodb+srv://username:password@cluster.mongodb.net/apk_cloud_launchpad
```

Optional:

```text
GITHUB_ACCESS_TOKEN=ghp_xxx
GITHUB_CODESPACE_NAME=your-codespace-name
```

## Hidden advanced envs

Ye `app.json` me prompt nahi hote, lekin manually set kiye ja sakte hain:

```text
REMOTE_BUILDER_TOKEN=match-builder-shared-secret
REMOTE_BUILDER_HEALTH_PATH=/health
BUILDER_REQUEST_TIMEOUT_SECONDS=900
CODESPACE_START_TIMEOUT_SECONDS=180
CODESPACE_WAKE_COOLDOWN_SECONDS=15
CODESPACE_IDLE_SHUTDOWN_SECONDS=90
CODESPACE_AUTO_STOP_ENABLED=true
SESSION_TTL_DAYS=30
API_RATE_LIMIT_MAX_REQUESTS=60
API_RATE_LIMIT_WINDOW_SECONDS=60
```

## Local run

Linux:

```bash
bash start-web.sh https://your-builder-url mongodb+srv://username:password@cluster.mongodb.net/apk_cloud_launchpad
```

Windows:

```powershell
.\start-web.bat https://your-builder-url mongodb+srv://username:password@cluster.mongodb.net/apk_cloud_launchpad
```

Ya `.env` file me `REMOTE_BUILDER_BASE_URL` aur `MONGODB_URL` dono set kar do.

Manual run on Linux:

```bash
export REMOTE_BUILDER_BASE_URL="https://your-builder-url"
export MONGODB_URL="mongodb+srv://username:password@cluster.mongodb.net/apk_cloud_launchpad"
python3 -m pip install -r requirements.txt
export PORT=8090
python3 -m portal_app
```

Manual run on Windows:

```powershell
$env:REMOTE_BUILDER_BASE_URL='https://your-builder-url'
$env:MONGODB_URL='mongodb+srv://username:password@cluster.mongodb.net/apk_cloud_launchpad'
python -m pip install -r requirements.txt
$env:PORT='8090'
python -m portal_app
```

## UI and auth flow

- `/register` par naya user create hota hai
- `/login` par existing user sign in karta hai
- `/` sirf authenticated users ke liye protected dashboard hai
- user sessions MongoDB-backed token system se manage hoti hain

## Codespace behavior

- Agar `GITHUB_ACCESS_TOKEN` aur `GITHUB_CODESPACE_NAME` set hain, to builder sleep hone par auto-wake ho jayega
- Jab active jobs finish ho jaati hain, app idle delay ke baad Codespace stop request bhej deta hai
- Next authenticated build request par Codespace fir se wake ho sakta hai

## Runtime notes

- `Procfile` gunicorn ko `portal_app:app` se run karta hai
- `requirements.txt` me `pymongo` aur `dnspython` added hain taki MongoDB Atlas/SRV URL chale
- `PORT` Heroku khud set karta hai
- Frontend browser me render hota hai, isliye source ko 100% hide nahi kiya ja sakta; real protection auth, backend logic, aur secure headers se aati hai
