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
import io
from discord import File

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('discord_bot')

# Load environment variables
load_dotenv()

# Define Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# Database file name
DATABASE_FILE = 'bot_data.db'

# Specific channel ID for admin commands
ALLOWED_ADMIN_CHANNEL_ID = 1383013260902531074

# Environment Variables
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
YEUMONEY_API_TOKEN = os.getenv('YEUMONEY_API_TOKEN')
PASTEBIN_DEV_KEY = os.getenv('PASTEBIN_DEV_KEY')
TEST_GUILD_ID = os.getenv('TEST_GUILD_ID')

# Cooldown for /getcredit command
GET_CREDIT_COOLDOWN_SECONDS = 5 * 60

# Dictionaries for quick add sessions
quick_add_ug_sessions = {}
quick_add_redfinger_sessions = {}

# Generate random alphanumeric code
def generate_random_code(length=20):
    characters = string.ascii_uppercase + string.digits
    return ''.join(random.choice(characters) for i in range(length))

# Initialize database and tables
def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()

    # Existing tables
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS redfinger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_json TEXT NOT NULL UNIQUE
        )
    ''')

    conn.commit()
    conn.close()

    # Deduplicate ug_phones
    initial_count, final_count = deduplicate_ug_phones_data()
    if initial_count != final_count:
        logger.info(f"Deduplication completed for ug_phones. Initial: {initial_count}, Final: {final_count}. Removed {initial_count - final_count} duplicates.")
    else:
        logger.info("No duplicates found in ug_phones table during startup deduplication.")

    # Deduplicate redfinger
    initial_count, final_count = deduplicate_redfinger_data()
    if initial_count != final_count:
        logger.info(f"Deduplication completed for redfinger. Initial: {initial_count}, Final: {final_count}. Removed {initial_count - final_count} duplicates.")
    else:
        logger.info("No duplicates found in redfinger table during startup deduplication.")

# Get user hcoin balance
def get_user_hcoin(user_id: int) -> int:
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT hcoin_balance FROM user_balances WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result:
        return result[0]
    return 0

# Update user hcoin balance
def update_user_hcoin(user_id: int, amount: int):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO user_balances (user_id, hcoin_balance) VALUES (?, COALESCE((SELECT hcoin_balance FROM user_balances WHERE user_id = ?), 0) + ?)", (user_id, user_id, amount))
    conn.commit()
    conn.close()

# Get last getcredit time
def get_last_getcredit_time(user_id: int) -> datetime | None:
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT last_getcredit_time FROM user_cooldowns WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result:
        return datetime.fromisoformat(result[0]).replace(tzinfo=timezone.utc)
    return None

# Set last getcredit time
def set_last_getcredit_time(user_id: int, timestamp: datetime):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO user_cooldowns (user_id, last_getcredit_time) VALUES (?, ?)", (user_id, timestamp.isoformat()))
    conn.commit()
    conn.close()

# Deduplicate ug_phones data
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

# Deduplicate redfinger data
def deduplicate_redfinger_data():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM redfinger")
    initial_count = cursor.fetchone()[0]
    cursor.execute('''
        CREATE TEMPORARY TABLE IF NOT EXISTS redfinger_temp AS
        SELECT MIN(id) as id, data_json
        FROM redfinger
        GROUP BY data_json;
    ''')
    cursor.execute('DELETE FROM redfinger;')
    cursor.execute('INSERT INTO redfinger SELECT id, data_json FROM redfinger_temp;')
    cursor.execute('DROP TABLE IF EXISTS redfinger_temp;')
    cursor.execute("SELECT COUNT(*) FROM redfinger")
    final_count = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return initial_count, final_count

# Create Pastebin paste
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

# Fetch Pastebin content
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

# Create short link
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

# Check if command is in allowed admin channel
def is_allowed_admin_channel(interaction: discord.Interaction) -> bool:
    if interaction.channel.id != ALLOWED_ADMIN_CHANNEL_ID:
        logger.warning(f"Admin command '{interaction.command.name}' attempted by {interaction.user.display_name} (ID: {interaction.user.id}) in unauthorized channel #{interaction.channel.name} (ID: {interaction.channel_id}).")
    return interaction.channel.id == ALLOWED_ADMIN_CHANNEL_ID

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self.quick_add_ug_sessions = quick_add_ug_sessions
        self.quick_add_redfinger_sessions = quick_add_redfinger_sessions

    async def setup_hook(self):
        if TEST_GUILD_ID:
            try:
                test_guild_id_int = int(TEST_GUILD_ID)
                test_guild = discord.Object(id=test_guild_id_int)
                self.tree.copy_global_to(guild=test_guild)
                await self.tree.sync(guild=test_guild)
                logger.info(f'Slash commands synced for TEST_GUILD_ID: {test_guild_id_int} (instant sync)!')
            except ValueError:
                logger.error(f"ERROR: Invalid TEST_GUILD_ID '{TEST_GUILD_ID}' in .env. Falling back to global sync.")
                await self.tree.sync()
                logger.info('Slash commands synced globally.')
            except Exception as e:
                logger.error(f"ERROR syncing to specific guild {TEST_GUILD_ID}: {e}. Falling back to global sync.")
                await self.tree.sync()
                logger.info('Slash commands synced globally.')
        else:
            await self.tree.sync()
            logger.info('Slash commands synced globally.')

    async def on_ready(self):
        logger.info(f'Logged in as {self.user}!')
        await self.loop.run_in_executor(None, init_db)
        logger.info("Database initialized or checked.")

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            logger.warning(f"CheckFailure for command '{interaction.command.name}' by {interaction.user}: {error}")
            try:
                await interaction.response.send_message(f"Bạn không có quyền sử dụng lệnh này.", ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send(f"Bạn không có quyền sử dụng lệnh này.", ephemeral=True)
        elif isinstance(error, app_commands.CommandInvokeError):
            logger.error(f"CommandInvokeError in command '{interaction.command.name}' by {interaction.user}: {error.original}")
            try:
                await interaction.response.send_message(f"Đã xảy ra lỗi khi thực thi lệnh: `{error.original}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send(f"Đã xảy ra lỗi khi thực thi lệnh: `{error.original}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)
        else:
            logger.error(f"Unhandled app command error in command '{interaction.command.name}' by {interaction.user}: {error}")
            try:
                await interaction.response.send_message(f"Đã xảy ra lỗi không mong muốn: `{error}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send(f"Đã xảy ra lỗi không mong muốn: `{error}`. Vui lòng liên hệ quản trị viên.", ephemeral=True)

bot = MyBot()

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
    elif user_id in bot.quick_add_redfinger_sessions:
        lower_content = content.lower()
        if lower_content in ["done", "xong", "hoàn tất"]:
            collected_data = bot.quick_add_redfinger_sessions.pop(user_id)
            logger.info(f"User {message.author.display_name} (ID: {user_id}) ended /quickaddredfinger session. Collected {len(collected_data)} items.")
            if not collected_data:
                embed = discord.Embed(
                    title="ℹ️ Phiên kết thúc!",
                    description="Bạn đã kết thúc phiên nhưng không có dữ liệu Redfinger nào được gửi.",
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
                        cursor.execute("INSERT OR IGNORE INTO redfinger (data_json) VALUES (?)", (data_item,))
                        if cursor.rowcount > 0:
                            added_count += 1
                        else:
                            skipped_count += 1
                    except sqlite3.Error as e:
                        error_count += 1
                        logger.error(f"SQLite Error adding Redfinger data for user {user_id}: {e}")
                    except Exception as e:
                        error_count += 1
                        logger.error(f"Unexpected error adding Redfinger data for user {user_id}: {e}")
                conn.commit()
                conn.close()
                description = f"**{added_count}** dữ liệu Redfinger đã được thêm thành công vào kho.\n"
                if skipped_count > 0:
                    description += f"**{skipped_count}** dữ liệu Redfinger bị bỏ qua (đã tồn tại).\n"
                if error_count > 0:
                    description += f"**{error_count}** dữ liệu Redfinger gặp lỗi khi thêm. Vui lòng kiểm tra console bot."
                embed = discord.Embed(
                    title="✅ Phiên Thêm Nhanh Redfinger Hoàn Tất!",
                    description=description,
                    color=discord.Color.green()
                )
                embed.set_footer(text="Phiên đã kết thúc. Bạn có thể dùng /list redfinger để xem.")
                await message.channel.send(embed=embed)
        elif lower_content == "cancel":
            if user_id in bot.quick_add_redfinger_sessions:
                bot.quick_add_redfinger_sessions.pop(user_id)
                logger.info(f"User {message.author.display_name} (ID: {user_id}) cancelled /quickaddredfinger session.")
                embed = discord.Embed(
                    title="❌ Phiên Thêm Nhanh Redfinger đã Hủy!",
                    description="Phiên nhập dữ liệu Redfinger của bạn đã bị hủy bỏ. Không có dữ liệu nào được lưu.",
                    color=discord.Color.red()
                )
                await message.channel.send(embed=embed)
        else:
            bot.quick_add_redfinger_sessions[user_id].append(content)
            logger.debug(f"User {message.author.display_name} (ID: {user_id}) added data to /quickaddredfinger session: {content[:50]}...")
            try:
                await message.add_reaction("✅")
            except discord.Forbidden:
                pass
    await bot.process_commands(message)

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
                    description=f'Dữ liệu Local Storage đã được lưu vào kho với ID: `{cursor.lastrowid}`.',
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

class RedfingerModal(ui.Modal, title='Nhập Redfinger'):
    data_input = ui.TextInput(
        label='Dán mã hoặc File Json',
        placeholder='Nhập dữ liệu Redfinger tại đây...',
        style=discord.TextStyle.paragraph,
        max_length=4000
    )

    async def on_submit(self, interaction: discord.Interaction):
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT OR IGNORE INTO redfinger (data_json) VALUES (?)", (self.data_input.value,))
            if cursor.rowcount > 0:
                embed = discord.Embed(
                    title="✅ Đã lưu thành công!",
                    description=f'Dữ liệu Redfinger đã được lưu vào kho với ID: `{cursor.lastrowid}`.',
                    color=discord.Color.green()
                )
                logger.info(f"Redfinger data added via modal by {interaction.user.display_name} (ID: {interaction.user.id}).")
            else:
                embed = discord.Embed(
                    title="ℹ️ Dữ liệu đã tồn tại!",
                    description='Dữ liệu Redfinger này đã có trong kho. Không có gì được thêm vào.',
                    color=discord.Color.blue()
                )
                logger.info(f"Duplicate Redfinger data attempted via modal by {interaction.user.display_name} (ID: {interaction.user.id}).")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except sqlite3.Error as e:
            logger.error(f"SQLite Error when saving Redfinger data via modal for {interaction.user.display_name}: {e}")
            embed = discord.Embed(
                title="❌ Lỗi lưu trữ!",
                description=f'Đã xảy ra lỗi khi lưu dữ liệu Redfinger: {e}\n'
                            f'Vui lòng kiểm tra console bot để biết chi tiết hoặc liên hệ quản trị viên.',
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            logger.critical(f"Unexpected error in RedfingerModal.on_submit for {interaction.user.display_name}: {e}")
            embed = discord.Embed(
                title="❌ Lỗi không mong muốn!",
                description=f'Đã xảy ra lỗi không mong muốn: {e}',
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        finally:
            conn.close()

@bot.tree.command(name='getcredit', description='Generate a code and get a short link (5-minute cooldown).')
async def get_credit(interaction: discord.Interaction):
    user_id = interaction.user.id
    current_time = discord.utils.utcnow()
    last_getcredit_time = await bot.loop.run_in_executor(None, get_last_getcredit_time, user_id)

    if last_getcredit_time:
        time_diff = (current_time - last_getcredit_time).total_seconds()
        if time_diff < GET_CREDIT_COOLDOWN_SECONDS:
            remaining_time = int(GET_CREDIT_COOLDOWN_SECONDS - time_diff)
            minutes, seconds = divmod(remaining_time, 60)
            embed = discord.Embed(
                title="⏳ Chưa thể lấy mã mới!",
                description=f"Bạn cần chờ thêm **{minutes} phút {seconds} giây** trước khi lấy mã mới.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.info(f"User {interaction.user.display_name} (ID: {user_id}) tried /getcredit but is on cooldown. Remaining: {remaining_time} seconds.")
            return

    code = generate_random_code()
    pastebin_url = create_pastebin_paste(code)
    if not pastebin_url:
        embed = discord.Embed(
            title="❌ Lỗi tạo mã!",
            description="Không thể tạo mã trên Pastebin. Vui lòng thử lại sau hoặc liên hệ quản trị viên.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    short_url = create_short_link(pastebin_url)
    if not short_url:
        embed = discord.Embed(
            title="❌ Lỗi rút ngắn URL!",
            description="Không thể rút ngắn URL Pastebin. Đây là URL gốc:\n" + pastebin_url,
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"User {interaction.user.display_name} (ID: {user_id}) used /getcredit but failed to create short link. Sent original Pastebin URL: {pastebin_url}")
        return

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO redemption_codes (code) VALUES (?)", (code,))
        conn.commit()
        logger.info(f"User {interaction.user.display_name} (ID: {user_id}) generated code {code} via /getcredit. Short URL: {short_url}")
        embed = discord.Embed(
            title="✅ Mã đã được tạo!",
            description=f"Đây là mã của bạn (hết hạn sau 10 phút):\n**{short_url}**",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await bot.loop.run_in_executor(None, set_last_getcredit_time, user_id, current_time)
    except sqlite3.Error as e:
        logger.error(f"SQLite Error saving redemption code for user {user_id}: {e}")
        embed = discord.Embed(
            title="❌ Lỗi lưu trữ mã!",
            description=f"Đã xảy ra lỗi khi lưu mã: {e}\nVui lòng liên hệ quản trị viên.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    finally:
        conn.close()

@bot.tree.command(name='remove', description='Remove a redemption code (Owner only).')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
@app_commands.describe(code='The redemption code to remove.')
async def remove(interaction: discord.Interaction, code: str):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM redemption_codes WHERE code = ?", (code,))
        if cursor.rowcount > 0:
            conn.commit()
            embed = discord.Embed(
                title="✅ Đã xóa mã!",
                description=f"Mã `{code}` đã được xóa khỏi kho.",
                color=discord.Color.green()
            )
            logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) removed redemption code {code}.")
        else:
            embed = discord.Embed(
                title="ℹ️ Không tìm thấy mã!",
                description=f"Mã `{code}` không tồn tại trong kho.",
                color=discord.Color.blue()
            )
            logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) tried to remove non-existent code {code}.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except sqlite3.Error as e:
        logger.error(f"SQLite Error when removing redemption code {code} by {interaction.user.display_name}: {e}")
        embed = discord.Embed(
            title="❌ Lỗi xóa mã!",
            description=f"Đã xảy ra lỗi khi xóa mã: {e}\nVui lòng kiểm tra console bot để biết chi tiết.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    finally:
        conn.close()

class RedeemMultipleCodesModal(ui.Modal, title='Redeem Multiple Codes'):
    codes_input = ui.TextInput(
        label='Dán các mã, mỗi mã một dòng',
        placeholder='Nhập các mã, mỗi mã một dòng...',
        style=discord.TextStyle.paragraph,
        max_length=4000
    )

    async def on_submit(self, interaction: discord.Interaction):
        codes_to_redeem = [code.strip() for code in self.codes_input.value.split('\n') if code.strip()]
        user_id = interaction.user.id
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        redeemed_count = 0
        invalid_codes = []
        already_redeemed = []
        try:
            for code in codes_to_redeem:
                cursor.execute("SELECT code FROM redemption_codes WHERE code = ?", (code,))
                result = cursor.fetchone()
                if result:
                    cursor.execute("DELETE FROM redemption_codes WHERE code = ?", (code,))
                    redeemed_count += 1
                    update_user_hcoin(user_id, 150)
                else:
                    if code:
                        already_redeemed.append(code)
            conn.commit()
            description = f"Đã đổi thành công **{redeemed_count} mã**, bạn nhận được **{redeemed_count * 150} Hcoin**!\n"
            if already_redeemed:
                description += f"**Các mã không hợp lệ hoặc đã được đổi**:\n" + "\n".join(f"- `{code}`" for code in already_redeemed)
            embed = discord.Embed(
                title="✅ Kết quả đổi mã!",
                description=description,
                color=discord.Color.green()
            )
            logger.info(f"User {interaction.user.display_name} (ID: {user_id}) redeemed {redeemed_count} codes via modal. Invalid/already redeemed: {len(already_redeemed)}")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except sqlite3.Error as e:
            logger.error(f"SQLite Error during multiple code redemption by {interaction.user.display_name}: {e}")
            embed = discord.Embed(
                title="❌ Lỗi đổi mã!",
                description=f"Đã xảy ra lỗi khi đổi mã: {e}\nVui lòng kiểm tra console bot để biết chi tiết.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        finally:
            conn.close()

@bot.tree.command(name='redeem', description='Redeem a single code or multiple codes.')
@app_commands.describe(code='Enter a single code (leave empty for multiple codes).')
async def redeem(interaction: discord.Interaction, code: str = None):
    user_id = interaction.user.id
    if code:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT code FROM redemption_codes WHERE code = ?", (code,))
            result = cursor.fetchone()
            if result:
                cursor.execute("DELETE FROM redemption_codes WHERE code = ?", (code,))
                update_user_hcoin(user_id, 150)
                conn.commit()
                embed = discord.Embed(
                    title="✅ Đổi mã thành công!",
                    description=f"Bạn đã đổi mã `{code}` và nhận được **150 Hcoin**!",
                    color=discord.Color.green()
                )
                logger.info(f"User {interaction.user.display_name} (ID: {user_id}) redeemed code {code}.")
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                embed = discord.Embed(
                    title="❌ Mã không hợp lệ!",
                    description=f"Mã `{code}` không tồn tại hoặc đã được đổi.",
                    color=discord.Color.red()
                )
                logger.info(f"User {interaction.user.display_name} (ID: {user_id}) tried to redeem invalid/already redeemed code {code}.")
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except sqlite3.Error as e:
            logger.error(f"SQLite Error during single code redemption by {interaction.user.display_name}: {e}")
            embed = discord.Embed(
                title="❌ Lỗi đổi mã!",
                description=f"Đã xảy ra lỗi khi đổi mã: {e}\nVui lòng liên hệ quản trị viên.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        finally:
            conn.close()
    else:
        await interaction.response.send_modal(RedeemMultipleCodesModal())

@bot.tree.command(name='quickredeemcode', description='Quickly redeem multiple codes.')
async def quick_redeem_code(interaction: discord.Interaction):
    logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) used /quickredeemcode (modal).")
    await interaction.response.send_modal(RedeemMultipleCodesModal())

@bot.tree.command(name='list', description='List redemption codes, Pastebin links, or Local Storage (Owner only).')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
@app_commands.describe(type_to_list='Choose what to list.')
@app_commands.choices(type_to_list=[
    app_commands.Choice(name="Codes", value="code"),
    app_commands.Choice(name="Pastebin Links", value="link"),
    app_commands.Choice(name="Local Storage", value="localstorage"),
    app_commands.Choice(name="Redfinger", value="redfinger")
])
async def list_items(interaction: discord.Interaction, type_to_list: str):
    await interaction.response.defer(ephemeral=True)
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    color = discord.Color.blue()
    max_embed_size = 6000
    max_field_size = 1024
    embeds = []
    current_embed = discord.Embed(title="", description="", color=color)
    current_size = 0
    items_per_embed = 10

    try:
        if type_to_list == "code":
            title = "🔢 Danh sách Redemption Codes"
            cursor.execute("SELECT code FROM redemption_codes")
            codes = cursor.fetchall()
            if not codes:
                description = "Hiện tại không có mã nào trong kho."
                current_embed = discord.Embed(title=title, description=description, color=color)
                await interaction.followup.send(embed=current_embed, ephemeral=True)
                conn.close()
                return
            formatted_codes = [f"- `{code[0]}`" for code in codes]
            description = "\n".join(formatted_codes[:items_per_embed])
            current_size = len(description)
            current_embed = discord.Embed(title=title, description=description, color=color)
            embeds.append(current_embed)
            for i in range(items_per_embed, len(formatted_codes), items_per_embed):
                description = "\n".join(formatted_codes[i:i + items_per_embed])
                if current_size + len(description) > max_embed_size:
                    current_embed = discord.Embed(title=title + " (Tiếp tục)", description=description, color=color)
                    embeds.append(current_embed)
                    current_size = len(description)
                else:
                    current_embed.description += "\n" + description
                    current_size += len(description)
        elif type_to_list == "link":
            title = "🔗 Danh sách Pastebin Links"
            cursor.execute("SELECT pastebin_url FROM hcoin_pastebin_links")
            links = cursor.fetchall()
            if not links:
                description = "Hiện tại không có Pastebin link nào trong kho."
                current_embed = discord.Embed(title=title, description=description, color=color)
                await interaction.followup.send(embed=current_embed, ephemeral=True)
                conn.close()
                return
            formatted_links = [f"- {link[0]}" for link in links]
            description = "\n".join(formatted_links[:items_per_embed])
            current_size = len(description)
            current_embed = discord.Embed(title=title, description=description, color=color)
            embeds.append(current_embed)
            for i in range(items_per_embed, len(formatted_links), items_per_embed):
                description = "\n".join(formatted_links[i:i + items_per_embed])
                if current_size + len(description) > max_embed_size:
                    current_embed = discord.Embed(title=title + " (Tiếp tục)", description=description, color=color)
                    embeds.append(current_embed)
                    current_size = len(description)
                else:
                    current_embed.description += "\n" + description
                    current_size += len(description)
        elif type_to_list == "localstorage":
            title = "📦 Kho Local Storage"
            cursor.execute("SELECT id, data_json FROM ug_phones")
            items = cursor.fetchall()
            if not items:
                description = "Hiện tại không có Local Storage nào trong kho."
                current_embed = discord.Embed(title=title, description=description, color=color)
                await interaction.followup.send(embed=current_embed, ephemeral=True)
                conn.close()
                return
            formatted_items_lines = [f"**ID: `{item_id}`**\n```json\n{item_content}\n```" for item_id, item_content in items]
            current_field = ""
            fields_count = 0
            for line in formatted_items_lines:
                if len(current_field) + len(line) > max_field_size or fields_count >= 25:
                    current_embed.add_field(name="Dữ liệu", value=current_field or "Không có dữ liệu.", inline=False)
                    current_size += len(current_field)
                    if current_size >= max_embed_size or fields_count >= 25:
                        embeds.append(current_embed)
                        current_embed = discord.Embed(title=title + " (Tiếp tục)", description="", color=color)
                        current_size = 0
                        fields_count = 0
                    current_field = line
                    fields_count += 1
                else:
                    current_field += "\n" + line if current_field else line
                    fields_count += 1
            if current_field:
                current_embed.add_field(name="Dữ liệu", value=current_field, inline=False)
                embeds.append(current_embed)
        elif type_to_list == "redfinger":
            title = "📦 Kho Redfinger"
            cursor.execute("SELECT id, data_json FROM redfinger")
            items = cursor.fetchall()
            if not items:
                description = "Hiện tại không có dữ liệu Redfinger nào trong kho."
                current_embed = discord.Embed(title=title, description=description, color=color)
                await interaction.followup.send(embed=current_embed, ephemeral=True)
                conn.close()
                return
            formatted_items_lines = [f"**ID: `{item_id}`**\n```json\n{item_content}\n```" for item_id, item_content in items]
            current_field = ""
            fields_count = 0
            for line in formatted_items_lines:
                if len(current_field) + len(line) > max_field_size or fields_count >= 25:
                    current_embed.add_field(name="Dữ liệu", value=current_field or "Không có dữ liệu.", inline=False)
                    current_size += len(current_field)
                    if current_size >= max_embed_size or fields_count >= 25:
                        embeds.append(current_embed)
                        current_embed = discord.Embed(title=title + " (Tiếp tục)", description="", color=color)
                        current_size = 0
                        fields_count = 0
                    current_field = line
                    fields_count += 1
                else:
                    current_field += "\n" + line if current_field else line
                    fields_count += 1
            if current_field:
                current_embed.add_field(name="Dữ liệu", value=current_field, inline=False)
                embeds.append(current_embed)
        for embed in embeds:
            await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) listed {type_to_list}.")
    except sqlite3.Error as e:
        logger.error(f"SQLite Error when listing {type_to_list} by {interaction.user.display_name}: {e}")
        embed = discord.Embed(
            title="❌ Lỗi liệt kê!",
            description=f"Đã xảy ra lỗi khi liệt kê {type_to_list}: {e}\nVui lòng kiểm tra console bot để biết chi tiết.",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    finally:
        conn.close()

@bot.tree.command(name='getugphone', description='Spend 150 Hcoin to receive a random Local Storage (sent via DM).')
async def get_ug_phone(interaction: discord.Interaction):
    user_id = interaction.user.id
    hcoin_balance = await bot.loop.run_in_executor(None, get_user_hcoin, user_id)
    if hcoin_balance < 150:
        embed = discord.Embed(
            title="❌ Không đủ Hcoin!",
            description=f"Bạn cần **150 Hcoin** để lấy Local Storage. Số dư hiện tại: **{hcoin_balance} Hcoin**.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"User {interaction.user.display_name} (ID: {user_id}) tried /getugphone but has insufficient Hcoin: {hcoin_balance}.")
        return

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, data_json FROM ug_phones ORDER BY RANDOM() LIMIT 1")
        result = cursor.fetchone()
        if not result:
            embed = discord.Embed(
                title="❌ Hết Local Storage!",
                description="Hiện tại không có Local Storage nào trong kho. Vui lòng thử lại sau.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.info(f"User {interaction.user.display_name} (ID: {user_id}) tried /getugphone but no Local Storage available.")
            conn.close()
            return

        ug_phone_id, ug_phone_data = result
        owner_role = discord.utils.get(interaction.guild.roles, name="Owner")
        is_owner = owner_role in interaction.user.roles if owner_role else False
        try:
            await interaction.user.send(f"Dưới đây là Local Storage của bạn:\n```json\n{ug_phone_data}\n```")
            if not is_owner:
                cursor.execute("DELETE FROM ug_phones WHERE id = ?", (ug_phone_id,))
                update_user_hcoin(user_id, -150)
                conn.commit()
            embed = discord.Embed(
                title="✅ Đã gửi Local Storage!",
                description="Local Storage đã được gửi qua DM. Vui lòng kiểm tra tin nhắn riêng.\n"
                            f"Số dư Hcoin hiện tại: **{hcoin_balance - 150 if not is_owner else hcoin_balance} Hcoin**.",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.info(f"User {interaction.user.display_name} (ID: {user_id}) received Local Storage ID {ug_phone_id} via /getugphone. Owner: {is_owner}.")
        except discord.Forbidden:
            embed = discord.Embed(
                title="❌ Không thể gửi DM!",
                description="Bot không thể gửi tin nhắn DM cho bạn. Vui lòng kiểm tra cài đặt DM của bạn và thử lại.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.warning(f"Failed to DM Local Storage to {interaction.user.display_name} (ID: {user_id}) due to DM restrictions.")
        except discord.HTTPException as e:
            embed = discord.Embed(
                title="❌ Lỗi gửi DM!",
                description=f"Đã xảy ra lỗi khi gửi DM: {e}\nVui lòng thử lại sau hoặc liên hệ quản trị viên.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.error(f"HTTP Error sending Local Storage DM to {interaction.user.display_name} (ID: {user_id}): {e}")
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.error(f"SQLite Error during /getugphone for {interaction.user.display_name}: {e}")
        embed = discord.Embed(
            title="❌ Lỗi truy xuất!",
            description=f"Đã xảy ra lỗi khi lấy Local Storage: {e}\nVui lòng liên hệ quản trị viên.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        conn.close()

@bot.tree.command(name='getredfinger', description='Spend 150 Hcoin to receive a random Redfinger data (sent via DM as .txt).')
async def get_redfinger(interaction: discord.Interaction):
    user_id = interaction.user.id
    hcoin_balance = await bot.loop.run_in_executor(None, get_user_hcoin, user_id)
    if hcoin_balance < 150:
        embed = discord.Embed(
            title="❌ Không đủ Hcoin!",
            description=f"Bạn cần **150 Hcoin** để lấy dữ liệu Redfinger. Số dư hiện tại: **{hcoin_balance} Hcoin**.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"User {interaction.user.display_name} (ID: {user_id}) tried /getredfinger but has insufficient Hcoin: {hcoin_balance}.")
        return

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, data_json FROM redfinger ORDER BY RANDOM() LIMIT 1")
        result = cursor.fetchone()
        if not result:
            embed = discord.Embed(
                title="❌ Hết dữ liệu Redfinger!",
                description="Hiện tại không có dữ liệu Redfinger nào trong kho. Vui lòng thử lại sau.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.info(f"User {interaction.user.display_name} (ID: {user_id}) tried /getredfinger but no Redfinger data available.")
            conn.close()
            return

        redfinger_id, redfinger_data = result
        owner_role = discord.utils.get(interaction.guild.roles, name="Owner")
        is_owner = owner_role in interaction.user.roles if owner_role else False
        try:
            file_content = io.StringIO(redfinger_data)
            file = discord.File(file_content, filename=f"redfinger_{redfinger_id}.txt")
            await interaction.user.send("Dưới đây là dữ liệu Redfinger của bạn:", file=file)
            file_content.close()
            if not is_owner:
                cursor.execute("DELETE FROM redfinger WHERE id = ?", (redfinger_id,))
                update_user_hcoin(user_id, -150)
                conn.commit()
            embed = discord.Embed(
                title="✅ Đã gửi dữ liệu Redfinger!",
                description="Dữ liệu Redfinger đã được gửi qua DM dưới dạng file .txt. Vui lòng kiểm tra tin nhắn riêng.\n"
                            f"Số dư Hcoin hiện tại: **{hcoin_balance - 150 if not is_owner else hcoin_balance} Hcoin**.",
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.info(f"User {interaction.user.display_name} (ID: {user_id}) received Redfinger data ID {redfinger_id} via /getredfinger. Owner: {is_owner}.")
        except discord.Forbidden:
            embed = discord.Embed(
                title="❌ Không thể gửi DM!",
                description="Bot không thể gửi tin nhắn DM cho bạn. Vui lòng kiểm tra cài đặt DM của bạn và thử lại.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.warning(f"Failed to DM Redfinger data to {interaction.user.display_name} (ID: {user_id}) due to DM restrictions.")
        except discord.HTTPException as e:
            embed = discord.Embed(
                title="❌ Lỗi gửi DM!",
                description=f"Đã xảy ra lỗi khi gửi DM: {e}\nVui lòng thử lại sau hoặc liên hệ quản trị viên.",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            logger.error(f"HTTP Error sending Redfinger data DM to {interaction.user.display_name} (ID: {user_id}): {e}")
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.error(f"SQLite Error during /getredfinger for {interaction.user.display_name}: {e}")
        embed = discord.Embed(
            title="❌ Lỗi truy xuất!",
            description=f"Đã xảy ra lỗi khi lấy dữ liệu Redfinger: {e}\nVui lòng liên hệ quản trị viên.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        conn.close()

@bot.tree.command(name='delete_ug_data', description='Delete Local Storage by data (Owner only).')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
@app_commands.describe(data='The Local Storage data to delete.')
async def delete_ug_data(interaction: discord.Interaction, data: str):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM ug_phones WHERE data_json = ?", (data,))
        if cursor.rowcount > 0:
            conn.commit()
            embed = discord.Embed(
                title="✅ Đã xóa Local Storage!",
                description="Local Storage đã được xóa khỏi kho.",
                color=discord.Color.green()
            )
            logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) deleted Local Storage data via /delete_ug_data.")
        else:
            embed = discord.Embed(
                title="ℹ️ Không tìm thấy Local Storage!",
                description="Local Storage này không tồn tại trong kho.",
                color=discord.Color.blue()
            )
            logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) tried to delete non-existent Local Storage data.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except sqlite3.Error as e:
        logger.error(f"SQLite Error when deleting Local Storage data by {interaction.user.display_name}: {e}")
        embed = discord.Embed(
            title="❌ Lỗi xóa Local Storage!",
            description=f"Đã xảy ra lỗi khi xóa Local Storage: {e}\nVui lòng kiểm tra console bot để biết chi tiết.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    finally:
        conn.close()

@bot.tree.command(name='delete_ug_by_id', description='Delete Local Storage by ID (Owner only).')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
@app_commands.describe(id='The ID of the Local Storage to delete.')
async def delete_ug_by_id(interaction: discord.Interaction, id: int):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM ug_phones WHERE id = ?", (id,))
        if cursor.rowcount > 0:
            conn.commit()
            embed = discord.Embed(
                title="✅ Đã xóa Local Storage!",
                description=f"Local Storage với ID `{id}` đã được xóa khỏi kho.",
                color=discord.Color.green()
            )
            logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) deleted Local Storage ID {id} via /delete_ug_by_id.")
        else:
            embed = discord.Embed(
                title="ℹ️ Không tìm thấy Local Storage!",
                description=f"Local Storage với ID `{id}` không tồn tại trong kho.",
                color=discord.Color.blue()
            )
            logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) tried to delete non-existent Local Storage ID {id}.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except sqlite3.Error as e:
        logger.error(f"SQLite Error when deleting Local Storage ID {id} by {interaction.user.display_name}: {e}")
        embed = discord.Embed(
            title="❌ Lỗi xóa Local Storage!",
            description=f"Đã xảy ra lỗi khi xóa Local Storage: {e}\nVui lòng kiểm tra console bot để biết chi tiết.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    finally:
        conn.close()

@bot.tree.command(name='balance', description='Check your Hcoin balance.')
async def balance(interaction: discord.Interaction):
    user_id = interaction.user.id
    hcoin_balance = await bot.loop.run_in_executor(None, get_user_hcoin, user_id)
    embed = discord.Embed(
        title="💰 Số dư Hcoin",
        description=f"Số dư Hcoin của bạn: **{hcoin_balance} Hcoin**",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    logger.info(f"User {interaction.user.display_name} (ID: {user_id}) checked balance: {hcoin_balance} Hcoin.")

@bot.tree.command(name='add_hcoin', description='Add Hcoin to a user (Owner only).')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
@app_commands.describe(user='The user to add Hcoin to.', amount='The amount of Hcoin to add.')
async def add_hcoin(interaction: discord.Interaction, user: discord.User, amount: int):
    if amount <= 0:
        embed = discord.Embed(
            title="❌ Số lượng không hợp lệ!",
            description="Số lượng Hcoin phải lớn hơn 0.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) tried to add invalid Hcoin amount {amount} to {user.display_name}.")
        return
    await bot.loop.run_in_executor(None, update_user_hcoin, user.id, amount)
    embed = discord.Embed(
        title="✅ Đã thêm Hcoin!",
        description=f"Đã thêm **{amount} Hcoin** vào tài khoản của {user.mention}.",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) added {amount} Hcoin to {user.display_name} (ID: {user.id}).")

@bot.tree.command(name='remove_hcoin', description='Remove Hcoin from a user (Owner only).')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
@app_commands.describe(user='The user to remove Hcoin from.', amount='The amount of Hcoin to remove.')
async def remove_hcoin(interaction: discord.Interaction, user: discord.User, amount: int):
    if amount <= 0:
        embed = discord.Embed(
            title="❌ Số lượng không hợp lệ!",
            description="Số lượng Hcoin phải lớn hơn 0.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) tried to remove invalid Hcoin amount {amount} from {user.display_name}.")
        return
    current_balance = await bot.loop.run_in_executor(None, get_user_hcoin, user.id)
    if current_balance < amount:
        embed = discord.Embed(
            title="❌ Không đủ Hcoin!",
            description=f"{user.mention} chỉ có **{current_balance} Hcoin**, không thể xóa **{amount} Hcoin**.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) tried to remove {amount} Hcoin from {user.display_name} but balance is {current_balance}.")
        return
    await bot.loop.run_in_executor(None, update_user_hcoin, user.id, -amount)
    embed = discord.Embed(
        title="✅ Đã xóa Hcoin!",
        description=f"Đã xóa **{amount} Hcoin** khỏi tài khoản của {user.mention}.",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) removed {amount} Hcoin from {user.display_name} (ID: {user.id}).")

@bot.tree.command(name='hcoin_top', description='Show top 10 users by Hcoin balance.')
async def hcoin_top(interaction: discord.Interaction):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id, hcoin_balance FROM user_balances ORDER BY hcoin_balance DESC LIMIT 10")
        top_users = cursor.fetchall()
        if not top_users:
            embed = discord.Embed(
                title="🏆 Bảng xếp hạng Hcoin",
                description="Hiện tại không có người dùng nào có Hcoin.",
                color=discord.Color.blue()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            conn.close()
            return
        description = ""
        for i, (user_id, balance) in enumerate(top_users, 1):
            user = await bot.fetch_user(user_id)
            display_name = user.display_name if user else f"User ID {user_id}"
            description += f"{i}. **{display_name}**: {balance} Hcoin\n"
        embed = discord.Embed(
            title="🏆 Bảng xếp hạng Hcoin",
            description=description,
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) viewed Hcoin leaderboard.")
    except sqlite3.Error as e:
        logger.error(f"SQLite Error during /hcoin_top for {interaction.user.display_name}: {e}")
        embed = discord.Embed(
            title="❌ Lỗi truy xuất!",
            description=f"Đã xảy ra lỗi khi lấy bảng xếp hạng: {e}\nVui lòng liên hệ quản trị viên.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    finally:
        conn.close()

@bot.tree.command(name='info', description='Show bot information and command list.')
async def info(interaction: discord.Interaction):
    embed = discord.Embed(
        title="ℹ️ Thông tin về Bot",
        description="Bot này cung cấp các lệnh để quản lý mã, Local Storage, Redfinger và Hcoin. Dưới đây là danh sách các lệnh:",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="Lệnh dành cho tất cả người dùng",
        value="""
        - `/getcredit`: Tạo mã và nhận link ngắn (cooldown 5 phút).
        - `/redeem`: Đổi một hoặc nhiều mã để nhận 150 Hcoin mỗi mã.
        - `/quickredeemcode`: Đổi nhanh nhiều mã qua modal.
        - `/getugphone`: Dùng 150 Hcoin để nhận một Local Storage ngẫu nhiên qua DM.
        - `/getredfinger`: Dùng 150 Hcoin để nhận một dữ liệu Redfinger ngẫu nhiên qua DM (dạng .txt).
        - `/balance`: Kiểm tra số dư Hcoin của bạn.
        - `/hcoin_top`: Xem top 10 người dùng có nhiều Hcoin nhất.
        - `/info`: Hiển thị thông tin này.
        """,
        inline=False
    )
    embed.add_field(
        name="Các lệnh Quản trị viên (chỉ trong kênh admin)",
        value="""
        - `/addugphone`: Thêm Local Storage thủ công.
        - `/addredfinger`: Thêm dữ liệu Redfinger thủ công.
        - `/quickaddug`: Thêm nhiều Local Storage qua tin nhắn.
        - `/quickaddredfinger`: Thêm nhiều dữ liệu Redfinger qua tin nhắn.
        - `/delete_ug_data`: Xóa Local Storage theo nội dung.
        - `/delete_ug_by_id`: Xóa Local Storage theo ID.
        - `/remove`: Xóa một mã redemption.
        - `/list`: Liệt kê mã, Pastebin links, Local Storage hoặc Redfinger.
        - `/add_hcoin`: Thêm Hcoin cho người dùng.
        - `/remove_hcoin`: Xóa Hcoin khỏi người dùng.
        - `/deduplicate_ugphone`: Xóa các Local Storage trùng lặp.
        - `/sync_commands`: Đồng bộ lại các lệnh (chỉ Owner bot).
        """,
        inline=False
    )
    embed.set_footer(text="Bot được tạo bởi [Tên của bạn].")
    await interaction.response.send_message(embed=embed, ephemeral=True)
    logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) used /info.")

@bot.tree.command(name='deduplicate_ugphone', description='Remove duplicate Local Storage entries (Owner only).')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
async def deduplicate_ugphone(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    initial_count, final_count = await bot.loop.run_in_executor(None, deduplicate_ug_phones_data)
    removed_count = initial_count - final_count
    if removed_count > 0:
        embed = discord.Embed(
            title="✅ Đã xóa trùng lặp!",
            description=f"Đã xóa **{removed_count}** Local Storage trùng lặp. Tổng số còn lại: **{final_count}**.",
            color=discord.Color.green()
        )
        logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) deduplicated ug_phones. Removed {removed_count} duplicates.")
    else:
        embed = discord.Embed(
            title="ℹ️ Không có trùng lặp!",
            description="Không tìm thấy Local Storage trùng lặp trong kho.",
            color=discord.Color.blue()
        )
        logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) ran /deduplicate_ugphone but no duplicates found.")
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name='quickaddug', description='Start a session to add multiple Local Storage entries (Owner only).')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
async def quick_add_ug(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in bot.quick_add_ug_sessions or user_id in bot.quick_add_redfinger_sessions:
        embed = discord.Embed(
            title="⚠️ Phiên đang hoạt động!",
            description="Bạn đã có một phiên nhập dữ liệu đang chạy. Gửi `done`, `xong`, hoặc `hoàn tất` để kết thúc, hoặc `cancel` để hủy.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"User {interaction.user.display_name} (ID: {user_id}) tried /quickaddug but already has an active session.")
        return
    bot.quick_add_ug_sessions[user_id] = []
    embed = discord.Embed(
        title="📥 Bắt đầu phiên nhập Local Storage!",
        description="Gửi từng Local Storage qua tin nhắn. Khi hoàn tất, gửi `done`, `xong`, hoặc `hoàn tất`. Để hủy, gửi `cancel`.",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    logger.info(f"User {interaction.user.display_name} (ID: {user_id}) started /quickaddug session.")

@bot.tree.command(name='quickaddredfinger', description='Start a session to add multiple Redfinger entries (Owner only).')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
async def quick_add_redfinger(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id in bot.quick_add_ug_sessions or user_id in bot.quick_add_redfinger_sessions:
        embed = discord.Embed(
            title="⚠️ Phiên đang hoạt động!",
            description="Bạn đã có một phiên nhập dữ liệu đang chạy. Gửi `done`, `xong`, hoặc `hoàn tất` để kết thúc, hoặc `cancel` để hủy.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"User {interaction.user.display_name} (ID: {user_id}) tried /quickaddredfinger but already has an active session.")
        return
    bot.quick_add_redfinger_sessions[user_id] = []
    embed = discord.Embed(
        title="📥 Bắt đầu phiên nhập Redfinger!",
        description="Gửi từng dữ liệu Redfinger qua tin nhắn. Khi hoàn tất, gửi `done`, `xong`, hoặc `hoàn tất`. Để hủy, gửi `cancel`.",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    logger.info(f"User {interaction.user.display_name} (ID: {user_id}) started /quickaddredfinger session.")

@bot.tree.command(name='addugphone', description='Add Local Storage info for users to receive.')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
async def add_ug_phone(interaction: discord.Interaction):
    logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) used /addugphone (modal).")
    await interaction.response.send_modal(UGPhoneModal())

@bot.tree.command(name='addredfinger', description='Add Redfinger data for users to receive.')
@commands.has_role("Owner")
@app_commands.check(is_allowed_admin_channel)
async def add_redfinger(interaction: discord.Interaction):
    logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) used /addredfinger (modal).")
    await interaction.response.send_modal(RedfingerModal())

@bot.tree.command(name='sync_commands', description='Sync slash commands (Bot Owner only).')
@commands.is_owner()
@app_commands.check(is_allowed_admin_channel)
async def sync_commands(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await bot.tree.sync()
        embed = discord.Embed(
            title="✅ Đã đồng bộ lệnh!",
            description="Các lệnh slash đã được đồng bộ hóa toàn cầu. Có thể mất đến 1 giờ để cập nhật.",
            color=discord.Color.green()
        )
        logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) synced slash commands globally.")
        if TEST_GUILD_ID:
            try:
                test_guild_id_int = int(TEST_GUILD_ID)
                test_guild = discord.Object(id=test_guild_id_int)
                await bot.tree.sync(guild=test_guild)
                embed.description += f"\nĐã đồng bộ tức thì cho guild ID: {test_guild_id_int}."
                logger.info(f"User {interaction.user.display_name} (ID: {interaction.user.id}) synced slash commands for TEST_GUILD_ID: {test_guild_id_int}.")
            except ValueError:
                logger.error(f"Invalid TEST_GUILD_ID '{TEST_GUILD_ID}' in .env during /sync_commands.")
                embed.description += f"\nLỗi: TEST_GUILD_ID '{TEST_GUILD_ID}' không hợp lệ, đã bỏ qua đồng bộ guild."
            except Exception as e:
                logger.error(f"Error syncing to TEST_GUILD_ID {TEST_GUILD_ID}: {e}")
                embed.description += f"\nLỗi đồng bộ guild ID {TEST_GUILD_ID}: {e}"
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error syncing commands by {interaction.user.display_name}: {e}")
        embed = discord.Embed(
            title="❌ Lỗi đồng bộ lệnh!",
            description=f"Đã xảy ra lỗi khi đồng bộ lệnh: {e}\nVui lòng kiểm tra console bot để biết chi tiết.",
            color=discord.Color.red()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

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
             interaction.command.name in ["remove", "list", "addugphone", "addredfinger", "quickaddug", "quickaddredfinger", "delete_ug_data", "delete_ug_by_id", "add_hcoin", "remove_hcoin", "deduplicate_ugphone", "sync_commands"]:
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
        print("Error: DISCORD_BOT_TOKEN not found in .env file. Please add it to proceed.")
