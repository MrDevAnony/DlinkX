import os
import re
import logging
import asyncio
import aiohttp
from urllib.parse import unquote, urlparse

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

# --- Constants ---
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)


# --- Helper Functions ---
async def get_link_info(url: str) -> dict:
    """
    Asynchronously fetches information about a URL using aiohttp.
    """
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
                
                # Sanitize file_name for safety
                file_name = re.sub(r'[\\/*?:"<>|]', "", file_name)

                return {
                    "success": True,
                    "url": str(r.url),
                    "file_name": file_name,
                    "content_size": int(r.headers.get('Content-Length', 0)),
                }
    except Exception as e:
        logging.error(f"AIOHTTP failed to fetch info for {url}: {e}")
        return {"success": False, "error": str(e)}

def format_bytes(size: int) -> str:
    """Formats a size in bytes into a human-readable string."""
    if size == 0:
        return "0 B"
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
    """Handler for the /start command."""
    sender = await event.get_sender()
    logging.info(f"User {sender.id} started the bot.")
    await event.reply(
        f"Hello, {sender.first_name}! 👋\n\n"
        "I am DlinkX, your advanced link downloader.\n"
        "Send me any direct download link and I will handle it for you."
    )

@bot.on(events.NewMessage(pattern=re.compile(r'https?://\S+')))
async def link_handler(event):
    """
    Handles incoming messages with links. It now presents inline buttons for confirmation.
    """
    url = event.text.strip()
    status_message = await event.reply("🔎 `Fetching link details...`")

    info = await get_link_info(url)

    if not info.get("success"):
        await status_message.edit(f"❌ **Error:** Could not fetch details for the link.\n`{info.get('error')}`")
        return

    # Create inline buttons
    buttons = [
        [Button.inline("✅ Proceed", data=f"dl_{info['url']}"),
         Button.inline("❌ Cancel", data="cancel")]
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
    """Handles button clicks from inline keyboards."""
    data = event.data.decode('utf-8')
    chat_id = event.chat_id
    message_id = event.message_id
    
    local_file_path = None

    try:
        if data == "cancel":
            await event.edit("**Download canceled by user.** 🤷‍♂️")
            return

        if data.startswith("dl_"):
            url = data[3:]
            
            # Acknowledge the button click immediately
            await event.answer("Request accepted! Starting process...")
            
            # --- Download and Upload Logic ---
            info = await get_link_info(url)
            if not info.get("success"):
                await event.edit(f"❌ **Error:** Could not re-verify the link.\n`{info.get('error')}`")
                return

            file_name = info['file_name']
            total_size = info['content_size']
            local_file_path = os.path.join(DOWNLOADS_DIR, f"{chat_id}_{message_id}_{file_name}")

            # --- Async Download ---
            downloaded_size = 0
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as r:
                    r.raise_for_status()
                    with open(local_file_path, 'wb') as f:
                        async for chunk in r.content.iter_chunked(1024 * 1024): # 1MB chunks
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            percentage = downloaded_size / total_size * 100
                            try:
                                await event.edit(
                                    f"**Downloading to server...**\n"
                                    f"`{format_bytes(downloaded_size)}` of `{format_bytes(total_size)}` ({percentage:.1f}%)"
                                )
                            except FloodWaitError as e:
                                logging.warning(f"Flood wait of {e.seconds}s. Pausing updates.")
                                await asyncio.sleep(e.seconds)

            await event.edit("`Download complete. Starting upload to Telegram...`")

            # --- Async Upload ---
            async def upload_progress_callback(current, total):
                percentage = current / total * 100
                try:
                    await event.edit(
                        f"**Uploading to you...**\n"
                        f"`{format_bytes(current)}` of `{format_bytes(total)}` ({percentage:.1f}%)"
                    )
                except FloodWaitError as e:
                    logging.warning(f"Flood wait of {e.seconds}s. Pausing updates.")
                    await asyncio.sleep(e.seconds)
            
            # Using DocumentAttributeFilename to ensure correct filename on Telegram
            attributes = [DocumentAttributeFilename(file_name=file_name)]
            await bot.send_file(
                chat_id,
                local_file_path,
                caption=f"`{file_name}`\n\nDownloaded via **DlinkX**.",
                progress_callback=upload_progress_callback,
                attributes=attributes
            )
            
            await event.delete() # Clean up the status message

    except Exception as e:
        logging.error(f"An error occurred in callback_handler: {e}")
        await event.edit(f"❌ **An unexpected error occurred:**\n`{str(e)}`")
    finally:
        # --- Cleanup ---
        if local_file_path and os.path.exists(local_file_path):
            os.remove(local_file_path)
            logging.info(f"Cleaned up temporary file: {local_file_path}")


# --- Main Execution ---
async def main():
    """Main function to run the bot."""
    logging.info("Bot is starting...")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    bot.loop.run_until_complete(main())