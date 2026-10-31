from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from groq import Groq
from openai import OpenAI
from pydantic import BaseModel, Field

from . import db
from .assistant import Assistant, current_context

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / "app" / "service_keys.txt")
load_dotenv(ROOT / ".env", override=True)

STATIC = ROOT / "public"
if not STATIC.exists():
    STATIC = ROOT / "static"
STATIC.mkdir(exist_ok=True)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("GROQ_LLM_MODEL") or os.getenv("LLM_MODEL") or "gpt-6-luna-pro"
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.aitunnel.ru/v1")
TTS_MODEL = os.getenv("TTS_MODEL", "eleven-v4-turbo")
TTS_VOICE = os.getenv("TTS_VOICE", "lily")

if "aitunnel.ru" in LLM_BASE_URL and "/v1" not in LLM_BASE_URL:
    LLM_BASE_URL = "https://api.aitunnel.ru/v1"

_conn = None
llm_client = OpenAI(api_key=LLM_API_KEY or "missing", base_url=LLM_BASE_URL)
groq_client = Groq(api_key=GROQ_API_KEY or "missing")


def get_conn():
    global _conn
    if _conn is None:
        _conn = db.connect()
        db.init_db(_conn)
    return _conn


def get_assistant() -> Assistant:
    return Assistant(get_conn(), llm_client, LLM_MODEL)


app = FastAPI(title="Планировщик")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
if not os.getenv("VERCEL"):
    app.mount("/static", StaticFiles(directory=STATIC), name="static")


class ChatIn(BaseModel):
    text: str
    history: list[dict[str, str]] = Field(default_factory=list)
    speak: bool = True


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/styles.css")
def styles() -> FileResponse:
    return FileResponse(STATIC / "styles.css")


@app.get("/app.js")
def script() -> FileResponse:
    return FileResponse(STATIC / "app.js")


@app.get("/api/health")
def health() -> dict:
    ctx = current_context()
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    return {
        "ok": True,
        "now": ctx["now"],
        "events": total,
        "model": LLM_MODEL,
        "csv": db.csv_path().is_file(),
        "groq": bool(GROQ_API_KEY),
        "llm": bool(LLM_API_KEY),
    }


@app.get("/api/events")
def events(scope: str = "upcoming") -> dict:
    ctx = current_context()
    conn = get_conn()
    if scope == "today":
        items = db.list_events(conn, date_from=ctx["today"], date_to=ctx["today"])
    elif scope == "week":
        items = db.list_events(conn, date_from=ctx["today"], date_to=ctx["week_end"])
    elif scope == "all":
        items = db.list_events(conn, date_from=None, date_to=None, limit=120)
    else:
        items = db.upcoming(conn, ctx["today"])
    return {"today": ctx["today"], "events": items}


@app.get("/api/events/{event_id}")
def event_detail(event_id: int) -> dict:
    item = db.get_event(get_conn(), event_id)
    if not item:
        raise HTTPException(404, "Запись не найдена")
    return item


HALLUCINATED = (
    "продолжение следует",
    "субтитр",
    "спасибо за просмотр",
    "подписывайтесь",
    "редактор субтитров",
    "ставьте лайк",
    "до новых встреч",
)


def clean_transcript(text: str) -> str:
    parts = re.split(r"[.!?…\n]+", text)
    kept = []
    for part in parts:
        norm = part.lower().replace("ё", "е").strip(" \t.,")
        if not norm:
            continue
        if any(marker in norm for marker in HALLUCINATED):
            continue
        kept.append(part.strip())
    return " ".join(kept).strip()


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...)) -> dict:
    if not GROQ_API_KEY:
        raise HTTPException(
            500,
            "Не найден ключ распознавания речи.",
        )
    payload = await audio.read()
    if not payload:
        raise HTTPException(400, "Пустая запись. Нажмите на кружок и скажите фразу ещё раз.")
    name = audio.filename or "speech.webm"
    mime = audio.content_type or "audio/webm"
    if "webm" in mime or name.endswith(".webm"):
        name = "speech.webm"
        mime = "audio/webm"
    elif "mp4" in mime or "m4a" in mime:
        name = "speech.mp4"
        mime = "audio/mp4"
    elif "ogg" in mime:
        name = "speech.ogg"
        mime = "audio/ogg"
    elif "wav" in mime:
        name = "speech.wav"
        mime = "audio/wav"
    try:
        result = groq_client.audio.transcriptions.create(
            file=(name, payload, mime),
            model=GROQ_STT_MODEL,
            language="ru",
            temperature=0,
            prompt="Запрос в календарь: задача, встреча, напоминание, что сегодня, найди.",
        )
    except Exception as exc:
        raise HTTPException(502, f"Распознавание не прошло: {exc}") from exc
    text = clean_transcript((getattr(result, "text", None) or str(result)).strip())
    return {"text": text}


def synthesize(text: str) -> bytes:
    if not text.strip():
        raise HTTPException(400, "Пустой текст для озвучки")
    voices = [TTS_VOICE, "lily", "alice", "jessica", "charlotte"]
    seen = []
    last_error = None
    for voice in voices:
        if not voice or voice in seen:
            continue
        seen.append(voice)
        try:
            response = llm_client.audio.speech.create(
                model=TTS_MODEL,
                voice=voice,
                input=text[:4000],
            )
            if hasattr(response, "read"):
                return response.read()
            if hasattr(response, "content"):
                return response.content
        except Exception as exc:
            last_error = exc
    raise HTTPException(500, f"TTS не вернул аудио: {last_error}")


@app.post("/api/chat")
def chat(body: ChatIn) -> dict:
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Пустой запрос")
    if not LLM_API_KEY:
        raise HTTPException(
            500,
            "Не найден ключ модели.",
        )
    conn = get_conn()
    result = get_assistant().think(text, body.history)
    audio_b64 = None
    if body.speak:
        try:
            import base64

            audio_b64 = base64.b64encode(synthesize(result["reply"])).decode("ascii")
        except Exception as exc:
            result["tts_error"] = str(exc)
    result["audio_base64"] = audio_b64
    result["upcoming"] = db.upcoming(conn, result["today"])
    return result


@app.post("/api/speak")
def speak(body: ChatIn) -> Response:
    audio = synthesize(body.text)
    return Response(content=audio, media_type="audio/mpeg")
