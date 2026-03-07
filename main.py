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

# --- [2] DB 초기화 (월간 테이블 포함 및 타임아웃 설정) ---
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    # DB 잠금 방지를 위해 timeout=10 추가
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cur = conn.cursor()
    # 전체 누적 테이블
    cur.execute('''CREATE TABLE IF NOT EXISTS user_stats 
                   (user_id INTEGER PRIMARY KEY, total_seconds INTEGER DEFAULT 0)''')
    # 월별 누적 테이블
    cur.execute('''CREATE TABLE IF NOT EXISTS monthly_stats 
                   (user_id INTEGER, month TEXT, seconds INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, month))''')
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
    s = seconds % 60
    return f"{h}시간 {m}분 {s}초"

def save_to_db(user_id, duration):
    current_month = datetime.now(KST).strftime("%Y-%m")
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cur = conn.cursor()
    
    # 1. 전체 누적 업데이트
    cur.execute('INSERT OR IGNORE INTO user_stats (user_id, total_seconds) VALUES (?, 0)', (user_id,))
    cur.execute('UPDATE user_stats SET total_seconds = total_seconds + ? WHERE user_id = ?', (duration, user_id))
    
    # 2. 이번 달 누적 업데이트
    cur.execute('INSERT OR IGNORE INTO monthly_stats (user_id, month, seconds) VALUES (?, ?, 0)', (user_id, current_month))
    cur.execute('UPDATE monthly_stats SET seconds = seconds + ? WHERE user_id = ? AND month = ?', (duration, user_id, current_month))
    
    conn.commit()
    conn.close()

# --- [5] 핵심 이벤트: 입퇴장 및 이동 기록 (상세 엠베드) ---
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

    def get_base_embed(title, color):
        embed = discord.Embed(title=title, color=color, timestamp=datetime.now(KST))
        avatar_url = member.display_avatar.url if member.display_avatar else None
        embed.set_author(name=f"{member.display_name} ({member.name})", icon_url=avatar_url)
        if avatar_url: embed.set_thumbnail(url=avatar_url)
        return embed

    # 1. 입장
    if before.channel is None and after.channel is not None:
        bot.active_sessions[member.id] = (now_plain, after.channel.id)
        if log_channel:
            embed = get_base_embed("📥 음성 채널 입장", 0x2ecc71)
            embed.add_field(name="채널", value=f"`{after.channel.name}`", inline=True)
            weight = 0.5 if after.channel.id == HALF_TIME_CHANNEL_ID else 1.0
            embed.add_field(name="적용 가중치", value=f"**{weight}x**", inline=True)
            await log_channel.send(embed=embed)

    # 2. 퇴장
    elif before.channel is not None and after.channel is None:
        if member.id in bot.active_sessions:
            join_time, channel_id = bot.active_sessions.pop(member.id)
            elapsed = int((now_plain - join_time).total_seconds())
            weight = 0.5 if channel_id == HALF_TIME_CHANNEL_ID else 1.0
            final_duration = int(elapsed * weight)
            save_to_db(member.id, final_duration)

            if log_channel:
                embed = get_base_embed("📤 음성 채널 퇴장", 0xe74c3c)
                m, s = divmod(final_duration, 60)
                embed.add_field(name="활동 시간", value=f"**{m}분 {s}초**", inline=True)
                embed.add_field(name="최종 가중치", value=f"{weight}x", inline=True)
                await log_channel.send(embed=embed)

    # 3. 채널 이동
    elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
        if member.id in bot.active_sessions:
            join_time, old_ch_id = bot.active_sessions.pop(member.id)
            elapsed = int((now_plain - join_time).total_seconds())
            weight = 0.5 if old_ch_id == HALF_TIME_CHANNEL_ID else 1.0
            final_duration = int(elapsed * weight)
            save_to_db(member.id, final_duration)
            
            bot.active_sessions[member.id] = (now_plain, after.channel.id)
            new_weight = 0.5 if after.channel.id == HALF_TIME_CHANNEL_ID else 1.0

            if log_channel:
                embed = get_base_embed("🔄 채널 이동 및 기록", 0x3498db)
                m, s = divmod(final_duration, 60)
                embed.add_field(name="이전 채널", value=f"`{before.channel.name}` ({weight}x)", inline=False)
                embed.add_field(name="현재 채널", value=f"`{after.channel.name}` ({new_weight}x)", inline=False)
                embed.add_field(name="이전 기록 저장", value=f"**{m}분 {s}초**", inline=True)
                await log_channel.send(embed=embed)

# --- [6] 스케줄러: 월간 정산 ---
async def monthly_force_save(bot_instance):
    now_kst = datetime.now(KST)
    now_plain = now_kst.replace(tzinfo=None)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cur = conn.cursor()
    active_count = 0
    for user_id, (join_time, channel_id) in list(bot_instance.active_sessions.items()):
        elapsed = int((now_plain - join_time).total_seconds())
        weight = 0.5 if channel_id == HALF_TIME_CHANNEL_ID else 1.0
        cur.execute('UPDATE user_stats SET total_seconds = total_seconds + ? WHERE user_id = ?', (int(elapsed * weight), user_id))
        bot_instance.active_sessions[user_id] = (now_plain, channel_id)
        active_count += 1
    cur.execute('INSERT OR REPLACE INTO settings VALUES ("last_reset_month", ?)', (now_kst.strftime("%Y-%m"),))
    conn.commit()
    conn.close()
    log_channel = bot_instance.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        await log_channel.send(embed=discord.Embed(title=f"📅 {now_kst.month}월 정산 완료", description=f"접속 중인 {active_count}명의 기록이 누적치에 반영되었습니다.", color=0x5865F2))

# --- [7] 일반 명령어 (Slash) ---
@bot.tree.command(name="순위표", description="접속 시간 랭킹을 확인합니다.")
@app_commands.choices(유형=[
    app_commands.Choice(name="전체 누적", value="total"),
    app_commands.Choice(name="이번 달 (월간)", value="monthly")
])
async def leaderboard_s(interaction: discord.Interaction, 유형: str = "total"):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cur = conn.cursor()
    current_month = datetime.now(KST).strftime("%Y-%m")
    
    if 유형 == "total":
        title, query = "🏆 전체 누적 랭킹", 'SELECT user_id, total_seconds FROM user_stats ORDER BY total_seconds DESC LIMIT 10'
        cur.execute(query)
    else:
        title = f"📅 {datetime.now(KST).month}월 이달의 랭킹"
        cur.execute('SELECT user_id, seconds FROM monthly_stats WHERE month = ? ORDER BY seconds DESC LIMIT 10', (current_month,))
    
    rows = cur.fetchall()
    conn.close()

    embed = discord.Embed(title=title, color=0xf1c40f if 유형 == "total" else 0x3498db)
    medal_icons = ["🥇", "🥈", "🥉", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅"]
    rank_list = ""
    for i, (uid, sec) in enumerate(rows):
        display_sec, online, _, _ = calculate_realtime(uid, sec, bot)
        user = interaction.guild.get_member(uid)
        name = user.display_name if user else f"유저({uid})"
        rank_list += f"{medal_icons[i]} **{i+1}위** | {'🟢' if online else '⚪'} **{name}**\n┗ ⏱️ `{format_time(display_sec)}` 누적\n\n"
    
    embed.description = rank_list or "아직 데이터가 없습니다."
    embed.set_footer(text=f"기준 시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')} KST")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="내기록", description="나의 순위와 시간을 확인합니다.")
async def my_record_s(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_PATH, timeout=10)
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

@bot.tree.command(name="접속확인", description="특정 유저의 상세 접속 정보를 확인합니다.")
@app_commands.describe(member="확인할 유저를 선택하세요.")
async def check_user_s(interaction: discord.Interaction, member: discord.Member):
    conn = sqlite3.connect(DB_PATH, timeout=10)
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
    
    avatar_url = member.display_avatar.url if member.display_avatar else None
    if avatar_url: embed.set_thumbnail(url=avatar_url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="도움말", description="봇의 모든 명령어 사용법을 안내합니다.")
async def help_s(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 기록 봇 사용 설명서", color=0x3498db, timestamp=datetime.now(KST))
    user_cmds = "🏆 `/순위표 [유형]`\n┗ 전체 또는 월간 TOP 10 확인\n👤 `/내기록`\n┗ 나의 순위와 시간 확인\n📡 `/접속확인 @유저`\n┗ 유저의 실시간 상태 확인"
    embed.add_field(name="🚀 일반 명령어", value=user_cmds, inline=False)
    if interaction.user.guild_permissions.administrator:
        admin_cmds = "💾 `/시간저장`\n┗ 세션 강제 DB 동기화\n🛠️ `/시간수정 @유저 초`\n┗ 누적 시간 수동 조정\n📦 `/db백업`\n┗ DB 파일 즉시 전송"
        embed.add_field(name="🛠️ 관리자 전용", value=admin_cmds, inline=False)
    await interaction.response.send_message(embed=embed)

# --- [8] 관리자 명령어 (Slash) ---
@bot.tree.command(name="시간저장", description="[관리자] 실시간 세션을 DB에 즉시 저장합니다.")
@app_commands.default_permissions(administrator=True)
async def force_save_s(interaction: discord.Interaction):
    now_plain = datetime.now(KST).replace(tzinfo=None)
    count = 0
    for user_id, (join_time, channel_id) in list(bot.active_sessions.items()):
        elapsed = int((now_plain - join_time).total_seconds())
        weight = 0.5 if channel_id == HALF_TIME_CHANNEL_ID else 1.0
        save_to_db(user_id, int(elapsed * weight))
        bot.active_sessions[user_id] = (now_plain, channel_id)
        count += 1
    await interaction.response.send_message(f"💾 {count}명의 세션을 DB에 동기화했습니다.", ephemeral=True)

@bot.tree.command(name="시간수정", description="[관리자] 시간 수정")
@app_commands.default_permissions(administrator=True)
async def adjust_time_s(interaction: discord.Interaction, member: discord.Member, seconds: int):
    save_to_db(member.id, seconds)
    conn = sqlite3.connect(DB_PATH, timeout=10)
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
