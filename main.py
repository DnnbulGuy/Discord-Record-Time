import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
import os
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# --- 설정 및 상수 ---
DB_PATH = '/app/data/discord_bot.db' 
HALF_TIME_CHANNEL_ID = int(os.getenv('HALF_TIME_CHANNEL_ID'))
LOG_CHANNEL_ID = int(os.getenv('TARGET_CHANNEL_ID'))
KST = timezone(timedelta(hours=9)) # 한국 표준시 설정

# --- DB 초기화 ---
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

# --- 봇 클래스 ---
class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        self.active_sessions = {} # {user_id: (join_time, channel_id)}

    async def setup_hook(self):
        await self.tree.sync()
        self.scheduler = AsyncIOScheduler(timezone=KST)
        self.scheduler.add_job(
            monthly_force_save, 
            CronTrigger(day=1, hour=0, minute=0, second=0),
            args=[self]
        )
        self.scheduler.start()
        print("✅ 슬래시 명령어 동기화 및 스케줄러 활성화!")

bot = MyBot()

# --- 월간 강제 정산 함수 ---
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
        bot_instance.active_sessions[user_id] = (now_plain, channel_id)
        active_count += 1

    cur.execute('INSERT OR REPLACE INTO settings VALUES ("last_reset_month", ?)', (current_month,))
    conn.commit()
    conn.close()

    log_channel = bot_instance.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(title=f"📅 {now_kst.strftime('%Y년 %m월')} 정산 완료", color=0x5865F2)
        embed.description = f"✅ 월간 자동 정산 완료 (접속 중 {active_count}명 정산)"
        await log_channel.send(embed=embed)

# --- 실시간 합산 순위표 로직 (수정됨) ---
async def get_leaderboard_embed(guild, bot_instance):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT user_id, total_seconds FROM user_stats ORDER BY total_seconds DESC LIMIT 10')
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return discord.Embed(description="📊 아직 데이터가 없습니다.", color=discord.Color.red())

    embed = discord.Embed(title="🏆 명예의 전당 (순위표)", color=0xf1c40f)
    medal_icons = ["🥇", "🥈", "🥉", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅"]
    rank_list = ""
    
    for i, (uid, total_sec) in enumerate(rows):
        current_session = 0
        is_online = False
        if uid in bot_instance.active_sessions:
            join_time, channel_id = bot_instance.active_sessions[uid]
            now_plain = datetime.now(KST).replace(tzinfo=None)
            elapsed = (now_plain - join_time).total_seconds()
            weight = 0.5 if channel_id == HALF_TIME_CHANNEL_ID else 1.0
            current_session = int(elapsed * weight)
            is_online = True
        
        display_total = total_sec + current_session
        user = guild.get_member(uid)
        name = user.display_name if user else f"유저({uid})"
        online_mark = "🟢" if is_online else "⚪"
        h, m = divmod(display_total // 60, 60)
        rank_list += f"{medal_icons[i]} **{i+1}위** | {online_mark} `{name}`\n┗ ⏱️ **{h}h {m % 60}m**\n\n"

    embed.add_field(name="━━━━━━━━━━━━━━━━━━", value=rank_list, inline=False)
    return embed

# --- 이벤트: 음성 업데이트 (KST 반영) ---
@bot.event
async def on_voice_state_update(member, before, after):
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    now_plain = datetime.now(KST).replace(tzinfo=None)

    # 1. 입장
    if before.channel is None and after.channel is not None:
        bot.active_sessions[member.id] = (now_plain, after.channel.id)
        if log_channel:
            await log_channel.send(f"📥 **{member.display_name}**님이 `{after.channel.name}`에 입장하셨습니다.")

    # 2. 퇴장
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
                await log_channel.send(f"📤 **{member.display_name}** 퇴장 | ⏱️ {m}분 {s}초 기록")

# --- 관리자 기능 공통 함수 ---
async def update_user_time(target_user, seconds: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('INSERT OR IGNORE INTO user_stats (user_id, total_seconds) VALUES (?, 0)', (target_user.id,))
    cur.execute('UPDATE user_stats SET total_seconds = total_seconds + ? WHERE user_id = ?', (seconds, target_user.id))
    cur.execute('SELECT total_seconds FROM user_stats WHERE user_id = ?', (target_user.id,))
    new_total = cur.fetchone()[0]
    conn.commit()
    conn.close()
    
    embed = discord.Embed(title="🛠️ 관리자 시간 수정 완료", color=0xe67e22)
    embed.add_field(name="대상", value=target_user.display_name)
    embed.add_field(name="변동", value=f"{seconds}초")
    embed.add_field(name="최종 누적", value=f"{new_total//3600}h {(new_total%3600)//60}m")
    return embed

# --- 명령어 등록 ---
@bot.command(name="순위표")
async def leaderboard_p(ctx): await ctx.send(embed=await get_leaderboard_embed(ctx.guild, bot))

@bot.tree.command(name="순위표", description="전체 접속 시간 순위를 확인합니다.")
async def leaderboard_s(interaction: discord.Interaction):
    await interaction.response.send_message(embed=await get_leaderboard_embed(interaction.guild, bot))

@bot.command(name="시간수정")
@commands.has_permissions(administrator=True)
async def adjust_time_p(ctx, member: discord.Member, seconds: int):
    await ctx.send(embed=await update_user_time(member, seconds))

@bot.tree.command(name="시간수정", description="관리자가 시간을 강제로 수정합니다.")
@app_commands.default_permissions(administrator=True)
async def adjust_time_s(interaction: discord.Interaction, member: discord.Member, seconds: int):
    await interaction.response.send_message(embed=await update_user_time(member, seconds))

bot.run(os.getenv('BOT_TOKEN'))
