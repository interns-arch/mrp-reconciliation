# Deploying to Render

This app was built to run on your own PC with no network access at all. Hosting
it on Render is a real trade-off, not just a different start command — read
"Before you deploy" first, then follow the steps.

---

## Before you deploy

**Your data leaves your machine.** Locally, the inventory and supplier price
lists never go anywhere. On Render they are uploaded to a server in a data
centre, parsed there, and held in that server's memory. The "100% offline" badge
turns into an amber "hosted" badge so the page stops claiming otherwise.

**A password becomes mandatory.** There is no login locally because only you can
reach `127.0.0.1`. A Render URL is public, so the app now refuses to start in
hosted mode unless `MRP_PASSWORD` is set, and every page and API call is behind
HTTP Basic auth.

**"Point at a file on disk" is switched off.** That option reads the machine the
app runs on. On your PC that is the point; on a server it would let any visitor
read files off the host, so it is disabled and hidden. Everything must be
uploaded — including the 51 MB inventory, over your connection, every session.

**Uploads do not survive a restart.** Files and results live in this process's
memory and in a temp folder. Render restarts services on deploy, on plan
changes, and (on the free plan) after inactivity. Anything uploaded is gone;
the page notices and asks you to re-select.

**One worker, always.** Because state is in-process, a second worker would
answer requests that know nothing about the files you just uploaded. Scale the
instance size, never `--workers`.

**Is it worth it?** For one person on one PC, local is faster, private and free.
Deploy when other people need to run the check themselves without installing
Python.

---

## Which plan

Measured with the real files — a 51.5 MB / 258,368-row inventory and a 373-row
Veedol price list:

| | |
|---|---|
| Baseline after imports | 72 MB |
| After parsing the inventory | 157 MB |
| **Peak during a full run** | **188 MB** |

So 512 MB is enough for this workload with room to spare. A much larger job
(750k inventory rows producing 250k issue rows) peaked at ~690 MB, so if your
inventory grows several times over, move up to a 2 GB instance.

- **Free** — works, but sleeps after ~15 minutes of inactivity and cold-starts
  slowly, and every sleep wipes your uploads. Fine for trying it out.
- **Starter** — recommended. No sleeping, same 512 MB.
- **Standard (2 GB)** — only if your inventory grows a lot.

---

## Steps

### 1. Put the code in a Git repository

Render deploys from GitHub/GitLab/Bitbucket. From `C:\Project\MRP_Project`:

```bash
git init && git add -A && git commit -m "MRP reconciliation app"
```

Create an empty repo on GitHub, then:

```bash
git remote add origin https://github.com/<you>/<repo>.git && git branch -M main && git push -u origin main
```

`.gitignore` already keeps out `__pycache__` and the large generated sample
files. **Do not commit real inventory or price lists** — they are business data
and the repo is the wrong place for them.

### 2. Create the service

**Option A — Blueprint (uses `render.yaml`, fewer clicks):**
Render dashboard → **New** → **Blueprint** → pick the repo → **Apply**.
Everything below is already set, including a generated password.

**Option B — manual:**
Render dashboard → **New** → **Web Service** → pick the repo, then:

| Setting | Value |
|---|---|
| Language | Python 3 |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn app:app --workers 1 --threads 4 --timeout 600 --bind 0.0.0.0:$PORT` |
| Health check path | `/api/mode` |
| Instance type | Starter |

### 3. Set the environment variables

| Key | Value | Why |
|---|---|---|
| `MRP_HOSTED` | `1` | Turns on hosted mode: auth required, path reading off |
| `MRP_USER` | `admin` | The login name |
| `MRP_PASSWORD` | *a long random password* | Required — the app will not start without it |
| `PYTHON_VERSION` | `3.12.10` | Matches what this was built and tested on |

With the Blueprint, `MRP_PASSWORD` is generated for you: open the service →
**Environment** to read it.

### 4. Open it

Render gives you `https://<name>.onrender.com`. The browser asks for the
username and password. Everything else works exactly as it does locally, minus
the disk-path option.

---

## Checking it worked

```bash
curl -u admin:YOUR_PASSWORD https://<name>.onrender.com/api/mode
```

Expected:

```json
{"ok": true, "hosted": true, "allow_local_paths": false, "auth": true}
```

Without `-u` you should get `401`. If you get `200`, the password is not set and
you should fix that immediately.

---

## If it fails to start

| In the logs | Meaning |
|---|---|
| `refuses to start: MRP_HOSTED is set but MRP_PASSWORD is empty` | Set `MRP_PASSWORD` and redeploy. Working as intended |
| `Worker was sent SIGKILL` / out of memory | The instance is too small for your file. Move up a plan |
| `Worker timeout` | Raise `--timeout` in the start command |
| Files "no longer registered" after a while | The service restarted or slept. Re-select the files |

---

## Running it locally afterwards

Nothing changes. Without `MRP_HOSTED`, there is no password, the disk-path
option is back, and the app is fully offline again:

```bash
cd C:\Project\MRP_Project && python app.py
```

`gunicorn` is marked `sys_platform != "win32"` in `requirements.txt`, so a local
Windows install skips it.
