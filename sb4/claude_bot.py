import os
import json
import asyncio
import discord
from discord.ext import commands
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN_2 = os.getenv("DISCORD_TOKEN_2")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

client_ai = Groq(api_key=GROQ_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!c", intents=intents)

MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_memory.json")
MAX_HISTORY = 40

SYSTEM_PROMPT = """You are Claude Code, an AI assistant made by Anthropic. You're talking through a Discord bot so you can communicate with Sam and with sb4 (Sam's other Discord bot).

You are helpful, direct, and technically sharp. You help with coding, money/investing ideas, and general questions. You speak casually like a knowledgeable friend.

Important context:
- Sam is a young developer learning to build things
- sb4 is Sam's other bot (also in this server) — it's powered by Groq/llama and handles general chat + voice
- You are Claude Code's Discord presence — you can coordinate with sb4 or chat with Sam directly

Keep responses concise unless detail is needed."""

def load_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                return {int(k): v for k, v in json.load(f).items()}
        except Exception:
            pass
    return {}

def save_memory(histories):
    try:
        with open(MEMORY_FILE, "w") as f:
            json.dump(histories, f)
    except Exception:
        pass

conversation_histories = load_memory()


@bot.event
async def on_ready():
    print(f"claude_bot is online as {bot.user}")


@bot.event
async def on_message(message):
    # Never respond to bots (prevents loops with sb4)
    if message.author.bot:
        return

    # Handle commands first
    if message.content.startswith("!c"):
        await bot.process_commands(message)
        return

    # Only respond when mentioned or in DMs
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = bot.user in message.mentions

    if not is_dm and not is_mentioned:
        return

    content = message.content
    if is_mentioned:
        content = content.replace(f"<@{bot.user.id}>", "").strip()

    if not content:
        await message.reply("Yeah?")
        return

    user_id = message.author.id
    if user_id not in conversation_histories:
        conversation_histories[user_id] = []

    conversation_histories[user_id].append({"role": "user", "content": content})
    if len(conversation_histories[user_id]) > MAX_HISTORY:
        conversation_histories[user_id] = conversation_histories[user_id][-MAX_HISTORY:]

    async with message.channel.typing():
        try:
            msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_histories[user_id]
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: client_ai.chat.completions.create(
                model="llama-3.1-8b-instant",
                max_tokens=1024,
                messages=msgs
            ))
            reply = response.choices[0].message.content
            conversation_histories[user_id].append({"role": "assistant", "content": reply})
            save_memory(conversation_histories)

            if len(reply) > 2000:
                for i in range(0, len(reply), 2000):
                    await message.reply(reply[i:i+2000])
            else:
                await message.reply(reply)

        except Exception as e:
            await message.reply(f"Something went wrong: {e}")


@bot.command(name="reset")
async def reset(ctx):
    conversation_histories.pop(ctx.author.id, None)
    save_memory(conversation_histories)
    await ctx.reply("Conversation reset.")


bot.run(DISCORD_TOKEN_2)
