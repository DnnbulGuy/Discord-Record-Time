import discord
from discord.ext import commands
import sqlite3
import os
from datetime import datetime

# --- 설정 ---
DB_PATH = '/app/data/discord_bot.db' 
# 50%만 인정할 휴게실 채널 ID (나머지 채널은 자동으로 100% 계산)
HALF_TIME_CHANNEL_ID = 987654321098765432 

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

active_sessions = {} # {user_id: (join_time, channel_id)}

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')

# 상단에 알림을 보낼 채널 ID 설정 (이미 있다면 pass)
LOG_CHANNEL_ID = 123456789012345678 # 알림 메시지가 올라올 텍스트 채널 ID

@bot.event
async def on_voice_state_update(member, before, after):
    # 알림을 보낼 텍스트 채널 객체 가져오기
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    
    # 1. 입장 (before.channel이 없고 after.channel이 있는 경우)
    if before.channel is None and after.channel is not None:
        start_time[member.id] = time.time()
        print(f"[기록 시작] {member.display_name} -> {after.channel.name}") # 터미널 로그
        
        if log_channel:
            await log_channel.send(f"📥 **{member.display_name}**님이 `{after.channel.name}` 채널에 입장하셨습니다.")

    # 2. 퇴장 (before.channel이 있고 after.channel이 없는 경우)
    elif before.channel is not None and after.channel is None:
        if member.id in start_time:
            end_time = time.time()
            duration = end_time - start_time[member.id]
            del start_time[member.id]

            # 가중치 계산 (기존 로직 유지)
            weight = 0.5 if before.channel.id == HALF_TIME_CHANNEL_ID else 1.0
            final_duration = duration * weight

            # DB 저장
            save_to_db(member.id, final_duration)
            
            print(f"[기록 완료] {member.display_name}: {int(final_duration)}초 (가중치 {weight}x)")
            
            if log_channel:
                m, s = divmod(int(final_duration), 60)
                await log_channel.send(
                    f"📤 **{member.display_name}**님이 `{before.channel.name}`에서 퇴장하셨습니다. "
                    f"(기록 시간: {m}분 {s}초, 가중치: {weight}x)"
                )

    # 3. 채널 이동 (before.channel과 after.channel이 모두 있고 서로 다른 경우)
    elif before.channel is not None and after.channel is not None and before.channel != after.channel:
        # 이동 시에는 기존 채널 퇴장 처리 후 새 채널 입장 처리를 한 번에 수행하거나, 
        # 간단하게 이동 알림만 띄울 수 있습니다.
        if log_channel:
            await log_channel.send(f"🔄 **{member.display_name}**님이 `{before.channel.name}` ➡️ `{after.channel.name}`(으)로 이동하셨습니다.")

@bot.event
async def on_voice_state_update(member, before, after):
    # 1. 입장 또는 채널 이동 (어느 채널이든)
    if after.channel is not None:
        if member.id not in active_sessions or active_sessions[member.id][1] != after.channel.id:
            active_sessions[member.id] = (datetime.now(), after.channel.id)

    # 2. 퇴장 또는 채널 이동 시 기존 기록 마감
    if before.channel is not None:
        if after.channel is None or after.channel.id != before.channel.id:
            if member.id in active_sessions:
                join_time, channel_id = active_sessions.pop(member.id)
                raw_duration = int((datetime.now() - join_time).total_seconds())
                
                # 가중치 적용: 특정 ID만 0.5, 나머지는 1.0
                weight = 0.5 if channel_id == HALF_TIME_CHANNEL_ID else 1.0
                final_duration = int(raw_duration * weight)

                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute('INSERT INTO voice_logs VALUES (?, ?, ?, ?)', 
                            (member.id, join_time.isoformat(), datetime.now().isoformat(), final_duration))
                cur.execute('''INSERT INTO user_stats (user_id, total_seconds) VALUES (?, ?)
                               ON CONFLICT(user_id) DO UPDATE SET total_seconds = total_seconds + ?''', 
                            (member.id, final_duration, final_duration))
                conn.commit()
                conn.close()

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
        # 1. 먼저 캐시에서 유저를 찾음
        user = ctx.guild.get_member(uid)
        
        # 2. 캐시에 없으면 API로 직접 서버에서 정보를 땡겨옴 (비동기 처리 필수)
        if user is None:
            try:
                user = await ctx.guild.fetch_member(uid)
            except:
                user = None

        name = user.display_name if user else f"Unknown({uid})"
        
        # 3. 시간 계산 (초 단위까지 보고 싶다면 s 추가)
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
