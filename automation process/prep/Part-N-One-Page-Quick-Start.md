# Part N — One-Page Quick-Start (Parts B→I condensed)

> **What this is:** the whole setup on one page — copy-paste commands in order. Use it as a checklist
> once you've read the full Parts. Deeper detail lives in [the master index](../Gameplay-to-Instagram%20Automation%20-%20Study%20Guide.md).

---

## 1) Install + folders (Part B)

```powershell
# Install Docker Desktop first (WSL2), then:
$root = "C:\gameplay-autopost"
$dirs = @("n8n-data","postgres-data","helper","config",
          "media\inbox","media\work","media\output","media\archive")
foreach ($d in $dirs) { New-Item -ItemType Directory -Force -Path "$root\$d" | Out-Null }
cd $root
# Generate an encryption key (copy the output):
-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 48 | ForEach-Object {[char]$_})
```

Create `.env` and `docker-compose.yml` (full contents in **Part B**), then:

```powershell
docker compose up -d
# open http://localhost:5678 → create owner login
```

## 2) Local AI reachable (Part C)

```powershell
setx OLLAMA_HOST "0.0.0.0"      # then QUIT + relaunch Ollama
ollama pull llama3.1:8b
ollama pull llava:7b
# ComfyUI: add  --listen 0.0.0.0  to its launch .bat, restart it
```

Create the three helper files (`requirements.txt`, `Dockerfile`, `app.py`) + add `helper` to compose
(**Part C / C6**), then:

```powershell
docker compose up -d --build helper
curl http://localhost:8000/health        # -> {"status":"ok"}
```

## 3) Add all helper endpoints (Parts D–I)

Paste these into `helper/app.py` (each Part has the code), then rebuild once:

| Endpoint | From |
|---|---|
| `/claim` | Part D |
| `/candidates` (+ `OLLAMA_URL`, `VISION_MODEL` env) | Part E |
| `StaticFiles /files` mount | Part F |
| `/render` (+ optional `/comfy_cover`, `COMFY_URL`) | Part G |
| `/config` | Part H |
| `/post_reel` (+ `IG_USERNAME/PASSWORD`) and `/archive` | Part I |

```powershell
docker compose up -d --build helper
```

## 4) Config files (Part H)

Create `config\style.json` and `config\hashtags.json` (templates in **Part H**). Edit `game`, `tone`,
`always_mention`, and your hashtag pools.

## 5) The workflow (Part L — fastest)

- Import [prep/workflow/gameplay-autoposter.workflow.json](workflow/gameplay-autoposter.workflow.json)
  into n8n (**Import from File**).
- Fill the **Config** node: `igUserId`, `publicBase`, `postMethod`, `game`.
- *(or build it node-by-node via Parts D–I.)*

## 6) Instagram (Part I)

**Path A (recommended):**
```powershell
# expose the output folder publicly while posting:
cloudflared tunnel --url http://localhost:8000
# copy the https URL -> Config.publicBase
```
- Link Creator account → Facebook Page; create Meta app; get **IG User ID** + **long-lived token**.
- n8n → Credentials → **Query Auth** named `access_token` = your token → attach to both IG nodes.

**Path B (no Page, burner only):** set `IG_USERNAME/IG_PASSWORD` in `.env`, `postMethod=instagrapi`.

## 7) Go

```powershell
# drop a clip:
#   copy your.mp4 -> C:\gameplay-autopost\media\inbox\
# in n8n: Test workflow  (or toggle Active for the 1-min schedule)
```

---

## Daily commands

| Task | Command (in `C:\gameplay-autopost`) |
|---|---|
| Start everything | `docker compose up -d` |
| Logs | `docker compose logs -f n8n` / `... helper` |
| Rebuild helper after code edits | `docker compose up -d --build helper` |
| Stop | `docker compose down` |
| Tunnel (Path A) | `cloudflared tunnel --url http://localhost:8000` |

## The 3 addresses (memorize)

| n8n calls… | URL |
|---|---|
| Ollama / ComfyUI (native) | `http://host.docker.internal:PORT` |
| Helper (Docker) | `http://helper:8000` |
| In a browser (you) | `http://localhost:PORT` |

---

## 60-second sanity checklist

- [ ] `docker compose ps` → n8n, postgres, helper all up.
- [ ] `ollama ps` shows **GPU**; ComfyUI started with `--listen 0.0.0.0`.
- [ ] `curl http://localhost:8000/health` → ok.
- [ ] Config filled; IG token credential attached (Path A) **or** `.env` creds set (Path B).
- [ ] Tunnel running + URL in `publicBase` (Path A).
- [ ] Clip in `inbox` → run → posted → moved to `archive`.

> Stuck? Jump to the master troubleshooting table in [Part K](Part-K-Run-Troubleshoot-Level-Up.md).
