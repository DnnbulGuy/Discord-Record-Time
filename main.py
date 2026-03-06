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
        intents.members = True # 멤버 정보 조회를 위해 활성화
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
        print("✅ 슬래시 명령어 동기화 및 KST 스케줄러 활성화!")

bot = MyBot()

# --- [기능 1] 세션 복구 및 로그인 완료 알림 ---
@bot.event
async def on_ready():
    now_plain = datetime.now(KST).replace(tzinfo=None)
    recovery_count = 0
    
    # 봇 시작 시 모든 서버의 모든 음성 채널 스캔
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for member in vc.members:
                if not member.bot:
                    bot.active_sessions[member.id] = (now_plain, vc.id)
                    recovery_count += 1
                    
    print(f'Logged in as {bot.user.name}')
    print(f"🔄 세션 복구 완료: {recovery_count}명의 유저 추적 시작 (기준 시각: {now_plain})")

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
        embed = discord.Embed(title=f"📅 {now_kst.strftime('%Y년 %m월')} 정산 알림", color=0x5865F2)
        embed.description = f"✅ 월간 자동 정산 및 기준 시각 갱신 완료 (접속자 {active_count}명)"
        await log_channel.send(embed=embed)

# --- 공용: 실시간 시간 계산 로직 ---
def calculate_realtime(user_id, saved_seconds, bot_instance):
    if user_id in bot_instance.active_sessions:
        join_time, channel_id = bot_instance.active_sessions[user_id]
        now_plain = datetime.now(KST).replace(tzinfo=None)
        elapsed = (now_plain - join_time).total_seconds()
        weight = 0.5 if channel_id == HALF_TIME_CHANNEL_ID else 1.0
        return saved_seconds + int(elapsed * weight), True, weight
    return saved_seconds, False, 1.0

# --- [기능 2] 개인 상세 조회 (/내기록) ---
@bot.tree.command(name="내기록", description="나의 상세 접속 기록과 순위를 확인합니다.")
async def my_record_s(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT user_id, total_seconds FROM user_stats ORDER BY total_seconds DESC')
    rows = cur.fetchall()
    conn.close()

    user_data = None
    rank = 0
    for i, (uid, sec) in enumerate(rows):
        if uid == interaction.user.id:
            user_data = sec
            rank = i + 1
            break

    total_sec, is_online, weight = calculate_realtime(interaction.user.id, user_data or 0, bot)
    h, m = divmod(total_sec // 60, 60)
    s = total_sec % 60

    embed = discord.Embed(title=f"👤 {interaction.user.display_name}님의 기록", color=0x2ecc71)
    embed.add_field(name="📊 현재 순위", value=f"전체 **{rank if rank > 0 else '-'}**위", inline=True)
    embed.add_field(name="⏱️ 총 누적 시간", value=f"**{h}시간 {m}분 {s}초**", inline=True)
    
    status = f"🟢 접속 중 ({weight}x 가중치)" if is_online else "⚪ 오프라인"
    embed.add_field(name="📡 상태", value=status, inline=False)
    await interaction.response.send_message(embed=embed)

# --- [기능 3] 도움말 명령어 ---
@bot.tree.command(name="도움말", description="봇의 모든 명령어와 사용법을 안내합니다.")
async def help_s(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 봇 사용 설명서", color=0x3498db)
    embed.add_field(name="🚀 일반 명령어", value=(
        "`/순위표` : 상위 10명의 랭킹 확인\n"
        "`/내기록` : 나의 상세 시간 및 순위 확인\n"
        "`/접속확인` : 현재 실시간 접속 상태 확인"
    ), inline=False)
    
    if interaction.user.guild_permissions.administrator:
        embed.add_field(name="🛠️ 관리자 명령어", value=(
            "`/시간수정 @유저 초` : 유저의 시간을 강제 수정 (+/-)"
        ), inline=False)
        
    embed.set_footer(text="매달 1일 00시 KST, 현재 접속 시간이 전체 누적치에 자동 합산됩니다.")
    await interaction.response.send_message(embed=embed)

# --- 순위표 (실시간 반영) ---
@bot.tree.command(name="순위표", description="전체 접속 시간 순위를 확인합니다.")
async def leaderboard_s(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT user_id, total_seconds FROM user_stats ORDER BY total_seconds DESC LIMIT 10')
    rows = cur.fetchall()
    conn.close()

    embed = discord.Embed(title="🏆 명예의 전당 (상위 10명)", color=0xf1c40f)
    rank_list = ""
    medal_icons = ["🥇", "🥈", "🥉", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅"]
    
    for i, (uid, total_sec) in enumerate(rows):
        display_total, is_online, _ = calculate_realtime(uid, total_sec, bot)
        user = interaction.guild.get_member(uid)
        name = user.display_name if user else f"유저({uid})"
        h, m = divmod(display_total // 60, 60)
        rank_list += f"{medal_icons[i]} **{i+1}위** | {'🟢' if is_online else '⚪'} `{name}` (**{h}h {m % 60}m**)\n"

    embed.description = rank_list
    await interaction.response.send_message(embed=embed)

# --- 이벤트: 음성 업데이트 ---
@bot.event
async def on_voice_state_update(member, before, after):
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    now_plain = datetime.now(KST).replace(tzinfo=None)

    if before.channel is None and after.channel is not None:
        bot.active_sessions[member.id] = (now_plain, after.channel.id)
        if log_channel: await log_channel.send(f"📥 **{member.display_name}** 입장 -> `{after.channel.name}`")

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
            if log_channel: await log_channel.send(f"📤 **{member.display_name}** 퇴장 | ⏱️ {final_duration//60}분 기록")

# --- 관리자: 시간 수정 ---
@bot.tree.command(name="시간수정", description="관리자가 특정 유저의 누적 시간을 강제로 수정합니다.")
@app_commands.default_permissions(administrator=True)
async def adjust_time_s(interaction: discord.Interaction, member: discord.Member, seconds: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('INSERT OR IGNORE INTO user_stats (user_id, total_seconds) VALUES (?, 0)', (member.id,))
    cur.execute('UPDATE user_stats SET total_seconds = total_seconds + ? WHERE user_id = ?', (seconds, member.id))
    cur.execute('SELECT total_seconds FROM user_stats WHERE user_id = ?', (member.id,))
    new_total = cur.fetchone()[0]
    conn.commit()
    conn.close()
    await interaction.response.send_message(f"🛠️ **{member.display_name}** 시간 수정 완료: 최종 {new_total//3600}h {(new_total%3600)//60}m")

bot.run(os.getenv('BOT_TOKEN'))
