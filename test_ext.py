import yt_dlp
import json

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

ydl_opts = {"quiet": True, "no_warnings": True}
with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    info = ydl.extract_info(URL, download=False)

video_formats = []
audio_formats = []

for f in info.get("formats", []):
    vcodec = f.get("vcodec")
    acodec = f.get("acodec")
    filesize = f.get("filesize") or f.get("filesize_approx")
    
    if vcodec != "none":
        video_formats.append({
            "id": f.get("format_id"),
            "height": f.get("height"),
            "ext": f.get("ext"),
            "acodec": acodec,
            "vcodec": vcodec,
            "size": filesize
        })
    if acodec != "none" and vcodec == "none":
        audio_formats.append({
            "id": f.get("format_id"),
            "abr": f.get("abr"),
            "ext": f.get("ext"),
            "size": filesize
        })

print(json.dumps({"videos": video_formats[-5:], "audios": audio_formats[-5:]}, indent=2))
