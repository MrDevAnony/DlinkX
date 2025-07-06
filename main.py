import os
import re
import logging
import asyncio
import aiohttp
import uuid
import time
import signal
import psutil
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.errors.rpcerrorlist import FloodWaitError, MessageNotModifiedError
from telethon.tl.types import DocumentAttributeFilename

# --- Basic Configuration ---
logging.basicConfig(format='[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s',
                    level=logging.INFO)
load_dotenv()

# --- Telegram API Credentials & Admin Config ---
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')
try:
    admin_ids_str = os.getenv('ADMIN_IDS', '')
    ADMIN_IDS = {int(admin_id.strip()) for admin_id in admin_ids_str.split(',') if admin_id}
except ValueError:
    logging.error("Invalid ADMIN_IDS in .env file. Please use comma-separated numeric IDs.")
    ADMIN_IDS = set()

# --- Initialize the Bot Client ---
bot = TelegramClient('DlinkX_bot', API_ID, API_HASH)

# --- Constants & State Management ---
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
DOWNLOAD_JOBS = {} 
BLACKLIST_FILE = "blacklist.txt"
BLACKLISTED_USERS = set()
USER_COOLDOWNS = {}
COOLDOWN_SECONDS = 10
MAX_CONCURRENT_DOWNLOADS = 2
download_queue = asyncio.Queue()

# --- Helper Functions ---
def format_bytes(size: int) -> str:
    if size == 0: return "0 B"
    power = 1024; n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size >= power and n < len(power_labels) - 1:
        size /= power; n += 1
    return f"{size:.2f} {power_labels[n]}B"

def format_speed(speed: float) -> str: return f"{format_bytes(int(speed))}/s"
def format_time(seconds: int) -> str:
    if seconds is None or seconds <= 0: return "N/A"
    seconds = int(seconds); minutes, seconds = divmod(seconds, 60); hours, minutes = divmod(minutes, 60)
    if hours > 0: return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0: return f"{minutes}m {seconds}s"
    else: return f"{seconds}s"
def load_blacklist():
    try:
        if os.path.exists(BLACKLIST_FILE):
            with open(BLACKLIST_FILE, 'r') as f: BLACKLISTED_USERS.update(int(line.strip()) for line in f)
            logging.info(f"Loaded {len(BLACKLISTED_USERS)} user(s) from blacklist.")
    except Exception as e: logging.error(f"Error loading blacklist file: {e}")
def update_blacklist_file():
    try:
        with open(BLACKLIST_FILE, 'w') as f:
            for user_id in BLACKLISTED_USERS: f.write(f"{user_id}\n")
    except Exception as e: logging.error(f"Error writing to blacklist file: {e}")
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
                # We still get the content_size, but we won't use it for the upload promise
                return {"success": True, "url": str(r.url), "file_name": file_name, "content_size": int(r.headers.get('Content-Length', 0))}
    except Exception as e:
        logging.error(f"AIOHTTP failed to fetch info for {url}: {e}"); return {"success": False, "error": str(e)}

# --- Event Handlers ---
@bot.on(events.NewMessage(pattern='/status'))
async def status_handler(event):
    if event.sender_id not in ADMIN_IDS: return
    cpu = psutil.cpu_percent(); ram = psutil.virtual_memory().percent
    active = len(DOWNLOAD_JOBS); queued = download_queue.qsize()
    await event.reply(f"**Bot Status**\n\n⚙️ **CPU:** `{cpu}%` | 🧠 **RAM:** `{ram}%`\n🔄 **Active:** `{active}` | ⏳ **Queued:** `{queued}`")
@bot.on(events.NewMessage(pattern=r'/ban (\d+)'))
async def ban_handler(event):
    if event.sender_id not in ADMIN_IDS: return
    try: user_id = int(event.pattern_match.group(1)); BLACKLISTED_USERS.add(user_id); update_blacklist_file(); await event.reply(f"✅ User `{user_id}` banned.")
    except Exception as e: await event.reply(f"❌ Error: {e}")
@bot.on(events.NewMessage(pattern=r'/unban (\d+)'))
async def unban_handler(event):
    if event.sender_id not in ADMIN_IDS: return
    try: user_id = int(event.pattern_match.group(1)); BLACKLISTED_USERS.discard(user_id); update_blacklist_file(); await event.reply(f"✅ User `{user_id}` unbanned.")
    except Exception as e: await event.reply(f"❌ Error: {e}")
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event): await event.reply("Hello! I am DlinkX, your smart file streamer.")
@bot.on(events.NewMessage(pattern=re.compile(r'https?://\S+')))
async def link_handler(event):
    user_id = event.sender_id
    if user_id in BLACKLISTED_USERS: logging.warning(f"Ignoring request from blacklisted user {user_id}"); return
    current_time = time.time()
    if current_time - USER_COOLDOWNS.get(user_id, 0) < COOLDOWN_SECONDS:
        await event.reply(f"Please wait `{COOLDOWN_SECONDS}` seconds."); return
    USER_COOLDOWNS[user_id] = current_time
    url = event.text.strip()
    status_message = await event.reply("🔎 `Fetching link details...`")
    info = await get_link_info(url)
    if not info.get("success"): await status_message.edit(f"❌ **Error:**\n`{info.get('error')}`"); return
    job_id = uuid.uuid4().hex[:16]
    DOWNLOAD_JOBS[job_id] = {"info": info, "cancellation_event": asyncio.Event(), "status_message": status_message}
    logging.info(f"Created job {job_id}")
    buttons = [[Button.inline("✅ Proceed", data=f"dl_{job_id}"), Button.inline("❌ Cancel", data=f"cancel_{job_id}")]]
    response_text = (f"**File Details:**\n\n**File Name:** `{info['file_name']}`\n**Size:** `{format_bytes(info['content_size'])}`")
    await status_message.edit(response_text, buttons=buttons)
@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data_parts = event.data.decode('utf-8').split('_', 1); action = data_parts[0]; job_id = data_parts[1]
    if action == "livecancel":
        if job := DOWNLOAD_JOBS.get(job_id): job["cancellation_event"].set(); await event.answer("Cancellation signal sent...")
        else: await event.answer("Job already completed or invalid.", alert=True)
        return
    job_data = DOWNLOAD_JOBS.get(job_id)
    if not job_data: await event.answer("Job expired or invalid.", alert=True); return
    if action == "cancel": DOWNLOAD_JOBS.pop(job_id, None); await event.edit("**Download canceled by user.** 🤷‍♂️"); return
    if action == "dl":
        try: await download_queue.put(job_id); await event.edit(f"✅ **Request accepted!** You are number **{download_queue.qsize()}** in the queue.", buttons=None)
        except Exception as e: await event.edit(f"❌ Error adding to queue: {e}")

# --- Smart Stream Worker ---
async def download_worker(name: str):
    while True:
        job_id = await download_queue.get(); logging.info(f"Worker {name} started job {job_id}")
        job_data = DOWNLOAD_JOBS.get(job_id)
        if not job_data: logging.warning(f"Invalid job_id: {job_id}"); download_queue.task_done(); continue
        
        status_message = job_data["status_message"]; info = job_data["info"]; cancellation_event = job_data["cancellation_event"]
        
        try:
            url, file_name, total_size = info['url'], info['file_name'], info['content_size']
            await status_message.edit("`Preparing to stream...`")

            def create_progress_callback():
                last_update, last_transferred = time.time(), 0
                async def progress_callback(current, total):
                    nonlocal last_update, last_transferred
                    if cancellation_event.is_set(): raise asyncio.CancelledError
                    current_time = time.time()
                    if current_time - last_update > 5:
                        speed = (current - last_transferred) / (current_time - last_update) if current_time > last_update else 0
                        # --- MODIFICATION: Handle unknown total size ---
                        if total:
                            percentage = current / total * 100
                            eta = (total - current) / speed if speed > 0 else None
                            progress_str = f"`{format_bytes(current)}` of `{format_bytes(total)}` ({percentage:.1f}%)"
                        else: # When total size is not known
                            progress_str = f"`{format_bytes(current)}` transferred"
                            eta = None
                        
                        try:
                            await status_message.edit(
                                f"**Streaming to Telegram...**\n"
                                f"Progress: {progress_str}\n"
                                f"Speed: `{format_speed(speed)}`\n"
                                f"ETA: `{format_time(eta)}`",
                                buttons=[[Button.inline("❌ Cancel", data=f"livecancel_{job_id}")]]
                            )
                        except (MessageNotModifiedError, FloodWaitError): pass
                        last_update, last_transferred = current_time, current
                return progress_callback

            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as r:
                    r.raise_for_status()
                    
                    attributes = [DocumentAttributeFilename(file_name=file_name)]
                    # --- FIX: Remove the `file_size` argument ---
                    # Let Telethon figure out the size as it uploads.
                    await bot.send_file(
                        status_message.chat_id,
                        file=r.content,
                        caption=f"`{file_name}`",
                        progress_callback=create_progress_callback(),
                        attributes=attributes,
                        cancellable=cancellation_event
                    )
            await status_message.delete()

        except asyncio.CancelledError:
            await status_message.edit("🚫 **Operation Canceled.**"); logging.info(f"Job {job_id} was cancelled successfully.")
        except Exception as e:
            logging.error(f"Error on job {job_id}: {e}"); await status_message.edit(f"❌ **An unexpected error occurred:**\n`{str(e)}`")
        finally:
            DOWNLOAD_JOBS.pop(job_id, None) 
            logging.info(f"Worker {name} finished job {job_id}")
            download_queue.task_done()

# --- Main Execution ---
async def shutdown(loop):
    logging.warning("Shutdown signal received. Cleaning up..."); [task.cancel() for task in asyncio.all_tasks() if t is not asyncio.current_task()]
    if bot.is_connected(): await bot.disconnect()
    await asyncio.gather(*[t for t in asyncio.all_tasks() if t is not asyncio.current_task()], return_exceptions=True)
async def main():
    load_blacklist()
    worker_tasks = [asyncio.create_task(download_worker(f"Worker-{i+1}")) for i in range(MAX_CONCURRENT_DOWNLOADS)]
    await bot.start(bot_token=BOT_TOKEN)
    logging.info(f"Bot is running with {MAX_CONCURRENT_DOWNLOADS} smart stream workers...")
    await asyncio.gather(bot.run_until_disconnected(), *worker_tasks)
if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    main_task = None
    def signal_handler():
        if main_task: main_task.cancel()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, signal_handler)
    try:
        main_task = loop.create_task(main())
        loop.run_until_complete(main_task)
    except asyncio.CancelledError: logging.info("Main task cancelled.")
    finally:
        cleanup_task = loop.create_task(shutdown(loop))
        loop.run_until_complete(cleanup_task)
        loop.close()
        logging.info("Event loop closed. Exiting.")