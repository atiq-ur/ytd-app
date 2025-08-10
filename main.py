import os
import re
import shutil
import tempfile
import uuid
import yt_dlp
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.responses import FileResponse
import subprocess

# In-memory "database" to track task progress
tasks = {}

app = FastAPI()
origins = ["http://localhost:3000"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Models for API requests and responses
class VideoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    quality_label: str


class TaskStatus(BaseModel):
    status: str
    progress: float = 0
    message: str = ""
    file_path: str | None = None
    filename: str | None = None


# The core download and processing logic
def run_download_process(task_id: str, url: str, quality_label: str):
    """This function runs in the background to download and process the video."""

    # --- THIS IS THE CORRECTED PROGRESS HOOK ---
    def progress_hook(d):
        """yt-dlp hook to update task status using raw numbers."""
        if d['status'] == 'downloading':
            tasks[task_id]['status'] = 'downloading'

            # Use raw byte counts to calculate percentage reliably
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
            if total_bytes:
                downloaded_bytes = d.get('downloaded_bytes', 0)
                tasks[task_id]['progress'] = (downloaded_bytes / total_bytes) * 100

            # Clean the speed string to remove any terminal color codes
            speed_str = d.get('speed_str', 'N/A').strip()
            cleaned_speed = re.sub(r'\x1b\[[0-9;]*m', '', speed_str)
            tasks[task_id]['message'] = f"Downloading... ({cleaned_speed})"

        elif d['status'] == 'finished':
            tasks[task_id]['progress'] = 100
            tasks[task_id]['status'] = 'merging'
            tasks[task_id]['message'] = "Download finished, merging formats..."

    temp_dir = tempfile.mkdtemp()
    try:
        tasks[task_id]['status'] = 'starting'
        tasks[task_id]['message'] = 'Preparing download...'

        source_path_template = os.path.join(temp_dir, 'source_video')

        ydl_opts_best = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'outtmpl': source_path_template,
            'quiet': True,
            'merge_output_format': 'mp4',
            'progress_hooks': [progress_hook],
            'noprogress': True,  # Suppress yt-dlp's own console progress bar
        }

        with yt_dlp.YoutubeDL(ydl_opts_best) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "video")
            actual_height = info.get('height')

        source_path_final = f"{source_path_template}.mp4"
        if not os.path.exists(source_path_final):
            source_path_final = f"{source_path_template}.mkv"
            if not os.path.exists(source_path_final):
                raise FileNotFoundError("Downloaded source file not found.")

        requested_height = int(re.search(r'(\d+)p', quality_label).group(1))
        output_path = source_path_final

        if requested_height < actual_height:
            tasks[task_id]['status'] = 're-encoding'
            tasks[task_id]['message'] = f'Re-encoding to {quality_label}...'
            tasks[task_id]['progress'] = 0

            final_path = os.path.join(temp_dir, 'final_video.mp4')
            ffmpeg_command = [
                'ffmpeg', '-i', source_path_final, '-vf', f'scale=-2:{requested_height}',
                '-c:a', 'copy', '-y', final_path
            ]
            subprocess.run(ffmpeg_command, check=True, capture_output=True)
            output_path = final_path

        tasks[task_id]['status'] = 'complete'
        tasks[task_id]['message'] = 'Download complete!'
        tasks[task_id]['progress'] = 100
        tasks[task_id]['file_path'] = output_path
        tasks[task_id]['filename'] = "".join([c for c in title if c.isalnum() or c in (' ', '-')]).rstrip() + ".mp4"

    except Exception as e:
        print(f"Error in task {task_id}: {e}")
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['message'] = str(e)


# --- API Endpoints (These are unchanged) ---
@app.post("/api/info")
def get_video_info(request: VideoRequest):
    ydl_opts = {'quiet': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(request.url, download=False)
            qualities = set()
            for f in info.get('formats', []):
                if f.get('vcodec') != 'none' and f.get('height'):
                    qualities.add(f"{f['height']}p")
            sorted_qualities = sorted(list(qualities), key=lambda x: int(x.replace('p', '')), reverse=True)
            return {"title": info.get("title", "N/A"), "thumbnail": info.get("thumbnail"),
                    "qualities": sorted_qualities}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/download")
def download_request(request: DownloadRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    tasks[task_id] = {'status': 'pending', 'progress': 0, 'message': 'Initializing...'}
    background_tasks.add_task(run_download_process, task_id, request.url, request.quality_label)
    return {"task_id": task_id}


@app.get("/api/status/{task_id}", response_model=TaskStatus)
def get_status(task_id: str):
    task = tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/api/fetch/{task_id}")
async def fetch_file(task_id: str):
    task = tasks.get(task_id)
    if not task or task.get('status') != 'complete':
        raise HTTPException(status_code=404, detail="File not ready or task not found.")

    file_path = task.get('file_path')
    filename = task.get('filename')

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found.")

    def cleanup():
        if os.path.exists(file_path):
            shutil.rmtree(os.path.dirname(file_path))
        tasks.pop(task_id, None)

    return FileResponse(path=file_path, media_type='video/mp4', filename=filename, background=cleanup)