import os
import io
import re
import sys
import time
import json
import logging
import asyncio
import tempfile
import discord
import requests
from pymongo import MongoClient

# Fix voice WebSocket SSL issues on Windows (ProactorEventLoop breaks it)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from discord.ext import commands, tasks
from groq import Groq
from dotenv import load_dotenv

logging.basicConfig(level=logging.DEBUG)
logging.getLogger('discord.voice_client').setLevel(logging.DEBUG)
logging.getLogger('discord.gateway').setLevel(logging.DEBUG)

# Patch voice WebSocket: use v8 + max_dave_protocol_version (Discord's 2024 requirement)
import threading as _threading
import discord.gateway as _dgw

@classmethod
async def _patched_voice_from_client(cls, client, *, resume=False, hook=None):
    gateway = f"wss://{client.endpoint}/?v=8"
    http = client._state.http
    socket = await http.ws_connect(gateway)
    ws = cls(socket, loop=client.loop, hook=hook)
    ws.gateway = gateway
    ws._connection = client
    ws._max_heartbeat_timeout = 60.0
    ws.thread_id = _threading.get_ident()
    if resume:
        await ws.resume()
    else:
        await ws.identify()
    return ws

async def _patched_identify(self):
    state = self._connection
    payload = {
        "op": self.IDENTIFY,
        "d": {
            "server_id": str(state.server_id),
            "user_id": str(state.user.id),
            "session_id": state.session_id,
            "token": state.token,
            "max_dave_protocol_version": 0,
        },
    }
    await self.send_as_json(payload)

_dgw.DiscordVoiceWebSocket.from_client = _patched_voice_from_client
_dgw.DiscordVoiceWebSocket.identify = _patched_identify

# Voice is only available when running locally (too heavy for cloud)
VOICE_ENABLED = os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "my_voice.wav"))

if VOICE_ENABLED:
    import speech_recognition as sr
    if not discord.opus.is_loaded():
        discord.opus._load_default()

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

client_ai = Groq(api_key=GROQ_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

MAX_HISTORY = 40

MONGO_URI = os.getenv("MONGO_URI")
if MONGO_URI:
    _mongo = MongoClient(MONGO_URI)
    _col = _mongo["sb4"]["memory"]
else:
    _col = None

def load_memory():
    if _col is not None:
        try:
            doc = _col.find_one({"_id": "histories"})
            if doc:
                return {int(k): v for k, v in doc["data"].items()}
        except Exception:
            pass
    return {}

def save_memory(histories):
    if _col is not None:
        try:
            _col.update_one(
                {"_id": "histories"},
                {"$set": {"data": {str(k): v for k, v in histories.items()}}},
                upsert=True
            )
        except Exception:
            pass

conversation_histories = load_memory()
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
- Also can chat about other stuff to and solve other problems

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

        def write(self, data, user):
            print(f"[audio] from {user}")
            self.last_spoke[user] = time.monotonic()
            super().write(data, user)

    async def transcribe(wav_bytes: bytes) -> str:
        def _do_transcribe():
            recognizer = sr.Recognizer()
            audio_file = io.BytesIO(wav_bytes)
            with sr.AudioFile(audio_file) as source:
                audio = recognizer.record(source)
            try:
                return recognizer.recognize_google(audio)
            except (sr.UnknownValueError, sr.RequestError):
                return ""
        return await asyncio.get_event_loop().run_in_executor(None, _do_transcribe)

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
        if guild_id not in guild_state:
            return
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
        loop = asyncio.get_event_loop()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_histories[user_id]
        response = await loop.run_in_executor(None, lambda: client_ai.chat.completions.create(
            model="llama-3.1-8b-instant",
            max_tokens=1024,
            messages=messages
        ))
        reply = response.choices[0].message.content
        conversation_histories[user_id].append({"role": "assistant", "content": reply})
        save_memory(conversation_histories)
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
        if best_user is not None and best_size > 10000:
            sink.audio_data[best_user].file.seek(0)
            wav_bytes = sink.audio_data[best_user].file.read()
            user_id = best_user.id if hasattr(best_user, 'id') else best_user
            asyncio.create_task(handle_voice_turn(guild_id, user_id, wav_bytes))
        else:
            state["processing"] = False
            state["recording"] = False
            await start_recording(guild_id)

    async def start_recording(guild_id: int):
        if guild_id not in guild_state:
            return
        state = guild_state[guild_id]
        vc = state["vc"]
        if vc.is_connected() and not state["recording"] and not state["processing"]:
            sink = TrackingSink()
            state["sink"] = sink
            state["recording"] = True
            try:
                vc.start_recording(sink, recording_callback, guild_id)
                print("[voice] start_recording called successfully")
            except Exception as e:
                print(f"[voice] start_recording ERROR: {e}")
                state["recording"] = False

    @tasks.loop(seconds=0.5)
    async def silence_detector():
        now = time.monotonic()
        for guild_id, state in list(guild_state.items()):
            if state["processing"] or not state.get("sink") or not state["vc"].is_connected():
                continue
            if not state["recording"]:
                continue
            spoke = state["sink"].last_spoke
            if not spoke:
                continue
            if now - max(spoke.values()) >= SILENCE_THRESHOLD:
                print("[silence] detected, stopping recording")
                state["processing"] = True
                state["recording"] = False
                state["vc"].stop_recording()


@bot.event
async def on_ready():
    if VOICE_ENABLED and not silence_detector.is_running():
        silence_detector.start()
    print(f"sb4 is online as {bot.user} | Voice: {'on' if VOICE_ENABLED else 'off (cloud mode)'}")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Allow other bots to talk to sb4 only if they mention it, with depth limit
    if message.author.bot:
        if bot.user not in message.mentions:
            return
        # Check depth tag to prevent infinite loops
        depth_match = re.search(r'\[d:(\d+)\]', message.content)
        depth = int(depth_match.group(1)) if depth_match else 0
        if depth >= 3:
            return

    # Handle commands first
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    content = message.content
    if bot.user in message.mentions:
        content = content.replace(f"<@{bot.user.id}>", "").strip()
    # Strip depth tag from content
    content = re.sub(r'\[d:\d+\]', '', content).strip()

    if not content:
        await message.reply("You didn't send any content. Please try again.")
        return

    # Fetch any GitHub URLs found in the message
    github_urls = re.findall(r'https://github\.com/\S+', content)
    github_context = []
    for url in github_urls:
        fetched = await asyncio.get_event_loop().run_in_executor(None, fetch_github_content, url)
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
            msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_histories[user_id]
            response = await asyncio.get_event_loop().run_in_executor(None, lambda: client_ai.chat.completions.create(
                model="llama-3.1-8b-instant",
                max_tokens=1024,
                messages=msgs
            ))
            reply = response.choices[0].message.content
            conversation_histories[user_id].append({"role": "assistant", "content": reply})
            save_memory(conversation_histories)

            # Add depth tag when replying to a bot
            if message.author.bot:
                depth_match = re.search(r'\[d:(\d+)\]', message.content)
                depth = int(depth_match.group(1)) if depth_match else 0
                reply = f"[d:{depth+1}] {reply}"

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
    member = ctx.guild.get_member(ctx.author.id)
    if not member or not member.voice:
        await ctx.reply("You're not in a voice channel.")
        return
    channel = member.voice.channel
    existing = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if existing:
        await existing.disconnect(force=True)
    try:
        vc = await channel.connect(timeout=30.0)
    except Exception as e:
        await ctx.reply(f"Couldn't connect to voice: {e}")
        return
    sink = TrackingSink()
    guild_state[ctx.guild.id] = {"vc": vc, "sink": sink, "processing": False, "recording": False}
    try:
        vc.start_recording(sink, recording_callback, ctx.guild.id)
        guild_state[ctx.guild.id]["recording"] = True
    except Exception as e:
        await ctx.reply(f"Recording failed: {e}")
        return
    await ctx.reply(f"Joined {channel.name}. Talk to me!")


@bot.command(name="leave")
async def leave(ctx):
    guild_id = ctx.guild.id
    if guild_id in guild_state:
        state = guild_state[guild_id]
        try:
            state["vc"].stop_recording()
        except Exception:
            pass
        await state["vc"].disconnect()
        del guild_state[guild_id]
    await ctx.reply("Left.")


@bot.command(name="reset")
async def reset(ctx):
    conversation_histories.pop(ctx.author.id, None)
    save_memory(conversation_histories)
    await ctx.reply("Conversation reset. Fresh start.")


bot.run(DISCORD_TOKEN)
