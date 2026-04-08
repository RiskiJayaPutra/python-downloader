from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp
import imageio_ffmpeg
import os
import uuid
import threading
import time
import traceback
import re

app = Flask(__name__)

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

download_tasks = {}


def cleanup_file(filepath, delay=300):
    def _remove():
        time.sleep(delay)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass
    t = threading.Thread(target=_remove, daemon=True)
    t.start()


def strip_ansi(text):
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

def progress_hook(task_id):
    def hook(d):
        task = download_tasks.get(task_id)
        if not task: return
        
        if "part" not in task:
            task["part"] = 1
            task["last_pct"] = 0.0

        if d["status"] == "downloading":
            percent_str = strip_ansi(d.get("_percent_str", "0%")).strip()
            speed = strip_ansi(d.get("_speed_str", "")).strip()
            eta = strip_ansi(d.get("_eta_str", "")).strip()
            
            try:
                curr_pct = float(percent_str.replace('%', ''))
            except ValueError:
                curr_pct = task["last_pct"]
                
            if curr_pct < task["last_pct"] - 30.0:
                task["part"] += 1
                
            task["last_pct"] = curr_pct
            part_str = f" (Bagian {task['part']})" if task["part"] > 1 else ""

            task["progress"] = percent_str
            task["speed"] = speed
            task["eta"] = eta
            task["part_str"] = part_str
            task["status"] = "downloading"
        elif d["status"] == "finished":
            pass # Keep status 'downloading' to prevent flickering until postprocessing starts
    return hook

def postprocessor_hook(task_id):
    def hook(d):
        task = download_tasks.get(task_id)
        if not task: return
        if d["status"] == "started":
            task["status"] = "processing"
            task["progress"] = "100%"
            task["part_str"] = ""
    return hook


@app.route("/")
def index():
    return render_template("index.html")


def format_size(bytes_size):
    if not bytes_size: return "~ MB"
    mb = bytes_size / (1024 * 1024)
    if mb < 1000:
        return f"{mb:.1f} MB"
    else:
        gb = mb / 1024
        return f"{gb:.2f} GB"

@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url: return jsonify({"error": "URL kosong."}), 400

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            
        title = info.get("title", "Video")
        thumbnail = info.get("thumbnail", "")
        
        formats = info.get("formats", [])
        
        # Audio formats options
        aud_options = []
        for f in formats:
            if f.get("acodec") != "none" and f.get("vcodec") == "none":
                abr = f.get("abr", 0) or 128
                f_size = f.get("filesize") or f.get("filesize_approx") or 0
                aud_options.append({
                    "id": f.get("format_id"),
                    "abr": int(abr),
                    "size_str": format_size(f_size) if f_size > 0 else "Ukuran Tidak Diketahui",
                    "bytes": f_size
                })
        
        auds = sorted(aud_options, key=lambda x: x["abr"], reverse=True)
        unique_auds = []
        seen_abrs = set()
        for a in auds:
            round_abr = round(a["abr"] / 10) * 10
            if round_abr not in seen_abrs:
                seen_abrs.add(round_abr)
                unique_auds.append(a)
                
        best_audio = unique_auds[0] if unique_auds else None
        audio_size = best_audio["bytes"] if best_audio else 0
        audio_id = best_audio["id"] if best_audio else ""
        
        vid_options = {}
        for f in formats:
            height = f.get("height")
            if not height or height < 144: continue
            if f.get("vcodec") == "none" or "images" in f.get("format_id", ""): continue
            
            ext = f.get("ext", "")
            if ext not in ["mp4", "webm"]: continue
            
            score = height * 1000 + (100 if ext == "mp4" else 0)
            
            f_size = f.get("filesize") or f.get("filesize_approx") or 0
            has_audio = f.get("acodec") != "none"
            
            total_size = f_size
            f_id = f.get("format_id")
            if not has_audio and audio_id:
                total_size += audio_size
                f_id = f"{f_id}+{audio_id}"
                
            item = {
                "id": f_id,
                "height": height,
                "ext": ext,
                "size_str": format_size(total_size) if total_size > 0 else "Ukuran Tidak Diketahui",
                "bytes": total_size
            }
            
            if height not in vid_options or score > vid_options[height]["score"]:
                item["score"] = score
                vid_options[height] = item

        vids = sorted(vid_options.values(), key=lambda x: x["height"], reverse=True)
        for v in vids: del v["score"]
        
        return jsonify({
            "title": title,
            "thumbnail": thumbnail,
            "videos": vids[:6],
            "audios": unique_auds[:5]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/download", methods=["POST"])
def download():
    data = request.get_json()
    url = data.get("url", "").strip()
    format_id = data.get("format_id", "best").strip()
    dl_type = data.get("type", "video").strip()

    if not url:
        return jsonify({"error": "URL tidak boleh kosong."}), 400

    task_id = str(uuid.uuid4())
    download_tasks[task_id] = {
        "status": "extracting",
        "progress": "0%",
        "speed": "",
        "eta": "",
        "part_str": "",
        "filename": None,
        "error": None,
        "title": None,
    }

    def run_download():
        try:
            download_tasks[task_id]["status"] = "extracting"

            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(url, download=False)

            title = info.get("title", "video")
            duration = info.get("duration", 0)
            download_tasks[task_id]["title"] = title
            download_tasks[task_id]["duration"] = duration
            download_tasks[task_id]["status"] = "downloading"
            download_tasks[task_id]["progress"] = "0%"

            output_template = os.path.join(DOWNLOAD_DIR, f"{task_id}_%(title).80s.%(ext)s")

            if dl_type == "audio":
                ydl_opts = {
                    "format": format_id,
                    "outtmpl": output_template,
                    "ffmpeg_location": FFMPEG_PATH,
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192", # yt-dlp will re-encode to 192 or use the source ABR if we don't specify, we can just leave it at 192 as default
                        }
                    ],
                    "progress_hooks": [progress_hook(task_id)],
                    "postprocessor_hooks": [postprocessor_hook(task_id)],
                    "noplaylist": True,
                    "quiet": True,
                    "no_warnings": True,
                    "socket_timeout": 30,
                    "retries": 3,
                }
            else:
                ydl_opts = {
                    "format": format_id,
                    "outtmpl": output_template,
                    "ffmpeg_location": FFMPEG_PATH,
                    "merge_output_format": "mp4",
                    "progress_hooks": [progress_hook(task_id)],
                    "postprocessor_hooks": [postprocessor_hook(task_id)],
                    "noplaylist": True,
                    "quiet": True,
                    "no_warnings": True,
                    "socket_timeout": 30,
                    "retries": 3,
                }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            expected_ext = "mp3" if dl_type == "audio" else "mp4"
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
            print(f"[ERROR] Task {task_id}: {traceback.format_exc()}")
            download_tasks[task_id]["status"] = "error"
            error_msg = str(e)
            if "Sign in to confirm" in error_msg or "bot" in error_msg.lower():
                error_msg = "YouTube memblokir request. Coba lagi nanti atau gunakan URL yang berbeda."
            elif "Video unavailable" in error_msg:
                error_msg = "Video tidak tersedia atau bersifat privat."
            elif "is not a valid URL" in error_msg:
                error_msg = "URL yang dimasukkan tidak valid."
            download_tasks[task_id]["error"] = error_msg

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
