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
from flask import Flask

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('discord_bot')

# Load environment variables from .env file
load_dotenv()

# Define Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Database file name
DATABASE_FILE = 'bot_data.db'

# Specific channel ID for admin commands (still used for non-owners)
ALLOWED_ADMIN_CHANNEL_ID = 1383013260902531074

# Owner user ID
OWNER_USER_ID = 1026107907646967838

# Environment Variables
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
YEUMONEY_API_TOKEN = os.getenv('YEUMONEY_API_TOKEN')
PASTEBIN_DEV_KEY = os.getenv('PASTEBIN_DEV_KEY')
TEST_GUILD_ID = os.getenv('TEST_GUILD_ID')

# Cooldown for /getcredit command (in seconds)
GET_CREDIT_COOLDOWN_SECONDS = 5 * 60

# Dictionary to store active multi-line input sessions for /quickaddug
quick_add_ug_sessions = {}

# Function to generate a random alphanumeric code
def generate_random_code(length=20):
    characters = string.ascii_uppercase + string.digits
    return ''.join(random.choice(characters) for i in range(length))

# Function to initialize the database and tables
def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS main_link (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS redemption_codes (
            code TEXT PRIMARY KEY
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_balances (
            user_id INTEGER PRIMARY KEY,
            hcoin_balance INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ug_phones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_json TEXT NOT NULL UNIQUE
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS hcoin_pastebin_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pastebin_url TEXT NOT NULL UNIQUE
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_cooldowns (
            user_id INTEGER PRIMARY KEY,
            last_getcredit_time TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()
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
    cursor.execute("SELECT COUNT(*) FROM ug_phones")
    initial_count = cursor.fetchone()[0]
    cursor.execute('''
        CREATE TEMPORARY TABLE IF NOT EXISTS ug_phones_temp AS
        SELECT MIN(id) as id, data_json
        FROM ug_phones
        GROUP BY data_json;
    ''')
    cursor.execute('DELETE FROM ug_phones;')
    cursor.execute('INSERT INTO ug_phones SELECT id, data_json FROM ug_phones_temp;')
    cursor.execute('DROP TABLE IF EXISTS ug_phones_temp;')
    cursor.execute("SELECT COUNT(*) FROM ug_phones")
    final_count = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return initial_count, final_count

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self.quick_add_ug_sessions = quick_add_ug_sessions

    async def setup_hook(self):
        if TEST_GUILD_ID:
            try:
                test_guild_id_int = int(TEST_GUILD_ID)
                test_guild = discord.Object(id=test_guild_id_int)
                self.tree.copy_global_to(guild=test_guild)
                await self.tree.sync(guild=test_guild)
                logger.info(f'Slash commands synced for TEST_GUILD_ID: {test_guild_id_int} (instant sync)! Old commands removed.')
            except ValueError:
                logger.error(f"ERROR: Invalid TEST_GUILD_ID '{TEST_GUILD_ID}' in .env. Falling back to global sync.")
                await self.tree.sync()
                logger.info('Slash commands synced globally (may take up to 1 hour to appear). Old commands removed.')
            except Exception as e:
                logger.error(f"ERROR syncing to specific guild {TEST_GUILD_ID}: {e}. Falling back to global sync.")
                await self.tree.sync()
                logger.info('Slash commands synced globally (may take up to 1 hour to appear). Old commands removed.')
        else:
            await self.tree.sync()
            logger.info('Slash commands synced globally (may take up to 1 hour to appear). Old commands removed.')

    async def on_ready(self):
        logger.info(f'Logged in as {self.user}!')
        logger.info(f'Bot ID: {self.user.id}')
        await self.loop.run_in_executor(None, init_db)
        logger.info("Database initialized or checked.")

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandInvokeError):
            logger.error(f"CommandInvokeError in command '{interaction.command.name}' by {interaction.user} (ID: {interaction.user.id}) in channel {interaction.channel} (ID: {interaction.channel_id}): {error.original}")
            try:
                await interaction.response.send_message(f"Đã xảy ra lỗi khi thực thi lệnh: `{error.original}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send(f"Đã xảy ra lỗi khi thực thi lệnh: `{error.original}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)
        elif isinstance(error, app_commands.CheckFailure):
            logger.warning(f"CheckFailure for command '{interaction.command.name}' by {interaction.user} (ID: {interaction.user.id}) in channel {interaction.channel} (ID: {interaction.channel_id}): {error}")
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

# Custom check for Owner user ID
def is_owner(interaction: discord.Interaction) -> bool:
    is_owner = interaction.user.id == OWNER_USER_ID
    if not is_owner:
        logger.warning(f"User {interaction.user.display_name} (ID: {interaction.user.id}) attempted to use an owner command but is not the owner.")
    return is_owner

# Modified check for admin channel (Owner bypasses channel restriction)
def is_allowed_admin_channel(interaction: discord.Interaction) -> bool:
    if interaction.user.id == OWNER_USER_ID:
        return True
    if interaction.channel.id != ALLOWED_ADMIN_CHANNEL_ID:
        logger.warning(f"Command '{interaction.command.name}' attempted by {interaction.user.display_name} (ID: {interaction.user.id}) in unauthorized channel #{interaction.channel.name} (ID: {interaction.channel_id}).")
    return interaction.channel.id == ALLOWED_ADMIN_CHANNEL_ID

@bot.event
async def on_message(message: discord.Message):
    if message.author.id == bot.user.id:
        return
    user_id = message.author.id
    content = message.content.strip()
    if user_id in bot.quick_add_ug_sessions:
        lower_content = content.lower()
        if lower_content in ["done", "xong", "hoàn tất"]:
            collected_data = bot.quick_add_ug_sessions.pop(user_id)
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
                for data_item in collected_data:
                    try:
                        cursor.execute("INSERT OR IGNORE INTO ug_phones (data_json) VALUES (?)", (data_item,))
                        if cursor.rowcount > 0:
                            added_count += 1
                        else:
                            skipped_count += 1
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
                bot.quick_add_ug_sessions.pop(user_id)
                logger.info(f"User {message.author.display_name} (ID: {user_id}) cancelled /quickaddug session.")
                embed = discord.Embed(
                    title="❌ Phiên Thêm Nhanh Local Storage đã Hủy!",
                    description="Phiên nhập Local Storage của bạn đã bị hủy bỏ. Không có dữ liệu nào được lưu.",
                    color=discord.Color.red()
                )
                await message.channel.send(embed=embed)
        else:
            bot.quick_add_ug_sessions[user_id].append(content)
            logger.debug(f"User {message.author.display_name} (ID: {user_id}) added data to /quickaddug session: {content[:50]}...")
            try:
                await message.add_reaction("✅")
            except discord.Forbidden:
                pass
    await bot.process_commands(message)

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
        'api_paste_private': '1',
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

def fetch_pastebin_content(pastebin_url: str):
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

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandInvokeError):
        logger.error(f"CommandInvokeError in command '{interaction.command.name}' by {interaction.user.display_name} (ID: {interaction.user.id}) in channel {interaction.channel} (ID: {interaction.channel_id}): {error.original}")
        try:
            await interaction.response.send_message(f"Đã xảy ra lỗi khi thực thi lệnh: `{error.original}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send(f"Đã xảy ra lỗi khi thực thi lệnh: `{error.original}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)
    elif isinstance(error, app_commands.CheckFailure):
        logger.warning(f"CheckFailure for command '{interaction.command.name}' by {interaction.user.display_name} (ID: {interaction.user.id}) in channel {interaction.channel} (ID: {interaction.channel_id}): {error}")
        message = "Bạn không phải là chủ sở hữu bot!" if interaction.user.id != OWNER_USER_ID else f"Lệnh này chỉ có thể được sử dụng trong kênh quản trị viên: <#{ALLOWED_ADMIN_CHANNEL_ID}>."
        try:
            await interaction.response.send_message(message, ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send(message, ephemeral=True)
    else:
        logger.critical(f"Unknown AppCommand Error in command '{interaction.command.name}' by {interaction.user.display_name} (ID: {interaction.user.id}) in channel {interaction.channel} (ID: {interaction.channel_id}): {error}")
        try:
            await interaction.response.send_message(f"Đã xảy ra lỗi không mong muốn: `{error}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)
        except discord.InteractionResponded:
            await interaction.followup.send(f"Đã xảy ra lỗi không mong muốn: `{error}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)

@bot.tree.command(name='getcredit', description='Get a new unique code by generating a Pastebin link.')
async def get_credit(interaction: discord.Interaction):
    user_id = interaction.user.id
    current_time = datetime.now(timezone.utc)
    if user_id != OWNER_USER_ID:  # Skip cooldown for owner
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
    generated_code = generate_random_code(20)
    logger.info(f"User {interaction.user.display_name} (ID: {user_id}) requested /getcredit. Generated code: {generated_code}")
    paste_title = f"Redeem Code for {interaction.user.name} - {generated_code}"
    expire_date = "10M"
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
    short_link = await bot.loop.run_in_executor(None, create_short_link, pastebin_url)
    if short_link:
        if user_id != OWNER_USER_ID:  # Set cooldown for non-owners
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
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.timestamp = discord.utils.utcnow()
        await interaction.followup.send(embed=embed)
    else:
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

@bot.tree.command(name='remove', description='Remove a specific redemption code from the list.')
@app_commands.check(is_owner)
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

class RedeemMultipleCodesModal(ui.Modal, title='Đổi Nhiều Mã'):
    codes_input = ui.TextInput(
        label='Dán mã (mỗi mã một dòng)',
        placeholder='Nhập mỗi mã đổi thưởng trên một dòng mới...',
        style=discord.TextStyle.paragraph,
        max_length=4000
    )
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        hcoin_per_code = 150
        raw_codes_input = self.codes_input.value
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
        for code in codes_to_redeem:
            try:
                cursor.execute("SELECT code FROM redemption_codes WHERE code = ?", (code,))
                existing_code = cursor.fetchone()
                if existing_code:
                    cursor.execute("DELETE FROM redemption_codes WHERE code = ?", (code,))
                    redeemed_count += 1
                    total_hcoin_earned += hcoin_per_code
                else:
                    invalid_count += 1
                    failed_codes.append(code)
            except sqlite3.Error as e:
                logger.error(f"SQLite Error processing code '{code}' for redemption by {user_id}: {e}")
                invalid_count += 1
                failed_codes.append(code)
            except Exception as e:
                logger.error(f"Unexpected error processing code '{code}' for redemption by {user_id}: {e}")
                invalid_count += 1
                failed_codes.append(code)
        conn.commit()
        if total_hcoin_earned > 0:
            await bot.loop.run_in_executor(None, update_user_hcoin, user_id, total_hcoin_earned)
        current_balance = await bot.loop.run_in_executor(None, get_user_hcoin, user_id)
        conn.close()
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

@bot.tree.command(name='redeem', description='Redeem a single code or show modal to redeem multiple codes.')
@app_commands.describe(code='The code you want to redeem (leave blank to show modal)')
async def redeem_code(interaction: discord.Interaction, code: str = None):
    if code:
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        hcoin_reward = 150
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT code FROM redemption_codes WHERE code = ?", (code,))
            existing_code = cursor.fetchone()
            if existing_code:
                cursor.execute("DELETE FROM redemption_codes WHERE code = ?", (code,))
                conn.commit()
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
        await interaction.response.send_modal(RedeemMultipleCodesModal())

@bot.tree.command(name='quickredeemcode', description='Redeem multiple codes directly at once.')
async def quick_redeem_code_command_modal(interaction: discord.Interaction):
    logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) used /quickredeemcode (modal).")
    await interaction.response.send_modal(RedeemMultipleCodesModal())

@bot.tree.command(name='list', description='Display a list of codes, Pastebin links, or Local Storage data.')
@app_commands.check(is_owner)
@app_commands.check(is_allowed_admin_channel)
@app_commands.describe(type_to_list='Choose what to list: "code", "link", or "localstorage".')
@app_commands.choices(type_to_list=[
    app_commands.Choice(name="Codes", value="code"),
    app_commands.Choice(name=" Pastebin Links", value="link"),
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
        cursor.execute("SELECT id, data_json FROM ug_phones")
        items = cursor.fetchall()
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
        formatted_items_lines = []
        for item_id, item_content in items:
            formatted_items_lines.append(f"**ID: `{item_id}`**\n```json\n{item_content}\n```")
        if len("\n".join(formatted_items_lines)) > 4000:
            await interaction.followup.send(embed=discord.Embed(
                title=title,
                description="Đang xử lý và gửi dữ liệu Local Storage. Điều này có thể cần nhiều tin nhắn.",
                color=discord.Color.blue()
            ), ephemeral=True)
            logger.info(f"Sending large Local Storage list to {interaction.user.display_name} (ID: {interaction.user.id}) in multiple messages.")
        current_embed_lines = []
        current_embed_length = 0
        max_embed_length = 3800
        for line in formatted_items_lines:
            line_length = len(line) + 1
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
        if current_embed_lines:
            embed_to_send = discord.Embed(
                title=title,
                description="\n".join(current_embed_lines),
                color=color
            )
            embed_to_send.set_footer(text=f"Tổng số {type_to_list.name.lower()}: {len(items)}")
            await interaction.followup.send(embed=embed_to_send, ephemeral=True)
        conn.close()
        return
    if len(description) > 4000:
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
        except Exception as e:
            logger.critical(f"Unexpected error in UGPhoneModal.on_submit for {interaction.user.display_name}: {e}")
            embed = discord.Embed(
                title="❌ Lỗi không mong muốn!",
                description=f'Đã xảy ra lỗi không mong muốn: {e}',
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        finally:
            conn.close()

@bot.tree.command(name='addugphone', description='Add Local Storage info for users to receive.')
@app_commands.check(is_owner)
@app_commands.check(is_allowed_admin_channel)
async def add_ug_phone(interaction: discord.Interaction):
    logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) used /addugphone (modal).")
    await interaction.response.send_modal(UGPhoneModal())

@bot.tree.command(name='quickaddug', description='Start a session to add multiple Local Storage entries.')
@app_commands.check(is_owner)
@app_commands.check(is_allowed_admin_channel)
async def quick_add_ug_command(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in bot.quick_add_ug_sessions:
        embed = discord.Embed(
            title="⚠️ Phiên đã hoạt động!",
            description="Bạn đã có một phiên nhập Local Storage đang hoạt động. Vui lòng gửi `done` để kết thúc hoặc `cancel` để hủy bỏ phiên hiện tại.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.warning(f"User {interaction.user.display_name} (ID: {user_id}) tried to start /quickaddug session but already has one.")
        return
    bot.quick_add_ug_sessions[user_id] = []
    embed = discord.Embed(
        title="✨ Đã bắt đầu phiên thêm nhanh Local Storage! ✨",
        description="Vui lòng bắt đầu dán các chuỗi Local Storage (mỗi chuỗi là một tin nhắn riêng biệt).\n"
                    "Khi bạn hoàn tất, hãy gửi tin nhắn `done` (hoặc `xong`, `hoàn tất`) để lưu trữ.\n"
                    "Gửi `cancel` để hủy bỏ phiên này.",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=False)
    logger.info(f"User {interaction.user.display_name} (ID: {user_id}) started a /quickaddug session.")

@bot.tree.command(name='getugphone', description='Use 150 coins to receive Local Storage.')
async def get_ug_phone_command(interaction: discord.Interaction):
    user_id = interaction.user.id
    cost = 150
    is_owner_user = user_id == OWNER_USER_ID
    if not is_owner_user:
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
    if not is_owner_user:
        await bot.loop.run_in_executor(None, update_user_hcoin, user_id, -cost)
        logger.info(f"User {interaction.user.display_name} (ID: {user_id}) used {cost} coins for Local Storage.")
    try:
        user_dm = await interaction.user.create_dm()
        dm_content = f"```\n{local_storage_data}\n```"
        chunk_size = 1990
        if len(dm_content) > chunk_size:
            chunks = [dm_content[i:i + chunk_size] for i in range(0, len(dm_content), chunk_size)]
            for i, chunk in enumerate(chunks):
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
        cursor.execute("DELETE FROM ug_phones WHERE id = ?", (item_id,))
        conn.commit()
        logger.info(f"Local Storage item with ID {item_id} successfully deleted from DB after being sent to user {user_id}.")
    except discord.Forbidden:
        embed = discord.Embed(
            title="🚫 Không thể gửi DM!",
            description='Tôi không thể gửi tin nhắn trực tiếp cho bạn. Vui lòng kiểm tra cài đặt quyền riêng tư của bạn (cho phép tin nhắn trực tiếp từ thành viên máy chủ). Local Storage không bị trừ và vẫn còn trong kho.',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.error(f"Failed to send DM to {user_id} for /getugphone (Forbidden). Local Storage ID {item_id} was NOT deleted.")
        if not is_owner_user:
            await bot.loop.run_in_executor(None, update_user_hcoin, user_id, cost)
            logger.info(f"Refunded {cost} coins to user {user_id} due to DM failure for Local Storage ID {item_id}.")
    except Exception as e:
        embed = discord.Embed(
            title="❌ Lỗi gửi DM!",
            description=f'Đã xảy ra lỗi khi gửi DM: {e}. Local Storage không bị trừ và vẫn còn trong kho.',
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.critical(f"Unexpected error sending DM to {user_id} for /getugphone: {e}. Local Storage ID {item_id} was NOT deleted.")
        if not is_owner_user:
            await bot.loop.run_in_executor(None, update_user_hcoin, user_id, cost)
            logger.info(f"Refunded {cost} coins to user {user_id} due to DM failure for Local Storage ID {item_id}.")
    finally:
        conn.close()

@bot.tree.command(name='delete_ug_data', description='Delete a Local Storage entry by its full content.')
@app_commands.check(is_owner)
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

@bot.tree.command(name='delete_ug_by_id', description='Delete a Local Storage entry by its unique ID.')
@app_commands.check(is_owner)
@app_commands.check(is_allowed_admin_channel)
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

@bot.tree.command(name='add_hcoin', description='Add Hcoin to a user.')
@app_commands.check(is_owner)
@app_commands.check(is_allowed_admin_channel)
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
    await interaction.response.send_message(embed=embed)
    logger.info(f"Owner {interaction.user.display_name} (ID: {interaction.user.id}) added {amount} coins to {user.display_name} (ID: {user.id}). New balance: {new_balance}.")

@bot.tree.command(name='remove_hcoin', description='Remove Hcoin from a user.')
@app_commands.check(is_owner)
@app_commands.check(is_allowed_admin_channel)
@app_commands.describe(user='The user to remove Hcoin from.', amount='The amount of Hcoin to remove.')
async def remove_hcoin(interaction: discord.Interaction, user: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("Số lượng Hcoin cần xóa phải lớn hơn 0.", ephemeral=True)
        return
    current_balance = await bot.loop.run_in_executor(None, get_user_hcoin, user.id)
    if current_balance < amount:
        embed = discord.Embed(
            title="⚠️ Không đủ Hcoin để xóa!",
            description=f'{user.mention} chỉ có **{current_balance} coin**. Không thể xóa **{amount} coin**.',
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.warning(f"Owner {interaction.user.display_name} (ID: {interaction.user.id}) tried to remove {amount} coins from {user.display_name} (ID: {user.id}), but user only has {current_balance}.")
        return
    await bot.loop.run_in_executor(None, update_user_hcoin, user.id, -amount)
    new_balance = await bot.loop.run_in_executor(None, get_user_hcoin, user.id)
    embed = discord.Embed(
        title="✅ Đã xóa Hcoin!",
        description=f'Đã xóa **{amount} coin** từ {user.mention}.',
        color=discord.Color.green()
    )
    embed.add_field(name="Số dư mới", value=f"**{new_balance} coin**", inline=True)
    await interaction.response.send_message(embed=embed)
    logger.info(f"Owner {interaction.user.display_name} (ID: {interaction.user.id}) removed {amount} coins from {user.display_name} (ID: {user.id}). New balance: {new_balance}.")

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
            user = await bot.fetch_user(user_id)
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
    embed.add_field(name="Các lệnh dành cho chủ sở hữu bot", value="""
    - `/addugphone`: Thêm Local Storage thủ công.
    - `/quickaddug`: Thêm nhiều Local Storage trong một phiên.
    - `/delete_ug_data`: Xóa Local Storage cụ thể (bằng nội dung).
    - `/delete_ug_by_id`: Xóa Local Storage cụ thể (bằng ID).
    - `/remove`: Xóa mã đổi thưởng.
    - `/list`: Liệt kê mã, link Pastebin hoặc Local Storage.
    - `/add_hcoin`: Thêm coin cho người dùng.
    - `/remove_hcoin`: Xóa coin khỏi người dùng.
    - `/sync_commands`: Đồng bộ lệnh slash.
    - `/deduplicate_ugphone`: Chạy deduplication thủ công.
    """, inline=False)
    embed.set_footer(text=f"Bot được tạo bởi [Tên hoặc Nhóm của bạn]")
    embed.timestamp = discord.utils.utcnow()
    await interaction.response.send_message(embed=embed, ephemeral=False)

@bot.tree.command(name='deduplicate_ugphone', description='Manually remove duplicate Local Storage entries.')
@app_commands.check(is_owner)
@app_commands.check(is_allowed_admin_channel)
async def deduplicate_ug_phone_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    logger.info(f"Owner {interaction.user.display_name} (ID: {interaction.user.id}) used /deduplicate_ugphone.")
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

@bot.tree.command(name="sync_commands", description="Syncs slash commands to Discord.")
@app_commands.check(is_owner)
@app_commands.check(is_allowed_admin_channel)
async def sync_commands(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    logger.info(f"Owner {interaction.user.display_name} (ID: {interaction.user.id}) used /sync_commands.")
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

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

if __name__ == "__main__":
    if DISCORD_BOT_TOKEN:
        try:
            bot.run(DISCORD_BOT_TOKEN)
        except Exception as e:
            logger.critical(f"Failed to run bot: {e}")
            print(f"Error: Failed to run bot. Please check your DISCORD_BOT_TOKEN in the .env file. Error: {e}")
    else:
        logger.critical("DISCORD_BOT_TOKEN not found in .env file.")
        print("Error: DISCORD_BOT_TOKEN not found in .env file. Please set it.")
