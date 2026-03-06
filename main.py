import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
import os
import shutil
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# --- [1] 설정 및 상수 ---
DB_PATH = '/app/data/discord_bot.db' 
HALF_TIME_CHANNEL_ID = int(os.getenv('HALF_TIME_CHANNEL_ID'))
LOG_CHANNEL_ID = int(os.getenv('TARGET_CHANNEL_ID'))
KST = timezone(timedelta(hours=9))

# --- [2] DB 초기화 ---
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS user_stats 
                   (user_id INTEGER PRIMARY KEY, total_seconds INTEGER DEFAULT 0)''')
    cur.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    conn.commit()
    conn.close()

init_db()

# --- [3] 봇 클래스 및 스케줄러 설정 ---
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.message_content = True
        intents.members = True 
        super().__init__(command_prefix='!', intents=intents)
        self.active_sessions = {} 

    async def setup_hook(self):
        await self.tree.sync()
        self.scheduler = AsyncIOScheduler(timezone=KST)
        self.scheduler.add_job(
            monthly_force_save, 
            CronTrigger(day=1, hour=0, minute=0, second=0),
            args=[self]
        )
        self.scheduler.start()
        print("✅ 시스템 동기화 및 KST 스케줄러 활성화!")

bot = MyBot()

# --- [4] 유틸리티 함수 ---
def calculate_realtime(user_id, saved_seconds, bot_instance):
    if user_id in bot_instance.active_sessions:
        join_time, channel_id = bot_instance.active_sessions[user_id]
        now_plain = datetime.now(KST).replace(tzinfo=None)
        elapsed = (now_plain - join_time).total_seconds()
        weight = 0.5 if channel_id == HALF_TIME_CHANNEL_ID else 1.0
        return (saved_seconds or 0) + int(elapsed * weight), True, weight, channel_id
    return (saved_seconds or 0), False, 1.0, None

def format_time(seconds):
    h, m = divmod(seconds // 60, 60)
    return f"{h}시간 {m % 60}분"

def save_to_db(user_id, duration):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('INSERT OR IGNORE INTO user_stats (user_id, total_seconds) VALUES (?, 0)', (user_id,))
    cur.execute('UPDATE user_stats SET total_seconds = total_seconds + ? WHERE user_id = ?', (duration, user_id))
    conn.commit()
    conn.close()

# --- [5] 핵심 이벤트: 입퇴장 및 이동 기록 ---
@bot.event
async def on_ready():
    now_plain = datetime.now(KST).replace(tzinfo=None)
    recovery_count = 0
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for member in vc.members:
                if not member.bot:
                    bot.active_sessions[member.id] = (now_plain, vc.id)
                    recovery_count += 1
    print(f'Logged in as {bot.user.name} | 복구 세션: {recovery_count}개')

@bot.event
async def on_voice_state_update(member, before, after):
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    now_plain = datetime.now(KST).replace(tzinfo=None)

    # 1. 입장
    if before.channel is None and after.channel is not None:
        bot.active_sessions[member.id] = (now_plain, after.channel.id)
        if log_channel: await log_channel.send(f"📥 **{member.display_name}** 입장 -> `{after.channel.name}`")

    # 2. 퇴장
    elif before.channel is not None and after.channel is None:
        if member.id in bot.active_sessions:
            join_time, channel_id = bot.active_sessions.pop(member.id)
            elapsed = int((now_plain - join_time).total_seconds())
            weight = 0.5 if channel_id == HALF_TIME_CHANNEL_ID else 1.0
            final_duration = int(elapsed * weight)
            save_to_db(member.id, final_duration)
            if log_channel: await log_channel.send(f"📤 **{member.display_name}** 퇴장 | ⏱️ {final_duration//60}분 기록 완료")

    # 3. 채널 이동 (기록 저장 후 새 세션 시작)
    elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
        if member.id in bot.active_sessions:
            join_time, old_ch_id = bot.active_sessions.pop(member.id)
            elapsed = int((now_plain - join_time).total_seconds())
            weight = 0.5 if old_ch_id == HALF_TIME_CHANNEL_ID else 1.0
            final_duration = int(elapsed * weight)
            save_to_db(member.id, final_duration)
            
            # 새 채널로 세션 갱신
            bot.active_sessions[member.id] = (now_plain, after.channel.id)
            if log_channel:
                await log_channel.send(f"🔄 **{member.display_name}** 이동: `{before.channel.name}` -> `{after.channel.name}` (⏱️ {final_duration//60}분 저장됨)")

# --- [6] 스케줄러: 월간 정산 ---
async def monthly_force_save(bot_instance):
    now_kst = datetime.now(KST)
    now_plain = now_kst.replace(tzinfo=None)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    active_count = 0
    for user_id, (join_time, channel_id) in list(bot_instance.active_sessions.items()):
        elapsed = int((now_plain - join_time).total_seconds())
        weight = 0.5 if channel_id == HALF_TIME_CHANNEL_ID else 1.0
        cur.execute('UPDATE user_stats SET total_seconds = total_seconds + ? WHERE user_id = ?', (int(elapsed * weight), user_id))
        bot_instance.active_sessions[user_id] = (now_plain, channel_id) # 기준점 갱신
        active_count += 1
    cur.execute('INSERT OR REPLACE INTO settings VALUES ("last_reset_month", ?)', (now_kst.strftime("%Y-%m"),))
    conn.commit()
    conn.close()
    log_channel = bot_instance.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        await log_channel.send(embed=discord.Embed(title=f"📅 {now_kst.month}월 정산 완료", description=f"접속 중인 {active_count}명의 기록이 누적치에 반영되었습니다.", color=0x5865F2))

# --- [7] 일반 명령어 (Slash) ---
@bot.tree.command(name="순위표", description="전체 접속 시간 랭킹 상위 10명을 확인합니다.")
async def leaderboard_s(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT user_id, total_seconds FROM user_stats ORDER BY total_seconds DESC LIMIT 10')
    rows = cur.fetchall()
    conn.close()

    embed = discord.Embed(title="🏆 전체 누적 랭킹", color=0xf1c40f)
    medal_icons = ["🥇", "🥈", "🥉", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅"]
    rank_list = ""
    for i, (uid, total_sec) in enumerate(rows):
        total, online, _, _ = calculate_realtime(uid, total_sec, bot)
        user = interaction.guild.get_member(uid)
        name = user.display_name if user else f"유저({uid})"
        rank_list += f"{medal_icons[i]} **{i+1}위** | {'🟢' if online else '⚪'} **{name}**\n┗ ⏱️ `{format_time(total)}` 누적\n\n"
    
    embed.description = rank_list or "기록이 없습니다."
    embed.set_footer(text=f"기준 시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')} KST")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="접속확인", description="특정 유저의 상세 접속 정보를 확인합니다.")
async def check_user_s(interaction: discord.Interaction, member: discord.Member):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT total_seconds FROM user_stats WHERE user_id = ?', (member.id,))
    row = cur.fetchone()
    conn.close()

    total, online, weight, ch_id = calculate_realtime(member.id, row[0] if row else 0, bot)
    embed = discord.Embed(title=f"📡 {member.display_name} 정보", color=0x2ecc71 if online else 0x95a5a6)
    embed.add_field(name="상태", value="접속 중 🟢" if online else "오프라인 ⚪", inline=True)
    embed.add_field(name="누적 시간", value=f"**{format_time(total)}**", inline=True)
    if online:
        channel = bot.get_channel(ch_id)
        embed.add_field(name="현재 채널", value=f"`{channel.name if channel else '알 수 없음'}` ({weight}x)", inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="내기록", description="나의 순위와 시간을 확인합니다.")
async def my_record_s(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT user_id, total_seconds FROM user_stats ORDER BY total_seconds DESC')
    rows = cur.fetchall()
    conn.close()

    sec, rank = 0, 0
    for i, (uid, s) in enumerate(rows):
        if uid == interaction.user.id: sec, rank = s, i + 1; break
    total, online, weight, _ = calculate_realtime(interaction.user.id, sec, bot)
    embed = discord.Embed(title=f"👤 {interaction.user.display_name} 기록", color=0x2ecc71)
    embed.add_field(name="순위", value=f"**{rank}위**", inline=True)
    embed.add_field(name="시간", value=f"**{format_time(total)}**", inline=True)
    embed.add_field(name="상태", value=f"{'접속 중 ('+str(weight)+'x)' if online else '오프라인'}", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="도움말", description="명령어 가이드")
async def help_s(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 도움말", description="`/순위표`, `/내기록`, `/접속확인 @유저`, `/시간저장`", color=0x3498db)
    if interaction.user.guild_permissions.administrator:
        embed.add_field(name="🛠️ 관리자", value="`/시간수정 @유저 초`, `/db백업`, `/시간저장`", inline=False)
    await interaction.response.send_message(embed=embed)

# --- [8] 관리자 명령어 (Slash) ---
@bot.tree.command(name="시간저장", description="[관리자] 현재 접속 중인 모든 유저의 시간을 DB에 즉시 저장합니다.")
@app_commands.default_permissions(administrator=True)
async def force_save_s(interaction: discord.Interaction):
    now_plain = datetime.now(KST).replace(tzinfo=None)
    count = 0
    for user_id, (join_time, channel_id) in list(bot.active_sessions.items()):
        elapsed = int((now_plain - join_time).total_seconds())
        weight = 0.5 if channel_id == HALF_TIME_CHANNEL_ID else 1.0
        save_to_db(user_id, int(elapsed * weight))
        bot.active_sessions[user_id] = (now_plain, channel_id) # 기준 시각 갱신
        count += 1
    await interaction.response.send_message(f"💾 실시간 세션 {count}개를 DB에 동기화했습니다.", ephemeral=True)

@bot.tree.command(name="시간수정", description="[관리자] 시간 수정")
@app_commands.default_permissions(administrator=True)
async def adjust_time_s(interaction: discord.Interaction, member: discord.Member, seconds: int):
    save_to_db(member.id, seconds)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT total_seconds FROM user_stats WHERE user_id = ?', (member.id,))
    new_t = cur.fetchone()[0]
    conn.close()
    await interaction.response.send_message(f"🛠️ {member.display_name} 수정 완료 (최종: {format_time(new_t)})")

@bot.tree.command(name="db백업", description="[관리자] DB 백업")
@app_commands.default_permissions(administrator=True)
async def db_backup_s(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    file_path = f"backup_{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}.db"
    shutil.copy2(DB_PATH, file_path)
    await interaction.followup.send(content="📦 DB 백업", file=discord.File(file_path))
    os.remove(file_path)

bot.run(os.getenv('BOT_TOKEN'))
