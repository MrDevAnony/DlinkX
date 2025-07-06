import os
import re
import logging
import asyncio
import aiohttp
import uuid  # Import the uuid library
from urllib.parse import unquote, urlparse
import time

from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.errors.rpcerrorlist import FloodWaitError
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
bot = TelegramClient('DlinkX_bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# --- Constants & State Management ---
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)
DOWNLOAD_JOBS = {}  # In-memory storage for active download jobs

# --- Helper Functions (No changes here) ---
async def get_link_info(url: str) -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(url, allow_redirects=True, timeout=10) as r:
                r.raise_for_status()
                file_name = "Unknown"
                if 'Content-Disposition' in r.headers:
                    cd = r.headers.get('Content-Disposition')
                    file_name_match = re.search(r'filename="?([^"]+)"?', cd)
                    if file_name_match:
                        file_name = unquote(file_name_match.group(1))
                else:
                    path = urlparse(str(r.url)).path
                    file_name = unquote(os.path.basename(path)) if path else "download"
                file_name = re.sub(r'[\\/*?:"<>|]', "", file_name)
                return {
                    "success": True, "url": str(r.url), "file_name": file_name,
                    "content_size": int(r.headers.get('Content-Length', 0)),
                }
    except Exception as e:
        logging.error(f"AIOHTTP failed to fetch info for {url}: {e}")
        return {"success": False, "error": str(e)}

def format_bytes(size: int) -> str:
    if size == 0: return "0 B"
    power = 1024
    n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size >= power and n < len(power_labels) - 1:
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}B"

# --- Event Handlers ---
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    await event.reply("Hello! I am DlinkX. Send me a direct download link.")

@bot.on(events.NewMessage(pattern=re.compile(r'https?://\S+')))
async def link_handler(event):
    """
    Handles links, stores job info, and sends buttons with a unique job ID.
    """
    url = event.text.strip()
    status_message = await event.reply("🔎 `Fetching link details...`")

    info = await get_link_info(url)

    if not info.get("success"):
        await status_message.edit(f"❌ **Error:** Could not fetch details for the link.\n`{info.get('error')}`")
        return

    # --- Create a unique job ID and store job details ---
    job_id = uuid.uuid4().hex[:16]  # A short, unique ID
    DOWNLOAD_JOBS[job_id] = info
    logging.info(f"Created job {job_id} for URL: {info['url']}")

    # --- Create inline buttons with the short job ID ---
    buttons = [
        [Button.inline("✅ Proceed", data=f"dl_{job_id}"),
         Button.inline("❌ Cancel", data=f"cancel_{job_id}")]
    ]
    
    response_text = (
        f"**File Details:**\n\n"
        f"**File Name:** `{info['file_name']}`\n"
        f"**Size:** `{format_bytes(info['content_size'])}`\n\n"
        f"Do you want to proceed?"
    )
    
    await status_message.edit(response_text, buttons=buttons)

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    """Handles button clicks with optimized progress updates."""
    data_parts = event.data.decode('utf-8').split('_', 1)
    action = data_parts[0]
    job_id = data_parts[1]
    
    chat_id = event.chat_id
    local_file_path = None

    job_info = DOWNLOAD_JOBS.pop(job_id, None)

    if not job_info:
        await event.answer("This download job has expired or is invalid.", alert=True)
        await event.edit("This action has expired.")
        return

    if action == "cancel":
        await event.edit("**Download canceled by user.** 🤷‍♂️")
        logging.info(f"Canceled job {job_id}.")
        return

    if action == "dl":
        try:
            await event.answer("Request accepted! Starting process...")
            
            url = job_info['url']
            file_name = job_info['file_name']
            total_size = job_info['content_size']
            local_file_path = os.path.join(DOWNLOADS_DIR, f"{chat_id}_{job_id}_{file_name}")

            # --- Optimized Async Download ---
            downloaded_size = 0
            last_update_time = time.time()

            async with aiohttp.ClientSession() as session:
                async with session.get(url) as r:
                    r.raise_for_status()
                    with open(local_file_path, 'wb') as f:
                        async for chunk in r.content.iter_chunked(1024 * 1024): # 1MB chunks
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            
                            # --- THROTTLING LOGIC ---
                            # Only edit the message every 5 seconds to avoid flood waits and improve speed
                            current_time = time.time()
                            if current_time - last_update_time > 5:
                                percentage = downloaded_size / total_size * 100
                                try:
                                    await event.edit(
                                        f"**Downloading to server...**\n"
                                        f"File: `{file_name}`\n"
                                        f"Progress: `{format_bytes(downloaded_size)}` of `{format_bytes(total_size)}` ({percentage:.1f}%)"
                                    )
                                    last_update_time = current_time
                                except FloodWaitError as e:
                                    await asyncio.sleep(e.seconds)

            await event.edit(f"`Upload starting for {file_name}...`")

            # --- Optimized Async Upload ---
            # We create a closure to keep track of the last update time for the upload progress
            def create_upload_callback():
                last_upload_update = time.time()
                
                async def upload_progress_callback(current, total):
                    nonlocal last_upload_update
                    current_time = time.time()
                    if current_time - last_upload_update > 5:
                        percentage = current / total * 100
                        try:
                            await event.edit(f"**Uploading to you...**\n`{format_bytes(current)}` of `{format_bytes(total)}` ({percentage:.1f}%)")
                            last_upload_update = current_time
                        except FloodWaitError as e:
                            await asyncio.sleep(e.seconds)
                
                return upload_progress_callback

            attributes = [DocumentAttributeFilename(file_name=file_name)]
            await bot.send_file(
                chat_id, local_file_path,
                caption=f"`{file_name}`\n\nDownloaded via **DlinkX**.",
                progress_callback=create_upload_callback(), 
                attributes=attributes
            )
            
            await event.delete()

        except Exception as e:
            logging.error(f"Error on job {job_id}: {e}")
            await event.edit(f"❌ **An unexpected error occurred:**\n`{str(e)}`")
        finally:
            if local_file_path and os.path.exists(local_file_path):
                os.remove(local_file_path)
                logging.info(f"Cleaned up temporary file for job {job_id}")

# --- Main Execution ---
async def main():
    logging.info("Bot is starting...")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    bot.loop.run_until_complete(main())