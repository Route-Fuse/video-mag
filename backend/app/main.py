import asyncio
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from .capture import Session, cameras, devices, loopbacks

session = Session()


@asynccontextmanager
async def lifespan(app):
    yield
    session.stop()


app = FastAPI(title="Video Amplification", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type"])


class Settings(BaseModel):
    mode: Literal["color", "motion"] = "color"
    gain: float = Field(default=20, ge=0, le=100, allow_inf_nan=False)
    low: float = Field(default=.7, gt=0, allow_inf_nan=False)
    high: float = Field(default=3, gt=0, allow_inf_nan=False)
    fps: Literal[30] = 30

    @model_validator(mode="after")
    def valid_band(self):
        if not self.low < self.high < self.fps / 2:
            raise ValueError("Use 0 < low < high < 15 Hz")
        return self


class Start(BaseModel):
    source: Literal["phone", "demo"] = "phone"
    serial: str | None = None
    camera_id: str | None = None
    sink: str | None = None


def checked(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/devices")
def get_devices():
    return {"devices": checked(devices), "loopbacks": loopbacks()}


@app.get("/api/cameras")
def get_cameras(serial: str):
    return checked(cameras, serial)


@app.get("/api/status")
def status():
    return session.status()


@app.put("/api/settings")
def settings(values: Settings):
    return checked(session.configure, values.model_dump())


@app.post("/api/start")
def start(values: Start):
    return checked(session.start, **values.model_dump())


@app.post("/api/stop")
def stop():
    return checked(session.stop)


@app.get("/api/stream/{kind}")
def stream(kind: Literal["original", "amplified"]):
    async def frames():
        previous = None
        while True:
            with session.lock:
                frame = session.frames[kind]
                active = session.state in ("starting", "running")
            if frame is not None and frame is not previous:
                previous = frame
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
            if not active:
                return
            await asyncio.sleep(1 / 30)
    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})
