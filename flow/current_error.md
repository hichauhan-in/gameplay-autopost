n8n error

import os, json, subprocess
import shutil, glob, time
import re, base64, requests
import math
import numpy as np

from motion import motion_score
from fastapi import FastAPI
from pydantic import BaseModel
from ultralytics import YOLO
import easyocr

app = FastAPI()
MEDIA = "/data/media"

@app.get("/health")
def health():
    return {"status": "ok"}

class ProbeIn(BaseModel):
    path: str  # relative to media, e.g. "inbox/clip.mp4"

@app.post("/probe")
def probe(inp: ProbeIn):
    full = os.path.join(MEDIA, inp.path)
    if not os.path.exists(full):
        return {"error": "file not found", "path": full}
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", full]
    out = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(out.stdout or "{}")
    fmt = data.get("format", {})
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    return {
        "duration": float(fmt.get("duration", 0) or 0),
        "size": int(fmt.get("size", 0) or 0),
        "width": v.get("width"),
        "height": v.get("height"),
        "fps": v.get("r_frame_rate"),
    }


@app.post("/claim")
def claim():
    files = sorted(glob.glob(os.path.join(MEDIA, "inbox", "*.mp4")))
    if not files:
        return {"empty": True}
    src = files[0]
    job = time.strftime("%Y%m%d-%H%M%S")
    name = os.path.splitext(os.path.basename(src))[0]
    workdir = os.path.join(MEDIA, "work", job)
    os.makedirs(workdir, exist_ok=True)
    dst = os.path.join(workdir, "source.mp4")
    shutil.move(src, dst)
    return {"empty": False, "jobId": job, "name": name, "path": f"work/{job}/source.mp4"}


VISION_BACKEND = os.environ.get(
    "VISION_BACKEND",
    "ollama"
).lower()

OLLAMA_URL = os.environ.get(
    "OLLAMA_URL",
    "http://host.docker.internal:11434"
)

LMSTUDIO_URL = os.environ.get(
    "LMSTUDIO_URL",
    "http://host.docker.internal:1234/v1/chat/completions"
)

VISION_MODEL = os.environ.get(
    "VISION_MODEL",
    "llava:13b"
)

# Hard cap on the coarse whole-segment scan. When scene detection is weak we
# fall back to fixed-interval sampling; a 15-minute segment at 2s would be
# ~450 frames, and every frame costs an ffmpeg extract + a YOLO inference.
# That stage is the heaviest in the pipeline and can exhaust container memory
# on long segments. We widen the interval so the coarse scan never exceeds
# this many frames (still far more than topMotion needs to pick from).
MAX_COARSE_FRAMES = int(os.environ.get("MAX_COARSE_FRAMES", "160"))

print("Loading YOLO...", flush=True)
YOLO_MODEL = YOLO("yolov8n.pt")
print("YOLO loaded.", flush=True)

YOLO_WEIGHTS = {

    "person": 2.0,

    "car": 1.5,
    "truck": 1.5,
    "bus": 1.5,
    "motorcycle": 1.5,
    "bicycle": 1.2,

    "airplane": 4.0,
    "train": 4.0,

    "boat": 2.0,

    "dog": 0.5,
    "cat": 0.5
}

OCR_WEIGHTS = {

    "ACE": 10,

    "VICTORY": 9,
    "CHAMPION": 9,

    "WIN": 8,

    "QUAD": 8,

    "TRIPLE": 7,

    "HEADSHOT": 6,

    "DOUBLE": 5,

    "ELIMINATION": 4,

    "KILL": 3,
    "DOWNED": 3,

    "+500": 5,
    "+250": 3,
    "+100": 1,

    "XP": 0.5
}

print("Loading EasyOCR...", flush=True)

ocr = easyocr.Reader(
    ['en'],
    gpu=False
)

print("EasyOCR loaded.", flush=True)

def _loudness_curve(full, hop=0.25):
    """
    Return (times[], loudness_dbfs[]) for the whole file.

    We decode the audio to mono 16 kHz PCM and compute RMS loudness (in dBFS)
    over short hops. This is far more reliable than scraping ffmpeg's ebur128
    stderr output, whose line format differs between ffmpeg builds and was
    silently producing zero samples here.
    """
    cmd = [
        "ffmpeg", "-nostats", "-v", "error",
        "-i", full,
        "-vn", "-ac", "1", "-ar", "16000",
        "-f", "s16le", "-"
    ]
    p = subprocess.run(cmd, capture_output=True)

    if not p.stdout:
        return [], []

    samples = np.frombuffer(p.stdout, dtype=np.int16)

    if samples.size == 0:
        return [], []

    audio = samples.astype(np.float32) / 32768.0

    sr = 16000
    win = max(1, int(hop * sr))

    times, vals = [], []
    for i in range(0, audio.size - win + 1, win):
        chunk = audio[i:i + win]
        rms = float(np.sqrt(np.mean(chunk * chunk)) + 1e-9)
        db = 20.0 * math.log10(rms)
        times.append(i / sr)
        vals.append(db)

    return times, vals

def _pick_peaks(times, vals, k, min_gap):
    """Greedily pick k loudest moments at least min_gap seconds apart."""
    order = sorted(range(len(vals)), key=lambda i: vals[i], reverse=True)
    chosen = []
    for i in order:
        t = times[i]
        if all(abs(t - c[0]) > min_gap for c in chosen):
            chosen.append((t, vals[i]))
        if len(chosen) >= k:
            break
    return chosen

def clip_audio_score(times, vals, start, end):
    """
    Smart loudness for a single clip window.

    Raw loudness is a bad highlight signal: a person speaking loudly into the
    mic stays loud the whole time and would always score high. Instead we score
    the largest short-term LOUDNESS JUMP inside the window.

    Sudden transients (gunfire, explosions, kill / reward stingers) cause big
    jumps; sustained speech or background music stays flat and scores low.
    """
    window = [
        (t, v)
        for t, v in zip(times, vals)
        if start <= t <= end
    ]

    if len(window) < 2:
        return 0.0

    best_jump = 0.0
    for i in range(1, len(window)):
        jump = window[i][1] - window[i - 1][1]
        if jump > best_jump:
            best_jump = jump

    return best_jump

def _extract_frame(full, t, out_path):
    subprocess.run(["ffmpeg", "-y", "-ss", str(t), "-i", full,
                    "-frames:v", "1", "-q:v", "3", out_path], capture_output=True)

def yolo_score(frame_path):

    results = YOLO_MODEL(frame_path, verbose=False)

    result = results[0]

    boxes = result.boxes

    if boxes is None or len(boxes) == 0:
        return 0, []

    score = 0
    detected = []

    names = result.names

    for cls, conf in zip(
        boxes.cls.tolist(),
        boxes.conf.tolist()
    ):

        label = names[int(cls)]

        detected.append(label)

        weight = YOLO_WEIGHTS.get(label, 1.0)

        score += conf * weight

    return score, detected

def _extract_clip_frames(full, start, end, out_dir):

    os.makedirs(out_dir, exist_ok=True)

    duration = end - start

    offsets = [
        0,
        duration * 0.25,
        duration * 0.50,
        duration * 0.75,
        duration * 0.95
    ]

    frames = []

    for i, offset in enumerate(offsets):

        t = start + offset

        out = os.path.join(out_dir, f"frame_{i}.jpg")

        _extract_frame(full, t, out)

        frames.append(out)

    return frames

def build_cv_summary(
    motion,
    yolo,
    ocr,
    audio
):

    summary = []

    if motion >= 0.80:
        summary.append("Very high motion detected.")
    elif motion >= 0.60:
        summary.append("Moderate motion detected.")
    elif motion >= 0.30:
        summary.append("Low motion detected.")
    else:
        summary.append("Almost no motion detected.")

    if yolo >= 0.80:
        summary.append("Many gameplay objects detected.")
    elif yolo >= 0.60:
        summary.append("Several gameplay objects detected.")
    elif yolo >= 0.30:
        summary.append("Few gameplay objects detected.")
    else:
        summary.append("Almost no gameplay objects detected.")

    if ocr >= 0.80:
        summary.append("Strong reward text detected.")
    elif ocr >= 0.50:
        summary.append("Some reward text detected.")
    elif ocr >= 0.20:
        summary.append("Very little reward text detected.")
    else:
        summary.append("No reward text detected.")

    if audio >= 0.80:
        summary.append("Strong impact sounds detected.")
    elif audio >= 0.50:
        summary.append("Some impact sounds detected.")
    elif audio >= 0.20:
        summary.append("Faint impact sounds detected.")
    else:
        summary.append("No notable impact sounds detected.")

    return summary

def build_prompt(
    motion,
    yolo,
    ocr,
    audio,
    clip_len
):

    cv_summary = build_cv_summary(
        motion,
        yolo,
        ocr,
        audio
    )

    return f"""
You are an expert esports highlight curator.

You are given 5 frames sampled from ONE single {clip_len:.0f}-second gameplay clip.
The frames are in chronological order:

Frame 1  ~  0%  (start of the clip)
Frame 2  ~ 25%
Frame 3  ~ 50%  (middle)
Frame 4  ~ 75%
Frame 5  ~ 95%  (end of the clip)

Judge the clip as ONE continuous moment. Read the progression of the
action across the 5 frames instead of rating each image on its own.

These frames were already pre-selected by an automated highlight
detection pipeline, so they are likely (but not guaranteed) interesting.

--- Computer Vision Analysis ---
These scores are RELATIVE to the other candidate clips detected in THIS
video (1.00 = strongest among the candidates, 0.00 = weakest). They are
NOT absolute quality measures, so treat them as ranking hints, not truth.

Motion Score : {motion:.2f}   (relative amount of on-screen movement)
YOLO Score   : {yolo:.2f}   (relative object count - weak signal, low weight)
OCR Score    : {ocr:.2f}   (relative reward text such as HEADSHOT, KILL, VICTORY, ACE)
Audio Score  : {audio:.2f}   (relative impact sounds: gunfire, explosions, reward stingers - NOT loud talking)

Summary:
{chr(10).join(cv_summary)}

--- Your task ---
1. gameplay : true ONLY if these frames clearly show real, live video-game
   gameplay. Set gameplay = false for anything that is not live gameplay,
   including: advertisements, sponsor / promotional / brand screens or
   logos, intros, outros, menus, loading screens, scoreboards, desktop,
   webcam / face-cam, black screens, OR frozen / static frames where almost
   nothing changes across the 5 frames.
2. approve  : true only if this is genuinely highlight-worthy. Reject
   (approve = false) advertisements, promos, intros / outros, and static
   or idle moments even if a game image is technically visible.
3. confidence : how strong this highlight is (see guide below).
4. Use the CV scores as supporting evidence:
   - If the visuals agree with the scores, raise your confidence.
   - If the scores look misleading (e.g. high motion but nothing happens),
     lower your confidence.

Consistency rules:
- If gameplay is false, approve MUST be false and confidence MUST be 0.00.
- If approve is false, confidence MUST be <= 0.30.
- If the 5 frames look almost identical (no real change), treat it as
  static: gameplay = false and confidence = 0.00.

Confidence guide:
1.00 = Exceptional highlight
0.90 = Excellent gameplay
0.80 = Strong highlight
0.70 = Good gameplay
0.60 = Average gameplay
0.40 = Weak highlight
0.20 = Probably not a highlight
0.00 = Not gameplay / definitely reject

Return ONLY this JSON object, nothing else:
{{
    "gameplay": true,
    "approve": true,
    "confidence": 0.0,
    "reason": "short reason under 12 words"
}}

Never return markdown. Never explain. Return JSON only.
"""

def parse_vision_response(data):

    gameplay = bool(
        data.get("gameplay", True)
    )

    approve = bool(
        data.get("approve", True)
    )

    confidence = float(
        data.get("confidence", 0.5)
    )

    reason = str(
        data.get("reason", "")
    )

    return (
        gameplay,
        approve,
        confidence,
        reason
    )

def vision_failed(e):

    return (
        True,
        True,
        0.5,
        f"vision-failed: {e}"
    )

def normalize_feature(
    frames,
    source_key,
    target_key
):

    values = [
        f[source_key]
        for f in frames
    ]

    min_value = min(values)
    max_value = max(values)

    for frame in frames:

        if max_value == min_value:
            frame[target_key] = 0.5
        else:
            frame[target_key] = (
                frame[source_key] - min_value
            ) / (max_value - min_value)

    print(
        f"\n=== {target_key.upper()} ===",
        flush=True
    )

    for frame in frames:
        print(
            frame[source_key],
            "->",
            round(frame[target_key], 3),
            flush=True
        )

def compute_fast_score(frame):

    return (
        frame["motion_norm"] * 0.40
        + frame["ocr_norm"] * 0.35
        + frame["audio_norm"] * 0.15
        + frame["yolo_norm"] * 0.10
    )

def split_video_jobs(
    duration_seconds,
    max_minutes=15,
    overlap_seconds=15
):

    max_duration = max_minutes * 60

    if duration_seconds <= max_duration:
        return [
            {
                "job": 1,
                "start": 0.0,
                "end": duration_seconds
            }
        ]

    jobs = []

    parts = math.ceil(
        duration_seconds / max_duration
    )

    for i in range(parts):

        start = i * max_duration

        if i > 0:
            start -= overlap_seconds

        end = min(
            (i + 1) * max_duration,
            duration_seconds
        )

        jobs.append({
            "job": i + 1,
            "start": start,
            "end": end
        })

    return jobs

def _vision_score_ollama(
    frame_paths,
    motion,
    yolo,
    ocr,
    audio,
    clip_len
):
    try:
        images = []

        for frame in frame_paths:
            with open(frame, "rb") as f:
                images.append(
                    base64.b64encode(f.read()).decode()
                )

        prompt = build_prompt(motion, yolo, ocr, audio, clip_len)

        #r = requests.post(f"{OLLAMA}/api/generate", json={
        #    "model": VISION_MODEL, "prompt": prompt, "images": [b64],
        #    "stream": False, "format": "json"}, timeout=180)

        #data = json.loads(r.json().get("response", "{}"))

        #print("Before requests.post()", flush=True)
        #print("OLLAMA =", OLLAMA_URL, flush=True)
        #print("MODEL =", VISION_MODEL, flush=True)
        r = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": VISION_MODEL,
            "prompt": prompt,
            "images": images,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0}
        },
        timeout=180
        )
        #print("After requests.post()", flush=True)
        #print("STATUS:", r.status_code, flush=True)
        #print("RAW:", r.text, flush=True)

        resp = r.json()
        print("PARSED:", resp, flush=True)

        data = json.loads(resp.get("response", "{}"))

        return parse_vision_response(data)

    except Exception as e:
        return vision_failed(e)

def _vision_score_lmstudio(
    frame_paths,
    motion,
    yolo,
    ocr,
    audio,
    clip_len
):

    try:

        images = []

        for frame in frame_paths:
            with open(frame, "rb") as f:
                b64 = base64.b64encode(
                    f.read()
                ).decode()

            images.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}"
                }
            })


        prompt = build_prompt(motion, yolo, ocr, audio, clip_len)

        content = [
            {
                "type": "text",
                "text": prompt
            }
        ]

        content.extend(images)

        payload = {
            "model": VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": content
                }
            ],
            "temperature": 0.1,
            "max_tokens": 200
        }

        r = requests.post(
            LMSTUDIO_URL,
            json=payload,
            timeout=300
        )

        print("LM Studio Status:", r.status_code, flush=True)

        resp = r.json()

        answer = resp["choices"][0]["message"]["content"]

        answer = (
            answer
            .replace("```json", "")
            .replace("```", "")
            .strip()
        )

        print(answer, flush=True)

        data = json.loads(answer)

        return parse_vision_response(data)

    except Exception as e:
        return vision_failed(e)

def _vision_score(
    frame_paths,
    motion,
    yolo,
    ocr,
    audio,
    clip_len
):

    print("\n" + "=" * 70, flush=True)
    print("VISION INFERENCE", flush=True)
    print(f"Backend : {VISION_BACKEND}", flush=True)
    print(f"Model   : {VISION_MODEL}", flush=True)
    print(f"Images  : {len(frame_paths)}", flush=True)
    print("=" * 70, flush=True)

    if VISION_BACKEND == "ollama":
        return _vision_score_ollama(
            frame_paths,
            motion,
            yolo,
            ocr,
            audio,
            clip_len
        )

    elif VISION_BACKEND == "lmstudio":
        return _vision_score_lmstudio(
            frame_paths,
            motion,
            yolo,
            ocr,
            audio,
            clip_len
        )

    raise ValueError(
        f"Unknown vision backend: {VISION_BACKEND}"
    )

class CandIn(BaseModel):
    path: str
    jobId: str
    clipLen: float = 15.0
    sampleInterval: float = 2.0
    topMotion: int = 20
    finalCandidates: int = 4
    # 0 == "auto": fall back to one clip length so highlights can sit
    # back-to-back instead of being forced 30 s apart.
    minGap: float = 0.0
    # 0 == no cap. When > 0, the merged result (across all segments of a long
    # video) is trimmed to at most this many clips, keeping the highest scored.
    maxCandidates: int = 0

class RenderClip(BaseModel):
    start: float
    end: float
    # Best-to-worst position (1 = best). Used only to name the output file so
    # the editor / uploader can post them in order. 0 == fall back to index.
    rank: int = 0


class RenderIn(BaseModel):
    path: str
    jobId: str
    clips: list[RenderClip]
    # Output framing. "9:16" = YouTube/Instagram Shorts (default), "4:5",
    # "1:1", "16:9", or "source" to keep the original frame untouched.
    aspect: str = "9:16"
    # How to reach the target aspect:
    #   "center" - crop the centre strip and fill the frame (most engaging
    #              for gameplay, but loses the left/right edges)
    #   "blur"   - whole frame fitted over a zoomed, blurred copy of itself
    #              (keeps all gameplay + HUD, no hard black bars)
    #   "fit"    - whole frame letterboxed on black
    cropMode: str = "center"
    # Fade-in / fade-out duration in seconds (0 disables).
    fade: float = 0.5

def detect_scenes(full, min_threshold=0.12):

    # One decode pass at a low threshold surfaces every candidate cut together
    # with its scene_score, so the caller can pick an effective threshold in
    # Python (adaptive) instead of re-decoding the video several times.
    cmd = [
        "ffmpeg",
        "-i", full,
        "-filter:v",
        f"select='gt(scene,{min_threshold})',metadata=print",
        "-vsync", "0",
        "-f", "null",
        "-"
    ]

    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )

    pairs = []
    cur_time = None

    for line in p.stderr.splitlines():

        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            cur_time = float(m.group(1))
            continue

        ms = re.search(r"scene_score=([0-9.]+)", line)
        if ms and cur_time is not None:
            pairs.append((cur_time, float(ms.group(1))))
            cur_time = None

    # Older/edge ffmpeg builds may not surface scene_score lines. Fall back to
    # treating every selected frame as a cut at the minimum threshold.
    if not pairs:
        for line in p.stderr.splitlines():
            m = re.search(r"pts_time:([0-9.]+)", line)
            if m:
                pairs.append((float(m.group(1)), min_threshold))

    return pairs

def ocr_score(image_path):

    results = ocr.readtext(image_path)

    text = " ".join(
        r[1]
        for r in results
    ).upper()

    score = 0

    matched = []

    for word, weight in OCR_WEIGHTS.items():

        if word in text:

            score += weight
            matched.append(word)

    return score, text, matched

def ocr_score_frames(frame_paths):
    """
    Run OCR over several frames of one clip and keep the strongest hit.

    Reward text (e.g. "DOUBLE KILL") often flashes for ~1 second and is gone by
    the middle frame, so scanning only the 50% frame misses it. We score every
    frame and take the max, merging the matched words and the text of the
    best-scoring frame.
    """
    best_score = 0
    best_text = ""
    all_hits = []

    for fp in frame_paths:
        score, text, hits = ocr_score(fp)

        if score > best_score:
            best_score = score
            best_text = text

        for h in hits:
            if h not in all_hits:
                all_hits.append(h)

    return best_score, best_text, all_hits

def sample_video(duration, interval=2.0):
    times = []

    t = interval

    while t < duration:
        times.append(round(t, 2))
        t += interval

    return times

def audio_peak_times(times, vals, min_gap, max_peaks):
    """
    Pick the timestamps where loudness JUMPS the most.

    A sudden rise in loudness is the onset of a loud event - a gunshot,
    explosion, hit or shout - which is exactly where highlights live. We rank
    by the size of the rise from the previous sample and keep the strongest,
    spaced at least ``min_gap`` seconds apart so one loud burst doesn't claim
    every slot.
    """
    if not times or len(times) < 3:
        return []

    rises = []
    for i in range(1, len(vals)):
        rise = vals[i] - vals[i - 1]
        if rise > 0:
            rises.append((rise, times[i]))

    rises.sort(reverse=True)

    kept = []
    for _, t in rises:
        if all(abs(t - k) >= min_gap for k in kept):
            kept.append(t)
        if len(kept) >= max_peaks:
            break

    return sorted(kept)

def merge_seed_times(*lists, min_gap, max_count):
    """
    Merge candidate timestamps from several sources into one clean list.

    Times closer than ``min_gap`` collapse to one (a scene cut and an audio
    peak a fraction of a second apart are the same moment), and the result is
    thinned uniformly if it still exceeds ``max_count``.
    """
    times = sorted({
        round(t, 2)
        for lst in lists
        for t in lst
    })

    kept = []
    for t in times:
        if not kept or t - kept[-1] >= min_gap:
            kept.append(t)

    if len(kept) > max_count:
        step = len(kept) / max_count
        kept = [kept[int(i * step)] for i in range(max_count)]

    return kept

# Target pixel size for each supported aspect ratio. Shorts/Reels/TikTok all
# use 1080x1920; the rest follow the same 1080-wide convention. "source"
# (None) keeps the original frame size.
ASPECT_DIMS = {
    "9:16": (1080, 1920),
    "4:5": (1080, 1350),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
    "source": None,
}


def build_video_filter(aspect, crop_mode, fade_dur, duration):
    """
    Build the ffmpeg filter for reframing a clip to ``aspect`` with the chosen
    ``crop_mode`` plus optional fade in/out.

    Returns ``(flag, filter_string, extra_maps)`` where ``flag`` is either
    ``"-vf"`` (single chain) or ``"-filter_complex"`` (blur needs a split), and
    ``extra_maps`` are any explicit ``-map`` args the complex graph requires.
    """
    dims = ASPECT_DIMS.get(aspect, ASPECT_DIMS["9:16"])

    fades = ""
    if fade_dur and fade_dur > 0 and duration > 2 * fade_dur:
        out_start = max(0.0, duration - fade_dur)
        fades = (
            f",fade=t=in:st=0:d={fade_dur}"
            f",fade=t=out:st={out_start:.3f}:d={fade_dur}"
        )

    # Keep the original frame size, just (optionally) fade.
    if dims is None:
        return "-vf", "null" + fades, []

    W, H = dims

    if crop_mode == "blur":
        # Whole frame fitted onto a zoomed, blurred copy of itself - nothing is
        # cropped away and there are no hard black bars.
        fc = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},gblur=sigma=25[bg2];"
            f"[fg]scale={W}:{H}:force_original_aspect_ratio=decrease[fg2];"
            f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2"
            f"{fades}[v]"
        )
        return "-filter_complex", fc, ["-map", "[v]", "-map", "0:a?"]

    if crop_mode == "fit":
        chain = (
            f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black"
            f"{fades}"
        )
        return "-vf", chain, []

    # Default "center": crop the centre strip to the target aspect, then scale.
    chain = (
        f"crop='min(iw,ih*{W}/{H})':'min(ih,iw*{H}/{W})',"
        f"scale={W}:{H}"
        f"{fades}"
    )
    return "-vf", chain, []


def render_clip(
    source,
    start,
    end,
    output,
    aspect="9:16",
    crop_mode="center",
    fade=0.5
):

    duration = end - start

    flag, filter_string, extra_maps = build_video_filter(
        aspect,
        crop_mode,
        fade,
        duration
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(start),
        "-i", source,
        "-t", str(duration),
        flag, filter_string,
    ]

    cmd += extra_maps

    cmd += [
        # crf 18 + faststart keeps the clip visually near-lossless and ready
        # for instant web playback; the source resolution is preserved unless
        # an aspect crop/scale was requested above.
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )

    print("=" * 80, flush=True)
    print("FFMPEG RETURN:", result.returncode, flush=True)
    print("OUTPUT FILE:", output, flush=True)
    print("ASPECT:", aspect, "CROP:", crop_mode, flush=True)
    print("STDERR:", result.stderr, flush=True)
    print("=" * 80, flush=True)


def split_video(
    source,
    start,
    end,
    output
):

    duration = end - start

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss", str(start),
            "-i", source,
            "-t", str(duration),
            "-c", "copy",
            output
        ],
        check=True,
        capture_output=True
    )

    return output

def refine_candidate(
    video_path,
    approx_time,
    duration,
    job_dir,
    window=7.0,
    step=0.5
):

    work = os.path.join(
        job_dir,
        "refine",
        f"candidate_{int(approx_time)}"
    )
    os.makedirs(work, exist_ok=True)

    start = max(0.0, approx_time - window)
    end = min(duration, approx_time + window)
    span = end - start

    if span <= 0:
        return approx_time, 0.0, 0.0

    fps = 1.0 / step

    # One ffmpeg pass dumps the whole window as frames, instead of spawning
    # ~(2*window/step) separate ffmpeg processes.
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-i", video_path,
            "-t", str(span),
            "-vf", f"fps={fps}",
            "-q:v", "3",
            os.path.join(work, "f_%04d.jpg")
        ],
        capture_output=True
    )

    frames = sorted(glob.glob(os.path.join(work, "f_*.jpg")))

    best_time = approx_time
    best_motion = -1.0
    motion_sum = 0.0
    motion_count = 0
    prev_frame = None

    for i, img in enumerate(frames):

        if prev_frame is not None:

            m = motion_score(prev_frame, img)

            motion_sum += m
            motion_count += 1

            if m > best_motion:
                best_motion = m
                # frame i was sampled at time start + i / fps
                best_time = start + i / fps

        prev_frame = img

    # Sustained motion = average frame-to-frame change across the whole window.
    # It represents how action-packed the clip really is, and (unlike the single
    # peak) is not faked by one scene cut inside the window.
    mean_motion = motion_sum / motion_count if motion_count else 0.0

    print(
        f"Refined {approx_time:.2f}s -> {best_time:.2f}s "
        f"(peak={best_motion:.2f}, mean={mean_motion:.2f})",
        flush=True
    )

    return best_time, best_motion, mean_motion


def print_final_results(selected):

    print("\n===== FINAL RANKING =====", flush=True)

    for i, frame in enumerate(selected, 1):

        print(
            f"{i}. "
            f"Time={frame['time']:.1f}s | "
            f"Motion={frame['motion_norm']:.2f} | "
            f"YOLO={frame['yolo_norm']:.2f} | "
            f"OCR={frame['ocr_norm']:.2f} | "
            f"Audio={frame['audio_norm']:.2f} | "
            f"Gameplay={frame['gameplay']} "
            f"Approve={frame['approve']} "
            f"Confidence={frame['confidence']:.2f} "
            f"Final={frame['final_score']:.2f} "
            f"Reason={frame['reason']}",
            flush=True
        )

def load_clip_frames(
    video_path,
    frame,
    job_dir
):

    # OCR and vision both ask for the same 5 clip frames. Extract once,
    # then reuse the cached paths (frame start/end no longer change here).
    cached = frame.get("clip_frames")
    if cached and all(os.path.exists(p) for p in cached):
        return cached

    clip_dir = os.path.join(
        job_dir,
        "clips",
        f"clip_{frame['idx']}"
    )

    clip_frames = _extract_clip_frames(
        video_path,
        frame["start"],
        frame["end"],
        clip_dir
    )

    frame["clip_frames"] = clip_frames
    return clip_frames

def process_job(
    full,
    dur,
    inp,
    job_dir,
    job_start=0.0,
    job_end=None
):

    if job_end is None:
        job_end = dur

    # Smart-loudness curve for the whole segment (computed once).
    # Used later to score each clip by its loudest sudden impact sound.
    audio_times, audio_vals = _loudness_curve(full)
    if audio_vals:
        print(
            f"Audio loudness samples: {len(audio_times)} "
            f"(min={min(audio_vals):.1f} dBFS, max={max(audio_vals):.1f} dBFS)",
            flush=True
        )
    else:
        print(
            "Audio loudness samples: 0 (no audio decoded)",
            flush=True
        )

    # ---- Candidate seeding -------------------------------------------------
    # Where should we take a first cheap look? Three sources, best first, so we
    # look where something actually happens instead of sampling blindly:
    #   1. Scene cuts  - the picture changed a lot (new room, kill cam, etc).
    #   2. Audio peaks - loudness jumped (gunshot, explosion, hit, shout).
    #   3. Uniform grid - last-resort backstop so we never come up empty.

    # One decode pass gives every cut + its scene_score; we then relax the
    # threshold in Python until we have enough cuts. Quiet gameplay rarely
    # trips the strict 0.30, so the adaptive step keeps us off the dumb grid.
    scene_pairs = detect_scenes(full)

    scene_threshold = 0.30
    scene_times = []
    for scene_threshold in (0.30, 0.20, 0.12):
        scene_times = [
            t for (t, s) in scene_pairs
            if s >= scene_threshold
        ]
        if len(scene_times) >= 20:
            break

    print(
        f"Detected {len(scene_times)} scene cuts "
        f"(threshold {scene_threshold:.2f})",
        flush=True
    )

    # Audio peaks come for free from the loudness curve computed above.
    peak_times = audio_peak_times(
        audio_times,
        audio_vals,
        min_gap=max(2.0, inp.clipLen / 2),
        max_peaks=MAX_COARSE_FRAMES
    )

    print(
        f"Found {len(peak_times)} audio peaks",
        flush=True
    )

    # Merge the two smart signals, collapsing near-duplicates and capping count.
    sample_times = merge_seed_times(
        scene_times,
        peak_times,
        min_gap=max(1.0, inp.clipLen / 4),
        max_count=MAX_COARSE_FRAMES
    )

    # Last-resort backstop: if scene + audio were too sparse (flat, silent
    # footage), add a capped uniform grid so the segment is still scanned.
    if len(sample_times) < 20:
        interval = inp.sampleInterval
        if dur / interval > MAX_COARSE_FRAMES:
            interval = dur / MAX_COARSE_FRAMES

        print(
            f"Sparse seeds. Adding {interval:.1f}-second grid backstop.",
            flush=True
        )

        sample_times = merge_seed_times(
            sample_times,
            sample_video(dur, interval=interval),
            min_gap=max(1.0, inp.clipLen / 4),
            max_count=MAX_COARSE_FRAMES
        )

    print(
        f"Sampling {len(sample_times)} frames",
        flush=True
    )

    frames_dir = os.path.join(
        job_dir,
        "frames"
    )

    os.makedirs(frames_dir, exist_ok=True)

    frames = []
    for i, t in enumerate(sample_times):
        start = max(0.0, t - inp.clipLen / 2)
        end = min(dur, start + inp.clipLen)
        fp = os.path.join(frames_dir, f"cand_{i}.jpg")
        _extract_frame(full, t, fp)
        frames.append({
            "idx": i, "time": t, "start": start, "end": end, "frame":fp
        })
    print(f"Extracted {len(frames)} frames", flush=True)
    
    motion_frames = []
    for i in range(1, len(frames)):
        prev = frames[i - 1]
        curr = frames[i]

        motion = motion_score(
        prev["frame"],
        curr["frame"]
        )
        
        yolo, yolo_hits = yolo_score(curr["frame"])

        motion_frames.append({
            "idx": curr["idx"], "time": curr["time"], 
            "start": curr["start"], "end": curr["end"],
            "motion": motion, "yolo": yolo, "yolo_hits": yolo_hits
        })
    
    motion_frames.sort(
    key=lambda x: x["motion"],
    reverse=True
    )

    max_possible = int(dur / inp.clipLen)

    candidate_count = max(
        2,
        min(inp.topMotion, max_possible)
    )

    print(
        f"Selecting top {candidate_count} candidates",
        flush=True
    )

    interesting = motion_frames[:candidate_count]

    # Refine search window scales with the clip (clipLen / 3), clamped so it
    # is never less than 7 s or more than 15 s on each side of the motion peak.
    # e.g. 10 s -> 7, 15 s -> 7, 30 s -> 10, 45 s and up -> 15.
    refine_window = max(7.0, min(inp.clipLen / 3.0, 15.0))

    for frame in interesting:

        refined_time, refined_peak, refined_mean = refine_candidate(
            full,
            frame["time"],
            dur,
            job_dir,
            window=refine_window
        )

        frame["time"] = refined_time
        # Score on sustained (mean) intra-window motion, not the single peak,
        # so a lone scene cut inside the window can't fake a high-action clip.
        # The peak still decides where to center the clip (refined_time).
        frame["motion"] = refined_mean

        frame["start"] = max(
            0,
            refined_time - inp.clipLen / 2
        )
        frame["end"] = min(
            dur,
            frame["start"] + inp.clipLen
        )

    print("\n===== AFTER REFINEMENT =====", flush=True)

    for frame in interesting:
        print(
            f"Time={frame['time']:.2f}s "
            f"Motion={frame['motion']:.2f} "
            f"YOLO={frame['yolo']:.2f}",
            f"Objects={frame['yolo_hits']}",
            flush=True
        )

    print(f"Scoring {len(interesting)} frames with OCR only", flush=True)

    scored = []
    for frame in interesting:

        clip_frames = load_clip_frames(
            full,
            frame,
            job_dir
        )

        ocr_points, ocr_text, ocr_hits = ocr_score_frames(clip_frames)

        frame["ocr"] = ocr_points
        frame["ocr_text"] = ocr_text
        frame["ocr_hits"] = ocr_hits

        frame["audio"] = clip_audio_score(
            audio_times,
            audio_vals,
            frame["start"],
            frame["end"]
        )

        print(
            f"OCR={frame['ocr']:.1f}",
            f"Hits={ocr_hits}",
            f"Audio={frame['audio']:.1f}",
            f"Text={frame['ocr_text'][:80]}",
            flush=True
        )
        frame["reason"] = ""

        scored.append(frame)

    normalize_feature(scored, "motion", "motion_norm") 

    normalize_feature(scored, "yolo", "yolo_norm")

    normalize_feature(scored, "ocr", "ocr_norm")

    normalize_feature(scored, "audio", "audio_norm")

    for frame in scored:

        frame["final_score"] = compute_fast_score(
            frame
        )

    scored.sort(
        key=lambda x: x["final_score"],
        reverse=True
    )

    llava_candidates = min(6, len(scored))

    interesting = scored[:llava_candidates]

    print(
        f"Running {VISION_BACKEND} ({VISION_MODEL}) on {len(interesting)} clips",
        flush=True
    )

    for frame in interesting:

        clip_frames = load_clip_frames(full, frame, job_dir)

        gameplay, approve, confidence, reason = _vision_score(
            clip_frames,
            frame["motion_norm"],
            frame["yolo_norm"],
            frame["ocr_norm"],
            frame["audio_norm"],
            inp.clipLen
        )

        frame["gameplay"] = gameplay
        frame["approve"] = approve
        frame["confidence"] = confidence
        frame["reason"] = reason
        if not gameplay or not approve:
            print(
                f"Rejected by Vision: {reason}",
                flush=True
            )
            # Drop the score so a rejected clip can never win
            frame["final_score"] = 0.0
            continue

        # Blend the vision model's confidence into the fast score.
        # llava is unreliable at the confidence NUMBER: it frequently returns
        # 0.00 for clips it simultaneously approves and calls "highlight-worthy".
        # A raw multiply would wrongly zero those good clips, so instead map
        # confidence onto a 0.5 - 1.0 multiplier. An approved clip keeps at
        # least half of its CV score, and higher confidence is rewarded on top.
        frame["final_score"] *= (0.5 + 0.5 * confidence)

    # Keep only clips the vision model actually approved. We rely on the
    # binary gameplay / approve flags, which llava sets reliably, rather than
    # its confidence number, which it does not (it often returns 0.00 for clips
    # it just approved). Ads, menus and static frames are already rejected via
    # gameplay = false in the prompt, so no extra confidence floor is needed.
    approved = [
        f for f in interesting
        if f.get("gameplay") and f.get("approve")
    ]

    approved.sort(
        key=lambda x: x["final_score"],
        reverse=True
    )

    # Effective spacing between kept clips. minGap == 0 means "auto" -> one
    # clip length, so highlights may sit back-to-back. We never allow a gap
    # smaller than clipLen, otherwise two kept clips would overlap in time.
    min_gap = inp.minGap if inp.minGap and inp.minGap > 0 else inp.clipLen
    min_gap = max(min_gap, inp.clipLen)

    selected = []

    for frame in approved:
        keep = True
        for chosen in selected:
            if abs(frame["time"] - chosen["time"]) < min_gap:
                keep = False
                break
        if keep:
            selected.append(frame)
        if len(selected) == inp.finalCandidates:
            break

    # Fields the n8n flow expects on each candidate.
    # motion_frames don't carry the original candidate frame path, so use the
    # cached middle clip frame as a representative thumbnail.
    for frame in selected:
        clip_frames = frame.get("clip_frames") or []
        thumb = clip_frames[2] if len(clip_frames) > 2 else (
            clip_frames[0] if clip_frames else None
        )
        frame["frame_rel"] = (
            os.path.relpath(thumb, MEDIA) if thumb else None
        )
        frame["vision_score"] = frame.get("confidence", 0.0)

    print_final_results(selected)

    return selected

@app.post("/candidates")
def candidates(inp: CandIn):
    full = os.path.join(MEDIA, inp.path)
    dur = probe(ProbeIn(path=inp.path))["duration"]
    jobs = split_video_jobs(dur)

    print(
        "\n========== VIDEO JOBS ==========",
        flush=True
    )

    for job in jobs:
        print(
            job,
            flush=True
        )

    print(
        "================================\n",
        flush=True
    )
    
    all_selected = []

    for job in jobs:

        print(
            f"\nProcessing Job {job['job']}",
            flush=True
        )

        if len(jobs) == 1:
            job_dir = os.path.join(
                MEDIA,
                "work",
                inp.jobId
            )
        else:
            job_dir = os.path.join(
                MEDIA,
                "work",
                inp.jobId,
                f"segment_{job['job']}"
            )

        os.makedirs(
            job_dir,
            exist_ok=True
        )

        if len(jobs) == 1:

            segment = full

        else:

            segment = os.path.join(
                job_dir,
                "source.mp4"
            )

            split_video(
                full,
                job["start"],
                job["end"],
                segment
            )

        segment_duration = (
            job["end"] -
            job["start"]
        )

        selected = process_job(
            segment,
            segment_duration,
            inp,
            job_dir
        )

        for frame in selected:

            frame["time"] += job["start"]
            frame["start"] += job["start"]
            frame["end"] += job["start"]

        all_selected.extend(selected)

    #print("INPUT PATH:", repr(inp.path))
    #info = probe(ProbeIn(path=inp.path))
    #import sys

    #print("=" * 80, flush=True)
    #print(f"INPUT PATH: {repr(inp.path)}", flush=True)
    #print(f"PROBE RESULT: {repr(info)}", flush=True)
    #print("=" * 80, flush=True)
    #sys.stdout.flush()
    #dur = info["duration"]

    all_selected.sort(
        key=lambda x: x["final_score"],
        reverse=True
    )

    # Same auto / non-overlapping spacing rule as inside process_job, applied
    # when merging clips from multiple segments of a long video.
    merge_gap = inp.minGap if inp.minGap and inp.minGap > 0 else inp.clipLen
    merge_gap = max(merge_gap, inp.clipLen)

    final = []

    for frame in all_selected:

        if all(
            abs(frame["time"] - f["time"]) >= merge_gap
            for f in final
        ):
                final.append(frame)

    # Optional overall cap across the whole video (all segments combined).
    # final is already sorted by final_score, so we keep the highest scored.
    if inp.maxCandidates and inp.maxCandidates > 0:
        final = final[:inp.maxCandidates]

    # final is already sorted best -> worst by final_score. Stamp an explicit
    # 1-based rank so the editor / uploader can post them in order.
    for idx, frame in enumerate(final):
        frame["rank"] = idx + 1

    return {
        "dur": dur,
        "candidates": final
    }

    

@app.post("/render")
def render(inp: RenderIn):

    source = os.path.join(
        MEDIA,
        inp.path
    )

    out_dir = os.path.join(
        MEDIA,
        "work",
        inp.jobId,
        "renders"
    )

    os.makedirs(
        out_dir,
        exist_ok=True
    )

    rendered = []

    print("SOURCE:", source, flush=True)
    print("EXISTS:", os.path.exists(source), flush=True)
    print("OUTDIR:", out_dir, flush=True)

    for i, clip in enumerate(inp.clips):

        rank = clip.rank if clip.rank else i + 1

        out_file = os.path.join(
            out_dir,
            f"clip_{rank}.mp4"
        )

        render_clip(
            source,
            clip.start,
            clip.end,
            out_file,
            aspect=inp.aspect,
            crop_mode=inp.cropMode,
            fade=inp.fade
        )

        rendered.append(
            f"work/{inp.jobId}/renders/clip_{rank}.mp4"
        )

    return {
        "clips": rendered
    }

    PS C:\Temp\gameplay-autopost> docker compose logs helper
helper-1  | WARNING ⚠️ user config directory '/root/.config/Ultralytics' is not writable, using '/tmp/Ultralytics'. Set YOLO_CONFIG_DIR to override.
helper-1  | Creating new Ultralytics Settings v0.0.6 file ✅
helper-1  | View Ultralytics Settings with 'yolo settings' or at '/tmp/Ultralytics/settings.json'
helper-1  | Update Settings with 'yolo settings key=value', i.e. 'yolo settings runs_dir=path/to/dir'. For help see https://docs.ultralytics.com/quickstart/#ultralytics-settings.
helper-1  | Loading YOLO...
Downloading https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n.pt to 'yolov8n.pt': 100% ━━━━━━━━━━━━ 6.2MB 13.0MB/s 0.5s
helper-1  | YOLO loaded.
helper-1  | Loading EasyOCR...
helper-1  | Using CPU. Note: This module is much faster with a GPU.
helper-1  | Downloading detection model, please wait. This may take several minutes depending upon your network connection.
Progress: |███████████████████████████████��Downloading recognition model, please wait. This may take several minutes depending upon your network connection.
Progress: |██████████████████████████████████████████████████| 100.0% CompleteEasyOCR loaded.
helper-1  | INFO:     Started server process [1]
helper-1  | INFO:     Waiting for application startup.
helper-1  | INFO:     Application startup complete.
helper-1  | INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
helper-1  | INFO:     172.18.0.4:45076 - "POST /claim HTTP/1.1" 200 OK
helper-1  | INFO:     172.18.0.4:45088 - "POST /probe HTTP/1.1" 200 OK
helper-1  |
helper-1  | ========== VIDEO JOBS ==========
helper-1  | {'job': 1, 'start': 0.0, 'end': 454.344853}
helper-1  | ================================
helper-1  |
helper-1  |
helper-1  | Processing Job 1
helper-1  | Audio loudness samples: 1817 (min=-50.8 dBFS, max=-2.1 dBFS)
helper-1  | Detected 144 scene cuts (threshold 0.30)
helper-1  | Found 37 audio peaks
helper-1  | Sampling 62 frames
helper-1  | Extracted 62 frames
helper-1  | Selecting top 20 candidates
helper-1  | Refined 341.96s -> 338.46s (peak=139.77, mean=42.51)
helper-1  | Refined 235.00s -> 235.50s (peak=43.92, mean=17.31)
helper-1  | Refined 450.77s -> 448.27s (peak=95.79, mean=37.90)
helper-1  | Refined 335.54s -> 331.54s (peak=147.32, mean=61.16)
helper-1  | Refined 296.03s -> 296.03s (peak=61.22, mean=32.62)
helper-1  | Refined 348.00s -> 346.00s (peak=68.35, mean=25.21)
helper-1  | Refined 33.07s -> 29.57s (peak=114.33, mean=35.32)
helper-1  | Refined 25.09s -> 29.59s (peak=116.65, mean=37.48)
helper-1  | Refined 5.74s -> 5.50s (peak=93.18, mean=33.82)
helper-1  | Refined 51.75s -> 55.75s (peak=111.92, mean=42.11)
helper-1  | Refined 264.33s -> 261.83s (peak=120.80, mean=45.43)
helper-1  | Refined 17.83s -> 21.83s (peak=66.15, mean=37.08)
helper-1  | Refined 245.25s -> 240.75s (peak=67.81, mean=15.15)
helper-1  | Refined 309.93s -> 303.93s (peak=72.59, mean=39.42)
helper-1  | Refined 11.78s -> 5.78s (peak=93.50, mean=36.02)
helper-1  | Refined 148.25s -> 154.75s (peak=133.83, mean=43.26)
helper-1  | Refined 205.51s -> 200.01s (peak=142.52, mean=59.87)
helper-1  | Refined 224.04s -> 221.54s (peak=145.53, mean=44.07)
helper-1  | Refined 328.96s -> 331.46s (peak=151.46, mean=53.04)
helper-1  | Refined 387.75s -> 381.75s (peak=76.44, mean=36.00)
helper-1  |
helper-1  | ===== AFTER REFINEMENT =====
helper-1  | Time=338.46s Motion=42.51 YOLO=0.77 Objects=['person']
helper-1  | Time=235.50s Motion=17.31 YOLO=1.09 Objects=['person']
helper-1  | Time=448.27s Motion=37.90 YOLO=0.00 Objects=[]
helper-1  | Time=331.54s Motion=61.16 YOLO=0.69 Objects=['boat']
helper-1  | Time=296.03s Motion=32.62 YOLO=0.00 Objects=[]
helper-1  | Time=346.00s Motion=25.21 YOLO=2.30 Objects=['airplane', 'kite']
helper-1  | Time=29.57s Motion=35.32 YOLO=2.48 Objects=['person', 'dog', 'banana']
helper-1  | Time=29.59s Motion=37.48 YOLO=2.95 Objects=['train']
helper-1  | Time=5.50s Motion=33.82 YOLO=0.42 Objects=['suitcase']
helper-1  | Time=55.75s Motion=42.11 YOLO=1.22 Objects=['person']
helper-1  | Time=261.83s Motion=45.43 YOLO=3.06 Objects=['person', 'suitcase', 'motorcycle', 'bowl', 'suitcase', 'suitcase']
helper-1  | Time=21.83s Motion=37.08 YOLO=0.84 Objects=['person']
helper-1  | Time=240.75s Motion=15.15 YOLO=0.40 Objects=['cell phone']
helper-1  | Time=303.93s Motion=39.42 YOLO=0.29 Objects=['giraffe']
helper-1  | Time=5.78s Motion=36.02 YOLO=0.57 Objects=['person']
helper-1  | Time=154.75s Motion=43.26 YOLO=0.00 Objects=[]
helper-1  | Time=200.01s Motion=59.87 YOLO=1.20 Objects=['person']
helper-1  | Time=221.54s Motion=44.07 YOLO=0.62 Objects=['person']
helper-1  | Time=331.46s Motion=53.04 YOLO=1.21 Objects=['traffic light', 'bench', 'person']
helper-1  | Time=381.75s Motion=36.00 YOLO=1.93 Objects=['chair', 'train']
helper-1  | Scoring 20 frames with OCR only
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=13.1 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=7.2 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=8.6 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=11.3 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=11.3 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=13.1 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=20.9 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=20.9 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=8.0 Hits=['WIN'] Audio=16.1 Text=REWIND 4
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=6.4 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=10.2 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=20.9 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=7.2 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=5.2 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=8.0 Hits=['WIN'] Audio=16.1 Text=REWIND 4
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=3.0 Hits=['KILL'] Audio=4.8 Text=BOWMEN JESUS WILL . BLAZE 6799   COFFEE IHL GOESBY TEROR POG TACOLEGEND   UOLA G
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=3.9 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=4.3 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=11.3 Text=
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | /usr/local/lib/python3.11/site-packages/torch/utils/data/dataloader.py:752: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
helper-1  |   super().__init__(loader)
helper-1  | OCR=0.0 Hits=[] Audio=11.0 Text=
helper-1  |
helper-1  | === MOTION_NORM ===
helper-1  | 42.513359857253086 -> 0.595
helper-1  | 17.307607391734184 -> 0.047
helper-1  | 37.897308905707476 -> 0.494
helper-1  | 61.16157224553111 -> 1.0
helper-1  | 32.62282008543917 -> 0.38
helper-1  | 25.208299676086675 -> 0.219
helper-1  | 35.31635829595872 -> 0.438
helper-1  | 37.482788075890554 -> 0.485
helper-1  | 33.82385795310691 -> 0.406
helper-1  | 42.11301288620436 -> 0.586
helper-1  | 45.42780303578319 -> 0.658
helper-1  | 37.082708473990486 -> 0.477
helper-1  | 15.154419769161521 -> 0.0
helper-1  | 39.423228604038066 -> 0.528
helper-1  | 36.02162414801955 -> 0.454
helper-1  | 43.260622980565195 -> 0.611
helper-1  | 59.87130734792953 -> 0.972
helper-1  | 44.06549746415252 -> 0.628
helper-1  | 53.039408506542564 -> 0.823
helper-1  | 36.000050053851595 -> 0.453
helper-1  |
helper-1  | === YOLO_NORM ===
helper-1  | 0.7741037607192993 -> 0.253
helper-1  | 1.0882501602172852 -> 0.356
helper-1  | 0 -> 0.0
helper-1  | 0.6925121545791626 -> 0.227
helper-1  | 0 -> 0.0
helper-1  | 2.2981028258800507 -> 0.752
helper-1  | 2.48440682888031 -> 0.813
helper-1  | 2.9506115913391113 -> 0.965
helper-1  | 0.42010533809661865 -> 0.137
helper-1  | 1.2227718830108643 -> 0.4
helper-1  | 3.056375965476036 -> 1.0
helper-1  | 0.8379254341125488 -> 0.274
helper-1  | 0.3981606662273407 -> 0.13
helper-1  | 0.2875466048717499 -> 0.094
helper-1  | 0.5743030309677124 -> 0.188
helper-1  | 0 -> 0.0
helper-1  | 1.1958723068237305 -> 0.391
helper-1  | 0.616462230682373 -> 0.202
helper-1  | 1.2091053426265717 -> 0.396
helper-1  | 1.926179975271225 -> 0.63
helper-1  |
helper-1  | === OCR_NORM ===
helper-1  | 0 -> 0.0
helper-1  | 0 -> 0.0
helper-1  | 0 -> 0.0
helper-1  | 0 -> 0.0
helper-1  | 0 -> 0.0
helper-1  | 0 -> 0.0
helper-1  | 0 -> 0.0
helper-1  | 0 -> 0.0
helper-1  | 8 -> 1.0
helper-1  | 0 -> 0.0
helper-1  | 0 -> 0.0
helper-1  | 0 -> 0.0
helper-1  | 0 -> 0.0
helper-1  | 0 -> 0.0
helper-1  | 8 -> 1.0
helper-1  | 3 -> 0.375
helper-1  | 0 -> 0.0
helper-1  | 0 -> 0.0
helper-1  | 0 -> 0.0
helper-1  | 0 -> 0.0
helper-1  |
helper-1  | === AUDIO_NORM ===
helper-1  | 13.147796789740061 -> 0.545
helper-1  | 7.211394577550774 -> 0.195
helper-1  | 8.574413807849643 -> 0.275
helper-1  | 11.303162355889018 -> 0.436
helper-1  | 11.320375184232226 -> 0.437
helper-1  | 13.147796789740061 -> 0.545
helper-1  | 20.87189951998188 -> 1.0
helper-1  | 20.87189951998188 -> 1.0
helper-1  | 16.098517634354188 -> 0.719
helper-1  | 6.392392142518355 -> 0.146
helper-1  | 10.152734055969251 -> 0.368
helper-1  | 20.87189951998188 -> 1.0
helper-1  | 7.211394577550774 -> 0.195
helper-1  | 5.222313474704389 -> 0.077
helper-1  | 16.098517634354188 -> 0.719
helper-1  | 4.757442124449979 -> 0.05
helper-1  | 3.9105377791858897 -> 0.0
helper-1  | 4.274547049452396 -> 0.021
helper-1  | 11.303162355889018 -> 0.436
helper-1  | 10.977699449406721 -> 0.417
helper-1  | Running ollama (llava:13b) on 6 clips
helper-1  |
helper-1  | ======================================================================
helper-1  | VISION INFERENCE
helper-1  | Backend : ollama
helper-1  | Model   : llava:13b
helper-1  | Images  : 5
helper-1  | ======================================================================
helper-1  | PARSED: {'model': 'llava:13b', 'created_at': '2026-06-28T20:24:03.4211986Z', 'response': '{\n"gameplay": true,\n"approve": true,\n"confidence": 0.0,\n"reason": "The frames show a continuous moment of gameplay with significant action and reward text detected."\n}', 'done': True, 'done_reason': 'stop', 'context': [29871, 13, 11889, 29901, 518, 2492, 29899, 29900, 3816, 2492, 29899, 29896, 3816, 2492, 29899, 29906, 3816, 2492, 29899, 29941, 3816, 2492, 29899, 29946, 29962, 13, 3492, 526, 385, 17924, 831, 4011, 12141, 3151, 1061, 29889, 13, 13, 3492, 526, 2183, 29871, 29945, 16608, 4559, 29881, 515, 6732, 29923, 2323, 29871, 29906, 29900, 29899, 7496, 3748, 1456, 20102, 29889, 13, 1576, 16608, 526, 297, 17168, 5996, 1797, 29901, 13, 13, 4308, 29871, 29896, 29871, 3695, 259, 29900, 29995, 29871, 313, 2962, 310, 278, 20102, 29897, 13, 4308, 29871, 29906, 29871, 3695, 29871, 29906, 29945, 29995, 13, 4308, 29871, 29941, 29871, 3695, 29871, 29945, 29900, 29995, 29871, 313, 17662, 29897, 13, 4308, 29871, 29946, 29871, 3695, 29871, 29955, 29945, 29995, 13, 4308, 29871, 29945, 29871, 3695, 29871, 29929, 29945, 29995, 29871, 313, 355, 310, 278, 20102, 29897, 13, 13, 29967, 566, 479, 278, 20102, 408, 6732, 29923, 9126, 3256, 29889, 7523, 278, 410, 11476, 310, 278, 13, 2467, 4822, 278, 29871, 29945, 16608, 2012, 310, 21700, 1269, 1967, 373, 967, 1914, 29889, 13, 13, 1349, 968, 16608, 892, 2307, 758, 29899, 8391, 491, 385, 3345, 630, 12141, 13, 29881, 2650, 428, 16439, 29892, 577, 896, 526, 5517, 313, 4187, 451, 22688, 29897, 8031, 29889, 13, 13, 5634, 20972, 478, 2459, 24352, 11474, 13, 1349, 968, 19435, 526, 5195, 29931, 1299, 18474, 304, 278, 916, 14020, 9335, 567, 17809, 297, 3446, 3235, 13, 9641, 313, 29896, 29889, 29900, 29900, 353, 4549, 342, 4249, 278, 21669, 29892, 29871, 29900, 29889, 29900, 29900, 353, 8062, 342, 467, 2688, 526, 13, 12256, 8380, 11029, 15366, 29892, 577, 7539, 963, 408, 24034, 26085, 29892, 451, 8760, 29889, 13, 13, 29924, 8194, 2522, 487, 584, 29871, 29900, 29889, 29946, 29945, 259, 313, 22925, 5253, 310, 373, 29899, 10525, 10298, 29897, 13, 29979, 29949, 3927, 2522, 487, 259, 584, 29871, 29900, 29889, 29896, 29929, 259, 313, 22925, 1203, 2302, 448, 8062, 7182, 29892, 4482, 7688, 29897, 13, 29949, 11341, 2522, 487, 1678, 584, 29871, 29896, 29889, 29900, 29900, 259, 313, 22925, 20751, 1426, 1316, 408, 17714, 3035, 7068, 2891, 29892, 476, 24071, 29892, 5473, 1783, 18929, 29892, 319, 4741, 29897, 13, 17111, 2522, 487, 29871, 584, 29871, 29900, 29889, 29955, 29906, 259, 313, 22925, 10879, 10083, 29901, 13736, 8696, 29892, 20389, 1080, 29892, 20751, 380, 19936, 448, 6058, 22526, 9963, 29897, 13, 13, 26289, 29901, 13, 29931, 340, 10884, 17809, 29889, 13, 2499, 3242, 694, 3748, 1456, 3618, 17809, 29889, 13, 5015, 549, 20751, 1426, 17809, 29889, 13, 9526, 10879, 10083, 17809, 29889, 13, 13, 5634, 3575, 3414, 11474, 13, 29896, 29889, 3748, 1456, 584, 1565, 6732, 16786, 565, 1438, 16608, 9436, 1510, 1855, 29892, 5735, 4863, 29899, 11802, 13, 259, 3748, 1456, 29889, 3789, 3748, 1456, 353, 2089, 363, 3099, 393, 338, 451, 5735, 3748, 1456, 29892, 13, 259, 3704, 29901, 18811, 275, 4110, 29892, 21955, 272, 847, 2504, 327, 1848, 847, 14982, 11844, 470, 13, 259, 1480, 359, 29892, 25956, 29892, 21950, 29892, 1757, 375, 29892, 8363, 11844, 29892, 8158, 24691, 29892, 14616, 29892, 13, 259, 1856, 11108, 847, 3700, 29899, 11108, 29892, 4628, 11844, 29892, 6323, 14671, 2256, 847, 2294, 16608, 988, 4359, 13, 259, 3078, 3620, 4822, 278, 29871, 29945, 16608, 29889, 13, 29906, 29889, 2134, 345, 29871, 584, 1565, 871, 565, 445, 338, 29120, 262, 873, 12141, 29899, 12554, 29891, 29889, 830, 622, 13, 259, 313, 9961, 345, 353, 2089, 29897, 18811, 275, 4110, 29892, 2504, 359, 29892, 25956, 847, 21950, 29892, 322, 2294, 13, 259, 470, 28132, 19462, 1584, 565, 263, 3748, 1967, 338, 5722, 1711, 7962, 29889, 13, 29941, 29889, 16420, 584, 920, 4549, 445, 12141, 338, 313, 4149, 10754, 2400, 467, 13, 29946, 29889, 4803, 278, 25778, 19435, 408, 20382, 10757, 29901, 13, 259, 448, 960, 278, 7604, 29879, 8661, 411, 278, 19435, 29892, 12020, 596, 16420, 29889, 13, 259, 448, 960, 278, 19435, 1106, 3984, 25369, 313, 29872, 29889, 29887, 29889, 1880, 10884, 541, 3078, 5930, 511, 13, 268, 5224, 596, 16420, 29889, 13, 13, 13696, 391, 3819, 6865, 29901, 13, 29899, 960, 3748, 1456, 338, 2089, 29892, 2134, 345, 341, 17321, 367, 2089, 322, 16420, 341, 17321, 367, 29871, 29900, 29889, 29900, 29900, 29889, 13, 29899, 960, 2134, 345, 338, 2089, 29892, 16420, 341, 17321, 367, 5277, 29871, 29900, 29889, 29941, 29900, 29889, 13, 29899, 960, 278, 29871, 29945, 16608, 1106, 4359, 13557, 313, 1217, 1855, 1735, 511, 7539, 372, 408, 13, 29871, 2294, 29901, 3748, 1456, 353, 2089, 322, 16420, 353, 29871, 29900, 29889, 29900, 29900, 29889, 13, 13, 16376, 5084, 10754, 29901, 13, 29896, 29889, 29900, 29900, 353, 8960, 284, 12141, 13, 29900, 29889, 29929, 29900, 353, 1222, 3729, 296, 3748, 1456, 13, 29900, 29889, 29947, 29900, 353, 3767, 549, 12141, 13, 29900, 29889, 29955, 29900, 353, 7197, 3748, 1456, 13, 29900, 29889, 29953, 29900, 353, 319, 19698, 3748, 1456, 13, 29900, 29889, 29946, 29900, 353, 1334, 557, 12141, 13, 29900, 29889, 29906, 29900, 353, 21606, 451, 263, 12141, 13, 29900, 29889, 29900, 29900, 353, 2216, 3748, 1456, 847, 11630, 12560, 13, 13, 11609, 6732, 16786, 445, 4663, 1203, 29892, 3078, 1683, 29901, 13, 29912, 13, 1678, 376, 11802, 1456, 1115, 1565, 29892, 13, 1678, 376, 9961, 345, 1115, 1565, 29892, 13, 1678, 376, 5527, 5084, 1115, 29871, 29900, 29889, 29900, 29892, 13, 1678, 376, 23147, 1115, 376, 12759, 2769, 1090, 29871, 29896, 29906, 3838, 29908, 13, 29913, 13, 13, 29940, 1310, 736, 2791, 3204, 29889, 12391, 5649, 29889, 7106, 4663, 871, 29889, 13, 13, 22933, 5425, 1254, 13566, 26254, 13, 29908, 11802, 1456, 1115, 1565, 29892, 13, 29908, 9961, 345, 1115, 1565, 29892, 13, 29908, 5527, 5084, 1115, 29871, 29900, 29889, 29900, 29892, 13, 29908, 23147, 1115, 376, 1576, 16608, 1510, 263, 9126, 3256, 310, 3748, 1456, 411, 7282, 3158, 322, 20751, 1426, 17809, 1213, 13, 29913], 'total_duration': 12354803800, 'load_duration': 8461390200, 'prompt_eval_count': 3766, 'prompt_eval_duration': 2767918000, 'eval_count': 50, 'eval_duration': 902436000}
helper-1  |
helper-1  | ======================================================================
helper-1  | VISION INFERENCE
helper-1  | Backend : ollama
helper-1  | Model   : llava:13b
helper-1  | Images  : 5
helper-1  | ======================================================================
helper-1  | PARSED: {'model': 'llava:13b', 'created_at': '2026-06-28T20:24:04.9097285Z', 'response': '{"gameplay":true,"approve":true,"confidence":0.0,"reason":"The frames show a continuous moment of gameplay with significant action and reward text detected."}', 'done': True, 'done_reason': 'stop', 'context': [29871, 13, 11889, 29901, 518, 2492, 29899, 29900, 3816, 2492, 29899, 29896, 3816, 2492, 29899, 29906, 3816, 2492, 29899, 29941, 3816, 2492, 29899, 29946, 29962, 13, 3492, 526, 385, 17924, 831, 4011, 12141, 3151, 1061, 29889, 13, 13, 3492, 526, 2183, 29871, 29945, 16608, 4559, 29881, 515, 6732, 29923, 2323, 29871, 29906, 29900, 29899, 7496, 3748, 1456, 20102, 29889, 13, 1576, 16608, 526, 297, 17168, 5996, 1797, 29901, 13, 13, 4308, 29871, 29896, 29871, 3695, 259, 29900, 29995, 29871, 313, 2962, 310, 278, 20102, 29897, 13, 4308, 29871, 29906, 29871, 3695, 29871, 29906, 29945, 29995, 13, 4308, 29871, 29941, 29871, 3695, 29871, 29945, 29900, 29995, 29871, 313, 17662, 29897, 13, 4308, 29871, 29946, 29871, 3695, 29871, 29955, 29945, 29995, 13, 4308, 29871, 29945, 29871, 3695, 29871, 29929, 29945, 29995, 29871, 313, 355, 310, 278, 20102, 29897, 13, 13, 29967, 566, 479, 278, 20102, 408, 6732, 29923, 9126, 3256, 29889, 7523, 278, 410, 11476, 310, 278, 13, 2467, 4822, 278, 29871, 29945, 16608, 2012, 310, 21700, 1269, 1967, 373, 967, 1914, 29889, 13, 13, 1349, 968, 16608, 892, 2307, 758, 29899, 8391, 491, 385, 3345, 630, 12141, 13, 29881, 2650, 428, 16439, 29892, 577, 896, 526, 5517, 313, 4187, 451, 22688, 29897, 8031, 29889, 13, 13, 5634, 20972, 478, 2459, 24352, 11474, 13, 1349, 968, 19435, 526, 5195, 29931, 1299, 18474, 304, 278, 916, 14020, 9335, 567, 17809, 297, 3446, 3235, 13, 9641, 313, 29896, 29889, 29900, 29900, 353, 4549, 342, 4249, 278, 21669, 29892, 29871, 29900, 29889, 29900, 29900, 353, 8062, 342, 467, 2688, 526, 13, 12256, 8380, 11029, 15366, 29892, 577, 7539, 963, 408, 24034, 26085, 29892, 451, 8760, 29889, 13, 13, 29924, 8194, 2522, 487, 584, 29871, 29900, 29889, 29946, 29896, 259, 313, 22925, 5253, 310, 373, 29899, 10525, 10298, 29897, 13, 29979, 29949, 3927, 2522, 487, 259, 584, 29871, 29900, 29889, 29896, 29946, 259, 313, 22925, 1203, 2302, 448, 8062, 7182, 29892, 4482, 7688, 29897, 13, 29949, 11341, 2522, 487, 1678, 584, 29871, 29896, 29889, 29900, 29900, 259, 313, 22925, 20751, 1426, 1316, 408, 17714, 3035, 7068, 2891, 29892, 476, 24071, 29892, 5473, 1783, 18929, 29892, 319, 4741, 29897, 13, 17111, 2522, 487, 29871, 584, 29871, 29900, 29889, 29955, 29906, 259, 313, 22925, 10879, 10083, 29901, 13736, 8696, 29892, 20389, 1080, 29892, 20751, 380, 19936, 448, 6058, 22526, 9963, 29897, 13, 13, 26289, 29901, 13, 29931, 340, 10884, 17809, 29889, 13, 2499, 3242, 694, 3748, 1456, 3618, 17809, 29889, 13, 5015, 549, 20751, 1426, 17809, 29889, 13, 9526, 10879, 10083, 17809, 29889, 13, 13, 5634, 3575, 3414, 11474, 13, 29896, 29889, 3748, 1456, 584, 1565, 6732, 16786, 565, 1438, 16608, 9436, 1510, 1855, 29892, 5735, 4863, 29899, 11802, 13, 259, 3748, 1456, 29889, 3789, 3748, 1456, 353, 2089, 363, 3099, 393, 338, 451, 5735, 3748, 1456, 29892, 13, 259, 3704, 29901, 18811, 275, 4110, 29892, 21955, 272, 847, 2504, 327, 1848, 847, 14982, 11844, 470, 13, 259, 1480, 359, 29892, 25956, 29892, 21950, 29892, 1757, 375, 29892, 8363, 11844, 29892, 8158, 24691, 29892, 14616, 29892, 13, 259, 1856, 11108, 847, 3700, 29899, 11108, 29892, 4628, 11844, 29892, 6323, 14671, 2256, 847, 2294, 16608, 988, 4359, 13, 259, 3078, 3620, 4822, 278, 29871, 29945, 16608, 29889, 13, 29906, 29889, 2134, 345, 29871, 584, 1565, 871, 565, 445, 338, 29120, 262, 873, 12141, 29899, 12554, 29891, 29889, 830, 622, 13, 259, 313, 9961, 345, 353, 2089, 29897, 18811, 275, 4110, 29892, 2504, 359, 29892, 25956, 847, 21950, 29892, 322, 2294, 13, 259, 470, 28132, 19462, 1584, 565, 263, 3748, 1967, 338, 5722, 1711, 7962, 29889, 13, 29941, 29889, 16420, 584, 920, 4549, 445, 12141, 338, 313, 4149, 10754, 2400, 467, 13, 29946, 29889, 4803, 278, 25778, 19435, 408, 20382, 10757, 29901, 13, 259, 448, 960, 278, 7604, 29879, 8661, 411, 278, 19435, 29892, 12020, 596, 16420, 29889, 13, 259, 448, 960, 278, 19435, 1106, 3984, 25369, 313, 29872, 29889, 29887, 29889, 1880, 10884, 541, 3078, 5930, 511, 13, 268, 5224, 596, 16420, 29889, 13, 13, 13696, 391, 3819, 6865, 29901, 13, 29899, 960, 3748, 1456, 338, 2089, 29892, 2134, 345, 341, 17321, 367, 2089, 322, 16420, 341, 17321, 367, 29871, 29900, 29889, 29900, 29900, 29889, 13, 29899, 960, 2134, 345, 338, 2089, 29892, 16420, 341, 17321, 367, 5277, 29871, 29900, 29889, 29941, 29900, 29889, 13, 29899, 960, 278, 29871, 29945, 16608, 1106, 4359, 13557, 313, 1217, 1855, 1735, 511, 7539, 372, 408, 13, 29871, 2294, 29901, 3748, 1456, 353, 2089, 322, 16420, 353, 29871, 29900, 29889, 29900, 29900, 29889, 13, 13, 16376, 5084, 10754, 29901, 13, 29896, 29889, 29900, 29900, 353, 8960, 284, 12141, 13, 29900, 29889, 29929, 29900, 353, 1222, 3729, 296, 3748, 1456, 13, 29900, 29889, 29947, 29900, 353, 3767, 549, 12141, 13, 29900, 29889, 29955, 29900, 353, 7197, 3748, 1456, 13, 29900, 29889, 29953, 29900, 353, 319, 19698, 3748, 1456, 13, 29900, 29889, 29946, 29900, 353, 1334, 557, 12141, 13, 29900, 29889, 29906, 29900, 353, 21606, 451, 263, 12141, 13, 29900, 29889, 29900, 29900, 353, 2216, 3748, 1456, 847, 11630, 12560, 13, 13, 11609, 6732, 16786, 445, 4663, 1203, 29892, 3078, 1683, 29901, 13, 29912, 13, 1678, 376, 11802, 1456, 1115, 1565, 29892, 13, 1678, 376, 9961, 345, 1115, 1565, 29892, 13, 1678, 376, 5527, 5084, 1115, 29871, 29900, 29889, 29900, 29892, 13, 1678, 376, 23147, 1115, 376, 12759, 2769, 1090, 29871, 29896, 29906, 3838, 29908, 13, 29913, 13, 13, 29940, 1310, 736, 2791, 3204, 29889, 12391, 5649, 29889, 7106, 4663, 871, 29889, 13, 13, 22933, 5425, 1254, 13566, 29901, 6377, 11802, 1456, 1115, 3009, 1699, 9961, 345, 1115, 3009, 1699, 5527, 5084, 1115, 29900, 29889, 29900, 1699, 23147, 4710, 1576, 16608, 1510, 263, 9126, 3256, 310, 3748, 1456, 411, 7282, 3158, 322, 20751, 1426, 17809, 1213, 29913], 'total_duration': 1440360700, 'load_duration': 37219400, 'prompt_eval_count': 3766, 'prompt_eval_duration': 543876000, 'eval_count': 39, 'eval_duration': 701524000}
helper-1  |
helper-1  | ======================================================================
helper-1  | VISION INFERENCE
helper-1  | Backend : ollama
helper-1  | Model   : llava:13b
helper-1  | Images  : 5
helper-1  | ======================================================================
helper-1  | PARSED: {'model': 'llava:13b', 'created_at': '2026-06-28T20:24:09.1998357Z', 'response': '{"gameplay":true,"approve":true,"confidence":0.0,"reason":"The frames show a continuous moment of gameplay with significant motion and impact sounds, indicating an engaging and dynamic scene."}', 'done': True, 'done_reason': 'stop', 'context': [29871, 13, 11889, 29901, 518, 2492, 29899, 29900, 3816, 2492, 29899, 29896, 3816, 2492, 29899, 29906, 3816, 2492, 29899, 29941, 3816, 2492, 29899, 29946, 29962, 13, 3492, 526, 385, 17924, 831, 4011, 12141, 3151, 1061, 29889, 13, 13, 3492, 526, 2183, 29871, 29945, 16608, 4559, 29881, 515, 6732, 29923, 2323, 29871, 29906, 29900, 29899, 7496, 3748, 1456, 20102, 29889, 13, 1576, 16608, 526, 297, 17168, 5996, 1797, 29901, 13, 13, 4308, 29871, 29896, 29871, 3695, 259, 29900, 29995, 29871, 313, 2962, 310, 278, 20102, 29897, 13, 4308, 29871, 29906, 29871, 3695, 29871, 29906, 29945, 29995, 13, 4308, 29871, 29941, 29871, 3695, 29871, 29945, 29900, 29995, 29871, 313, 17662, 29897, 13, 4308, 29871, 29946, 29871, 3695, 29871, 29955, 29945, 29995, 13, 4308, 29871, 29945, 29871, 3695, 29871, 29929, 29945, 29995, 29871, 313, 355, 310, 278, 20102, 29897, 13, 13, 29967, 566, 479, 278, 20102, 408, 6732, 29923, 9126, 3256, 29889, 7523, 278, 410, 11476, 310, 278, 13, 2467, 4822, 278, 29871, 29945, 16608, 2012, 310, 21700, 1269, 1967, 373, 967, 1914, 29889, 13, 13, 1349, 968, 16608, 892, 2307, 758, 29899, 8391, 491, 385, 3345, 630, 12141, 13, 29881, 2650, 428, 16439, 29892, 577, 896, 526, 5517, 313, 4187, 451, 22688, 29897, 8031, 29889, 13, 13, 5634, 20972, 478, 2459, 24352, 11474, 13, 1349, 968, 19435, 526, 5195, 29931, 1299, 18474, 304, 278, 916, 14020, 9335, 567, 17809, 297, 3446, 3235, 13, 9641, 313, 29896, 29889, 29900, 29900, 353, 4549, 342, 4249, 278, 21669, 29892, 29871, 29900, 29889, 29900, 29900, 353, 8062, 342, 467, 2688, 526, 13, 12256, 8380, 11029, 15366, 29892, 577, 7539, 963, 408, 24034, 26085, 29892, 451, 8760, 29889, 13, 13, 29924, 8194, 2522, 487, 584, 29871, 29896, 29889, 29900, 29900, 259, 313, 22925, 5253, 310, 373, 29899, 10525, 10298, 29897, 13, 29979, 29949, 3927, 2522, 487, 259, 584, 29871, 29900, 29889, 29906, 29941, 259, 313, 22925, 1203, 2302, 448, 8062, 7182, 29892, 4482, 7688, 29897, 13, 29949, 11341, 2522, 487, 1678, 584, 29871, 29900, 29889, 29900, 29900, 259, 313, 22925, 20751, 1426, 1316, 408, 17714, 3035, 7068, 2891, 29892, 476, 24071, 29892, 5473, 1783, 18929, 29892, 319, 4741, 29897, 13, 17111, 2522, 487, 29871, 584, 29871, 29900, 29889, 29946, 29946, 259, 313, 22925, 10879, 10083, 29901, 13736, 8696, 29892, 20389, 1080, 29892, 20751, 380, 19936, 448, 6058, 22526, 9963, 29897, 13, 13, 26289, 29901, 13, 29963, 708, 1880, 10884, 17809, 29889, 13, 2499, 3242, 694, 3748, 1456, 3618, 17809, 29889, 13, 3782, 20751, 1426, 17809, 29889, 13, 29943, 2365, 10879, 10083, 17809, 29889, 13, 13, 5634, 3575, 3414, 11474, 13, 29896, 29889, 3748, 1456, 584, 1565, 6732, 16786, 565, 1438, 16608, 9436, 1510, 1855, 29892, 5735, 4863, 29899, 11802, 13, 259, 3748, 1456, 29889, 3789, 3748, 1456, 353, 2089, 363, 3099, 393, 338, 451, 5735, 3748, 1456, 29892, 13, 259, 3704, 29901, 18811, 275, 4110, 29892, 21955, 272, 847, 2504, 327, 1848, 847, 14982, 11844, 470, 13, 259, 1480, 359, 29892, 25956, 29892, 21950, 29892, 1757, 375, 29892, 8363, 11844, 29892, 8158, 24691, 29892, 14616, 29892, 13, 259, 1856, 11108, 847, 3700, 29899, 11108, 29892, 4628, 11844, 29892, 6323, 14671, 2256, 847, 2294, 16608, 988, 4359, 13, 259, 3078, 3620, 4822, 278, 29871, 29945, 16608, 29889, 13, 29906, 29889, 2134, 345, 29871, 584, 1565, 871, 565, 445, 338, 29120, 262, 873, 12141, 29899, 12554, 29891, 29889, 830, 622, 13, 259, 313, 9961, 345, 353, 2089, 29897, 18811, 275, 4110, 29892, 2504, 359, 29892, 25956, 847, 21950, 29892, 322, 2294, 13, 259, 470, 28132, 19462, 1584, 565, 263, 3748, 1967, 338, 5722, 1711, 7962, 29889, 13, 29941, 29889, 16420, 584, 920, 4549, 445, 12141, 338, 313, 4149, 10754, 2400, 467, 13, 29946, 29889, 4803, 278, 25778, 19435, 408, 20382, 10757, 29901, 13, 259, 448, 960, 278, 7604, 29879, 8661, 411, 278, 19435, 29892, 12020, 596, 16420, 29889, 13, 259, 448, 960, 278, 19435, 1106, 3984, 25369, 313, 29872, 29889, 29887, 29889, 1880, 10884, 541, 3078, 5930, 511, 13, 268, 5224, 596, 16420, 29889, 13, 13, 13696, 391, 3819, 6865, 29901, 13, 29899, 960, 3748, 1456, 338, 2089, 29892, 2134, 345, 341, 17321, 367, 2089, 322, 16420, 341, 17321, 367, 29871, 29900, 29889, 29900, 29900, 29889, 13, 29899, 960, 2134, 345, 338, 2089, 29892, 16420, 341, 17321, 367, 5277, 29871, 29900, 29889, 29941, 29900, 29889, 13, 29899, 960, 278, 29871, 29945, 16608, 1106, 4359, 13557, 313, 1217, 1855, 1735, 511, 7539, 372, 408, 13, 29871, 2294, 29901, 3748, 1456, 353, 2089, 322, 16420, 353, 29871, 29900, 29889, 29900, 29900, 29889, 13, 13, 16376, 5084, 10754, 29901, 13, 29896, 29889, 29900, 29900, 353, 8960, 284, 12141, 13, 29900, 29889, 29929, 29900, 353, 1222, 3729, 296, 3748, 1456, 13, 29900, 29889, 29947, 29900, 353, 3767, 549, 12141, 13, 29900, 29889, 29955, 29900, 353, 7197, 3748, 1456, 13, 29900, 29889, 29953, 29900, 353, 319, 19698, 3748, 1456, 13, 29900, 29889, 29946, 29900, 353, 1334, 557, 12141, 13, 29900, 29889, 29906, 29900, 353, 21606, 451, 263, 12141, 13, 29900, 29889, 29900, 29900, 353, 2216, 3748, 1456, 847, 11630, 12560, 13, 13, 11609, 6732, 16786, 445, 4663, 1203, 29892, 3078, 1683, 29901, 13, 29912, 13, 1678, 376, 11802, 1456, 1115, 1565, 29892, 13, 1678, 376, 9961, 345, 1115, 1565, 29892, 13, 1678, 376, 5527, 5084, 1115, 29871, 29900, 29889, 29900, 29892, 13, 1678, 376, 23147, 1115, 376, 12759, 2769, 1090, 29871, 29896, 29906, 3838, 29908, 13, 29913, 13, 13, 29940, 1310, 736, 2791, 3204, 29889, 12391, 5649, 29889, 7106, 4663, 871, 29889, 13, 13, 22933, 5425, 1254, 13566, 29901, 6377, 11802, 1456, 1115, 3009, 1699, 9961, 345, 1115, 3009, 1699, 5527, 5084, 1115, 29900, 29889, 29900, 1699, 23147, 4710, 1576, 16608, 1510, 263, 9126, 3256, 310, 3748, 1456, 411, 7282, 10884, 322, 10879, 10083, 29892, 23941, 385, 3033, 6751, 322, 7343, 9088, 1213, 29913], 'total_duration': 4250480100, 'load_duration': 42399900, 'prompt_eval_count': 3767, 'prompt_eval_duration': 2834722000, 'eval_count': 46, 'eval_duration': 827166000}
helper-1  |
helper-1  | ======================================================================
helper-1  | VISION INFERENCE
helper-1  | Backend : ollama
helper-1  | Model   : llava:13b
helper-1  | Images  : 5
helper-1  | ======================================================================
helper-1  | PARSED: {'model': 'llava:13b', 'created_at': '2026-06-28T20:24:13.6117418Z', 'response': '{\n"gameplay": true,\n"approve": true,\n"confidence": 0.0,\n"reason": "Continuous moment of gameplay with significant action and impact sounds."\n}', 'done': True, 'done_reason': 'stop', 'context': [29871, 13, 11889, 29901, 518, 2492, 29899, 29900, 3816, 2492, 29899, 29896, 3816, 2492, 29899, 29906, 3816, 2492, 29899, 29941, 3816, 2492, 29899, 29946, 29962, 13, 3492, 526, 385, 17924, 831, 4011, 12141, 3151, 1061, 29889, 13, 13, 3492, 526, 2183, 29871, 29945, 16608, 4559, 29881, 515, 6732, 29923, 2323, 29871, 29906, 29900, 29899, 7496, 3748, 1456, 20102, 29889, 13, 1576, 16608, 526, 297, 17168, 5996, 1797, 29901, 13, 13, 4308, 29871, 29896, 29871, 3695, 259, 29900, 29995, 29871, 313, 2962, 310, 278, 20102, 29897, 13, 4308, 29871, 29906, 29871, 3695, 29871, 29906, 29945, 29995, 13, 4308, 29871, 29941, 29871, 3695, 29871, 29945, 29900, 29995, 29871, 313, 17662, 29897, 13, 4308, 29871, 29946, 29871, 3695, 29871, 29955, 29945, 29995, 13, 4308, 29871, 29945, 29871, 3695, 29871, 29929, 29945, 29995, 29871, 313, 355, 310, 278, 20102, 29897, 13, 13, 29967, 566, 479, 278, 20102, 408, 6732, 29923, 9126, 3256, 29889, 7523, 278, 410, 11476, 310, 278, 13, 2467, 4822, 278, 29871, 29945, 16608, 2012, 310, 21700, 1269, 1967, 373, 967, 1914, 29889, 13, 13, 1349, 968, 16608, 892, 2307, 758, 29899, 8391, 491, 385, 3345, 630, 12141, 13, 29881, 2650, 428, 16439, 29892, 577, 896, 526, 5517, 313, 4187, 451, 22688, 29897, 8031, 29889, 13, 13, 5634, 20972, 478, 2459, 24352, 11474, 13, 1349, 968, 19435, 526, 5195, 29931, 1299, 18474, 304, 278, 916, 14020, 9335, 567, 17809, 297, 3446, 3235, 13, 9641, 313, 29896, 29889, 29900, 29900, 353, 4549, 342, 4249, 278, 21669, 29892, 29871, 29900, 29889, 29900, 29900, 353, 8062, 342, 467, 2688, 526, 13, 12256, 8380, 11029, 15366, 29892, 577, 7539, 963, 408, 24034, 26085, 29892, 451, 8760, 29889, 13, 13, 29924, 8194, 2522, 487, 584, 29871, 29900, 29889, 29946, 29929, 259, 313, 22925, 5253, 310, 373, 29899, 10525, 10298, 29897, 13, 29979, 29949, 3927, 2522, 487, 259, 584, 29871, 29900, 29889, 29929, 29955, 259, 313, 22925, 1203, 2302, 448, 8062, 7182, 29892, 4482, 7688, 29897, 13, 29949, 11341, 2522, 487, 1678, 584, 29871, 29900, 29889, 29900, 29900, 259, 313, 22925, 20751, 1426, 1316, 408, 17714, 3035, 7068, 2891, 29892, 476, 24071, 29892, 5473, 1783, 18929, 29892, 319, 4741, 29897, 13, 17111, 2522, 487, 29871, 584, 29871, 29896, 29889, 29900, 29900, 259, 313, 22925, 10879, 10083, 29901, 13736, 8696, 29892, 20389, 1080, 29892, 20751, 380, 19936, 448, 6058, 22526, 9963, 29897, 13, 13, 26289, 29901, 13, 29931, 340, 10884, 17809, 29889, 13, 14804, 3748, 1456, 3618, 17809, 29889, 13, 3782, 20751, 1426, 17809, 29889, 13, 5015, 549, 10879, 10083, 17809, 29889, 13, 13, 5634, 3575, 3414, 11474, 13, 29896, 29889, 3748, 1456, 584, 1565, 6732, 16786, 565, 1438, 16608, 9436, 1510, 1855, 29892, 5735, 4863, 29899, 11802, 13, 259, 3748, 1456, 29889, 3789, 3748, 1456, 353, 2089, 363, 3099, 393, 338, 451, 5735, 3748, 1456, 29892, 13, 259, 3704, 29901, 18811, 275, 4110, 29892, 21955, 272, 847, 2504, 327, 1848, 847, 14982, 11844, 470, 13, 259, 1480, 359, 29892, 25956, 29892, 21950, 29892, 1757, 375, 29892, 8363, 11844, 29892, 8158, 24691, 29892, 14616, 29892, 13, 259, 1856, 11108, 847, 3700, 29899, 11108, 29892, 4628, 11844, 29892, 6323, 14671, 2256, 847, 2294, 16608, 988, 4359, 13, 259, 3078, 3620, 4822, 278, 29871, 29945, 16608, 29889, 13, 29906, 29889, 2134, 345, 29871, 584, 1565, 871, 565, 445, 338, 29120, 262, 873, 12141, 29899, 12554, 29891, 29889, 830, 622, 13, 259, 313, 9961, 345, 353, 2089, 29897, 18811, 275, 4110, 29892, 2504, 359, 29892, 25956, 847, 21950, 29892, 322, 2294, 13, 259, 470, 28132, 19462, 1584, 565, 263, 3748, 1967, 338, 5722, 1711, 7962, 29889, 13, 29941, 29889, 16420, 584, 920, 4549, 445, 12141, 338, 313, 4149, 10754, 2400, 467, 13, 29946, 29889, 4803, 278, 25778, 19435, 408, 20382, 10757, 29901, 13, 259, 448, 960, 278, 7604, 29879, 8661, 411, 278, 19435, 29892, 12020, 596, 16420, 29889, 13, 259, 448, 960, 278, 19435, 1106, 3984, 25369, 313, 29872, 29889, 29887, 29889, 1880, 10884, 541, 3078, 5930, 511, 13, 268, 5224, 596, 16420, 29889, 13, 13, 13696, 391, 3819, 6865, 29901, 13, 29899, 960, 3748, 1456, 338, 2089, 29892, 2134, 345, 341, 17321, 367, 2089, 322, 16420, 341, 17321, 367, 29871, 29900, 29889, 29900, 29900, 29889, 13, 29899, 960, 2134, 345, 338, 2089, 29892, 16420, 341, 17321, 367, 5277, 29871, 29900, 29889, 29941, 29900, 29889, 13, 29899, 960, 278, 29871, 29945, 16608, 1106, 4359, 13557, 313, 1217, 1855, 1735, 511, 7539, 372, 408, 13, 29871, 2294, 29901, 3748, 1456, 353, 2089, 322, 16420, 353, 29871, 29900, 29889, 29900, 29900, 29889, 13, 13, 16376, 5084, 10754, 29901, 13, 29896, 29889, 29900, 29900, 353, 8960, 284, 12141, 13, 29900, 29889, 29929, 29900, 353, 1222, 3729, 296, 3748, 1456, 13, 29900, 29889, 29947, 29900, 353, 3767, 549, 12141, 13, 29900, 29889, 29955, 29900, 353, 7197, 3748, 1456, 13, 29900, 29889, 29953, 29900, 353, 319, 19698, 3748, 1456, 13, 29900, 29889, 29946, 29900, 353, 1334, 557, 12141, 13, 29900, 29889, 29906, 29900, 353, 21606, 451, 263, 12141, 13, 29900, 29889, 29900, 29900, 353, 2216, 3748, 1456, 847, 11630, 12560, 13, 13, 11609, 6732, 16786, 445, 4663, 1203, 29892, 3078, 1683, 29901, 13, 29912, 13, 1678, 376, 11802, 1456, 1115, 1565, 29892, 13, 1678, 376, 9961, 345, 1115, 1565, 29892, 13, 1678, 376, 5527, 5084, 1115, 29871, 29900, 29889, 29900, 29892, 13, 1678, 376, 23147, 1115, 376, 12759, 2769, 1090, 29871, 29896, 29906, 3838, 29908, 13, 29913, 13, 13, 29940, 1310, 736, 2791, 3204, 29889, 12391, 5649, 29889, 7106, 4663, 871, 29889, 13, 13, 22933, 5425, 1254, 13566, 26254, 13, 29908, 11802, 1456, 1115, 1565, 29892, 13, 29908, 9961, 345, 1115, 1565, 29892, 13, 29908, 5527, 5084, 1115, 29871, 29900, 29889, 29900, 29892, 13, 29908, 23147, 1115, 376, 1323, 8675, 681, 3256, 310, 3748, 1456, 411, 7282, 3158, 322, 10879, 10083, 1213, 13, 29913], 'total_duration': 4373991800, 'load_duration': 35605700, 'prompt_eval_count': 3764, 'prompt_eval_duration': 2849790000, 'eval_count': 47, 'eval_duration': 850171000}
helper-1  |
helper-1  | ======================================================================
helper-1  | VISION INFERENCE
helper-1  | Backend : ollama
helper-1  | Model   : llava:13b
helper-1  | Images  : 5
helper-1  | ======================================================================
helper-1  | PARSED: {'model': 'llava:13b', 'created_at': '2026-06-28T20:24:18.2552967Z', 'response': '{\n"gameplay": true,\n"approve": true,\n"confidence": 0.80,\n"reason": "High motion detected in frames with gameplay elements."\n}', 'done': True, 'done_reason': 'stop', 'context': [29871, 13, 11889, 29901, 518, 2492, 29899, 29900, 3816, 2492, 29899, 29896, 3816, 2492, 29899, 29906, 3816, 2492, 29899, 29941, 3816, 2492, 29899, 29946, 29962, 13, 3492, 526, 385, 17924, 831, 4011, 12141, 3151, 1061, 29889, 13, 13, 3492, 526, 2183, 29871, 29945, 16608, 4559, 29881, 515, 6732, 29923, 2323, 29871, 29906, 29900, 29899, 7496, 3748, 1456, 20102, 29889, 13, 1576, 16608, 526, 297, 17168, 5996, 1797, 29901, 13, 13, 4308, 29871, 29896, 29871, 3695, 259, 29900, 29995, 29871, 313, 2962, 310, 278, 20102, 29897, 13, 4308, 29871, 29906, 29871, 3695, 29871, 29906, 29945, 29995, 13, 4308, 29871, 29941, 29871, 3695, 29871, 29945, 29900, 29995, 29871, 313, 17662, 29897, 13, 4308, 29871, 29946, 29871, 3695, 29871, 29955, 29945, 29995, 13, 4308, 29871, 29945, 29871, 3695, 29871, 29929, 29945, 29995, 29871, 313, 355, 310, 278, 20102, 29897, 13, 13, 29967, 566, 479, 278, 20102, 408, 6732, 29923, 9126, 3256, 29889, 7523, 278, 410, 11476, 310, 278, 13, 2467, 4822, 278, 29871, 29945, 16608, 2012, 310, 21700, 1269, 1967, 373, 967, 1914, 29889, 13, 13, 1349, 968, 16608, 892, 2307, 758, 29899, 8391, 491, 385, 3345, 630, 12141, 13, 29881, 2650, 428, 16439, 29892, 577, 896, 526, 5517, 313, 4187, 451, 22688, 29897, 8031, 29889, 13, 13, 5634, 20972, 478, 2459, 24352, 11474, 13, 1349, 968, 19435, 526, 5195, 29931, 1299, 18474, 304, 278, 916, 14020, 9335, 567, 17809, 297, 3446, 3235, 13, 9641, 313, 29896, 29889, 29900, 29900, 353, 4549, 342, 4249, 278, 21669, 29892, 29871, 29900, 29889, 29900, 29900, 353, 8062, 342, 467, 2688, 526, 13, 12256, 8380, 11029, 15366, 29892, 577, 7539, 963, 408, 24034, 26085, 29892, 451, 8760, 29889, 13, 13, 29924, 8194, 2522, 487, 584, 29871, 29900, 29889, 29947, 29906, 259, 313, 22925, 5253, 310, 373, 29899, 10525, 10298, 29897, 13, 29979, 29949, 3927, 2522, 487, 259, 584, 29871, 29900, 29889, 29946, 29900, 259, 313, 22925, 1203, 2302, 448, 8062, 7182, 29892, 4482, 7688, 29897, 13, 29949, 11341, 2522, 487, 1678, 584, 29871, 29900, 29889, 29900, 29900, 259, 313, 22925, 20751, 1426, 1316, 408, 17714, 3035, 7068, 2891, 29892, 476, 24071, 29892, 5473, 1783, 18929, 29892, 319, 4741, 29897, 13, 17111, 2522, 487, 29871, 584, 29871, 29900, 29889, 29946, 29946, 259, 313, 22925, 10879, 10083, 29901, 13736, 8696, 29892, 20389, 1080, 29892, 20751, 380, 19936, 448, 6058, 22526, 9963, 29897, 13, 13, 26289, 29901, 13, 29963, 708, 1880, 10884, 17809, 29889, 13, 29943, 809, 3748, 1456, 3618, 17809, 29889, 13, 3782, 20751, 1426, 17809, 29889, 13, 29943, 2365, 10879, 10083, 17809, 29889, 13, 13, 5634, 3575, 3414, 11474, 13, 29896, 29889, 3748, 1456, 584, 1565, 6732, 16786, 565, 1438, 16608, 9436, 1510, 1855, 29892, 5735, 4863, 29899, 11802, 13, 259, 3748, 1456, 29889, 3789, 3748, 1456, 353, 2089, 363, 3099, 393, 338, 451, 5735, 3748, 1456, 29892, 13, 259, 3704, 29901, 18811, 275, 4110, 29892, 21955, 272, 847, 2504, 327, 1848, 847, 14982, 11844, 470, 13, 259, 1480, 359, 29892, 25956, 29892, 21950, 29892, 1757, 375, 29892, 8363, 11844, 29892, 8158, 24691, 29892, 14616, 29892, 13, 259, 1856, 11108, 847, 3700, 29899, 11108, 29892, 4628, 11844, 29892, 6323, 14671, 2256, 847, 2294, 16608, 988, 4359, 13, 259, 3078, 3620, 4822, 278, 29871, 29945, 16608, 29889, 13, 29906, 29889, 2134, 345, 29871, 584, 1565, 871, 565, 445, 338, 29120, 262, 873, 12141, 29899, 12554, 29891, 29889, 830, 622, 13, 259, 313, 9961, 345, 353, 2089, 29897, 18811, 275, 4110, 29892, 2504, 359, 29892, 25956, 847, 21950, 29892, 322, 2294, 13, 259, 470, 28132, 19462, 1584, 565, 263, 3748, 1967, 338, 5722, 1711, 7962, 29889, 13, 29941, 29889, 16420, 584, 920, 4549, 445, 12141, 338, 313, 4149, 10754, 2400, 467, 13, 29946, 29889, 4803, 278, 25778, 19435, 408, 20382, 10757, 29901, 13, 259, 448, 960, 278, 7604, 29879, 8661, 411, 278, 19435, 29892, 12020, 596, 16420, 29889, 13, 259, 448, 960, 278, 19435, 1106, 3984, 25369, 313, 29872, 29889, 29887, 29889, 1880, 10884, 541, 3078, 5930, 511, 13, 268, 5224, 596, 16420, 29889, 13, 13, 13696, 391, 3819, 6865, 29901, 13, 29899, 960, 3748, 1456, 338, 2089, 29892, 2134, 345, 341, 17321, 367, 2089, 322, 16420, 341, 17321, 367, 29871, 29900, 29889, 29900, 29900, 29889, 13, 29899, 960, 2134, 345, 338, 2089, 29892, 16420, 341, 17321, 367, 5277, 29871, 29900, 29889, 29941, 29900, 29889, 13, 29899, 960, 278, 29871, 29945, 16608, 1106, 4359, 13557, 313, 1217, 1855, 1735, 511, 7539, 372, 408, 13, 29871, 2294, 29901, 3748, 1456, 353, 2089, 322, 16420, 353, 29871, 29900, 29889, 29900, 29900, 29889, 13, 13, 16376, 5084, 10754, 29901, 13, 29896, 29889, 29900, 29900, 353, 8960, 284, 12141, 13, 29900, 29889, 29929, 29900, 353, 1222, 3729, 296, 3748, 1456, 13, 29900, 29889, 29947, 29900, 353, 3767, 549, 12141, 13, 29900, 29889, 29955, 29900, 353, 7197, 3748, 1456, 13, 29900, 29889, 29953, 29900, 353, 319, 19698, 3748, 1456, 13, 29900, 29889, 29946, 29900, 353, 1334, 557, 12141, 13, 29900, 29889, 29906, 29900, 353, 21606, 451, 263, 12141, 13, 29900, 29889, 29900, 29900, 353, 2216, 3748, 1456, 847, 11630, 12560, 13, 13, 11609, 6732, 16786, 445, 4663, 1203, 29892, 3078, 1683, 29901, 13, 29912, 13, 1678, 376, 11802, 1456, 1115, 1565, 29892, 13, 1678, 376, 9961, 345, 1115, 1565, 29892, 13, 1678, 376, 5527, 5084, 1115, 29871, 29900, 29889, 29900, 29892, 13, 1678, 376, 23147, 1115, 376, 12759, 2769, 1090, 29871, 29896, 29906, 3838, 29908, 13, 29913, 13, 13, 29940, 1310, 736, 2791, 3204, 29889, 12391, 5649, 29889, 7106, 4663, 871, 29889, 13, 13, 22933, 5425, 1254, 13566, 26254, 13, 29908, 11802, 1456, 1115, 1565, 29892, 13, 29908, 9961, 345, 1115, 1565, 29892, 13, 29908, 5527, 5084, 1115, 29871, 29900, 29889, 29947, 29900, 29892, 13, 29908, 23147, 1115, 376, 16382, 10884, 17809, 297, 16608, 411, 3748, 1456, 3161, 1213, 13, 29913], 'total_duration': 4605926600, 'load_duration': 37315500, 'prompt_eval_count': 3766, 'prompt_eval_duration': 2875407000, 'eval_count': 44, 'eval_duration': 790824000}
helper-1  |
helper-1  | ======================================================================
helper-1  | VISION INFERENCE
helper-1  | Backend : ollama
helper-1  | Model   : llava:13b
helper-1  | Images  : 5
helper-1  | ======================================================================
helper-1  | PARSED: {'model': 'llava:13b', 'created_at': '2026-06-28T20:24:22.6859723Z', 'response': '{"gameplay":true,"approve":true,"confidence":0.0,"reason":"The frames show a continuous moment of gameplay with significant motion and action, indicating that it is live video-game gameplay."}', 'done': True, 'done_reason': 'stop', 'context': [29871, 13, 11889, 29901, 518, 2492, 29899, 29900, 3816, 2492, 29899, 29896, 3816, 2492, 29899, 29906, 3816, 2492, 29899, 29941, 3816, 2492, 29899, 29946, 29962, 13, 3492, 526, 385, 17924, 831, 4011, 12141, 3151, 1061, 29889, 13, 13, 3492, 526, 2183, 29871, 29945, 16608, 4559, 29881, 515, 6732, 29923, 2323, 29871, 29906, 29900, 29899, 7496, 3748, 1456, 20102, 29889, 13, 1576, 16608, 526, 297, 17168, 5996, 1797, 29901, 13, 13, 4308, 29871, 29896, 29871, 3695, 259, 29900, 29995, 29871, 313, 2962, 310, 278, 20102, 29897, 13, 4308, 29871, 29906, 29871, 3695, 29871, 29906, 29945, 29995, 13, 4308, 29871, 29941, 29871, 3695, 29871, 29945, 29900, 29995, 29871, 313, 17662, 29897, 13, 4308, 29871, 29946, 29871, 3695, 29871, 29955, 29945, 29995, 13, 4308, 29871, 29945, 29871, 3695, 29871, 29929, 29945, 29995, 29871, 313, 355, 310, 278, 20102, 29897, 13, 13, 29967, 566, 479, 278, 20102, 408, 6732, 29923, 9126, 3256, 29889, 7523, 278, 410, 11476, 310, 278, 13, 2467, 4822, 278, 29871, 29945, 16608, 2012, 310, 21700, 1269, 1967, 373, 967, 1914, 29889, 13, 13, 1349, 968, 16608, 892, 2307, 758, 29899, 8391, 491, 385, 3345, 630, 12141, 13, 29881, 2650, 428, 16439, 29892, 577, 896, 526, 5517, 313, 4187, 451, 22688, 29897, 8031, 29889, 13, 13, 5634, 20972, 478, 2459, 24352, 11474, 13, 1349, 968, 19435, 526, 5195, 29931, 1299, 18474, 304, 278, 916, 14020, 9335, 567, 17809, 297, 3446, 3235, 13, 9641, 313, 29896, 29889, 29900, 29900, 353, 4549, 342, 4249, 278, 21669, 29892, 29871, 29900, 29889, 29900, 29900, 353, 8062, 342, 467, 2688, 526, 13, 12256, 8380, 11029, 15366, 29892, 577, 7539, 963, 408, 24034, 26085, 29892, 451, 8760, 29889, 13, 13, 29924, 8194, 2522, 487, 584, 29871, 29900, 29889, 29929, 29955, 259, 313, 22925, 5253, 310, 373, 29899, 10525, 10298, 29897, 13, 29979, 29949, 3927, 2522, 487, 259, 584, 29871, 29900, 29889, 29941, 29929, 259, 313, 22925, 1203, 2302, 448, 8062, 7182, 29892, 4482, 7688, 29897, 13, 29949, 11341, 2522, 487, 1678, 584, 29871, 29900, 29889, 29900, 29900, 259, 313, 22925, 20751, 1426, 1316, 408, 17714, 3035, 7068, 2891, 29892, 476, 24071, 29892, 5473, 1783, 18929, 29892, 319, 4741, 29897, 13, 17111, 2522, 487, 29871, 584, 29871, 29900, 29889, 29900, 29900, 259, 313, 22925, 10879, 10083, 29901, 13736, 8696, 29892, 20389, 1080, 29892, 20751, 380, 19936, 448, 6058, 22526, 9963, 29897, 13, 13, 26289, 29901, 13, 29963, 708, 1880, 10884, 17809, 29889, 13, 29943, 809, 3748, 1456, 3618, 17809, 29889, 13, 3782, 20751, 1426, 17809, 29889, 13, 3782, 18697, 10879, 10083, 17809, 29889, 13, 13, 5634, 3575, 3414, 11474, 13, 29896, 29889, 3748, 1456, 584, 1565, 6732, 16786, 565, 1438, 16608, 9436, 1510, 1855, 29892, 5735, 4863, 29899, 11802, 13, 259, 3748, 1456, 29889, 3789, 3748, 1456, 353, 2089, 363, 3099, 393, 338, 451, 5735, 3748, 1456, 29892, 13, 259, 3704, 29901, 18811, 275, 4110, 29892, 21955, 272, 847, 2504, 327, 1848, 847, 14982, 11844, 470, 13, 259, 1480, 359, 29892, 25956, 29892, 21950, 29892, 1757, 375, 29892, 8363, 11844, 29892, 8158, 24691, 29892, 14616, 29892, 13, 259, 1856, 11108, 847, 3700, 29899, 11108, 29892, 4628, 11844, 29892, 6323, 14671, 2256, 847, 2294, 16608, 988, 4359, 13, 259, 3078, 3620, 4822, 278, 29871, 29945, 16608, 29889, 13, 29906, 29889, 2134, 345, 29871, 584, 1565, 871, 565, 445, 338, 29120, 262, 873, 12141, 29899, 12554, 29891, 29889, 830, 622, 13, 259, 313, 9961, 345, 353, 2089, 29897, 18811, 275, 4110, 29892, 2504, 359, 29892, 25956, 847, 21950, 29892, 322, 2294, 13, 259, 470, 28132, 19462, 1584, 565, 263, 3748, 1967, 338, 5722, 1711, 7962, 29889, 13, 29941, 29889, 16420, 584, 920, 4549, 445, 12141, 338, 313, 4149, 10754, 2400, 467, 13, 29946, 29889, 4803, 278, 25778, 19435, 408, 20382, 10757, 29901, 13, 259, 448, 960, 278, 7604, 29879, 8661, 411, 278, 19435, 29892, 12020, 596, 16420, 29889, 13, 259, 448, 960, 278, 19435, 1106, 3984, 25369, 313, 29872, 29889, 29887, 29889, 1880, 10884, 541, 3078, 5930, 511, 13, 268, 5224, 596, 16420, 29889, 13, 13, 13696, 391, 3819, 6865, 29901, 13, 29899, 960, 3748, 1456, 338, 2089, 29892, 2134, 345, 341, 17321, 367, 2089, 322, 16420, 341, 17321, 367, 29871, 29900, 29889, 29900, 29900, 29889, 13, 29899, 960, 2134, 345, 338, 2089, 29892, 16420, 341, 17321, 367, 5277, 29871, 29900, 29889, 29941, 29900, 29889, 13, 29899, 960, 278, 29871, 29945, 16608, 1106, 4359, 13557, 313, 1217, 1855, 1735, 511, 7539, 372, 408, 13, 29871, 2294, 29901, 3748, 1456, 353, 2089, 322, 16420, 353, 29871, 29900, 29889, 29900, 29900, 29889, 13, 13, 16376, 5084, 10754, 29901, 13, 29896, 29889, 29900, 29900, 353, 8960, 284, 12141, 13, 29900, 29889, 29929, 29900, 353, 1222, 3729, 296, 3748, 1456, 13, 29900, 29889, 29947, 29900, 353, 3767, 549, 12141, 13, 29900, 29889, 29955, 29900, 353, 7197, 3748, 1456, 13, 29900, 29889, 29953, 29900, 353, 319, 19698, 3748, 1456, 13, 29900, 29889, 29946, 29900, 353, 1334, 557, 12141, 13, 29900, 29889, 29906, 29900, 353, 21606, 451, 263, 12141, 13, 29900, 29889, 29900, 29900, 353, 2216, 3748, 1456, 847, 11630, 12560, 13, 13, 11609, 6732, 16786, 445, 4663, 1203, 29892, 3078, 1683, 29901, 13, 29912, 13, 1678, 376, 11802, 1456, 1115, 1565, 29892, 13, 1678, 376, 9961, 345, 1115, 1565, 29892, 13, 1678, 376, 5527, 5084, 1115, 29871, 29900, 29889, 29900, 29892, 13, 1678, 376, 23147, 1115, 376, 12759, 2769, 1090, 29871, 29896, 29906, 3838, 29908, 13, 29913, 13, 13, 29940, 1310, 736, 2791, 3204, 29889, 12391, 5649, 29889, 7106, 4663, 871, 29889, 13, 13, 22933, 5425, 1254, 13566, 29901, 6377, 11802, 1456, 1115, 3009, 1699, 9961, 345, 1115, 3009, 1699, 5527, 5084, 1115, 29900, 29889, 29900, 1699, 23147, 4710, 1576, 16608, 1510, 263, 9126, 3256, 310, 3748, 1456, 411, 7282, 10884, 322, 3158, 29892, 23941, 393, 372, 338, 5735, 4863, 29899, 11802, 3748, 1456, 1213, 29913], 'total_duration': 4390677700, 'load_duration': 43074300, 'prompt_eval_count': 3766, 'prompt_eval_duration': 2854394000, 'eval_count': 48, 'eval_duration': 866404000}
helper-1  |
helper-1  | ===== FINAL RANKING =====
helper-1  | 1. Time=331.5s | Motion=0.82 | YOLO=0.40 | OCR=0.00 | Audio=0.44 | Gameplay=True Approve=True Confidence=0.80 Final=0.39 Reason=High motion detected in frames with gameplay elements.
helper-1  | 2. Time=5.8s | Motion=0.45 | YOLO=0.19 | OCR=1.00 | Audio=0.72 | Gameplay=True Approve=True Confidence=0.00 Final=0.33 Reason=The frames show a continuous moment of gameplay with significant action and reward text detected.
helper-1  | 3. Time=29.6s | Motion=0.49 | YOLO=0.97 | OCR=0.00 | Audio=1.00 | Gameplay=True Approve=True Confidence=0.00 Final=0.22 Reason=Continuous moment of gameplay with significant action and impact sounds.
helper-1  | 4. Time=200.0s | Motion=0.97 | YOLO=0.39 | OCR=0.00 | Audio=0.00 | Gameplay=True Approve=True Confidence=0.00 Final=0.21 Reason=The frames show a continuous moment of gameplay with significant motion and action, indicating that it is live video-game gameplay.
helper-1  | INFO:     172.18.0.4:45094 - "POST /candidates HTTP/1.1" 200 OK
PS C:\Temp\gameplay-autopost>