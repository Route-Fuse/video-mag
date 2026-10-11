"""File based wrist crop stabilization and FastDMM comparison jobs."""
import json
import math
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[2]
JOBS = ROOT / ".runtime" / "video-jobs"
FASTMM_ROOT = Path("/home/vinnavan/user/models/video-mag/Fast-MM")
FASTMM_PYTHON = Path("/home/vinnavan/user/models/video-mag/.venv/bin/python")
FASTMM_ROOT = Path(os.environ.get("VIDEO_MAG_FASTMM_ROOT", str(FASTMM_ROOT)))
FASTMM_PYTHON = Path(os.environ.get("VIDEO_MAG_FASTMM_PYTHON", str(FASTMM_PYTHON)))
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}
GAINS = (5, 10, 20)
_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_active: str | None = None


def create_job(filename: str, points: list, low_hz: float, high_hz: float) -> dict:
    global _active
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("Choose an MP4, MOV, MKV, AVI, or M4V video")
    if len(points) != 4 or any(len(point) != 2 for point in points):
        raise ValueError("Select exactly four points around the wrist")
    polygon = np.asarray(points, dtype=np.float32)
    if not np.isfinite(polygon).all() or np.any(polygon < 0) or np.any(polygon > 1):
        raise ValueError("Wrist points must be inside the video")
    area = abs(cv2.contourArea(polygon))
    if area < 0.0005:
        raise ValueError("The four wrist points need to enclose a larger area")
    if not (math.isfinite(low_hz) and math.isfinite(high_hz) and 0 < low_hz < high_hz):
        raise ValueError("Set a valid frequency band in Hz")
    with _lock:
        if _active and _jobs.get(_active, {}).get("state") in {"uploading", "queued", "preparing", "magnifying"}:
            raise ValueError("Another video is still processing")
        job_id = uuid.uuid4().hex
        job_dir = JOBS / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        job = {"id": job_id, "state": "uploading", "filename": Path(filename).name,
               "points": polygon.tolist(), "low_hz": float(low_hz), "high_hz": float(high_hz),
               "created": time.time(), "progress": 0, "current_gain": None,
               "completed_gains": [], "error": None, "files": []}
        _jobs[job_id] = job
        _active = job_id
        return public_status(job)


def job_path(job_id: str) -> Path:
    if not job_id.isalnum() or len(job_id) != 32 or job_id not in _jobs:
        raise ValueError("Video processing job not found")
    return JOBS / job_id


def set_uploaded(job_id: str):
    with _lock:
        job_path(job_id)
        _jobs[job_id]["state"] = "queued"


def public_status(job: dict) -> dict:
    return {key: job[key] for key in ("id", "state", "filename", "progress", "current_gain",
                                      "completed_gains", "error", "files", "low_hz", "high_hz")}


def status(job_id: str) -> dict:
    with _lock:
        path = job_path(job_id)
        return public_status(_jobs[job_id])


def file_path(job_id: str, name: str) -> Path:
    with _lock:
        base = job_path(job_id)
        if name not in _jobs[job_id]["files"]:
            raise ValueError("Result file is not ready")
        path = base / name
        if not path.is_file():
            raise ValueError("Result file is no longer available")
        return path


def delete_job(job_id: str):
    global _active
    with _lock:
        job_path(job_id)
        if _jobs[job_id]["state"] in {"uploading", "queued", "preparing", "magnifying"}:
            raise ValueError("Wait for processing to finish before clearing its files")
        shutil.rmtree(JOBS / job_id, ignore_errors=True)
        del _jobs[job_id]
        if _active == job_id:
            _active = None


def abort_upload(job_id: str):
    global _active
    with _lock:
        job_path(job_id)
        if _jobs[job_id]["state"] != "uploading":
            raise ValueError("Upload already started processing")
        shutil.rmtree(JOBS / job_id, ignore_errors=True)
        del _jobs[job_id]
        if _active == job_id:
            _active = None


def _update(job_id: str, **values):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(values)


def _expanded_crop(points: np.ndarray, width: int, height: int):
    pixels = points * np.array([width - 1, height - 1], dtype=np.float32)
    x, y, w, h = cv2.boundingRect(pixels.reshape((-1, 1, 2)))
    margin_x, margin_y = max(12, int(w * .2)), max(12, int(h * .2))
    x0, y0 = max(0, x - margin_x), max(0, y - margin_y)
    x1, y1 = min(width, x + w + margin_x), min(height, y + h + margin_y)
    crop_w, crop_h = x1 - x0, y1 - y0
    if crop_w < 32 or crop_h < 32:
        raise ValueError("Select a larger wrist region so the model can process it")
    return x0, y0, crop_w, crop_h


def _prepare_stable_crop(job_id: str, crop_path: Path, original_crop_path: Path):
    job = _jobs[job_id]
    source = JOBS / job_id / "source" / job["filename"]
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise ValueError("Could not open the uploaded video")
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    width, height = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    if not (math.isfinite(fps) and 1 <= fps <= 240 and width >= 64 and height >= 64 and count > 1):
        capture.release()
        raise ValueError("The video has unsupported dimensions, frame rate, or duration")
    if job["high_hz"] >= fps / 2:
        capture.release()
        raise ValueError(f"High cutoff must be below Nyquist ({fps / 2:.3g} Hz at {fps:.3g} FPS)")
    x0, y0, crop_w, crop_h = _expanded_crop(np.asarray(job["points"], dtype=np.float32), width, height)
    original_crop_path.parent.mkdir(parents=True, exist_ok=True)

    encode_crop = subprocess.Popen([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s:v", f"{crop_w}x{crop_h}", "-r", f"{fps:.9g}", "-i", "-", "-an", "-c:v", "ffv1",
        "-level", "3", str(crop_path),
    ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    reference = None
    warp = np.eye(2, 3, dtype=np.float32)
    frame_index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            crop = frame[y0:y0 + crop_h, x0:x0 + crop_w]
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            if reference is None:
                reference = gray.astype(np.float32) / 255
                aligned = crop
            else:
                try:
                    cv2.findTransformECC(reference, gray.astype(np.float32) / 255, warp,
                                         cv2.MOTION_TRANSLATION,
                                         (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 40, 1e-5),
                                         None, 5)
                    warp[:, 2] = np.clip(warp[:, 2], [-crop_w * .18, -crop_h * .18], [crop_w * .18, crop_h * .18])
                except cv2.error:
                    pass
                aligned = cv2.warpAffine(crop, warp, (crop_w, crop_h),
                                         flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                         borderMode=cv2.BORDER_REPLICATE)
            encode_crop.stdin.write(aligned.tobytes())
            frame_index += 1
            if frame_index % 15 == 0:
                _update(job_id, progress=min(12, int(12 * frame_index / max(count, 1))))
    finally:
        capture.release()
        if encode_crop.stdin:
            encode_crop.stdin.close()
    err = encode_crop.stderr.read().decode(errors="replace") if encode_crop.stderr else ""
    code = encode_crop.wait()
    if code or frame_index < 2:
        raise ValueError(f"Could not prepare the stabilized crop: {err[-1200:]}")

    preview = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(crop_path), "-an",
        "-c:v", "libx264rgb", "-crf", "0", "-preset", "ultrafast", str(original_crop_path),
    ], capture_output=True, text=True, timeout=600)
    if preview.returncode:
        raise ValueError(f"Could not encode the stabilized crop preview: {preview.stderr[-1200:]}")
    _update(job_id, crop_size=[crop_w, crop_h], fps=round(fps, 4), frame_count=frame_index)
    return fps


def _run_job(job_id: str):
    try:
        job_dir = job_path(job_id)
        work = job_dir / "work"
        work.mkdir(exist_ok=True)
        stable = work / "stabilized_crop.mkv"
        result_dir = job_dir / "results"
        result_dir.mkdir(exist_ok=True)
        original = result_dir / "crop_original.mp4"
        _update(job_id, state="preparing", progress=1, error=None)
        fps = _prepare_stable_crop(job_id, stable, original)
        shutil.rmtree(job_dir / "source", ignore_errors=True)
        if not FASTMM_PYTHON.is_file() or not (FASTMM_ROOT / "run.py").is_file():
            raise ValueError(f"FastDMM setup was not found at {FASTMM_ROOT}; configure VIDEO_MAG_FASTMM_ROOT and VIDEO_MAG_FASTMM_PYTHON")
        low, high = _jobs[job_id]["low_hz"], _jobs[job_id]["high_hz"]
        low_ratio, high_ratio = low / fps, high / fps
        completed = []
        _update(job_id, state="magnifying", progress=12, files=[original.name])
        for index, gain in enumerate(GAINS):
            _update(job_id, current_gain=gain, progress=12 + index * 29)
            output_dir = work / f"gain-{gain}"
            command = [str(FASTMM_PYTHON), str(FASTMM_ROOT / "run.py"), "--input", str(stable),
                       "--output-dir", str(output_dir), "--model", "fast-dmm", "--filter", "butter",
                       "--low", f"{low_ratio:.9g}", "--high", f"{high_ratio:.9g}",
                       "--alpha", str(gain), "--device", "cuda"]
            result = subprocess.run(command, cwd=FASTMM_ROOT, capture_output=True, text=True, timeout=3600)
            if result.returncode:
                raise ValueError(f"FastDMM failed at {gain}×: {(result.stderr or result.stdout)[-1800:]}")
            candidates = list(output_dir.rglob("*.mp4"))
            if not candidates:
                raise ValueError(f"FastDMM completed at {gain}× but did not create an output video")
            output_name = f"fastdmm_{gain}x.mp4"
            shutil.copy2(candidates[0], result_dir / output_name)
            completed.append(gain)
            _update(job_id, completed_gains=completed, files=[original.name, *[f"fastdmm_{n}x.mp4" for n in completed]],
                    progress=12 + (index + 1) * 29)
        shutil.rmtree(work, ignore_errors=True)
        _update(job_id, state="completed", progress=100, current_gain=None)
    except Exception as exc:
        try:
            shutil.rmtree(JOBS / job_id / "work", ignore_errors=True)
        except OSError:
            pass
        _update(job_id, state="error", error=str(exc)[-2400:], current_gain=None)


def start_job(job_id: str):
    with _lock:
        job_path(job_id)
        if _jobs[job_id]["state"] != "queued":
            raise ValueError("Video upload is incomplete")
        _jobs[job_id]["state"] = "preparing"
    threading.Thread(target=_run_job, args=(job_id,), name=f"video-job-{job_id[:8]}", daemon=True).start()
