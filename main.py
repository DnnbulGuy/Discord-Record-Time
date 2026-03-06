import discord
from discord.ext import commands
import sqlite3
import os
from datetime import datetime

# --- 설정 ---
DB_PATH = '/app/data/discord_bot.db' 
HALF_TIME_CHANNEL_ID = int(os.getenv('HALF_TIME_CHANNEL_ID'))
LOG_CHANNEL_ID = int(os.getenv('TARGET_CHANNEL_ID'))

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS voice_logs 
                   (user_id INTEGER, join_time TEXT, leave_time TEXT, duration INTEGER)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS user_stats 
                   (user_id INTEGER PRIMARY KEY, total_seconds INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()

init_db()

intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# 유저의 세션을 추적하는 딕셔너리 {user_id: (join_time, channel_id)}
active_sessions = {}

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')

@bot.event
async def on_voice_state_update(member, before, after):
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    
    # 1. 입장 (채널에 새로 들어옴)
    if before.channel is None and after.channel is not None:
        active_sessions[member.id] = (datetime.now(), after.channel.id)
        print(f"[기록 시작] {member.display_name} -> {after.channel.name}")
        if log_channel:
            await log_channel.send(f"📥 **{member.display_name}**님이 `{after.channel.name}` 채널에 입장하셨습니다.")

    # 2. 퇴장 (채널에서 완전히 나감)
    elif before.channel is not None and after.channel is None:
        if member.id in active_sessions:
            join_time, channel_id = active_sessions.pop(member.id)
            raw_duration = int((datetime.now() - join_time).total_seconds())
            
            # 가중치 판별 및 계산
            is_half_time = (channel_id == HALF_TIME_CHANNEL_ID)
            weight = 0.5 if is_half_time else 1.0
            final_duration = int(raw_duration * weight)
            
            # DB 저장
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute('INSERT INTO voice_logs VALUES (?, ?, ?, ?)', 
                        (member.id, join_time.isoformat(), datetime.now().isoformat(), final_duration))
            cur.execute('''INSERT INTO user_stats (user_id, total_seconds) VALUES (?, ?)
                           ON CONFLICT(user_id) DO UPDATE SET total_seconds = total_seconds + ?''', 
                        (member.id, final_duration, final_duration))
            conn.commit()
            conn.close()

            # 로그 메시지 조립
            weight_notice = "(⚠️ 50% 가중치 적용됨)" if is_half_time else "(100% 정상 기록)"
            print(f"[기록 완료] {member.display_name}: {final_duration}초 {weight_notice}")
            
            if log_channel:
                m, s = divmod(final_duration, 60)
                await log_channel.send(
                    f"📤 **{member.display_name}**님이 `{before.channel.name}`에서 퇴장하셨습니다.\n"
                    f"⏱️ **최종 기록 시간:** {m}분 {s}초 {weight_notice}"
                )

    # 3. 채널 이동 (이전 채널과 다음 채널이 모두 있는 경우)
    elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
        # 기존 기록 정산
        if member.id in active_sessions:
            join_time, old_channel_id = active_sessions.pop(member.id)
            raw_duration = int((datetime.now() - join_time).total_seconds())
            
            weight = 0.5 if old_channel_id == HALF_TIME_CHANNEL_ID else 1.0
            final_duration = int(raw_duration * weight)

            # DB 저장 로직 (중복 방지를 위해 실제로는 함수화하는 게 좋음)
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute('INSERT INTO voice_logs VALUES (?, ?, ?, ?)', (member.id, join_time.isoformat(), datetime.now().isoformat(), final_duration))
            cur.execute('INSERT INTO user_stats (user_id, total_seconds) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET total_seconds = total_seconds + ?', (member.id, final_duration, final_duration))
            conn.commit()
            conn.close()

        # 새로운 채널 세션 시작
        active_sessions[member.id] = (datetime.now(), after.channel.id)
        if log_channel:
            await log_channel.send(f"🔄 **{member.display_name}**님이 `{before.channel.name}` ➡️ `{after.channel.name}`(으)로 이동하셨습니다.")

@bot.command(name="전체현황")
@commands.has_permissions(administrator=True)
async def total_stats(ctx):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT user_id, total_seconds FROM user_stats ORDER BY total_seconds DESC LIMIT 10')
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await ctx.send("📊 데이터가 없습니다.")
        return

    table = "순위 | 유저명 | 시간\n--- | --- | ---\n"
    for i, (uid, sec) in enumerate(rows, 1):
        user = ctx.guild.get_member(uid)
        if user is None:
            try:
                user = await ctx.guild.fetch_member(uid)
            except:
                user = None

        name = user.display_name if user else f"Unknown({uid})"
        h, m = divmod(sec // 60, 60)
        s = sec % 60
        table += f"{i}위 | {name} | {h}h {m}m {s}s\n"

    embed = discord.Embed(
        title="📂 관리자 대시보드", 
        description=f"```\n{table}```",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

bot.run(os.getenv('BOT_TOKEN'))
