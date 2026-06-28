# Part L — Ready-to-Import n8n Workflow (skip the manual building)

> **What this is:** a pre-built workflow JSON you can **import** straight into n8n instead of adding
> 20 nodes by hand. File: [workflow/gameplay-autoposter.workflow.json](workflow/gameplay-autoposter.workflow.json)

> ⚠️ **Prerequisites:** this assumes your **helper endpoints exist** (Parts C–I added `/claim`,
> `/probe`, `/candidates`, `/render`, `/config`, `/post_reel`, `/archive`) and Ollama is reachable.
> Import won't create those — it only builds the n8n side.

---

## L1. What's in it

The **auto-mode** happy path (no manual review form), with **both** posting methods behind a Switch:

```
Schedule → Claim → Got a clip? → Probe → Job Info → Config → Find Best Moments →
Pick Best → Chosen Clip → Render → Reel → Get Config → Write Caption → Assemble Post →
Post Text → Post Method ┬─(graph)→ IG Create Container → Wait 30s → IG Publish ─┐
                        └─(instagrapi)→ IG Post via Helper ─────────────────────┴→ Archive
```

> The **manual review** step (Part F) and ComfyUI polish (Part G) are intentionally left out to keep
> the import clean — add them later by following those Parts.

---

## L2. Import it (2 ways)

**From the file:**
1. n8n → **Overview / Workflows** → top-right **⋯** (or the "+") → **Import from File…**
2. Choose `C:\gameplay-autopost\prep` … actually the file lives in this guide at
   `prep/workflow/gameplay-autoposter.workflow.json` — point the picker at it.
3. It opens as a new, **inactive** workflow named **Gameplay Auto-Poster**.

**By paste:** open the `.json`, copy all, then in a blank n8n canvas press **Ctrl+V** — the nodes
appear instantly.

---

## L3. Fill in the blanks (required before it runs)

| Where | Set this |
|---|---|
| **Config** node → `igUserId` | your Instagram User ID (Part I A1) |
| **Config** node → `publicBase` | your current Cloudflare tunnel URL (Part I A3) |
| **Config** node → `postMethod` | `graph` (recommended) or `instagrapi` |
| **Config** node → `game` | the game name for captions |
| **IG Create Container** + **IG Publish** | **Authentication → Generic → Query Auth** → select your `IG Graph Token` credential (Part I A4) |
| Models | ensure `llama3.1:8b` (caption) + `llava:7b` (helper vision) are pulled |

> If you use **instagrapi** instead, set `postMethod=instagrapi`, set `IG_USERNAME/IG_PASSWORD` in
> `.env` (Part I B1), and you can ignore the two Graph nodes + the tunnel.

---

## L4. Run it

1. Make sure containers are up (`docker compose up -d`) and Ollama/ComfyUI are running.
2. (Path A only) start the tunnel and paste its URL into **Config → publicBase**.
3. Drop a clip in `media\inbox\`, then click **Test workflow** (or toggle **Active** for the
   1-minute schedule).
4. Watch it run node-by-node. Fix any red node by checking its address (`helper:8000` vs
   `host.docker.internal`) — that's 90% of issues.

---

## L5. If a node imports with a warning

n8n auto-migrates node versions on import, so a node may ask you to re-open it. If so:
- **HTTP nodes:** confirm **Method/URL** and that **Send Body = JSON** (or **Send Query** for IG).
- **Set nodes:** confirm the field assignments are present.
- **Switch:** confirm the two rules (`postMethod == graph` / `== instagrapi`).

Everything matches the step-by-step in Parts D–I, so you can cross-check any node there.

---

## ✅ Checkpoint

- [ ] Workflow imported as **Gameplay Auto-Poster**.
- [ ] Config filled (igUserId, publicBase, postMethod, game).
- [ ] IG nodes have the Query Auth credential attached (Path A).
- [ ] A test clip flows end-to-end.

## 🧠 Memory Hooks

- **Import ≠ helper.** You still need the helper endpoints from Parts C–I.
- **Config node = the only place you edit per-run values.**
- **Red node? Check the URL host first.**

## ➡️ Related

Add the human-in-the-loop form from [Part F](Part-F-Manual-Override.md), or the polish recipes in
[Part M](Part-M-Helper-Addons-Subtitles-Letterbox.md).
