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
KST = timezone(timedelta(hours=9)) # 한국 표준시 설정

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

# --- [3] 봇 클래스 정의 ---
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
        # 월간 정산 스케줄러 (매달 1일 00:00 KST)
        self.scheduler = AsyncIOScheduler(timezone=KST)
        self.scheduler.add_job(
            monthly_force_save, 
            CronTrigger(day=1, hour=0, minute=0, second=0),
            args=[self]
        )
        self.scheduler.start()
        print("✅ 시스템 스케줄러 및 슬래시 명령어 동기화 완료!")

bot = MyBot()

# --- [4] 유틸리티: 실시간 시간 계산 함수 ---
def calculate_realtime(user_id, saved_seconds, bot_instance):
    if user_id in bot_instance.active_sessions:
        join_time, channel_id = bot_instance.active_sessions[user_id]
        now_plain = datetime.now(KST).replace(tzinfo=None)
        elapsed = (now_plain - join_time).total_seconds()
        weight = 0.5 if channel_id == HALF_TIME_CHANNEL_ID else 1.0
        return (saved_seconds or 0) + int(elapsed * weight), True, weight
    return (saved_seconds or 0), False, 1.0

# --- [5] 핵심 이벤트: 봇 준비 및 세션 복구 ---
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
    print(f'Logged in as {bot.user.name}')
    print(f"🔄 세션 복구: {recovery_count}명의 유저를 다시 추적합니다.")

# --- [6] 핵심 이벤트: 음성 채널 상태 업데이트 ---
@bot.event
async def on_voice_state_update(member, before, after):
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    now_plain = datetime.now(KST).replace(tzinfo=None)

    # 입장
    if before.channel is None and after.channel is not None:
        bot.active_sessions[member.id] = (now_plain, after.channel.id)
        if log_channel: await log_channel.send(f"📥 **{member.display_name}** 입장 -> `{after.channel.name}`")

    # 퇴장
    elif before.channel is not None and after.channel is None:
        if member.id in bot.active_sessions:
            join_time, channel_id = bot.active_sessions.pop(member.id)
            elapsed = int((now_plain - join_time).total_seconds())
            weight = 0.5 if channel_id == HALF_TIME_CHANNEL_ID else 1.0
            final_duration = int(elapsed * weight)

            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute('INSERT OR IGNORE INTO user_stats (user_id, total_seconds) VALUES (?, 0)', (member.id,))
            cur.execute('UPDATE user_stats SET total_seconds = total_seconds + ? WHERE user_id = ?', (final_duration, member.id))
            conn.commit()
            conn.close()
            if log_channel:
                m, s = divmod(final_duration, 60)
                await log_channel.send(f"📤 **{member.display_name}** 퇴장 | ⏱️ {m}분 {s}초 기록 완료")

# --- [7] 스케줄러: 월간 강제 정산 및 공지 ---
async def monthly_force_save(bot_instance):
    now_kst = datetime.now(KST)
    current_month = now_kst.strftime("%Y-%m")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    active_count = 0
    for user_id, (join_time, channel_id) in list(bot_instance.active_sessions.items()):
        now_plain = now_kst.replace(tzinfo=None)
        elapsed = int((now_plain - join_time).total_seconds())
        weight = 0.5 if channel_id == HALF_TIME_CHANNEL_ID else 1.0
        final_duration = int(elapsed * weight)
        cur.execute('UPDATE user_stats SET total_seconds = total_seconds + ? WHERE user_id = ?', (final_duration, user_id))
        bot_instance.active_sessions[user_id] = (now_plain, channel_id) # 세션 갱신
        active_count += 1

    cur.execute('INSERT OR REPLACE INTO settings VALUES ("last_reset_month", ?)', (current_month,))
    conn.commit()
    conn.close()

    log_channel = bot_instance.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(title=f"📅 {now_kst.strftime('%Y년 %m월')} 정산 리포트", color=0x5865F2)
        embed.description = f"✅ 월간 자동 정산 완료! 접속 중인 {active_count}명의 기록이 안전하게 누적되었습니다."
        await log_channel.send(embed=embed)

# --- [8] 일반 명령어: 순위표, 내기록, 도움말 ---
@bot.tree.command(name="순위표", description="전체 접속 시간 랭킹 상위 10명을 확인합니다.")
async def leaderboard_s(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT user_id, total_seconds FROM user_stats ORDER BY total_seconds DESC LIMIT 10')
    rows = cur.fetchall()
    conn.close()

    embed = discord.Embed(title="🏆 명예의 전당 (Top 10)", color=0xf1c40f)
    medal_icons = ["🥇", "🥈", "🥉", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅"]
    rank_list = ""
    for i, (uid, total_sec) in enumerate(rows):
        total, online, _ = calculate_realtime(uid, total_sec, bot)
        user = interaction.guild.get_member(uid)
        name = user.display_name if user else f"유저({uid})"
        h, m = divmod(total // 60, 60)
        rank_list += f"{medal_icons[i]} **{i+1}위** | {'🟢' if online else '⚪'} `{name}` (**{h}h {m % 60}m**)\n"
    embed.description = rank_list
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="내기록", description="나의 상세 접속 기록과 현재 순위를 확인합니다.")
async def my_record_s(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT user_id, total_seconds FROM user_stats ORDER BY total_seconds DESC')
    rows = cur.fetchall()
    conn.close()

    user_sec, rank = 0, 0
    for i, (uid, sec) in enumerate(rows):
        if uid == interaction.user.id:
            user_sec, rank = sec, i + 1; break

    total, online, weight = calculate_realtime(interaction.user.id, user_sec, bot)
    h, m = divmod(total // 60, 60)
    embed = discord.Embed(title=f"👤 {interaction.user.display_name}님의 기록", color=0x2ecc71)
    embed.add_field(name="📊 순위", value=f"**{rank if rank > 0 else '-'}**위", inline=True)
    embed.add_field(name="⏱️ 시간", value=f"**{h}시간 {m % 60}분**", inline=True)
    embed.add_field(name="📡 상태", value=f"{'🟢 접속 중 ('+str(weight)+'x)' if online else '⚪ 오프라인'}", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="도움말", description="명령어 사용법을 안내합니다.")
async def help_s(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 봇 사용 설명서", color=0x3498db)
    embed.description = "모든 명령어는 `/` 슬래시를 사용하거나 `!` 접두사로 이용 가능합니다."
    embed.add_field(name="🚀 명령어", value="`/순위표`, `/내기록`, `/접속확인`", inline=False)
    if interaction.user.guild_permissions.administrator:
        embed.add_field(name="🛠️ 관리자", value="`/시간수정 @유저 초`, `/db백업`", inline=False)
    await interaction.response.send_message(embed=embed)

# --- [9] 관리자 명령어: 시간수정, DB백업 ---
@bot.tree.command(name="시간수정", description="[관리자] 유저의 누적 시간을 수정합니다.")
@app_commands.default_permissions(administrator=True)
async def adjust_time_s(interaction: discord.Interaction, member: discord.Member, seconds: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('INSERT OR IGNORE INTO user_stats (user_id, total_seconds) VALUES (?, 0)', (member.id,))
    cur.execute('UPDATE user_stats SET total_seconds = total_seconds + ? WHERE user_id = ?', (seconds, member.id))
    cur.execute('SELECT total_seconds FROM user_stats WHERE user_id = ?', (member.id,))
    new_t = cur.fetchone()[0]
    conn.commit(); conn.close()
    await interaction.response.send_message(f"🛠️ {member.display_name} 수정 완료 (최종: {new_t//3600}h {(new_t%3600)//60}m)")

@bot.tree.command(name="db백업", description="[관리자] 현재 데이터베이스 파일을 즉시 전송받습니다.")
@app_commands.default_permissions(administrator=True)
async def db_backup_s(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    backup_file = f"backup_{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}.db"
    shutil.copy2(DB_PATH, backup_file)
    await interaction.followup.send(content="📦 DB 백업 파일입니다.", file=discord.File(backup_file))
    os.remove(backup_file)

bot.run(os.getenv('BOT_TOKEN'))
