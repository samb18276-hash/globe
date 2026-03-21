import os
import io
import re
import time
import asyncio
import tempfile
import discord
import requests
from discord.ext import commands, tasks
from groq import Groq
from dotenv import load_dotenv

# Voice is only available when running locally (too heavy for cloud)
VOICE_ENABLED = os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "my_voice.wav"))

if VOICE_ENABLED:
    import speech_recognition as sr

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

client_ai = Groq(api_key=GROQ_API_KEY)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

conversation_histories = {}
MAX_HISTORY = 20
SILENCE_THRESHOLD = 1.5  # seconds of silence before processing

def fetch_github_content(url):
    # File URL: github.com/user/repo/blob/branch/path
    file_match = re.match(r'https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)', url)
    if file_match:
        user, repo, branch, path = file_match.groups()
        raw_url = f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}"
        try:
            resp = requests.get(raw_url, timeout=10)
            if resp.status_code == 200:
                return f"Contents of `{path}` from GitHub:\n```\n{resp.text[:6000]}\n```"
        except Exception:
            pass

    # Repo URL: github.com/user/repo
    repo_match = re.match(r'https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$', url)
    if repo_match:
        user, repo = repo_match.groups()
        try:
            resp = requests.get(f"https://api.github.com/repos/{user}/{repo}/git/trees/HEAD?recursive=1", timeout=10)
            if resp.status_code == 200:
                files = [i['path'] for i in resp.json().get('tree', []) if i['type'] == 'blob']
                return f"Files in `{user}/{repo}` on GitHub:\n" + "\n".join(f"- {f}" for f in files[:60])
        except Exception:
            pass

    return None


SYSTEM_PROMPT = """You are sb4, a sharp and helpful assistant focused on helping the user make money and build cool things. You specialize in:
- Side hustles and passive income ideas
- Investing (stocks, crypto, real estate, index funds)
- Starting and growing online businesses
- Freelancing and monetizing skills
- Budgeting, saving, and building wealth
- Spotting trends and opportunities early
- Helping young investors make the right choices
- Coding in any programming language (Python, JavaScript, HTML/CSS, Java, C++, C#, Rust, Go, TypeScript, Bash, SQL, and more)
- Debugging code, explaining how code works, and writing code from scratch
- Helping with Discord bots, websites, games, automation scripts, and any other software projects

You speak casually and directly — no fluff, no filler. Keep responses concise unless the user asks for detail. You're like a smart friend who's good with money, business, and coding.

When coding, follow these rules:
- Write clean, simple code — no over-engineering, no unnecessary complexity
- Only add what's actually needed for the task, nothing extra
- Use code blocks with the language specified (e.g. ```python)
- After the code, give a short plain-English explanation of what it does and how to use it
- If something could go wrong or needs setup (like installing a library), mention it briefly
- Don't add excessive comments — only comment where the logic isn't obvious
- Prefer editing existing code over rewriting everything from scratch
- If the user shows you broken code, find the actual bug and fix it — don't rewrite the whole thing
- Lead with the solution, not a long explanation of what you're about to do"""

# Voice state per guild (local only)
guild_state = {}

if VOICE_ENABLED:
    tts_model = None

    def get_tts():
        global tts_model
        if tts_model is None:
            from TTS.api import TTS
            tts_model = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
        return tts_model

    class TrackingSink(discord.sinks.WaveSink):
        def __init__(self):
            super().__init__()
            self.last_spoke = {}

        def write(self, data):
            if data.user:
                self.last_spoke[data.user.id] = time.monotonic()
            super().write(data)

    async def transcribe(wav_bytes: bytes) -> str:
        recognizer = sr.Recognizer()
        audio_file = io.BytesIO(wav_bytes)
        with sr.AudioFile(audio_file) as source:
            audio = recognizer.record(source)
        try:
            return recognizer.recognize_google(audio)
        except (sr.UnknownValueError, sr.RequestError):
            return ""

    async def synthesize(text: str) -> str:
        ref_wav = os.path.join(os.path.dirname(os.path.abspath(__file__)), "my_voice.wav")
        out_path = tempfile.mktemp(suffix=".wav")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: get_tts().tts_to_file(
            text=text,
            speaker_wav=ref_wav,
            language="en",
            file_path=out_path
        ))
        return out_path

    async def handle_voice_turn(guild_id: int, user_id: int, wav_bytes: bytes):
        state = guild_state[guild_id]
        text = await transcribe(wav_bytes)
        if not text:
            state["processing"] = False
            await start_recording(guild_id)
            return
        if user_id not in conversation_histories:
            conversation_histories[user_id] = []
        conversation_histories[user_id].append({"role": "user", "content": text})
        if len(conversation_histories[user_id]) > MAX_HISTORY:
            conversation_histories[user_id] = conversation_histories[user_id][-MAX_HISTORY:]
        response = client_ai.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=1024,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + conversation_histories[user_id]
        )
        reply = response.choices[0].message.content
        conversation_histories[user_id].append({"role": "assistant", "content": reply})
        tts_path = await synthesize(reply)
        def after(error):
            try:
                os.unlink(tts_path)
            except Exception:
                pass
            state["processing"] = False
            asyncio.run_coroutine_threadsafe(start_recording(guild_id), bot.loop)
        vc = state["vc"]
        if vc.is_connected():
            vc.play(discord.FFmpegPCMAudio(tts_path), after=after)

    async def recording_callback(sink: TrackingSink, guild_id: int):
        if guild_id not in guild_state:
            return
        state = guild_state[guild_id]
        best_user, best_size = None, 0
        for uid, audio_data in sink.audio_data.items():
            audio_data.file.seek(0, 2)
            size = audio_data.file.tell()
            if size > best_size:
                best_size = size
                best_user = uid
        if best_user and best_size > 10000:
            sink.audio_data[best_user].file.seek(0)
            wav_bytes = sink.audio_data[best_user].file.read()
            asyncio.create_task(handle_voice_turn(guild_id, best_user, wav_bytes))
        else:
            state["processing"] = False
            await start_recording(guild_id)

    async def start_recording(guild_id: int):
        if guild_id not in guild_state:
            return
        state = guild_state[guild_id]
        vc = state["vc"]
        if vc.is_connected() and not vc.is_recording() and not state["processing"]:
            sink = TrackingSink()
            state["sink"] = sink
            vc.start_recording(sink, recording_callback, guild_id)

    @tasks.loop(seconds=0.5)
    async def silence_detector():
        now = time.monotonic()
        for guild_id, state in list(guild_state.items()):
            if state["processing"] or not state.get("sink") or not state["vc"].is_connected():
                continue
            if not state["vc"].is_recording():
                continue
            spoke = state["sink"].last_spoke
            if not spoke:
                continue
            if now - max(spoke.values()) >= SILENCE_THRESHOLD:
                state["processing"] = True
                state["vc"].stop_recording()


@bot.event
async def on_ready():
    if VOICE_ENABLED:
        silence_detector.start()
    print(f"sb4 is online as {bot.user} | Voice: {'on' if VOICE_ENABLED else 'off (cloud mode)'}")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user in message.mentions

    if not is_dm and not is_mentioned:
        await bot.process_commands(message)
        return

    content = message.content
    if is_mentioned:
        content = content.replace(f"<@{bot.user.id}>", "").strip()

    if not content:
        await message.reply("What's up? Ask me anything — money, business, investing, or whatever.")
        return

    # Fetch any GitHub URLs found in the message
    github_urls = re.findall(r'https://github\.com/\S+', content)
    github_context = []
    for url in github_urls:
        fetched = fetch_github_content(url)
        if fetched:
            github_context.append(fetched)
    if github_context:
        content = content + "\n\n" + "\n\n".join(github_context)

    user_id = message.author.id

    if user_id not in conversation_histories:
        conversation_histories[user_id] = []

    conversation_histories[user_id].append({"role": "user", "content": content})
    if len(conversation_histories[user_id]) > MAX_HISTORY:
        conversation_histories[user_id] = conversation_histories[user_id][-MAX_HISTORY:]

    async with message.channel.typing():
        try:
            response = client_ai.chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=1024,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}] + conversation_histories[user_id]
            )
            reply = response.choices[0].message.content
            conversation_histories[user_id].append({"role": "assistant", "content": reply})

            if len(reply) > 2000:
                for i in range(0, len(reply), 2000):
                    await message.reply(reply[i:i+2000])
            else:
                await message.reply(reply)

        except Exception as e:
            await message.reply(f"Something went wrong: {e}")

    await bot.process_commands(message)


@bot.command(name="join")
async def join(ctx):
    if not VOICE_ENABLED:
        await ctx.reply("Voice is only available when running locally.")
        return
    if not ctx.author.voice:
        await ctx.reply("You're not in a voice channel.")
        return
    channel = ctx.author.voice.channel
    vc = await channel.connect()
    guild_state[ctx.guild.id] = {"vc": vc, "sink": None, "processing": False}
    await start_recording(ctx.guild.id)
    await ctx.reply(f"Joined {channel.name}. Talk to me!")


@bot.command(name="leave")
async def leave(ctx):
    guild_id = ctx.guild.id
    if guild_id in guild_state:
        state = guild_state[guild_id]
        if state["vc"].is_recording():
            state["vc"].stop_recording()
        await state["vc"].disconnect()
        del guild_state[guild_id]
    await ctx.reply("Left.")


@bot.command(name="reset")
async def reset(ctx):
    conversation_histories.pop(ctx.author.id, None)
    await ctx.reply("Conversation reset. Fresh start.")


bot.run(DISCORD_TOKEN)
