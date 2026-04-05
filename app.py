from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp
import imageio_ffmpeg
import os
import uuid
import threading
import time

app = Flask(__name__)

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

download_tasks = {}


def cleanup_file(filepath, delay=300):
    """Hapus file setelah delay tertentu (default 5 menit)."""
    def _remove():
        time.sleep(delay)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass
    t = threading.Thread(target=_remove, daemon=True)
    t.start()


def progress_hook(task_id):
    def hook(d):
        if d["status"] == "downloading":
            percent = d.get("_percent_str", "0%").strip()
            download_tasks[task_id]["progress"] = percent
            download_tasks[task_id]["status"] = "downloading"
        elif d["status"] == "finished":
            download_tasks[task_id]["status"] = "processing"
            download_tasks[task_id]["progress"] = "100%"
    return hook


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/download", methods=["POST"])
def download():
    data = request.get_json()
    url = data.get("url", "").strip()
    fmt = data.get("format", "mp4").strip().lower()

    if not url:
        return jsonify({"error": "URL tidak boleh kosong."}), 400

    task_id = str(uuid.uuid4())
    download_tasks[task_id] = {
        "status": "starting",
        "progress": "0%",
        "filename": None,
        "error": None,
        "title": None,
    }

    def run_download():
        try:
            output_template = os.path.join(DOWNLOAD_DIR, f"{task_id}_%(title)s.%(ext)s")

            if fmt == "mp3":
                ydl_opts = {
                    "format": "bestaudio/best",
                    "outtmpl": output_template,
                    "ffmpeg_location": FFMPEG_PATH,
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192",
                        }
                    ],
                    "progress_hooks": [progress_hook(task_id)],
                    "noplaylist": True,
                    "quiet": True,
                    "no_warnings": True,
                }
            else:
                ydl_opts = {
                    "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                    "outtmpl": output_template,
                    "ffmpeg_location": FFMPEG_PATH,
                    "merge_output_format": "mp4",
                    "progress_hooks": [progress_hook(task_id)],
                    "noplaylist": True,
                    "quiet": True,
                    "no_warnings": True,
                }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "video")
                download_tasks[task_id]["title"] = title

                if fmt == "mp3":
                    expected_ext = "mp3"
                else:
                    expected_ext = "mp4"

                found_file = None
                for f in os.listdir(DOWNLOAD_DIR):
                    if f.startswith(task_id) and f.endswith(f".{expected_ext}"):
                        found_file = f
                        break

                if not found_file:
                    for f in os.listdir(DOWNLOAD_DIR):
                        if f.startswith(task_id):
                            found_file = f
                            break

                if found_file:
                    download_tasks[task_id]["filename"] = found_file
                    download_tasks[task_id]["status"] = "done"
                    filepath = os.path.join(DOWNLOAD_DIR, found_file)
                    cleanup_file(filepath, delay=300)
                else:
                    download_tasks[task_id]["status"] = "error"
                    download_tasks[task_id]["error"] = "File hasil download tidak ditemukan."

        except Exception as e:
            download_tasks[task_id]["status"] = "error"
            download_tasks[task_id]["error"] = str(e)

    thread = threading.Thread(target=run_download, daemon=True)
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/api/status/<task_id>")
def status(task_id):
    task = download_tasks.get(task_id)
    if not task:
        return jsonify({"error": "Task tidak ditemukan."}), 404
    return jsonify(task)


@app.route("/api/file/<task_id>")
def get_file(task_id):
    task = download_tasks.get(task_id)
    if not task or task["status"] != "done":
        return jsonify({"error": "File belum siap atau tidak ditemukan."}), 404

    filepath = os.path.join(DOWNLOAD_DIR, task["filename"])
    if not os.path.exists(filepath):
        return jsonify({"error": "File sudah dihapus dari server."}), 404

    title = task.get("title", "download")
    ext = os.path.splitext(task["filename"])[1]
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip()
    download_name = f"{safe_title}{ext}" if safe_title else f"download{ext}"

    return send_file(filepath, as_attachment=True, download_name=download_name)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
