import discord
from discord.ext import commands
import os
from discord import app_commands, ui
from dotenv import load_dotenv
import requests
import json
import sqlite3
import random
import asyncio
import string
from datetime import datetime, timedelta, timezone
import logging
from flask import Flask # Added for optional web server

# Configure logging
logging.basicConfig(level=logging.INFO, # Set to INFO for general logging, DEBUG for more verbose
                    format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('discord_bot') # Get a logger for your bot

# Load environment variables from .env file
load_dotenv()

# Define Intents
intents = discord.Intents.default()
intents.message_content = True # Required for on_message event to read message content
intents.members = True # Required for fetching members in some commands if needed

# Database file name
DATABASE_FILE = 'bot_data.db'

# Specific channel ID for admin commands
# Replace with your actual Discord channel ID where admin commands are allowed
ALLOWED_ADMIN_CHANNEL_ID = 1383013260902531074 # YOUR ADMIN CHANNEL ID HERE (Replace with your actual channel ID)

# Environment Variables
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
YEUMONEY_API_TOKEN = os.getenv('YEUMONEY_API_TOKEN')
PASTEBIN_DEV_KEY = os.getenv('PASTEBIN_DEV_KEY')
# For instant guild command syncing during development
TEST_GUILD_ID = os.getenv('TEST_GUILD_ID') # This will be the ID from your .env

# Cooldown for /getcredit command (in seconds)
GET_CREDIT_COOLDOWN_SECONDS = 5 * 60 # 5 minutes (300 seconds)

# Dictionary to store active multi-line input sessions for /quickaddug
# Key: user_id, Value: list of collected local storage strings
quick_add_ug_sessions = {}

# Function to generate a random alphanumeric code
def generate_random_code(length=20):
    characters = string.ascii_uppercase + string.digits
    return ''.join(random.choice(characters) for i in range(length))

# Function to initialize the database and tables
def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    # main_link table: (No change - existing table, though not actively used for links now)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS main_link (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL
        )
    ''')

    # redemption_codes table: Stores redemption codes, will be deleted after use
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS redemption_codes (
            code TEXT PRIMARY KEY
        )
    ''')

    # user_balances table: User hcoin balances
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_balances (
            user_id INTEGER PRIMARY KEY,
            hcoin_balance INTEGER DEFAULT 0
        )
    ''')

    # ug_phones table: Local Storage data (now with UNIQUE constraint)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ug_phones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_json TEXT NOT NULL UNIQUE
        )
    ''')

    # hcoin_pastebin_links table: (No longer actively used for /getcredit, but kept for compatibility)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hcoin_pastebin_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pastebin_url TEXT NOT NULL UNIQUE
        )
    ''')

    # New table for /getcredit cooldown
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_cooldowns (
            user_id INTEGER PRIMARY KEY,
            last_getcredit_time TEXT NOT NULL
        )
    ''')

    conn.commit()
    conn.close()

    # It's good practice to run deduplication on startup if there's a chance
    # existing data might violate the UNIQUE constraint if it was added later.
    initial_count, final_count = deduplicate_ug_phones_data()
    if initial_count != final_count:
        logger.info(f"Deduplication completed for ug_phones. Initial: {initial_count}, Final: {final_count}. Removed {initial_count - final_count} duplicates.")
    else:
        logger.info("No duplicates found in ug_phones table during startup deduplication.")


# Function to get user hcoin balance
def get_user_hcoin(user_id: int) -> int:
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT hcoin_balance FROM user_balances WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result:
        return result[0]
    return 0

# Function to update user hcoin balance
def update_user_hcoin(user_id: int, amount: int):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO user_balances (user_id, hcoin_balance) VALUES (?, COALESCE((SELECT hcoin_balance FROM user_balances WHERE user_id = ?), 0) + ?)", (user_id, user_id, amount))
    conn.commit()
    conn.close()

# Function to get last getcredit time for a user
def get_last_getcredit_time(user_id: int) -> datetime | None:
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT last_getcredit_time FROM user_cooldowns WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result:
        return datetime.fromisoformat(result[0]).replace(tzinfo=timezone.utc)
    return None

# Function to set last getcredit time for a user
def set_last_getcredit_time(user_id: int, timestamp: datetime):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO user_cooldowns (user_id, last_getcredit_time) VALUES (?, ?)", (user_id, timestamp.isoformat()))
    conn.commit()
    conn.close()

# Function to deduplicate ug_phones data
def deduplicate_ug_phones_data():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    # Get initial count of records
    cursor.execute("SELECT COUNT(*) FROM ug_phones")
    initial_count = cursor.fetchone()[0]

    # Create a temporary table with unique data_json, keeping the smallest ID
    cursor.execute('''
        CREATE TEMPORARY TABLE IF NOT EXISTS ug_phones_temp AS
        SELECT MIN(id) as id, data_json
        FROM ug_phones
        GROUP BY data_json;
    ''')

    # Delete all records from the original table
    cursor.execute('DELETE FROM ug_phones;')

    # Insert unique records back from the temporary table
    cursor.execute('INSERT INTO ug_phones SELECT id, data_json FROM ug_phones_temp;')

    # Drop the temporary table
    cursor.execute('DROP TABLE IF EXISTS ug_phones_temp;')

    # Get final count of records
    cursor.execute("SELECT COUNT(*) FROM ug_phones")
    final_count = cursor.fetchone()[0]

    conn.commit()
    conn.close()

    return initial_count, final_count


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        # Store a reference to the global `quick_add_ug_sessions`
        self.quick_add_ug_sessions = quick_add_ug_sessions

    async def setup_hook(self):
        # Synchronize slash commands with Discord
        if TEST_GUILD_ID:
            try:
                test_guild_id_int = int(TEST_GUILD_ID)
                test_guild = discord.Object(id=test_guild_id_int)
                # Copies global commands to the specific guild
                self.tree.copy_global_to(guild=test_guild)
                # Syncs commands for the test guild. This will add new commands,
                # update changed commands, and DELETE commands no longer present in the code.
                await self.tree.sync(guild=test_guild)
                logger.info(f'Slash commands synced for TEST_GUILD_ID: {test_guild_id_int} (instant sync)! Old commands removed.')
            except ValueError:
                logger.error(f"ERROR: Invalid TEST_GUILD_ID '{TEST_GUILD_ID}' in .env. Falling back to global sync.")
                # Syncs commands globally. This will also delete commands no longer present in the code.
                await self.tree.sync()
                logger.info('Slash commands synced globally (may take up to 1 hour to appear). Old commands removed.')
            except Exception as e:
                logger.error(f"ERROR syncing to specific guild {TEST_GUILD_ID}: {e}. Falling back to global sync.")
                # Syncs commands globally. This will also delete commands no longer present in the code.
                await self.tree.sync()
                logger.info('Slash commands synced globally (may take up to 1 hour to appear). Old commands removed.')
        else:
            # Syncs commands globally. This will also delete commands no longer present in the code.
            await self.tree.sync()
            logger.info('Slash commands synced globally (may take up to 1 hour to appear). Old commands removed.')

    async def on_ready(self):
        logger.info(f'Logged in as {self.user}!')
        logger.info(f'Bot ID: {self.user.id}')
        # Initialize or check the database
        await self.loop.run_in_executor(None, init_db)
        logger.info("Database initialized or checked.")

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        # This is a global error handler for app commands.
        # Use it to catch unhandled errors from your slash commands.
        if isinstance(error, app_commands.CommandInvokeError):
            logger.error(f"CommandInvokeError in command '{interaction.command.name}' by {interaction.user} (ID: {interaction.user.id}) in channel {interaction.channel} (ID: {interaction.channel_id}): {error.original}")
            # Try to send a more user-friendly error message
            try:
                await interaction.response.send_message(f"Đã xảy ra lỗi khi thực thi lệnh: `{error.original}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send(f"Đã xảy ra lỗi khi thực thi lệnh: `{error.original}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)
        elif isinstance(error, app_commands.CheckFailure):
            # Already handled in @bot.tree.error, but good to have a log here too
            logger.warning(f"CheckFailure for command '{interaction.command.name}' by {interaction.user} (ID: {interaction.user.id}) in channel {interaction.channel} (ID: {interaction.channel_id}): {error}")
            # The specific error handler `on_app_command_error` will send the message.
            try:
                await interaction.response.send_message(f"Bạn không có quyền sử dụng lệnh này.", ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send(f"Bạn không có quyền sử dụng lệnh này.", ephemeral=True)
        else:
            logger.error(f"Unhandled app command error in command '{interaction.command.name}' by {interaction.user} (ID: {interaction.user.id}) in channel {interaction.channel} (ID: {interaction.channel_id}): {error}")
            try:
                await interaction.response.send_message(f"Đã xảy ra lỗi không mong muốn: `{error}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send(f"Đã xảy ra lỗi không mong muốn: `{error}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)


bot = MyBot()

# --- on_message event for /quickaddug session ---
@bot.event
async def on_message(message: discord.Message):
    # Ignore messages from the bot itself
    if message.author.id == bot.user.id:
        return

    user_id = message.author.id
    content = message.content.strip() # Keep original case for data storage, use .lower() for keyword checks

    # Check if the user is in a quick_add_ug session
    if user_id in bot.quick_add_ug_sessions: # Use bot.quick_add_ug_sessions
        lower_content = content.lower()
        if lower_content in ["done", "xong", "hoàn tất"]:
            collected_data = bot.quick_add_ug_sessions.pop(user_id) # Get data and remove session
            logger.info(f"User {message.author.display_name} (ID: {user_id}) ended /quickaddug session. Collected {len(collected_data)} items.")

            if not collected_data:
                embed = discord.Embed(
                    title="ℹ️ Phiên kết thúc!",
                    description="Bạn đã kết thúc phiên nhưng không có Local Storage nào được gửi.",
                    color=discord.Color.light_grey()
                )
                await message.channel.send(embed=embed)
            else:
                conn = sqlite3.connect(DATABASE_FILE)
                cursor = conn.cursor()
                added_count = 0
                skipped_count = 0
                error_count = 0

                # Iterate through collected data and insert into DB
                for data_item in collected_data:
                    try:
                        # Use INSERT OR IGNORE to automatically handle duplicates based on UNIQUE constraint
                        cursor.execute("INSERT OR IGNORE INTO ug_phones (data_json) VALUES (?)", (data_item,))
                        if cursor.rowcount > 0: # rowcount > 0 means a new row was inserted
                            added_count += 1
                        else:
                            skipped_count += 1 # Item already exists
                    except sqlite3.Error as e:
                        error_count += 1
                        logger.error(f"SQLite Error adding Local Storage data for user {user_id}: {e}")
                    except Exception as e:
                        error_count += 1
                        logger.error(f"Unexpected error adding Local Storage data for user {user_id}: {e}")
                conn.commit()
                conn.close()

                description = f"**{added_count}** Local Storage đã được thêm thành công vào kho.\n"
                if skipped_count > 0:
                    description += f"**{skipped_count}** Local Storage bị bỏ qua (đã tồn tại).\n"
                if error_count > 0:
                    description += f"**{error_count}** Local Storage gặp lỗi khi thêm. Vui lòng kiểm tra console bot."

                embed = discord.Embed(
                    title="✅ Phiên Thêm Nhanh Local Storage Hoàn Tất!",
                    description=description,
                    color=discord.Color.green()
                )
                embed.set_footer(text="Phiên đã kết thúc. Bạn có thể dùng /list localstorage để xem.")
                await message.channel.send(embed=embed)

        elif lower_content == "cancel":
            if user_id in bot.quick_add_ug_sessions:
                bot.quick_add_ug_sessions.pop(user_id) # Remove session
                logger.info(f"User {message.author.display_name} (ID: {user_id}) cancelled /quickaddug session.")
                embed = discord.Embed(
                    title="❌ Phiên Thêm Nhanh Local Storage đã Hủy!",
                    description="Phiên nhập Local Storage của bạn đã bị hủy bỏ. Không có dữ liệu nào được lưu.",
                    color=discord.Color.red()
                )
                await message.channel.send(embed=embed)
        else:
            # Add the message content to the user's session data
            bot.quick_add_ug_sessions[user_id].append(content) # Use original content here
            logger.debug(f"User {message.author.display_name} (ID: {user_id}) added data to /quickaddug session: {content[:50]}...") # Log first 50 chars
            # Optional: Give a visual confirmation that the message was received
            try:
                await message.add_reaction("✅") # React with a checkmark to confirm receipt
            except discord.Forbidden:
                pass # Bot might not have permission to add reactions

    # Always process other commands after handling custom on_message logic
    # This line is crucial for any prefix commands you might have, or other on_message events.
    await bot.process_commands(message)

# Function to create a paste on Pastebin
def create_pastebin_paste(text_content: str, title: str = "Bot Paste", paste_format: str = "text", expire_date: str = "10M"):
    if not PASTEBIN_DEV_KEY:
        logger.error("Error: PASTEBIN_DEV_KEY is not set in environment variables.")
        return None

    api_url = "https://pastebin.com/api/api_post.php"
    payload = {
        'api_dev_key': PASTEBIN_DEV_KEY,
        'api_option': 'paste',
        'api_paste_code': text_content,
        'api_paste_name': title,
        'api_paste_format': paste_format,
        'api_paste_private': '1', # Unlisted (access only via link)
        'api_paste_expire_date': expire_date,
    }

    try:
        response = requests.post(api_url, data=payload)
        response.raise_for_status()

        if response.status_code == 200 and response.text.startswith("https://pastebin.com/"):
            logger.info(f"Successfully created Pastebin: {response.text}")
            return response.text
        else:
            logger.error(f"Error creating paste on Pastebin.com. API response: {response.text}")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error connecting to Pastebin.com API: {e}")
        return None

# Function to fetch raw content from a Pastebin URL
def fetch_pastebin_content(pastebin_url: str):
    # Convert regular Pastebin URL to raw URL
    if "pastebin.com/" in pastebin_url:
        paste_id = pastebin_url.split('/')[-1]
        raw_url = f"https://pastebin.com/raw/{paste_id}"
    else:
        logger.warning(f"Invalid Pastebin URL format: {pastebin_url}")
        return None

    try:
        response = requests.get(raw_url)
        response.raise_for_status()
        logger.info(f"Successfully fetched content from raw Pastebin URL: {raw_url}")
        return response.text.strip()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching content from raw Pastebin URL '{raw_url}': {e}")
        return None

# Function to create Yeumoney short link
def create_short_link(long_url: str):
    if not YEUMONEY_API_TOKEN:
        logger.error("Error: YEUMONEY_API_TOKEN is not set in environment variables.")
        return None

    api_url = "https://yeumoney.com/QL_api.php"
    params = {
        "token": YEUMONEY_API_TOKEN,
        "url": long_url,
        "format": "json"
    }

    try:
        response = requests.get(api_url, params=params)
        response.raise_for_status()
        result = response.json()

        if result.get("status") == "success" and "shortenedUrl" in result:
            logger.info(f"Successfully created short link: {result['shortenedUrl']}")
            return result["shortenedUrl"]
        else:
            error_message = result.get("message", "Unknown API error.")
            logger.error(f"Error creating short link on Yeumoney.com. API response: {result}. Error: {error_message}")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error connecting to Yeumoney.com API: {e}")
        return None
    except json.JSONDecodeError:
        logger.error(f"Error decoding JSON from Yeumoney.com API response: {response.text}")
        return None


# Custom check for Admin channel
def is_allowed_admin_channel(interaction: discord.Interaction) -> bool:
    """Checks if the command is used in the allowed admin channel."""
    if interaction.channel.id != ALLOWED_ADMIN_CHANNEL_ID:
        logger.warning(f"Admin command '{interaction.command.name}' attempted by {interaction.user.display_name} (ID: {interaction.user.id}) in unauthorized channel #{interaction.channel.name} (ID: {interaction.channel_id}).")
    return interaction.channel.id == ALLOWED_ADMIN_CHANNEL_ID

# Error handler for command checks (This is `bot.tree.error` handler)
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        if isinstance(error, commands.MissingRole):
            logger.warning(f"User {interaction.user.display_name} (ID: {interaction.user.id}) tried to use '{interaction.command.name}' but is missing required role.")
            try:
                await interaction.response.send_message(f"Bạn không có vai trò cần thiết để sử dụng lệnh này.", ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send(f"Bạn không có vai trò cần thiết để sử dụng lệnh này.", ephemeral=True)
        elif isinstance(error, app_commands.CheckFailure) and interaction.command and \
             interaction.command.name in ["remove", "list", "addugphone", "sync_codes", "sync_commands", "deduplicate_ugphone", "quickaddug", "delete_ug_data", "delete_ug_by_id", "add_hcoin", "remove_hcoin"]: # Added "delete_ug_by_id" here
            logger.warning(f"User {interaction.user.display_name} (ID: {interaction.user.id}) tried to use admin command '{interaction.command.name}' in wrong channel.")
            try:
                await interaction.response.send_message(
                    f"Lệnh này chỉ có thể được sử dụng trong kênh quản trị viên: <#{ALLOWED_ADMIN_CHANNEL_ID}>.",
                    ephemeral=True
                )
            except discord.InteractionResponded:
                await interaction.followup.send(
                    f"Lệnh này chỉ có thể được sử dụng trong kênh quản trị viên: <#{ALLOWED_ADMIN_CHANNEL_ID}>.",
                    ephemeral=True
                )
        else:
            logger.error(f"Unhandled CheckFailure for command '{interaction.command.name}' by {interaction.user.display_name} (ID: {interaction.user.id}): {error}")
            try:
                await interaction.response.send_message(f"Bạn không có quyền sử dụng lệnh này.", ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send(f"Bạn không có quyền sử dụng lệnh này.", ephemeral=True)
    elif isinstance(error, app_commands.CommandInvokeError):
        logger.error(f"CommandInvokeError in command '{interaction.command.name}' by {interaction.user.display_name} (ID: {interaction.user.id}) in channel {interaction.channel} (ID: {interaction.channel_id}): {error.original}")
        try:
            await interaction.response.send_message(f"Đã xảy ra lỗi khi thực thi lệnh: `{error.original}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send(f"Đã xảy ra lỗi khi thực thi lệnh: `{error.original}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)
    else:
        logger.critical(f"Unknown AppCommand Error in command '{interaction.command.name}' by {interaction.user.display_name} (ID: {interaction.user.id}) in channel {interaction.channel} (ID: {interaction.channel_id}): {error}")
        try:
            await interaction.response.send_message(f"Đã xảy ra lỗi không mong muốn: `{error}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send(f"Đã xảy ra lỗi không mong muốn: `{error}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)


# --- Commands for Link and Code ---

# /getcredit command (NOW generates a random code, creates Pastebin, saves code, shortens link)
@bot.tree.command(name='getcredit', description='Get a new unique code by generating a Pastebin link.')
async def get_credit(interaction: discord.Interaction):
    user_id = interaction.user.id
    current_time = datetime.now(timezone.utc) # Get current UTC time

    # Check cooldown
    last_time_used = await bot.loop.run_in_executor(None, get_last_getcredit_time, user_id)
    if last_time_used:
        time_elapsed = current_time - last_time_used
        if time_elapsed.total_seconds() < GET_CREDIT_COOLDOWN_SECONDS:
            remaining_time = timedelta(seconds=GET_CREDIT_COOLDOWN_SECONDS) - time_elapsed
            minutes, seconds = divmod(remaining_time.total_seconds(), 60)

            embed = discord.Embed(
                title="⏳ Đang trong thời gian hồi chiêu!",
                description=f"Bạn chỉ có thể sử dụng lệnh này mỗi {GET_CREDIT_COOLDOWN_SECONDS // 60} phút một lần.\n"
                            f"Vui lòng đợi **{int(minutes)}p {int(seconds)}s** trước khi thử lại.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

    await interaction.response.defer()

    # 1. Generate a random unique code
    generated_code = generate_random_code(20) # 20 characters alphanumeric
    logger.info(f"User {interaction.user.display_name} (ID: {user_id}) requested /getcredit. Generated code: {generated_code}")

    # 2. Create a Pastebin with this generated code as content
    paste_title = f"Redeem Code for {interaction.user.name} - {generated_code}"
    expire_date = "10M" # Expires in 10 minutes (link is primary, code is in DB)

    # Use bot.loop.run_in_executor for blocking I/O (requests to Pastebin)
    pastebin_url = await bot.loop.run_in_executor(None, create_pastebin_paste, generated_code, paste_title, "text", expire_date)

    if not pastebin_url:
        embed = discord.Embed(
            title="❌ Không thể tạo mã!",
            description='Không thể tạo Pastebin cho mã của bạn. Vui lòng thử lại sau hoặc liên hệ quản trị viên.',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.error(f"Failed to create Pastebin for user {user_id}'s /getcredit request.")
        return

    # 3. Save the generated code into redemption_codes table
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO redemption_codes (code) VALUES (?)", (generated_code,))
        conn.commit()
        logger.info(f"Code {generated_code} saved to DB for user {user_id}.")
    except sqlite3.IntegrityError:
        conn.close()
        embed = discord.Embed(
            title="❌ Lỗi tạo mã!",
            description='Không thể tạo mã duy nhất. Vui lòng thử lại.',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.error(f"IntegrityError: Generated code {generated_code} already exists in DB for user {user_id}.")
        return
    finally:
        conn.close()

    # 4. Create a short link for the Pastebin URL
    # Use bot.loop.run_in_executor for blocking I/O (requests to Yeumoney)
    short_link = await bot.loop.run_in_executor(None, create_short_link, pastebin_url)

    if short_link:
        # 5. Record the usage time for cooldown
        await bot.loop.run_in_executor(None, set_last_getcredit_time, user_id, current_time)
        logger.info(f"User {user_id} used /getcredit, cooldown set. Short link: {short_link}")

        embed = discord.Embed(
            title="✨ Liên kết mã mới của bạn! ✨",
            description=f"Xin chào **{interaction.user.display_name}**! Đây là liên kết mã duy nhất mới của bạn. "
                        f"Sử dụng mã bên trong liên kết này với `/redeem` để nhận phần thưởng của bạn!",
            color=discord.Color.green()
        )
        embed.add_field(name="🔗 Lấy mã của bạn tại đây:", value=f"**<{short_link}>**", inline=False)
        embed.set_footer(text=f"Bạn có thể sử dụng /getcredit lại sau {GET_CREDIT_COOLDOWN_SECONDS // 60} phút.")
        embed.set_thumbnail(url=interaction.user.display_avatar.url) # Display user's avatar
        embed.timestamp = discord.utils.utcnow()

        await interaction.followup.send(embed=embed)
    else:
        # If short link creation fails, clean up the generated code in DB
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM redemption_codes WHERE code = ?", (generated_code,))
        conn.commit()
        conn.close()
        logger.error(f"Failed to create short link for Pastebin {pastebin_url}. Deleted code {generated_code} from DB.")

        embed = discord.Embed(
            title="❌ Không thể tạo liên kết!",
            description='Không thể tạo liên kết rút gọn vào lúc này. Mã đã tạo đã bị xóa. Vui lòng thử lại sau.',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# /remove command (removes a code from the database)
@bot.tree.command(name='remove', description='Remove a specific redemption code from the list.')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
@app_commands.describe(code='The code you want to remove (e.g., ABCDE12345)')
async def remove_code(interaction: discord.Interaction, code: str):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM redemption_codes WHERE code = ?", (code,))
    conn.commit()
    if cursor.rowcount > 0:
        embed = discord.Embed(
            title="✅ Mã đã xóa thành công!",
            description=f'Mã `{code}` đã được xóa thành công.',
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"Code {code} removed by {interaction.user.display_name} (ID: {interaction.user.id}).")
    else:
        embed = discord.Embed(
            title="❌ Không tìm thấy mã!",
            description=f'Mã `{code}` không tồn tại trong danh sách.',
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.warning(f"Attempt to remove non-existent code {code} by {interaction.user.display_name} (ID: {interaction.user.id}).")
    conn.close()


# Modal for redeeming multiple codes
class RedeemMultipleCodesModal(ui.Modal, title='Đổi Nhiều Mã'):
    codes_input = ui.TextInput(
        label='Dán mã (mỗi mã một dòng)',
        placeholder='Nhập mỗi mã đổi thưởng trên một dòng mới...',
        style=discord.TextStyle.paragraph,
        max_length=4000 # Max length for modal input
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True) # Defer the response as processing might take time

        user_id = interaction.user.id
        hcoin_per_code = 150 # Amount of hcoin rewarded per code

        raw_codes_input = self.codes_input.value
        # Split input by newline, strip whitespace, and filter out empty lines
        codes_to_redeem = [code.strip() for code in raw_codes_input.split('\n') if code.strip()]

        logger.info(f"User {interaction.user.display_name} (ID: {user_id}) submitted {len(codes_to_redeem)} codes via quickredeemmodal.")

        if not codes_to_redeem:
            embed = discord.Embed(
                title="⚠️ Không có mã nào được cung cấp!",
                description="Vui lòng nhập ít nhất một mã để đổi.",
                color=discord.Color.orange()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        redeemed_count = 0
        invalid_count = 0
        total_hcoin_earned = 0
        failed_codes = []

        # Process each code
        for code in codes_to_redeem:
            try:
                cursor.execute("SELECT code FROM redemption_codes WHERE code = ?", (code,))
                existing_code = cursor.fetchone()

                if existing_code:
                    # Code is valid, delete it and update balance
                    cursor.execute("DELETE FROM redemption_codes WHERE code = ?", (code,))
                    # No need to commit here for each, commit once at the end for efficiency

                    redeemed_count += 1
                    total_hcoin_earned += hcoin_per_code
                else:
                    invalid_count += 1
                    failed_codes.append(code)
            except sqlite3.Error as e:
                logger.error(f"SQLite Error processing code '{code}' for redemption by {user_id}: {e}")
                invalid_count += 1 # Treat as invalid for the user report
                failed_codes.append(code)
            except Exception as e:
                logger.error(f"Unexpected error processing code '{code}' for redemption by {user_id}: {e}")
                invalid_count += 1 # Treat as invalid
                failed_codes.append(code)

        # Commit all changes to DB at once after processing all codes
        conn.commit()

        # Update user's hcoin balance if any codes were redeemed
        if total_hcoin_earned > 0:
            await bot.loop.run_in_executor(None, update_user_hcoin, user_id, total_hcoin_earned)

        current_balance = await bot.loop.run_in_executor(None, get_user_hcoin, user_id)
        conn.close()

        # Prepare the response embed
        title = "✨ Kết Quả Đổi Mã ✨"
        color = discord.Color.green() if redeemed_count > 0 else discord.Color.orange()
        description_parts = []

        if redeemed_count > 0:
            description_parts.append(f"✅ Đã đổi thành công **{redeemed_count}** mã.")
            description_parts.append(f"Bạn nhận được tổng cộng **{total_hcoin_earned} coin**.")
            logger.info(f"User {interaction.user.display_name} (ID: {user_id}) redeemed {redeemed_count} codes for {total_hcoin_earned} coins. New balance: {current_balance}.")

        if invalid_count > 0:
            description_parts.append(f"❌ **{invalid_count}** mã không hợp lệ hoặc đã được sử dụng.")
            if failed_codes:
                # Truncate if too many codes for embed limits
                failed_codes_str = ", ".join(failed_codes[:10])
                if len(failed_codes) > 10:
                    failed_codes_str += f", ...và {len(failed_codes) - 10} mã khác"
                description_parts.append(f"Các mã không đổi được: `{failed_codes_str}`")
            logger.warning(f"User {interaction.user.display_name} (ID: {user_id}) had {invalid_count} invalid/used codes. Failed codes: {', '.join(failed_codes)}.")


        description_parts.append(f"\n**Số Coin Hiện Tại:** **{current_balance} coin**")

        embed = discord.Embed(
            title=title,
            description="\n".join(description_parts),
            color=color
        )
        embed.set_footer(text="Cảm ơn bạn đã sử dụng dịch vụ!")
        embed.timestamp = discord.utils.utcnow()

        await interaction.followup.send(embed=embed, ephemeral=False)


# /redeem command (Individual code redemption, now also shows the modal)
@bot.tree.command(name='redeem', description='Redeem a single code or show modal to redeem multiple codes.')
@app_commands.describe(code='The code you want to redeem (leave blank to show modal)')
async def redeem_code(interaction: discord.Interaction, code: str = None):
    if code:
        await interaction.response.defer(ephemeral=True) # Defer for single code processing

        user_id = interaction.user.id
        hcoin_reward = 150 # Amount of hcoin rewarded per code

        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT code FROM redemption_codes WHERE code = ?", (code,))
            existing_code = cursor.fetchone()

            if existing_code:
                cursor.execute("DELETE FROM redemption_codes WHERE code = ?", (code,))
                conn.commit() # Commit immediately for single code

                await bot.loop.run_in_executor(None, update_user_hcoin, user_id, hcoin_reward)
                current_balance = await bot.loop.run_in_executor(None, get_user_hcoin, user_id)

                embed = discord.Embed(
                    title="✅ Đổi mã thành công!",
                    description=f'Bạn đã đổi mã `{code}` và nhận được **{hcoin_reward} coin**.',
                    color=discord.Color.green()
                )
                embed.add_field(name="Số dư hiện tại", value=f"**{current_balance} coin**", inline=True)
                await interaction.followup.send(embed=embed, ephemeral=False)
                logger.info(f"User {interaction.user.display_name} (ID: {user_id}) redeemed code {code} for {hcoin_reward} coins. New balance: {current_balance}.")
            else:
                embed = discord.Embed(
                    title="❌ Mã không hợp lệ!",
                    description=f'Mã `{code}` không tồn tại hoặc đã được sử dụng.',
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                logger.warning(f"User {interaction.user.display_name} (ID: {user_id}) tried to redeem invalid/used code {code}.")
        except sqlite3.Error as e:
            logger.error(f"SQLite Error during /redeem for user {user_id}, code {code}: {e}")
            embed = discord.Embed(
                title="❌ Lỗi!",
                description='Đã xảy ra lỗi khi đổi mã của bạn. Vui lòng thử lại sau.',
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        finally:
            conn.close()
    else:
        logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) used /redeem without a code, showing modal.")
        # If no code is provided, show the modal
        await interaction.response.send_modal(RedeemMultipleCodesModal())


# /quickredeemcode command (NOW just calls the modal for multiple codes)
@bot.tree.command(name='quickredeemcode', description='Redeem multiple codes directly at once.')
async def quick_redeem_code_command_modal(interaction: discord.Interaction):
    logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) used /quickredeemcode (modal).")
    # This command will now just show the modal
    await interaction.response.send_modal(RedeemMultipleCodesModal())


# /list command (List codes, Pastebin links, or Local Storage)
@bot.tree.command(name='list', description='Display a list of codes, Pastebin links, or Local Storage data.')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
@app_commands.describe(type_to_list='Choose what to list: "code", "link", or "localstorage".')
@app_commands.choices(type_to_list=[
    app_commands.Choice(name="Codes", value="code"),
    app_commands.Choice(name="Pastebin Links", value="link"),
    app_commands.Choice(name="Local Storage", value="localstorage")
])
async def list_items(interaction: discord.Interaction, type_to_list: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) used /list {type_to_list.value}.")

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    title = ""
    color = discord.Color.blue()
    items = []

    if type_to_list.value == "code":
        title = "📜 Danh sách mã"
        cursor.execute("SELECT code FROM redemption_codes")
        items = cursor.fetchall()
        if not items:
            description = 'Không còn mã nào trong hệ thống.'
        else:
            response_lines = ["**Danh sách các mã còn lại (dùng cho /redeem):**"]
            for i, item_tuple in enumerate(items):
                response_lines.append(f"`{i+1}.` `{item_tuple[0]}`")
            description = "\n".join(response_lines)

    elif type_to_list.value == "link":
        title = "📜 Danh sách liên kết Pastebin"
        cursor.execute("SELECT pastebin_url FROM hcoin_pastebin_links")
        items = cursor.fetchall()
        if not items:
            description = 'Hiện tại không có liên kết Pastebin nào trong danh sách.'
        else:
            response_lines = ["**Danh sách các liên kết Pastebin chưa sử dụng:**"]
            for i, item_tuple in enumerate(items):
                response_lines.append(f"`{i+1}.` <{item_tuple[0]}>")
            description = "\n".join(response_lines)

    elif type_to_list.value == "localstorage":
        title = "📦 Kho Local Storage"
        # THAY ĐỔI DÒNG NÀY: Lấy cả ID và data_json
        cursor.execute("SELECT id, data_json FROM ug_phones")
        items = cursor.fetchall() # items will now be a list of tuples like (id, data_json)

        if not items:
            description = 'Hiện tại không có Local Storage nào trong kho.'
            embed = discord.Embed(
                title=title,
                description=description,
                color=color
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            conn.close()
            return

        # Prepare formatted lines for each item
        formatted_items_lines = []
        # THAY ĐỔI DÒNG NÀY ĐỂ HIỂN THỊ ID CỦA MỖI MỤC
        for item_id, item_content in items: # Giờ đây vòng lặp lấy cả ID và nội dung
            # Format each item with its unique ID and a code block
            # Tùy chỉnh định dạng để hiển thị ID:
            formatted_items_lines.append(f"**ID: `{item_id}`**\n```json\n{item_content}\n```")

        # Send initial message to indicate processing if there's a lot of data
        if len("\n".join(formatted_items_lines)) > 4000: # Check if overall content is very large
            await interaction.followup.send(embed=discord.Embed(
                title=title,
                description="Đang xử lý và gửi dữ liệu Local Storage. Điều này có thể cần nhiều tin nhắn.",
                color=discord.Color.blue()
            ), ephemeral=True)
            logger.info(f"Sending large Local Storage list to {interaction.user.display_name} (ID: {interaction.user.id}) in multiple messages.")

        current_embed_lines = []
        current_embed_length = 0
        max_embed_length = 3800 # Leave some buffer for title, footer, etc. (max 4096)

        for line in formatted_items_lines:
            line_length = len(line) + 1 # +1 for newline character

            # If adding this line exceeds the embed limit, send the current embed and start a new one
            if current_embed_length + line_length > max_embed_length:
                embed_to_send = discord.Embed(
                    title=title,
                    description="\n".join(current_embed_lines),
                    color=color
                )
                await interaction.followup.send(embed=embed_to_send, ephemeral=True)
                current_embed_lines = []
                current_embed_length = 0

            current_embed_lines.append(line)
            current_embed_length += line_length

        # Send any remaining content in the last embed
        if current_embed_lines:
            embed_to_send = discord.Embed(
                title=title,
                description="\n".join(current_embed_lines),
                color=color
            )
            embed_to_send.set_footer(text=f"Tổng số {type_to_list.name.lower()}: {len(items)}")
            await interaction.followup.send(embed=embed_to_send, ephemeral=True)

        conn.close()
        return # Exit the function as response has been handled with multiple followups

    # For 'code' and 'link' options, this logic remains
    if len(description) > 4000: # Discord embed description limit is 4096 characters
        embed = discord.Embed(
            title=title,
            description="Danh sách quá dài để hiển thị hoàn toàn. Vui lòng kiểm tra cơ sở dữ liệu để xem toàn bộ danh sách.",
            color=color
        )
        embed.set_footer(text=f"Tổng số {type_to_list.name.lower()}: {len(items)}")
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.warning(f"List for {type_to_list.value} was too long for single embed, truncated for {interaction.user.display_name}.")
    else:
        embed = discord.Embed(
            title=title,
            description=description,
            color=color
        )
        embed.set_footer(text=f"Tổng số {type_to_list.name.lower()}: {len(items)}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    conn.close()


# --- Commands and Modal for Local Storage ---

class UGPhoneModal(ui.Modal, title='Nhập Local Storage'):
    data_input = ui.TextInput(
        label='Dán mã hoặc File Json',
        placeholder='Nhập Local Storage tại đây...',
        style=discord.TextStyle.paragraph,
        max_length=4000
    )

    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        try:
            # Use INSERT OR IGNORE to handle duplicates if they are manually entered via this modal
            cursor.execute("INSERT OR IGNORE INTO ug_phones (data_json) VALUES (?)", (self.data_input.value,))
            if cursor.rowcount > 0:
                embed = discord.Embed(
                    title="✅ Đã lưu thành công!",
                    description='Dữ liệu Local Storage đã được lưu vào kho.',
                    color=discord.Color.green()
                )
                logger.info(f"Local Storage added via modal by {interaction.user.display_name} (ID: {interaction.user.id}).")
            else:
                 embed = discord.Embed(
                    title="ℹ️ Dữ liệu đã tồn tại!",
                    description='Dữ liệu Local Storage này đã có trong kho. Không có gì được thêm vào.',
                    color=discord.Color.blue()
                )
                 logger.info(f"Duplicate Local Storage attempted via modal by {interaction.user.display_name} (ID: {interaction.user.id}).")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except sqlite3.Error as e:
            logger.error(f"SQLite Error when saving UG Phone data via modal for {interaction.user.display_name}: {e}")
            embed = discord.Embed(
                title="❌ Lỗi lưu trữ!",
                description=f'Đã xảy ra lỗi khi lưu dữ liệu Local Storage: {e}\n'
                            f'Vui lòng kiểm tra console bot để biết chi tiết hoặc liên hệ quản trị viên.',
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e: # Catch any other unexpected errors
            logger.critical(f"Unexpected error in UGPhoneModal.on_submit for {interaction.user.display_name}: {e}")
            embed = discord.Embed(
                title="❌ Lỗi không mong muốn!",
                description=f'Đã xảy ra lỗi không mong muốn: {e}',
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        finally:
            conn.close()


# /addugphone command (For "Owner" role AND specific channel only)
@bot.tree.command(name='addugphone', description='Add Local Storage info for users to receive.')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
async def add_ug_phone(interaction: discord.Interaction):
    logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) used /addugphone (modal).")
    await interaction.response.send_modal(UGPhoneModal())

# /quickaddug command (Admin only - to add multiple Local Storage entries in a session)
@bot.tree.command(name='quickaddug', description='Start a session to add multiple Local Storage entries.')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
async def quick_add_ug_command(interaction: discord.Interaction):
    user_id = interaction.user.id

    if user_id in bot.quick_add_ug_sessions: # Use bot.quick_add_ug_sessions
        embed = discord.Embed(
            title="⚠️ Phiên đã hoạt động!",
            description="Bạn đã có một phiên nhập Local Storage đang hoạt động. Vui lòng gửi `done` để kết thúc hoặc `cancel` để hủy bỏ phiên hiện tại.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.warning(f"User {interaction.user.display_name} (ID: {user_id}) tried to start /quickaddug session but already has one.")
        return

    bot.quick_add_ug_sessions[user_id] = [] # Use bot.quick_add_ug_sessions
    embed = discord.Embed(
        title="✨ Đã bắt đầu phiên thêm nhanh Local Storage! ✨",
        description="Vui lòng bắt đầu dán các chuỗi Local Storage (mỗi chuỗi là một tin nhắn riêng biệt).\n"
                    "Khi bạn hoàn tất, hãy gửi tin nhắn `done` (hoặc `xong`, `hoàn tất`) để lưu trữ.\n"
                    "Gửi `cancel` để hủy bỏ phiên này.",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=False) # Not ephemeral, so user sees instructions
    logger.info(f"User {interaction.user.display_name} (ID: {user_id}) started a /quickaddug session.")


# /getugphone command (User command) - Updated to delete after sending
@bot.tree.command(name='getugphone', description='Use 150 coins to receive Local Storage.')
async def get_ug_phone_command(interaction: discord.Interaction):
    user_id = interaction.user.id
    cost = 150

    is_owner_role = False
    # Check if the user has the 'Owner' role
    for role in interaction.user.roles:
        if role.name == "Owner": # Case-sensitive
            is_owner_role = True
            break

    if not is_owner_role: # If not an Owner, check coin balance
        current_balance = await bot.loop.run_in_executor(None, get_user_hcoin, user_id)
        if current_balance < cost:
            embed = discord.Embed(
                title="💰 Không đủ tiền!",
                description=f'Bạn không có đủ **{cost} coin** để nhận Local Storage. Số dư hiện tại của bạn là **{current_balance} coin**.',
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.warning(f"User {interaction.user.display_name} (ID: {user_id}) tried to /getugphone but had insufficient balance ({current_balance} < {cost}).")
            return

    await interaction.response.defer(ephemeral=True)

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    cursor.execute("SELECT id, data_json FROM ug_phones ORDER BY RANDOM() LIMIT 1")
    result = cursor.fetchone()

    if not result:
        embed = discord.Embed(
            title="⚠️ Kho trống!",
            description='Hiện tại không có Local Storage nào trong kho. Vui lòng thử lại sau hoặc liên hệ quản trị viên.',
            color=discord.Color.orange()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        conn.close()
        logger.warning(f"User {interaction.user.display_name} (ID: {user_id}) tried to /getugphone, but ug_phones table is empty.")
        return

    item_id, local_storage_data = result

    # Deduct coins if not an Owner (Moved this before sending DM to ensure deduction occurs)
    if not is_owner_role:
        await bot.loop.run_in_executor(None, update_user_hcoin, user_id, -cost)
        logger.info(f"User {interaction.user.display_name} (ID: {user_id}) used {cost} coins for Local Storage.")

    try:
        user_dm = await interaction.user.create_dm()

        dm_content = f"```\n{local_storage_data}\n```"

        chunk_size = 1990 # Discord message limit is 2000 characters
        if len(dm_content) > chunk_size:
            # Split content into chunks if too long
            chunks = [dm_content[i:i + chunk_size] for i in range(0, len(dm_content), chunk_size)]
            for i, chunk in enumerate(chunks):
                # Send each chunk, indicating part number if desired
                await user_dm.send(f"Phần {i+1}/{len(chunks)}:\n{chunk}")
            embed = discord.Embed(
                title="📦 Local Storage đã gửi!",
                description=f'Local Storage đã được gửi đến tin nhắn riêng của bạn (gồm {len(chunks)} phần). Vui lòng kiểm tra DM của bạn!',
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"Sent Local Storage (in {len(chunks)} parts) to DM of {user_id}.")
        else:
            await user_dm.send(dm_content)
            embed = discord.Embed(
                title="📦 Local Storage đã gửi!",
                description=f'Local Storage đã được gửi đến tin nhắn riêng của bạn. Vui lòng kiểm tra DM của bạn!',
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"Sent Local Storage to DM of {user_id}.")

        # --- IMPORTANT: DELETE THE USED LOCAL STORAGE AFTER SUCCESSFUL DM ---
        # NOTE: If you want to keep the data after it's sent (allowing multiple uses), comment out the following 3 lines.
        cursor.execute("DELETE FROM ug_phones WHERE id = ?", (item_id,))
        conn.commit()
        logger.info(f"Local Storage item with ID {item_id} successfully deleted from DB after being sent to user {user_id}.")


    except discord.Forbidden:
        # If DM fails, do NOT delete the Local Storage from DB
        embed = discord.Embed(
            title="🚫 Không thể gửi DM!",
            description='Tôi không thể gửi tin nhắn trực tiếp cho bạn. Vui lòng kiểm tra cài đặt quyền riêng tư của bạn (cho phép tin nhắn trực tiếp từ thành viên máy chủ). Local Storage không bị trừ và vẫn còn trong kho.',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.error(f"Failed to send DM to {user_id} for /getugphone (Forbidden). Local Storage ID {item_id} was NOT deleted.")
        # If coins were deducted, refund them as the user didn't get the data.
        if not is_owner_role:
             await bot.loop.run_in_executor(None, update_user_hcoin, user_id, cost) # Refund
             logger.info(f"Refunded {cost} coins to user {user_id} due to DM failure for Local Storage ID {item_id}.")
    except Exception as e:
        # If any other error occurs during DM sending, do NOT delete the Local Storage from DB
        embed = discord.Embed(
            title="❌ Lỗi gửi DM!",
            description=f'Đã xảy ra lỗi khi gửi DM: {e}. Local Storage không bị trừ và vẫn còn trong kho.',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.critical(f"Unexpected error sending DM to {user_id} for /getugphone: {e}. Local Storage ID {item_id} was NOT deleted.")
        # If coins were deducted, refund them as the user didn't get the data.
        if not is_owner_role:
             await bot.loop.run_in_executor(None, update_user_hcoin, user_id, cost) # Refund
             logger.info(f"Refunded {cost} coins to user {user_id} due to DM failure for Local Storage ID {item_id}.")
    finally:
        conn.close() # Always close the connection

# /delete_ug_data command (Admin only - to delete specific Local Storage by its full content)
@bot.tree.command(name='delete_ug_data', description='Delete a Local Storage entry by its full content.')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
@app_commands.describe(data_to_delete='The exact Local Storage string to delete.')
async def delete_ug_data(interaction: discord.Interaction, data_to_delete: str):
    await interaction.response.defer(ephemeral=True)
    logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) used /delete_ug_data.")

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM ug_phones WHERE data_json = ?", (data_to_delete,))
        conn.commit()

        if cursor.rowcount > 0:
            embed = discord.Embed(
                title="✅ Xóa Local Storage Thành Công!",
                description="Dữ liệu Local Storage đã được xóa khỏi kho.",
                color=discord.Color.green()
            )
            logger.info(f"Local Storage deleted by {interaction.user.display_name} (ID: {interaction.user.id}).")
        else:
            embed = discord.Embed(
                title="❌ Không tìm thấy dữ liệu!",
                description="Không tìm thấy dữ liệu Local Storage khớp với nội dung bạn cung cấp.",
                color=discord.Color.red()
            )
            logger.warning(f"Local Storage not found for deletion by {interaction.user.display_name} (ID: {interaction.user.id}).")
        await interaction.followup.send(embed=embed, ephemeral=True)

    except sqlite3.Error as e:
        logger.error(f"SQLite Error deleting UG Phone data via /delete_ug_data for {interaction.user.display_name}: {e}")
        embed = discord.Embed(
            title="❌ Lỗi xóa!",
            description=f'Đã xảy ra lỗi khi xóa dữ liệu Local Storage: {e}\n'
                        f'Vui lòng kiểm tra console bot để biết chi tiết hoặc liên hệ quản trị viên.',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        logger.critical(f"Unexpected error in /delete_ug_data for {interaction.user.display_name}: {e}")
        embed = discord.Embed(
            title="❌ Lỗi không mong muốn!",
            description=f'Đã xảy ra lỗi không mong muốn: {e}',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    finally:
        conn.close()

# /delete_ug_by_id command (Admin only - to delete specific Local Storage by its ID)
@bot.tree.command(name='delete_ug_by_id', description='Delete a Local Storage entry by its unique ID (Admin only).')
@commands.has_role("Owner") # Restrict to Owner role
@app_commands.check(is_allowed_admin_channel) # Restrict to admin channel
@app_commands.describe(item_id='The unique ID of the Local Storage entry to delete.')
async def delete_ug_by_id(interaction: discord.Interaction, item_id: int):
    await interaction.response.defer(ephemeral=True)
    logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) used /delete_ug_by_id with ID: {item_id}.")

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM ug_phones WHERE id = ?", (item_id,))
        conn.commit()

        if cursor.rowcount > 0:
            embed = discord.Embed(
                title="✅ Xóa Local Storage Thành Công!",
                description=f"Dữ liệu Local Storage với ID `{item_id}` đã được xóa khỏi kho.",
                color=discord.Color.green()
            )
            logger.info(f"Local Storage with ID {item_id} deleted by {interaction.user.display_name} (ID: {interaction.user.id}).")
        else:
            embed = discord.Embed(
                title="❌ Không tìm thấy ID!",
                description=f"Không tìm thấy dữ liệu Local Storage với ID `{item_id}`.",
                color=discord.Color.red()
            )
            logger.warning(f"Local Storage with ID {item_id} not found for deletion by {interaction.user.display_name} (ID: {interaction.user.id}).")
        await interaction.followup.send(embed=embed, ephemeral=True)

    except sqlite3.Error as e:
        logger.error(f"SQLite Error deleting UG Phone data via /delete_ug_by_id for {interaction.user.display_name}: {e}")
        embed = discord.Embed(
            title="❌ Lỗi xóa!",
            description=f'Đã xảy ra lỗi khi xóa dữ liệu Local Storage: {e}\n'
                        f'Vui lòng kiểm tra console bot để biết chi tiết hoặc liên hệ quản trị viên.',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        logger.critical(f"Unexpected error in /delete_ug_by_id for {interaction.user.display_name}: {e}")
        embed = discord.Embed(
            title="❌ Lỗi không mong muốn!",
            description=f'Đã xảy ra lỗi không mong muốn: {e}',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    finally:
        conn.close()

# --- Commands for Hcoin Management (Admin Only) ---

@bot.tree.command(name='balance', description='Check your Hcoin balance.')
async def balance(interaction: discord.Interaction):
    user_id = interaction.user.id
    current_balance = await bot.loop.run_in_executor(None, get_user_hcoin, user_id)

    embed = discord.Embed(
        title="💰 Số dư Hcoin của bạn",
        description=f'Bạn hiện có **{current_balance} coin**.',
        color=discord.Color.gold()
    )
    embed.set_footer(text="Sử dụng coin để nhận Local Storage!")
    await interaction.response.send_message(embed=embed, ephemeral=True)
    logger.info(f"User {interaction.user.display_name} (ID: {user_id}) checked balance: {current_balance} coins.")

@bot.tree.command(name='add_hcoin', description='Add Hcoin to a user (Admin only).')
@commands.has_role("Owner") # Restrict to Owner role
@app_commands.check(is_allowed_admin_channel) # Restrict to admin channel
@app_commands.describe(user='The user to add Hcoin to.', amount='The amount of Hcoin to add.')
async def add_hcoin(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("Số lượng Hcoin thêm phải lớn hơn 0.", ephemeral=True)
        return

    await bot.loop.run_in_executor(None, update_user_hcoin, user.id, amount)
    new_balance = await bot.loop.run_in_executor(None, get_user_hcoin, user.id)

    embed = discord.Embed(
        title="✅ Đã thêm Hcoin!",
        description=f'Đã thêm **{amount} coin** cho {user.mention}.',
        color=discord.Color.green()
    )
    embed.add_field(name="Số dư mới", value=f"**{new_balance} coin**", inline=True)
    await interaction.response.send_message(embed=embed) # Not ephemeral, can be public
    logger.info(f"Admin {interaction.user.display_name} (ID: {interaction.user.id}) added {amount} coins to {user.display_name} (ID: {user.id}). New balance: {new_balance}.")


@bot.tree.command(name='remove_hcoin', description='Remove Hcoin from a user (Admin only).')
@commands.has_role("Owner") # Restrict to Owner role
@app_commands.check(is_allowed_admin_channel) # Restrict to admin channel
@app_commands.describe(user='The user to remove Hcoin from.', amount='The amount of Hcoin to remove.')
async def remove_hcoin(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("Số lượng Hcoin cần xóa phải lớn hơn 0.", ephemeral=True)
        return

    # To ensure balance doesn't go negative, fetch first
    current_balance = await bot.loop.run_in_executor(None, get_user_hcoin, user.id)
    if current_balance < amount:
        embed = discord.Embed(
            title="⚠️ Không đủ Hcoin để xóa!",
            description=f'{user.mention} chỉ có **{current_balance} coin**. Không thể xóa **{amount} coin**.',
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.warning(f"Admin {interaction.user.display_name} (ID: {interaction.user.id}) tried to remove {amount} coins from {user.display_name} (ID: {user.id}), but user only has {current_balance}.")
        return

    await bot.loop.run_in_executor(None, update_user_hcoin, user.id, -amount)
    new_balance = await bot.loop.run_in_executor(None, get_user_hcoin, user.id)

    embed = discord.Embed(
        title="✅ Đã xóa Hcoin!",
        description=f'Đã xóa **{amount} coin** từ {user.mention}.',
        color=discord.Color.green()
    )
    embed.add_field(name="Số dư mới", value=f"**{new_balance} coin**", inline=True)
    await interaction.response.send_message(embed=embed) # Not ephemeral, can be public
    logger.info(f"Admin {interaction.user.display_name} (ID: {interaction.user.id}) removed {amount} coins from {user.display_name} (ID: {user.id}). New balance: {new_balance}.")


@bot.tree.command(name='hcoin_top', description='Show top Hcoin balances.')
async def hcoin_top(interaction: discord.Interaction):
    await interaction.response.defer()
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, hcoin_balance FROM user_balances ORDER BY hcoin_balance DESC LIMIT 10")
    top_users = cursor.fetchall()
    conn.close()

    if not top_users:
        embed = discord.Embed(
            title="🏆 Bảng xếp hạng Hcoin",
            description="Chưa có ai trong bảng xếp hạng Hcoin.",
            color=discord.Color.gold()
        )
        await interaction.followup.send(embed=embed)
        return

    description = "**Top 10 người dùng có nhiều Hcoin nhất:**\n\n"
    for i, (user_id, balance) in enumerate(top_users):
        try:
            user = await bot.fetch_user(user_id) # Fetch user object by ID
            user_name = user.display_name
        except discord.NotFound:
            user_name = f"Người dùng không tồn tại (ID: {user_id})"
        except Exception:
            user_name = f"Không thể lấy tên (ID: {user_id})"
        description += f"**{i+1}.** {user_name}: **{balance} coin**\n"

    embed = discord.Embed(
        title="🏆 Bảng xếp hạng Hcoin",
        description=description,
        color=discord.Color.gold()
    )
    embed.set_footer(text="Ai sẽ là người đứng đầu?")
    await interaction.followup.send(embed=embed)
    logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) viewed Hcoin top list.")


# /info command
@bot.tree.command(name='info', description='Get information about the bot.')
async def info(interaction: discord.Interaction):
    embed = discord.Embed(
        title="ℹ️ Thông tin Bot",
        description="Chào mừng bạn đến với bot của chúng tôi!",
        color=discord.Color.purple()
    )
    embed.add_field(name="Chức năng chính", value="""
    - `/getcredit`: Nhận mã đổi thưởng để lấy coin.
    - `/redeem`: Đổi mã để nhận coin.
    - `/getugphone`: Sử dụng coin để nhận Local Storage.
    - `/balance`: Kiểm tra số dư coin của bạn.
    - `/hcoin_top`: Xem bảng xếp hạng Hcoin.
    """, inline=False)
    embed.add_field(name="Các lệnh Quản trị viên (chỉ trong kênh admin)", value="""
    - `/addugphone`: Thêm Local Storage thủ công.
    - `/quickaddug`: Thêm nhiều Local Storage trong một phiên.
    - `/delete_ug_data`: Xóa Local Storage cụ thể (bằng nội dung).
    - `/delete_ug_by_id`: Xóa Local Storage cụ thể (bằng ID).
    - `/remove`: Xóa mã đổi thưởng.
    - `/list`: Liệt kê mã, link Pastebin hoặc Local Storage.
    - `/add_hcoin`: Thêm coin cho người dùng.
    - `/remove_hcoin`: Xóa coin khỏi người dùng.
    - `/sync_commands`: Đồng bộ lệnh slash (chỉ cho chủ bot).
    - `/deduplicate_ugphone`: Chạy deduplication thủ công.
    """, inline=False)
    embed.set_footer(text=f"Bot được tạo bởi [Tên hoặc Nhóm của bạn]")
    embed.timestamp = discord.utils.utcnow()
    await interaction.response.send_message(embed=embed, ephemeral=False)


# Admin command to manually deduplicate ug_phones table
@bot.tree.command(name='deduplicate_ugphone', description='Manually remove duplicate Local Storage entries (Admin only).')
@commands.has_role("Owner") # Restrict to Owner role
@app_commands.check(is_allowed_admin_channel) # Restrict to admin channel
async def deduplicate_ug_phone_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    logger.info(f"Admin {interaction.user.display_name} (ID: {interaction.user.id}) used /deduplicate_ugphone.")

    try:
        initial_count, final_count = await bot.loop.run_in_executor(None, deduplicate_ug_phones_data)
        removed_count = initial_count - final_count

        if removed_count > 0:
            embed = discord.Embed(
                title="✅ Trùng lặp đã xử lý!",
                description=f"Đã tìm thấy và loại bỏ **{removed_count}** mục Local Storage trùng lặp.\n"
                            f"Tổng số mục ban đầu: **{initial_count}**\n"
                            f"Tổng số mục sau khi deduplicate: **{final_count}**",
                color=discord.Color.green()
            )
            logger.info(f"Deduplication successful for ug_phones. Removed {removed_count} duplicates.")
        else:
            embed = discord.Embed(
                title="ℹ️ Không có trùng lặp!",
                description="Không tìm thấy mục Local Storage trùng lặp nào trong kho.",
                color=discord.Color.blue()
            )
            logger.info("No duplicates found in ug_phones table.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        logger.critical(f"Error during deduplication via /deduplicate_ugphone for {interaction.user.display_name}: {e}")
        embed = discord.Embed(
            title="❌ Lỗi khi deduplicate!",
            description=f'Đã xảy ra lỗi khi xử lý trùng lặp: {e}',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# Admin command to manually sync slash commands (only for the bot owner)
@bot.tree.command(name="sync_commands", description="Syncs slash commands to Discord (Owner only).")
@commands.is_owner() # Only the bot owner can use this
@app_commands.check(is_allowed_admin_channel)
async def sync_commands(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    logger.info(f"Bot owner {interaction.user.display_name} (ID: {interaction.user.id}) used /sync_commands.")

    try:
        if TEST_GUILD_ID:
            test_guild_id_int = int(TEST_GUILD_ID)
            test_guild = discord.Object(id=test_guild_id_int)
            bot.tree.copy_global_to(guild=test_guild)
            await bot.tree.sync(guild=test_guild)
            embed = discord.Embed(
                title="✅ Đồng bộ lệnh thành công!",
                description=f"Đã đồng bộ lệnh Slash cho guild test `{test_guild_id_int}`.",
                color=discord.Color.green()
            )
            logger.info(f"Slash commands synced to TEST_GUILD_ID: {test_guild_id_int}.")
        else:
            await bot.tree.sync()
            embed = discord.Embed(
                title="✅ Đồng bộ lệnh thành công!",
                description="Đã đồng bộ lệnh Slash toàn cầu. Các lệnh có thể mất tới 1 giờ để xuất hiện.",
                color=discord.Color.green()
            )
            logger.info("Slash commands synced globally.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error syncing commands for {interaction.user.display_name}: {e}")
        embed = discord.Embed(
            title="❌ Lỗi đồng bộ lệnh!",
            description=f"Đã xảy ra lỗi khi đồng bộ lệnh: `{e}`",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

# --- Flask Web Server for Uptime Monitoring ---
# Gunicorn will run this Flask app. We no longer need to run it in a separate thread
# within the bot's Python script, as Gunicorn handles the web server part.
app = Flask(__name__)

@app.route('/')
def home():
    # Simple health check endpoint
    return "Bot is running!"

# --- Run the Discord Bot ---
if __name__ == "__main__":
    if DISCORD_BOT_TOKEN:
        try:
            # When run via 'python my_bot.py' directly, this will start the bot.
            # When run by Gunicorn, Gunicorn will manage the 'app' Flask instance,
            # and the bot's operations will run in the same process/workers.
            bot.run(DISCORD_BOT_TOKEN)
        except Exception as e:
            logger.critical(f"Failed to run bot: {e}")
            print(f"Error: Failed to run bot. Please check your DISCORD_BOT_TOKEN in the .env file. Error: {e}")
    else:
        logger.critical("DISCORD_BOT_TOKEN not found in .env file.")
        print("Error: DISCORD_BOT_TOKEN not found in .env file. Please set it.")