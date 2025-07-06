import os
import re
import logging
import asyncio
import requests
from urllib.parse import unquote, urlparse
import time

from dotenv import load_dotenv
from telethon import TelegramClient, events

# --- Basic Configuration ---
# Enable logging for better debugging.
logging.basicConfig(format='[%(levelname) 5s/%(asctime)s] %(name)s: %(message)s',
                    level=logging.INFO)

# Load environment variables from .env file.
load_dotenv()


# --- Telegram API Credentials ---
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')


# --- Initialize the Bot Client ---
# 'DlinkX_bot' is the session name. It will be created on the first run.
bot = TelegramClient('DlinkX_bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)


# --- Helper Functions ---
def get_link_info(url: str) -> dict:
    """
    Fetches information about a URL without downloading the content.
    Returns a dictionary with 'file_name', 'content_type', and 'content_size'.
    """
    try:
        # Use a HEAD request to get headers without downloading the body.
        with requests.head(url, allow_redirects=True, stream=True, timeout=10) as r:
            r.raise_for_status()  # Raise an exception for bad status codes.

            # --- Extract File Name ---
            file_name = "Unknown"
            if 'content-disposition' in r.headers:
                # Try to get filename from 'Content-Disposition' header.
                cd = r.headers.get('content-disposition')
                file_name_match = re.search(r'filename="?([^"]+)"?', cd)
                if file_name_match:
                    file_name = unquote(file_name_match.group(1))
            else:
                # Fallback to getting filename from URL path.
                path = urlparse(url).path
                file_name = unquote(os.path.basename(path)) if path else "download"

            # --- Extract Content Type and Size ---
            content_type = r.headers.get('content-type', 'application/octet-stream')
            content_size = int(r.headers.get('content-length', 0))

            return {
                "success": True,
                "url": r.url, # The final URL after redirects.
                "file_name": file_name,
                "content_type": content_type,
                "content_size": content_size,
            }
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to fetch info for {url}: {e}")
        return {"success": False, "error": str(e)}

def format_bytes(size: int) -> str:
    """Formats a size in bytes into a human-readable string (KB, MB, GB)."""
    if size == 0:
        return "0 B"
    power = 1024
    n = 0
    power_labels = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size >= power and n < len(power_labels) -1 :
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}B"


# --- Event Handlers ---
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    """Handler for the /start command."""
    sender = await event.get_sender()
    logging.info(f"User {sender.id} started the bot.")
    await event.reply(f"Hello, {sender.first_name}! 👋\n\nI am DlinkX, your advanced link downloader.\nSend me any direct download link and I will handle it for you.")

@bot.on(events.NewMessage(pattern=re.compile(r'https?://\S+')))
async def link_handler(event):
    """Handles incoming messages containing links using a 'download then upload' strategy."""
    url = event.text.strip()
    chat_id = event.chat_id
    conversation_timeout = 300 # 5 minutes

    # Define a directory to store downloads.
    DOWNLOADS_DIR = "downloads"
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    
    local_file_path = None # Variable to hold the path of the downloaded file.

    try:
        async with bot.conversation(chat_id, timeout=conversation_timeout) as conv:
            # --- Step 1: Fetch and display link info ---
            status_message = await conv.send_message("🔎 `Fetching link details...`")
            info = get_link_info(url)

            if not info.get("success"):
                await status_message.edit(f"❌ **Error:** Could not fetch details for the link.\n`{info.get('error')}`")
                return

            # Sanitize file_name to be used in a path.
            file_name = re.sub(r'[\\/*?:"<>|]', "", info['file_name']) # Remove invalid path characters.
            content_size_formatted = format_bytes(info['content_size'])
            
            response_text = (
                f"**File Details:**\n\n"
                f"**File Name:** `{file_name}`\n"
                f"**Size:** `{content_size_formatted}`\n\n"
                f"Do you want to proceed with the download? (yes/no)"
            )
            
            await status_message.edit(response_text)

            # --- Step 2: Wait for user confirmation ---
            user_response = await conv.get_response()

            if user_response.text.lower().strip() != 'yes':
                await conv.send_message("Download canceled by user. 🤷‍♂️")
                return

            await user_response.delete()
            
            # --- Step 3: Download the file to the server ---
            await status_message.edit("`Preparing to download...`")
            
            local_file_path = os.path.join(DOWNLOADS_DIR, f"{chat_id}_{file_name}")
            total_size = info['content_size']
            downloaded_size = 0
            
            with requests.get(info['url'], stream=True) as r:
                r.raise_for_status()
                with open(local_file_path, 'wb') as f:
                    # Download in chunks to manage memory usage for large files.
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        percentage = downloaded_size / total_size * 100
                        time.sleep(1)  # Simulate a delay for the progress update.
                        await status_message.edit(
                            f"**Downloading to server...**\n"
                            f"`{format_bytes(downloaded_size)}` of `{format_bytes(total_size)}` ({percentage:.1f}%)"
                        )
            
            await status_message.edit("`Download complete. Starting upload to Telegram...`")

            # --- Step 4: Upload the local file to Telegram ---
            async def upload_progress_callback(current, total):
                percentage = current / total * 100
                await status_message.edit(
                    f"**Uploading to you...**\n"
                    f"`{format_bytes(current)}` of `{format_bytes(total)}` ({percentage:.1f}%)"
                )

            await bot.send_file(
                chat_id,
                local_file_path,
                caption=f"`{file_name}`\n\nDownloaded via **DlinkX**.",
                progress_callback=upload_progress_callback
            )
            
            await status_message.delete()

    except asyncio.TimeoutError:
        await bot.send_message(chat_id, "Conversation timed out. Please send the link again.")
    except Exception as e:
        logging.error(f"An unexpected error occurred in link_handler: {e}")
        await event.reply(f"An unexpected error occurred: `{str(e)}`")
    finally:
        # --- Step 5: Clean up ---
        # Ensure the downloaded file is deleted from the server after upload or on error.
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