import os
import re
import logging
import asyncio
import aiohttp
import uuid
import time
import signal
import psutil # For system status
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.errors.rpcerrorlist import FloodWaitError, MessageNotModifiedError
from telethon.tl.types import DocumentAttributeFilename

# --- Basic Configuration ---
logging.basicConfig(format='[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s',
                    level=logging.INFO)
load_dotenv()

# --- Telegram API Credentials ---
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')

# --- Initialize the Bot Client ---
bot = TelegramClient('DlinkX_bot', API_ID, API_HASH)

# --- Constants & State Management ---
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
DOWNLOAD_JOBS = {} 

# --- NEW: Queue, Risk & Admin Management ---
ADMIN_IDS = {123456789, 987654321}  # <-- IMPORTANT: Replace with your numeric User ID(s)
BLACKLIST_FILE = "blacklist.txt"
BLACKLISTED_USERS = set()
USER_COOLDOWNS = {}
COOLDOWN_SECONDS = 10
MAX_CONCURRENT_DOWNLOADS = 2 # Number of parallel downloads
download_queue = asyncio.Queue()


# --- Helper Functions ---
def format_bytes(size: int) -> str:
    if size == 0: return "0 B"; power = 1024; n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size >= power and n < len(power_labels) - 1:
        size /= power; n += 1
    return f"{size:.2f} {power_labels[n]}B"

def format_speed(speed: float) -> str:
    return f"{format_bytes(int(speed))}/s"

def format_time(seconds: int) -> str:
    if seconds is None or seconds <= 0: return "N/A"
    seconds = int(seconds); minutes, seconds = divmod(seconds, 60); hours, minutes = divmod(minutes, 60)
    if hours > 0: return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0: return f"{minutes}m {seconds}s"
    else: return f"{seconds}s"
    
def load_blacklist():
    """Loads blacklisted user IDs from the file into memory."""
    try:
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, 'r') as f:
                BLACKLISTED_USERS.update(int(line.strip()) for line in f)
            logging.info(f"Loaded {len(BLACKLISTED_USERS)} user(s) from blacklist.")
    except Exception as e:
        logging.error(f"Error loading blacklist file: {e}")

def update_blacklist_file():
    """Saves the current blacklist set to the file."""
    try:
        with open(BLACKLIST_FILE, 'w') as f:
            for user_id in BLACKLISTED_USERS:
                f.write(f"{user_id}\n")
    except Exception as e:
        logging.error(f"Error writing to blacklist file: {e}")

async def get_link_info(url: str) -> dict:
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.head(url, allow_redirects=True, timeout=10) as r:
                r.raise_for_status(); file_name = "Unknown"
                if 'Content-Disposition' in r.headers:
                    cd = r.headers.get('Content-Disposition'); file_name_match = re.search(r'filename="?([^"]+)"?', cd)
                    if file_name_match: file_name = unquote(file_name_match.group(1))
                else:
                    path = urlparse(str(r.url)).path; file_name = unquote(os.path.basename(path)) if path else "download"
                file_name = re.sub(r'[\\/*?:"<>|]', "", file_name)
                return {"success": True, "url": str(r.url), "file_name": file_name, "content_size": int(r.headers.get('Content-Length', 0))}
    except Exception as e:
        logging.error(f"AIOHTTP failed to fetch info for {url}: {e}"); return {"success": False, "error": str(e)}

# --- Admin Commands ---
@bot.on(events.NewMessage(pattern='/status'))
async def status_handler(event):
    if event.sender_id not in ADMIN_IDS: return
    
    cpu_usage = psutil.cpu_percent()
    ram_usage = psutil.virtual_memory().percent
    active_jobs = len(DOWNLOAD_JOBS)
    queued_jobs = download_queue.qsize()

    status_text = (
        f"**DlinkX Bot Status**\n\n"
        f"⚙️ **CPU Usage:** `{cpu_usage}%`\n"
        f"🧠 **RAM Usage:** `{ram_usage}%`\n"
        f"🔄 **Active Downloads:** `{active_jobs}`\n"
        f"⏳ **Queued Jobs:** `{queued_jobs}`"
    )
    await event.reply(status_text)

@bot.on(events.NewMessage(pattern=r'/ban (\d+)'))
async def ban_handler(event):
    if event.sender_id not in ADMIN_IDS: return
    try:
        user_id_to_ban = int(event.pattern_match.group(1))
        BLACKLISTED_USERS.add(user_id_to_ban)
        update_blacklist_file()
        await event.reply(f"✅ User `{user_id_to_ban}` has been banned.")
    except Exception as e:
        await event.reply(f"❌ Error banning user: {e}")

@bot.on(events.NewMessage(pattern=r'/unban (\d+)'))
async def unban_handler(event):
    if event.sender_id not in ADMIN_IDS: return
    try:
        user_id_to_unban = int(event.pattern_match.group(1))
        BLACKLISTED_USERS.discard(user_id_to_unban)
        update_blacklist_file()
        await event.reply(f"✅ User `{user_id_to_unban}` has been unbanned.")
    except Exception as e:
        await event.reply(f"❌ Error unbanning user: {e}")

# --- Core Bot Logic ---
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    await event.reply("Hello! I am DlinkX, your reliable file downloader.")

@bot.on(events.NewMessage(pattern=re.compile(r'https?://\S+')))
async def link_handler(event):
    user_id = event.sender_id

    # --- Risk Reduction: Blacklist Check ---
    if user_id in BLACKLISTED_USERS:
        logging.warning(f"Ignoring request from blacklisted user {user_id}")
        return

    # --- Risk Reduction: Cooldown Check ---
    current_time = time.time()
    last_req_time = USER_COOLDOWNS.get(user_id, 0)
    if current_time - last_req_time < COOLDOWN_SECONDS:
        await event.reply(f"Please wait `{COOLDOWN_SECONDS}` seconds before sending a new link.")
        return
    USER_COOLDOWNS[user_id] = current_time

    url = event.text.strip()
    status_message = await event.reply("🔎 `Fetching link details...`")
    info = await get_link_info(url)
    if not info.get("success"):
        await status_message.edit(f"❌ **Error:**\n`{info.get('error')}`"); return

    job_id = uuid.uuid4().hex[:16]
    DOWNLOAD_JOBS[job_id] = {"info": info, "cancellation_event": asyncio.Event(), "status_message": status_message}
    logging.info(f"Created job {job_id}")

    buttons = [[Button.inline("✅ Proceed", data=f"dl_{job_id}"), Button.inline("❌ Cancel", data=f"cancel_{job_id}")]]
    response_text = (f"**File Details:**\n\n**File Name:** `{info['file_name']}`\n**Size:** `{format_bytes(info['content_size'])}`")
    await status_message.edit(response_text, buttons=buttons)

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data_parts = event.data.decode('utf-8').split('_', 1)
    action = data_parts[0]
    job_id = data_parts[1]
    
    if action == "livecancel":
        job = DOWNLOAD_JOBS.get(job_id)
        if job: job["cancellation_event"].set(); logging.info(f"Cancellation requested for job {job_id}."); await event.answer("Cancellation signal sent...")
        else: await event.answer("This job is already completed or invalid.", alert=True)
        return

    job_data = DOWNLOAD_JOBS.get(job_id)
    if not job_data:
        await event.answer("This job has expired or is invalid.", alert=True); return

    if action == "cancel":
        DOWNLOAD_JOBS.pop(job_id, None); await event.edit("**Download canceled by user.** 🤷‍♂️"); logging.info(f"Canceled job {job_id}.")
        return

    if action == "dl":
        try:
            # --- Queue Logic ---
            await download_queue.put(job_id)
            position = download_queue.qsize()
            await event.edit(f"✅ **Request accepted!** You are number **{position}** in the queue.", buttons=None)
        except Exception as e:
            await event.edit(f"❌ Error adding to queue: {e}")

async def download_worker(name: str):
    """A worker that processes download jobs from the queue."""
    while True:
        job_id = await download_queue.get()
        logging.info(f"Worker {name} started processing job {job_id}")
        
        job_data = DOWNLOAD_JOBS.get(job_id)
        if not job_data:
            logging.warning(f"Worker {name} found an invalid job_id: {job_id}"); download_queue.task_done(); continue
        
        status_message = job_data["status_message"]
        info = job_data["info"]
        cancellation_event = job_data["cancellation_event"]
        local_file_path = None
        
        try:
            url, file_name, total_size = info['url'], info['file_name'], info['content_size']
            local_file_path = os.path.join(DOWNLOADS_DIR, f"{status_message.chat_id}_{job_id}_{file_name}")

            downloaded_size, last_update_time, last_downloaded_size = 0, time.time(), 0
            cancel_button = [[Button.inline("❌ Cancel Operation", data=f"livecancel_{job_id}")]]
            
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as r:
                    r.raise_for_status()
                    with open(local_file_path, 'wb') as f:
                        async for chunk in r.content.iter_chunked(1024 * 1024):
                            if cancellation_event.is_set(): raise asyncio.CancelledError
                            f.write(chunk); downloaded_size += len(chunk); current_time = time.time()
                            if current_time - last_update_time > 5:
                                percentage = downloaded_size / total_size * 100 if total_size > 0 else 0
                                speed = (downloaded_size - last_downloaded_size) / (current_time - last_update_time)
                                eta = (total_size - downloaded_size) / speed if speed > 0 else None
                                try: await status_message.edit(f"**Downloading...**\nProgress: `{format_bytes(downloaded_size)}` of `{format_bytes(total_size)}` ({percentage:.1f}%)\nSpeed: `{format_speed(speed)}`\nETA: `{format_time(eta)}`", buttons=cancel_button)
                                except (MessageNotModifiedError, FloodWaitError): pass
                                last_update_time, last_downloaded_size = current_time, downloaded_size
            
            await status_message.edit("`Upload starting...`", buttons=cancel_button)
            
            def create_upload_callback():
                last_upload_update, last_uploaded_size = time.time(), 0
                async def upload_progress_callback(current, total):
                    nonlocal last_upload_update, last_uploaded_size
                    if cancellation_event.is_set(): raise asyncio.CancelledError
                    current_time = time.time()
                    if current_time - last_upload_update > 5:
                        percentage = current / total * 100; speed = (current - last_uploaded_size) / (current_time - last_upload_update)
                        eta = (total - current) / speed if speed > 0 else None
                        try: await status_message.edit(f"**Uploading...**\nProgress: `{format_bytes(current)}` of `{format_bytes(total)}` ({percentage:.1f}%)\nSpeed: `{format_speed(speed)}`\nETA: `{format_time(eta)}`", buttons=cancel_button)
                        except (MessageNotModifiedError, FloodWaitError): pass
                        last_upload_update, last_uploaded_size = current_time, current
                return upload_progress_callback

            attributes = [DocumentAttributeFilename(file_name=file_name)]
            await bot.send_file(status_message.chat_id, local_file_path, caption=f"`{file_name}`", progress_callback=create_upload_callback(), attributes=attributes, cancellable=cancellation_event)
            await status_message.delete()

        except asyncio.CancelledError:
            await status_message.edit("🚫 **Operation Canceled.**"); logging.info(f"Job {job_id} was cancelled successfully.")
        except Exception as e:
            logging.error(f"Error on job {job_id}: {e}"); await status_message.edit(f"❌ **An unexpected error occurred:**\n`{str(e)}`")
        finally:
            DOWNLOAD_JOBS.pop(job_id, None)
            if local_file_path and os.path.exists(local_file_path):
                os.remove(local_file_path)
            logging.info(f"Worker {name} finished job {job_id}")
            download_queue.task_done()

# --- Graceful Shutdown and Main Execution ---
async def shutdown(loop):
    logging.warning("Shutdown signal received. Cleaning up...")
    for filename in os.listdir(DOWNLOADS_DIR):
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        try: os.remove(file_path)
        except Exception: pass
    if bot.is_connected(): await bot.disconnect(); logging.info("Bot disconnected.")
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]; [task.cancel() for task in tasks]
    await asyncio.gather(*tasks, return_exceptions=True); loop.stop()

async def main():
    load_blacklist()
    # Create and start worker tasks
    worker_tasks = []
    for i in range(MAX_CONCURRENT_DOWNLOADS):
        task = asyncio.create_task(download_worker(f"Worker-{i+1}"))
        worker_tasks.append(task)
    
    await bot.start(bot_token=BOT_TOKEN)
    logging.info(f"Bot is running with {MAX_CONCURRENT_DOWNLOADS} download workers...")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(loop)))
    try: loop.run_until_complete(main())
    finally: loop.close(); logging.info("Event loop closed. Exiting.")