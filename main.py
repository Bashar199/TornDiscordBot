import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os

load_dotenv()
token = os.getenv("DISCORD_TOKEN")
handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot= commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

@bot.event
async def on_member_join(member):
    await member.send(f"Welcome to the server {member.mention}!")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    
    if "shrek" in message.content.lower():
        await message.delete()
        await message.channel.send(f"Shrek is a good boy {message.author.mention}!")

    await bot.process_commands(message)

# !hello
@bot.command()
async def hello(ctx):
    await ctx.send(f"Hello {ctx.author.mention}!")

@bot.command()
async def assign(ctx):
    role = discord.utils.get(ctx.guild.roles, name="test")
    if role:
        await ctx.author.add_roles(role)
        await ctx.send(f"You have been assigned the {role.name} role {ctx.author.mention}!")
    else:
        await ctx.send("Role not found")

@bot.command()
async def remove(ctx):
    role = discord.utils.get(ctx.guild.roles, name="test")
    if role:
        await ctx.author.remove_roles(role)
        await ctx.send(f"You have been removed from the {role.name} role {ctx.author.mention}!")
    else:
        await ctx.send("Role not found")

@bot.command()
@commands.has_role("test")
async def test(ctx):
    await ctx.send(f"Hello {ctx.author.mention}!")

@test.error
async def test_error(ctx, error):
    if isinstance(error, commands.MissingRole):
        await ctx.send(f"You do not have permission to use this command {ctx.author.mention}!")


bot.run(token, log_handler=handler, log_level=logging.DEBUG)