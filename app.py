import os
import re
import sys
import time
import uuid
import shutil
import tempfile
import threading
import subprocess
from collections import deque

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
CHANNEL_CHAT_ID = os.getenv("CHANNEL_CHAT_ID", "").strip()
TELEGRAM_SECRET = os.getenv("TELEGRAM_SECRET", "").strip()

CLIP_CROP = os.getenv("CLIP_CROP", "default")
CLIP_RATIO = os.getenv("CLIP_RATIO", "9:16")
CLIP_SUBTITLE = os.getenv("CLIP_SUBTITLE", "n").lower()  # y / n
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "tiny")

MAX_TG_FILE_BYTES = 50 * 1024 * 1024
APP_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_ROOT = os.path.join(tempfile.gettempdir(), "ytclip_jobs")

RUN_LOCK = threading.Lock()
RECENT_UPDATES = deque(maxlen=300)
RECENT_SET = set()
RECENT_LOCK = threading.Lock()

YT_URL_RE = re.compile(
    r"(https?://(?:www\.)?(?:youtube\.com/watch\?v=[\w\-]{6,}|youtube\.com/shorts/[\w\-]{6,}|youtu\.be/[\w\-]{6,})(?:[^\s]*)?)",
    re.IGNORECASE,
)

TG_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


def remember_update(update_id: int) -> bool:
    with RECENT_LOCK:
        if update_id in RECENT_SET:
            return False
        if len(RECENT_UPDATES) == RECENT_UPDATES.maxlen:
            oldest = RECENT_UPDATES.popleft()
            RECENT_SET.discard(oldest)
        RECENT_UPDATES.append(update_id)
        RECENT_SET.add(update_id)
        return True


def tg_api(method: str, data=None, files=None, timeout=120):
    url = f"{TG_BASE}/{method}"
    resp = requests.post(url, data=data, files=files, timeout=timeout)
    try:
        payload = resp.json()
    except Exception:
        raise RuntimeError(f"Telegram returned non-JSON response: {resp.text[:300]}")

    if not payload.get("ok"):
        raise RuntimeError(payload.get("description", f"Telegram API error on {method}"))

    return payload["result"]


def send_text(chat_id: str, text: str):
    return tg_api(
        "sendMessage",
        data={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        },
        timeout=60,
    )


def copy_to_admin(from_chat_id: str, message_id: int):
    if not ADMIN_CHAT_ID:
        return None
    return tg_api(
        "copyMessage",
        data={
            "chat_id": ADMIN_CHAT_ID,
            "from_chat_id": from_chat_id,
            "message_id": str(message_id),
        },
        timeout=120,
    )


def upload_clip_to_channel(file_path: str, caption: str):
    if not CHANNEL_CHAT_ID:
        raise RuntimeError("CHANNEL_CHAT_ID belum diset")

    size = os.path.getsize(file_path)
    if size > MAX_TG_FILE_BYTES:
        raise RuntimeError(
            f"File terlalu besar untuk Bot API cloud Telegram: {size / (1024 * 1024):.1f} MB"
        )

    method = "sendVideo" if file_path.lower().endswith(".mp4") else "sendDocument"
    field_name = "video" if method == "sendVideo" else "document"

    with open(file_path, "rb") as f:
        return tg_api(
            method,
            data={
                "chat_id": CHANNEL_CHAT_ID,
                "caption": caption[:1024],
            },
            files={
                field_name: f,
            },
            timeout=1800,
        )


def extract_youtube_url(text: str) -> str | None:
    if not text:
        return None
    match = YT_URL_RE.search(text)
    return match.group(1) if match else None


def list_mp4_files(folder: str):
    if not os.path.isdir(folder):
        return []
    files = []
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if os.path.isfile(path) and name.lower().endswith(".mp4"):
            files.append(path)
    files.sort()
    return files


def run_clipper_job(url: str, requester_chat_id: str):
    if not RUN_LOCK.acquire(blocking=False):
        if requester_chat_id:
            send_text(requester_chat_id, "Masih ada job lain yang jalan. Tunggu yang sekarang selesai dulu.")
        return

    job_id = uuid.uuid4().hex[:8]
    work_dir = os.path.join(JOBS_ROOT, job_id)
    out_dir = os.path.join(work_dir, "clips")

    os.makedirs(out_dir, exist_ok=True)

    try:
        if requester_chat_id:
            send_text(
                requester_chat_id,
                f"Job diterima.\nID: {job_id}\nMode: {CLIP_CROP} | Ratio: {CLIP_RATIO} | Subtitle: {CLIP_SUBTITLE.upper()}",
            )

        env = os.environ.copy()
        env["OUTPUT_DIR"] = out_dir
        env.setdefault("MAX_CLIPS", "3")
        env.setdefault("MAX_DURATION", "45")
        env.setdefault("MIN_SCORE", "0.45")
        env.setdefault("PADDING", "8")
        env.setdefault("USE_SUBTITLE", "1" if CLIP_SUBTITLE == "y" else "0")
        env.setdefault("OUTPUT_RATIO", CLIP_RATIO)
        env.setdefault("WHISPER_MODEL", WHISPER_MODEL)

        cmd = [
            sys.executable,
            "run.py",
            "--url",
            url,
            "--crop",
            CLIP_CROP,
            "--subtitle",
            "y" if CLIP_SUBTITLE == "y" else "n",
            "--ratio",
            CLIP_RATIO,
            "--no-update-ytdlp",
        ]

        if CLIP_SUBTITLE == "y":
            cmd += ["--whisper-model", WHISPER_MODEL]

        proc = subprocess.run(
            cmd,
            cwd=APP_DIR,
            env=env,
            text=True,
            capture_output=True,
            timeout=7200,
        )

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        if proc.returncode != 0:
            err_text = stderr or stdout or "Unknown error"
            send_text(
                requester_chat_id,
                f"Gagal membuat clip.\nID: {job_id}\nError:\n{err_text[:3500]}",
            )
            return

        clips = list_mp4_files(out_dir)
        if not clips:
            send_text(
                requester_chat_id,
                f"Job selesai tapi tidak ada file MP4 yang ditemukan.\nID: {job_id}",
            )
            return

        uploaded_count = 0

        for i, clip_path in enumerate(clips, start=1):
            file_name = os.path.basename(clip_path)
            size_mb = os.path.getsize(clip_path) / (1024 * 1024)

            caption = (
                f"✅ Clip selesai\n"
                f"Job: {job_id}\n"
                f"Part: {i}/{len(clips)}\n"
                f"File: {file_name}\n"
                f"Size: {size_mb:.1f} MB"
            )

            sent = upload_clip_to_channel(clip_path, caption)
            uploaded_count += 1

            channel_message_id = sent["message_id"]

            try:
                copy_to_admin(CHANNEL_CHAT_ID, channel_message_id)
            except Exception as copy_err:
                send_text(
                    requester_chat_id,
                    f"Video berhasil dikirim ke channel, tapi copy ke DM admin gagal.\n"
                    f"message_id={channel_message_id}\n"
                    f"Error: {copy_err}",
                )

            send_text(
                requester_chat_id,
                f"Uploaded ke channel.\nJob: {job_id}\nmessage_id={channel_message_id}\nfile={file_name}",
            )

        send_text(
            requester_chat_id,
            f"Semua proses selesai.\nJob: {job_id}\nTotal clip terupload: {uploaded_count}",
        )

    except subprocess.TimeoutExpired:
        send_text(requester_chat_id, f"Job timeout.\nID: {job_id}")
    except Exception as e:
        send_text(requester_chat_id, f"Job error.\nID: {job_id}\nError: {str(e)[:3500]}")
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass
        RUN_LOCK.release()


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "busy": RUN_LOCK.locked(),
            "admin_set": bool(ADMIN_CHAT_ID),
            "channel_set": bool(CHANNEL_CHAT_ID),
            "time": int(time.time()),
        }
    )


@app.post("/telegram/webhook")
def telegram_webhook():
    if TELEGRAM_SECRET:
        incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if incoming_secret != TELEGRAM_SECRET:
            return jsonify({"ok": False, "error": "forbidden"}), 403

    update = request.get_json(silent=True) or {}
    update_id = update.get("update_id")

    if isinstance(update_id, int):
        if not remember_update(update_id):
            return jsonify({"ok": True, "duplicate": True})

    # Detect channel id from channel post
    if "channel_post" in update:
        channel_post = update["channel_post"]
        chat = channel_post.get("chat", {})
        detected_channel_id = str(chat.get("id", ""))

        if detected_channel_id and ADMIN_CHAT_ID:
            try:
                send_text(
                    ADMIN_CHAT_ID,
                    f"Detected channel post.\nCHANNEL_CHAT_ID = {detected_channel_id}\n"
                    f"Kalau ini channel yang benar, simpan ke Environment Variables lalu restart app.",
                )
            except Exception:
                pass

        return jsonify({"ok": True, "type": "channel_post"})

    message = update.get("message")
    if not message:
        return jsonify({"ok": True, "ignored": True})

    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    chat_type = chat.get("type", "")
    text = (message.get("text") or "").strip()

    if chat_type != "private":
        return jsonify({"ok": True, "ignored": "non_private_chat"})

    if text in {"/start", "/id"}:
        summary = (
            f"Chat ID kamu: {chat_id}\n"
            f"ADMIN_CHAT_ID set: {'yes' if ADMIN_CHAT_ID else 'no'}\n"
            f"CHANNEL_CHAT_ID set: {'yes' if CHANNEL_CHAT_ID else 'no'}\n"
            f"Mode: crop={CLIP_CROP}, ratio={CLIP_RATIO}, subtitle={CLIP_SUBTITLE}"
        )
        send_text(chat_id, summary)
        return jsonify({"ok": True})

    if text == "/help":
        send_text(
            chat_id,
            "Kirim salah satu:\n"
            "1. URL YouTube langsung\n"
            "2. /clip <url>\n\n"
            "Command setup:\n"
            "/start atau /id untuk lihat chat ID",
        )
        return jsonify({"ok": True})

    if ADMIN_CHAT_ID and chat_id != ADMIN_CHAT_ID:
        send_text(chat_id, "Bot ini sedang dibatasi hanya untuk admin.")
        return jsonify({"ok": True})

    if not CHANNEL_CHAT_ID:
        send_text(
            chat_id,
            "CHANNEL_CHAT_ID belum diset.\n"
            "Post pesan test di private channel dulu, lalu lihat DM/log untuk ID channel-nya.",
        )
        return jsonify({"ok": True})

    if text.startswith("/clip "):
        url = extract_youtube_url(text[6:].strip())
    else:
        url = extract_youtube_url(text)

    if not url:
        send_text(chat_id, "Saya butuh URL YouTube yang valid.")
        return jsonify({"ok": True})

    worker = threading.Thread(
        target=run_clipper_job,
        args=(url, chat_id),
        daemon=True,
    )
    worker.start()

    return jsonify({"ok": True, "accepted": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
