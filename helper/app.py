import os, json, subprocess
import shutil, glob, time
import re, base64, requests

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

print("Loading YOLO...", flush=True)
YOLO_MODEL = YOLO("yolov8n.pt")
print("YOLO loaded.", flush=True)

print("Loading EasyOCR...", flush=True)

ocr = easyocr.Reader(
    ['en'],
    gpu=False
)

print("EasyOCR loaded.", flush=True)

def _loudness_curve(full):
    """Return (times[], momentary_LUFS[]) using ffmpeg's ebur128 meter."""
    cmd = ["ffmpeg", "-nostats", "-i", full, "-af", "ebur128=metadata=1", "-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    times, vals = [], []
    for line in p.stderr.splitlines():
        m = re.search(r"t:\s*([0-9.]+).*?M:\s*(-?[0-9.]+)", line)
        if m:
            times.append(float(m.group(1)))
            vals.append(float(m.group(2)))
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

def _extract_frame(full, t, out_path):
    subprocess.run(["ffmpeg", "-y", "-ss", str(t), "-i", full,
                    "-frames:v", "1", "-q:v", "3", out_path], capture_output=True)

def yolo_score(frame_path):

    results = YOLO_MODEL(frame_path, verbose=False)

    result = results[0]

    boxes = result.boxes

    if boxes is None:
        return 0

    score = 0

    for conf in boxes.conf.tolist():
        score += conf

    return score

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

def _vision_score_ollama(frame_paths):
    try:
        images = []

        for frame in frame_paths:
            with open(frame, "rb") as f:
                images.append(
                    base64.b64encode(f.read()).decode()
                )
        prompt = """
        You are reviewing 5 images sampled chronologically from the SAME 15-second video clip.

        Your task is to determine:

        1. Is this clip primarily REAL GAMEPLAY?
        2. If it is gameplay, how exciting would it be as a YouTube Shorts or TikTok gaming highlight?

        GAMEPLAY includes:
        - player controlling a character
        - combat
        - enemies
        - weapons
        - boss fights
        - racing
        - platforming
        - exploration

        NOT GAMEPLAY includes:
        - advertisements
        - sponsor messages
        - promotional videos
        - game trailers
        - cinematic cutscenes
        - loading screens
        - menus
        - inventory screens
        - settings screens
        - static logos
        - title screens
        - streamer webcam only
        - intermission screens
        - overlays without actual gameplay

        The 5 images represent the SAME clip.
        Judge the ENTIRE clip, not an individual image.

        When scoring gameplay, prioritize clips that contain:
        - firefights
        - kills or eliminations
        - bosses
        - explosions
        - intense movement
        - close calls
        - clutch moments
        - visually exciting action

        Avoid giving high scores to:
        - walking
        - looting
        - waiting
        - idle gameplay
        - menus
        - advertisements
        - static scenes
        
        Assume the goal is to maximize viewer retention on YouTube Shorts and TikTok. Prefer clips that would make a viewer stop scrolling and continue watching.

        Scoring:

        10 = Incredible highlight, instantly shareable
        9 = Outstanding action
        8 = Intense combat
        7 = Good action
        6 = Decent gameplay
        5 = Average gameplay
        4 = Mostly slow gameplay
        3 = Very little action
        2 = Barely gameplay
        1 = Idle or uninteresting gameplay

        If the clip is NOT gameplay:

        {
            "gameplay": false,
            "score": 0,
            "reason": "short reason"
        }

        If the clip IS gameplay:

        {
            "gameplay": true,
            "score": <integer 1-10>,
            "reason": "<maximum 8 words>"
        }

        Return ONLY valid JSON.
        Do not include markdown.
        Do not include explanations.
        Do not output any text outside the JSON.
        """

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
            "format": "json"
        },
        timeout=180
        )
        #print("After requests.post()", flush=True)
        #print("STATUS:", r.status_code, flush=True)
        #print("RAW:", r.text, flush=True)

        resp = r.json()
        print("PARSED:", resp, flush=True)

        data = json.loads(resp.get("response", "{}"))

        gameplay = str(
            data.get("gameplay", True)
        ).lower() == "true"
        score = float(data.get("score", 0))
        reason = str(data.get("reason", ""))
        return gameplay, score, reason
    except Exception as e:
        return True, 0, f"score-failed:{e}"


def _vision_score(frame_paths):

    print("\n" + "=" * 70, flush=True)
    print("VISION INFERENCE", flush=True)
    print(f"Backend : {VISION_BACKEND}", flush=True)
    print(f"Model   : {VISION_MODEL}", flush=True)
    print(f"Images  : {len(frame_paths)}", flush=True)
    print("=" * 70, flush=True)

    if VISION_BACKEND == "ollama":
        return _vision_score_ollama(frame_paths)

    elif VISION_BACKEND == "lmstudio":
        return _vision_score_lmstudio(frame_paths)

    raise ValueError(
        f"Unknown vision backend: {VISION_BACKEND}"
    )

def _vision_score_lmstudio(frame_paths):

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

        prompt = """
        (YOUR CURRENT PROMPT HERE)
        """

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

        gameplay = str(
            data.get("gameplay", True)
        ).lower() == "true"

        score = float(
            data.get("score", 0)
        )

        reason = data.get(
            "reason",
            ""
        )

        return gameplay, score, reason

    except Exception as e:

        return True, 0, f"lmstudio-failed:{e}"

class CandIn(BaseModel):
    path: str
    jobId: str
    clipLen: float = 15.0
    count: int = 4
    minGap: float = 8.0

class RenderClip(BaseModel):
    start: float
    end: float


class RenderIn(BaseModel):
    path: str
    jobId: str
    clips: list[RenderClip]

def detect_scenes(full):

    cmd = [
        "ffmpeg",
        "-i", full,
        "-filter:v",
        "select='gt(scene,0.30)',showinfo",
        "-vsync", "0",
        "-f", "null",
        "-"
    ]

    p = subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )

    times = []

    for line in p.stderr.splitlines():

        m = re.search(r"pts_time:([0-9.]+)", line)

        if m:
            times.append(float(m.group(1)))

    return times

def ocr_score(image_path):

    results = ocr.readtext(image_path)

    text = " ".join(
        r[1]
        for r in results
    ).upper()

    score = 0

    keywords = [
        "HEADSHOT",
        "DOUBLE",
        "TRIPLE",
        "QUAD",
        "ACE",
        "KILL",
        "ELIMINATION",
        "VICTORY",
        "WIN",
        "DOWNED",
        "XP",
        "+100",
        "+250",
        "+500"
    ]

    for word in keywords:
        if word in text:
            score += 1

    return score, text

def sample_video(duration, interval=2.0):
    times = []

    t = interval

    while t < duration:
        times.append(round(t, 2))
        t += interval

    return times

def render_clip(source, start, end, output):

    duration = end - start

    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss", str(start),
            "-i", source,
            "-t", str(duration),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-c:a", "aac",
            output
        ],
        capture_output=True,
        text=True
    )

    print("=" * 80, flush=True)
    print("FFMPEG RETURN:", result.returncode, flush=True)
    print("OUTPUT FILE:", output, flush=True)
    print("STDERR:", result.stderr, flush=True)
    print("=" * 80, flush=True)

@app.post("/candidates")
def candidates(inp: CandIn):
    full = os.path.join(MEDIA, inp.path)
    dur = probe(ProbeIn(path=inp.path))["duration"]

    #print("INPUT PATH:", repr(inp.path))
    #info = probe(ProbeIn(path=inp.path))
    #import sys

    #print("=" * 80, flush=True)
    #print(f"INPUT PATH: {repr(inp.path)}", flush=True)
    #print(f"PROBE RESULT: {repr(info)}", flush=True)
    #print("=" * 80, flush=True)
    #sys.stdout.flush()
    #dur = info["duration"]

    sample_times = detect_scenes(full)

    print(
        f"Detected {len(sample_times)} scene changes",
        flush=True
    )

    # Fallback if scene detection found too few scenes
    if len(sample_times) < 20:
        print(
            "Too few scene changes. Falling back to 2-second sampling.",
            flush=True
        )

        sample_times = sample_video(
            dur,
            interval=2.0
        )

    print(
        f"Sampling {len(sample_times)} frames",
        flush=True
    )

    frames_dir = os.path.join(
        MEDIA,
        "work",
        inp.jobId,
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
            "idx": i, "time": t, "start": start, "end": end, "frame":fp,
            "frame_rel": f"work/{inp.jobId}/frames/cand_{i}.jpg"
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
        
        yolo = yolo_score(curr["frame"])

        motion_frames.append({
            "idx": curr["idx"], "time": curr["time"], 
            "start": curr["start"], "end": curr["end"], 
            "frame": curr["frame"], "frame_rel": curr["frame_rel"],
            "motion": motion, "yolo": yolo
        })
    
    motion_frames.sort(
    key=lambda x: x["motion"],
    reverse=True
    )

    max_possible = int(dur / inp.clipLen)

    candidate_count = max(
        2,
        min(12, max_possible // 2)
    )

    print(
        f"Selecting top {candidate_count} candidates",
        flush=True
    )

    interesting = motion_frames[:candidate_count]

    for frame in interesting:
        print(
            "Motion:",
            round(frame["motion"], 2),
            "YOLO:",
            round(frame["yolo"], 2),
            flush=True
        )

    print(f"Scoring {len(interesting)} frames with OCR only", flush=True)

    scored = []
    for frame in interesting:

        clip_dir = os.path.join(
            MEDIA,
            "work",
            inp.jobId,
            f"clip_{frame['idx']}"
        )

        clip_frames = _extract_clip_frames(
            full,
            frame["start"],
            frame["end"],
            clip_dir
        )

        pts, txt = ocr_score(clip_frames[2])

        frame["ocr"] = pts
        frame["ocr_text"] = txt

        print(
            "OCR:",
            frame["ocr"],
            "|",
            frame["ocr_text"][:120],
            flush=True
        )

        frame["vision_score"] = 0
        frame["reason"] = ""

        scored.append(frame)

    motions = [f["motion"] for f in scored]
    min_motion = min(motions)
    max_motion = max(motions)

    for frame in scored:

        if max_motion == min_motion:
            frame["motion_norm"] = 0.5
        else:
            frame["motion_norm"] = (
                frame["motion"] - min_motion
            ) / (max_motion - min_motion)   

    for frame in scored:
        print(
            frame["motion"],
            "->",
            round(frame["motion_norm"], 3),
            flush=True
        ) 

    for frame in scored:

        frame["final_score"] = (
            frame["motion_norm"] * 4
            + frame["yolo"] * 2
            + frame["ocr"] * 1
        )

    scored.sort(
        key=lambda x: x["final_score"],
        reverse=True
    )

    llava_candidates = min(6, len(scored))

    interesting = scored[:llava_candidates]

    print(
        f"Running LLaVA on {len(interesting)} clips",
        flush=True
    )

    for frame in interesting:

        clip_dir = os.path.join(
            MEDIA,
            "work",
            inp.jobId,
            f"clip_{frame['idx']}"
        )

        clip_frames = _extract_clip_frames(
            full,
            frame["start"],
            frame["end"],
            clip_dir
        )

        gameplay, score, reason = _vision_score(clip_frames)

        frame["gameplay"] = gameplay
        frame["vision_score"] = score
        frame["reason"] = reason

        if not gameplay:
            frame["final_score"] = -9999

            print(
                "Rejected:",
                reason,
                flush=True
            )
            continue

        # Add LLaVA's opinion to the existing fast score
        frame["final_score"] += frame["vision_score"] * 2

    interesting.sort(
        key=lambda x: x["final_score"],
        reverse=True
    )

    selected = []
    MIN_DISTANCE = 30

    for frame in interesting:
        keep = True
        for chosen in selected:
            if abs(frame["time"] - chosen["time"]) < MIN_DISTANCE:
                keep = False
                break
        if keep:
            selected.append(frame)
        if len(selected) == inp.count:
            break

    print("\n===== FINAL RANKING =====", flush=True)

    for i, frame in enumerate(selected, 1):
        print(
            f"{i}. "
            f"Time={frame['time']:.1f}s | "
            f"Gameplay={frame['gameplay']} | "
            f"Final={frame['final_score']:.2f} | "
            f"Vision={frame['vision_score']} | "
            f"Motion={frame['motion_norm']:.2f} | "
            f"YOLO={frame['yolo']:.2f} | "
            f"OCR={frame['ocr']} | "
            f"Reason={frame['reason']}",
            flush=True
)

    return {
        "dur": dur,
        "candidates": selected
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

        out_file = os.path.join(
            out_dir,
            f"clip_{i+1}.mp4"
        )

        render_clip(
            source,
            clip.start,
            clip.end,
            out_file
        )

        rendered.append(
            f"work/{inp.jobId}/renders/clip_{i+1}.mp4"
        )

    return {
        "clips": rendered
    }