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

@bot.command(name="접속확인")
async def check_my_time(ctx):
    user_id = ctx.author.id
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # 1. DB에서 저장된 누적 시간 가져오기
    cur.execute('SELECT total_seconds FROM user_stats WHERE user_id = ?', (user_id,))
    row = cur.fetchone()
    conn.close()

    saved_seconds = row[0] if row else 0
    current_session_seconds = 0
    is_active = False

    # 2. 명령어를 입력한 '지금 이 순간'의 시간을 가져와서 실시간 계산
    if user_id in active_sessions:
        join_time, channel_id = active_sessions[user_id] # 봇이 기억하는 입장 시간
        now = datetime.now()
        
        # 지금(명령어 입력 시점) - 들어온 시간 = 실시간 접속 초
        elapsed = int((now - join_time).total_seconds())
        
        # 현재 채널 가중치 적용
        weight = 0.5 if channel_id == HALF_TIME_CHANNEL_ID else 1.0
        current_session_seconds = elapsed * weight
        is_active = True

    # 3. 최종 합산
    total_sec = saved_seconds + current_session_seconds
    
    # 시간 포맷팅 (h/m/s)
    h, m = divmod(int(total_sec) // 60, 60)
    s = int(total_sec) % 60
    
    color = discord.Color.blue() if is_active else discord.Color.grey()
    status_icon = "🟢" if is_active else "⚪"
    
    embed = discord.Embed(
        title=f"{status_icon} {ctx.author.display_name}님의 실시간 접속 정보",
        description=f"현재까지 누적된 총 시간은\n**{h}시간 {m}분 {s}초** 입니다.",
        color=color
    )
    
    if is_active:
        embed.set_footer(text=f"현재 세션 가중치: {weight}x 적용 중")
    
    await ctx.send(embed=embed)

@bot.command(name="전체현황")
async def total_stats(ctx):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT user_id, total_seconds FROM user_stats')
    db_rows = cur.fetchall()
    conn.close()

    # 1. 모든 유저의 실시간 합산 데이터 생성
    all_data = []
    for uid, saved_sec in db_rows:
        current_session = 0
        is_online = False
        
        # 실시간 세션 계산 (접속 중인 경우)
        if uid in active_sessions:
            join_time, channel_id = active_sessions[uid]
            elapsed = (datetime.now() - join_time).total_seconds()
            weight = 0.5 if channel_id == HALF_TIME_CHANNEL_ID else 1.0
            current_session = elapsed * weight
            is_online = True
            
        all_data.append({
            'uid': uid,
            'total': saved_sec + current_session,
            'is_online': is_online
        })

    # 2. 합산 시간 기준 내림차순 정렬 (상위 10명)
    all_data.sort(key=lambda x: x['total'], reverse=True)
    top_10 = all_data[:10]

    if not top_10:
        await ctx.send("📊 아직 기록된 데이터가 없습니다.")
        return

    # 3. 임베드 UI 구성
    embed = discord.Embed(
        title="🏆 전체 접속 시간 랭킹 (실시간)",
        description="채널에 머문 누적 시간 순위입니다. (실시간 합산 중)",
        color=discord.Color.gold(),
        timestamp=datetime.now()
    )

    medal_icons = ["🥇", "🥈", "🥉", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅"]
    
    rank_list = ""
    for i, data in enumerate(top_10):
        user = ctx.guild.get_member(data['uid'])
        if user is None:
            try: user = await ctx.guild.fetch_member(data['uid'])
            except: user = None
            
        name = user.display_name if user else f"Unknown({data['uid']})"
        online_mark = "🟢" if data['is_online'] else "⚪"
        
        h, m = divmod(int(data['total']) // 60, 60)
        s = int(data['total']) % 60
        
        rank_list += f"{medal_icons[i]} **{i+1}위** | {online_mark} `{name}`\n┗ ⏱️ **{h}h {m}m {s}s**\n\n"

    embed.add_field(name="━━━━━━━━━━━━━━━━━━", value=rank_list, inline=False)
    embed.set_footer(text=f"요청자: {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
    
    await ctx.send(embed=embed)

bot.run(os.getenv('BOT_TOKEN'))
