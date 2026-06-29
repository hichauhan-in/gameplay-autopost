# LM Studio Config — AMD 7800X3D + RX 7900 XT + 32 GB RAM

Settings for running the vision backend in **LM Studio** for this pipeline.
Used when `VISION_BACKEND=lmstudio` in `docker-compose.yml`.

---

## Model

| Setting       | Value                                    |
|---------------|------------------------------------------|
| Model         | `unsloth/Qwen2.5-VL-7B-Instruct-GGUF`    |
| Quantization  | `Q4_K_S`                                 |
| Model ID      | `qwen2.5-vl-7b-instruct`                 |

Model ID must match `VISION_MODEL=qwen2.5-vl-7b-instruct` in compose.

---

## Context & GPU Offload

| Setting               | Value             | Notes                                              |
|-----------------------|-------------------|----------------------------------------------------|
| Context Length        | `8192`            | Prompt + 5 images fits easily; faster, less VRAM.  |
| GPU Offload           | Maximum (28/28)   | Full offload; whole model on the 7900 XT.          |
| Unified KV Cache      | ON                |                                                    |
| Offload KV Cache→GPU  | ON                |                                                    |
| Keep Model in Memory  | ON                | Avoids reload between requests.                     |
| Flash Attention       | ON (if supported) | Turn off if unstable on the ROCm build.            |

---

## Inference

| Setting           | Value           | Notes                                      |
|-------------------|-----------------|--------------------------------------------|
| Temperature       | `0.1`           | Classification, not creative.              |
| Top-K             | `40`            |                                            |
| Top-P             | `0.9`           | Optional; low temp already dominates.      |
| Context Overflow  | Truncate Middle |                                            |
| CPU Threads       | `6`             | Leaves cores free; GPU does the work.      |
| Max Response Tok. | `128`–`256`     | Output is tiny JSON.                       |
| System Prompt     | Empty           | App sends the full prompt every request.   |

> The app already sets `temperature: 0.1` and `max_tokens: 200` per request,
> so those override the UI. Set Top-K/overflow/threads in the UI.

---

## Suggested tweaks

- **Seed = fixed (e.g. 0)** if your build allows it — makes scoring fully
  reproducible run-to-run.
- **Max tokens 256** over 128 — leaves headroom for longer `reason` strings;
  cost is negligible.
- **Top-P 0.9** as a safety net alongside `temp 0.1` for stable JSON.

---

## API

```
http://host.docker.internal:1234/v1/chat/completions
```

`docker-compose.yml` →
```
- VISION_BACKEND=lmstudio
- VISION_MODEL=qwen2.5-vl-7b-instruct
- LMSTUDIO_URL=http://host.docker.internal:1234/v1/chat/completions
```

---

## Quick checklist

```
Model:              Qwen2.5-VL-7B-Instruct (Q4_K_S)
Context:            8192
GPU Offload:        Maximum
Unified KV Cache:   ON
Offload KV Cache:   ON
Keep Model Loaded:  ON
Flash Attention:    ON (if supported)
Temperature:        0.1
Top-K:              40
Max Response Tok.:  128-256
Context Overflow:   Truncate Middle
CPU Threads:        6
System Prompt:      Empty
```

---

## Notes

- Multi-image (5 frames/request) works; JSON valid; gameplay + confidence correct.
- ~40s slower end-to-end than Ollama; both kept and switchable.
