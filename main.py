import os
import re
import logging
import asyncio
import aiohttp
import uuid
import time
import signal
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

# --- Helper Functions (Unchanged) ---
def format_bytes(size: int) -> str:
    if size == 0: return "0 B"
    power = 1024; n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size >= power and n < len(power_labels) - 1:
        size /= power; n += 1
    return f"{size:.2f} {power_labels[n]}B"

def format_speed(speed_bytes_per_sec: float) -> str:
    if speed_bytes_per_sec == 0: return "0 B/s"
    return f"{format_bytes(int(speed_bytes_per_sec))}/s"

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

# --- Event Handlers ---
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    await event.reply("Hello! I am DlinkX. Send me a direct download link.")

@bot.on(events.NewMessage(pattern=re.compile(r'https?://\S+')))
async def link_handler(event):
    url = event.text.strip()
    status_message = await event.reply("🔎 `Fetching link details...`")
    info = await get_link_info(url)
    if not info.get("success"):
        await status_message.edit(f"❌ **Error:**\n`{info.get('error')}`")
        return

    job_id = uuid.uuid4().hex[:16]
    DOWNLOAD_JOBS[job_id] = {"info": info, "cancellation_event": asyncio.Event()}
    logging.info(f"Created job {job_id}")

    buttons = [[Button.inline("✅ Proceed", data=f"dl_{job_id}"), Button.inline("❌ Cancel", data=f"cancel_{job_id}")]]
    response_text = (
        f"**File Details:**\n\n"
        f"**File Name:** `{info['file_name']}`\n"
        f"**Size:** `{format_bytes(info['content_size'])}`"
    )
    await status_message.edit(response_text, buttons=buttons)

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    data_parts = event.data.decode('utf-8').split('_', 1)
    action = data_parts[0]
    job_id = data_parts[1]
    
    # --- Live Cancel Handling ---
    if action == "livecancel":
        job = DOWNLOAD_JOBS.get(job_id)
        if job:
            job["cancellation_event"].set()
            logging.info(f"Cancellation requested for job {job_id}.")
            await event.answer("Cancellation signal sent...")
        else:
            await event.answer("This job is already completed or invalid.", alert=True)
        return

    # --- **FIX**: Use .get() instead of .pop() to keep the job state alive ---
    job_data = DOWNLOAD_JOBS.get(job_id)
    if not job_data:
        await event.answer("This job has expired or is invalid.", alert=True)
        return

    if action == "cancel":
        DOWNLOAD_JOBS.pop(job_id, None) # Clean up the job
        await event.edit("**Download canceled by user.** 🤷‍♂️")
        logging.info(f"Canceled job {job_id}.")
        return

    if action == "dl":
        local_file_path = None
        info = job_data["info"]
        cancellation_event = job_data["cancellation_event"]
        try:
            await event.answer("Request accepted! Starting process...")
            url, file_name, total_size = info['url'], info['file_name'], info['content_size']
            local_file_path = os.path.join(DOWNLOADS_DIR, f"{event.chat_id}_{job_id}_{file_name}")

            downloaded_size, last_update_time, last_downloaded_size = 0, time.time(), 0
            cancel_button = [[Button.inline("❌ Cancel Operation", data=f"livecancel_{job_id}")]]
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as r:
                    r.raise_for_status()
                    with open(local_file_path, 'wb') as f:
                        async for chunk in r.content.iter_chunked(1024 * 1024):
                            if cancellation_event.is_set(): raise asyncio.CancelledError
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            current_time = time.time()
                            if current_time - last_update_time > 5:
                                percentage = downloaded_size / total_size * 100
                                speed = (downloaded_size - last_downloaded_size) / (current_time - last_update_time)
                                try:
                                    await event.edit(
                                        f"**Downloading...**\n"
                                        f"Progress: `{format_bytes(downloaded_size)}` of `{format_bytes(total_size)}` ({percentage:.1f}%)\n"
                                        f"Speed: `{format_speed(speed)}`",
                                        buttons=cancel_button)
                                except (MessageNotModifiedError, FloodWaitError): pass
                                last_update_time, last_downloaded_size = current_time, downloaded_size

            await event.edit("`Upload starting...`", buttons=cancel_button)
            
            def create_upload_callback():
                last_upload_update, last_uploaded_size = time.time(), 0
                async def upload_progress_callback(current, total):
                    nonlocal last_upload_update, last_uploaded_size
                    if cancellation_event.is_set(): raise asyncio.CancelledError
                    current_time = time.time()
                    if current_time - last_upload_update > 5:
                        percentage = current / total * 100
                        speed = (current - last_uploaded_size) / (current_time - last_upload_update)
                        try:
                            await event.edit(
                                f"**Uploading...**\n"
                                f"Progress: `{format_bytes(current)}` of `{format_bytes(total)}` ({percentage:.1f}%)\n"
                                f"Speed: `{format_speed(speed)}`",
                                buttons=cancel_button)
                        except (MessageNotModifiedError, FloodWaitError): pass
                        last_upload_update, last_uploaded_size = current_time, current
                return upload_progress_callback

            attributes = [DocumentAttributeFilename(file_name=file_name)]
            await bot.send_file(event.chat_id, local_file_path,
                caption=f"`{file_name}`", progress_callback=create_upload_callback(),
                attributes=attributes, cancellable=cancellation_event)
            
            await event.delete()

        except asyncio.CancelledError:
            await event.edit("🚫 **Operation Canceled.**")
            logging.info(f"Job {job_id} was cancelled successfully.")
        except Exception as e:
            logging.error(f"Error on job {job_id}: {e}")
            await event.edit(f"❌ **An unexpected error occurred:**\n`{str(e)}`")
        finally:
            # --- **FIX**: Clean up job and file in the finally block ---
            DOWNLOAD_JOBS.pop(job_id, None) 
            if local_file_path and os.path.exists(local_file_path):
                os.remove(local_file_path)
                logging.info(f"Cleaned up for job {job_id}")

# --- Graceful Shutdown and Main Execution ---
async def shutdown(loop):
    """Graceful shutdown handler."""
    logging.warning("Shutdown signal received. Cleaning up...")
    # Clean up all files in the downloads directory
    for filename in os.listdir(DOWNLOADS_DIR):
        file_path = os.path.join(DOWNLOADS_DIR, filename)
        try:
            os.remove(file_path)
            logging.info(f"Deleted orphan file: {filename}")
        except Exception as e:
            logging.error(f"Failed to delete {file_path}: {e}")
    
    # Disconnect the bot
    if bot.is_connected():
        await bot.disconnect()
        logging.info("Bot disconnected.")
    
    # Find and cancel all pending tasks
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    [task.cancel() for task in tasks]
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()

async def main():
    """Main function to start the bot and register signal handlers."""
    await bot.start(bot_token=BOT_TOKEN)
    logging.info("Bot is running...")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    # --- **FIX**: Use asyncio's built-in signal handling ---
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(loop)))
    
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
        logging.info("Event loop closed. Exiting.")