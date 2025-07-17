import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import asyncio
import datetime
import re
from datetime import datetime, timedelta

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

@bot.command()
async def dm(ctx,*, message):
    await ctx.author.send(f"you said {message}")

@bot.command()
async def reply(ctx):
    await ctx.reply("this is a reply")

@bot.command()
async def poll(ctx, *, question):
    # Create an embed for the poll
    embed = discord.Embed(
        title="📊 Poll",
        description=question,
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"Poll started by {ctx.author.name} • React with ✅ or ❌ to vote")
    
    # Send the embed and add reactions
    poll_message = await ctx.send(embed=embed)
    await poll_message.add_reaction("✅")
    await poll_message.add_reaction("❌")
    
    try:
        # Wait for 60 seconds and then fetch the message again to get final reactions
        await asyncio.sleep(60)
        poll_message = await ctx.channel.fetch_message(poll_message.id)
        
        # Count reactions (subtract 1 from each to exclude bot's reactions)
        yes_votes = next((reaction.count - 1 for reaction in poll_message.reactions if str(reaction.emoji) == "✅"), 0)
        no_votes = next((reaction.count - 1 for reaction in poll_message.reactions if str(reaction.emoji) == "❌"), 0)
        total_votes = yes_votes + no_votes
        
        # Calculate percentages
        next_percentage = (next_votes / total_votes * 100) if total_votes > 0 else 0
        stop_percentage = (stop_votes / total_votes * 100) if total_votes > 0 else 0
        
        # Create results embed
        results_embed = discord.Embed(
            title="⛓️ Next Chain Results",
            description=question,
            color=discord.Color.gold()
        )
        results_embed.add_field(
            name="Results",
            value=f"⏭️ Next: {next_votes} votes ({next_percentage:.1f}%)\n⏹️ Stop: {stop_votes} votes ({stop_percentage:.1f}%)",
            inline=False
        )
        results_embed.set_footer(text=f"Total votes: {total_votes}")
        
        await ctx.send(embed=results_embed)
        
    except Exception as e:
        await ctx.send("An error occurred while collecting next chain results. Please try again.")
        print(f"Next chain error: {e}")

# Store active chains and their timers
active_chains = {}

def parse_time(time_str):
    """Convert time string like '5h' or '30m' to seconds"""
    match = re.match(r'(\d+)([hm])', time_str.lower())
    if not match:
        return None
    
    amount, unit = match.groups()
    amount = int(amount)
    
    if unit == 'h':
        return amount * 3600  # hours to seconds
    elif unit == 'm':
        return amount * 60    # minutes to seconds

def format_time_remaining(seconds):
    """Format seconds into hours, minutes, seconds"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    else:
        return f"{minutes}m {seconds}s"

@bot.command(name='chain')
async def chain(ctx, time_str: str):
    global active_chains
    
    # Check if there's already an active chain in this channel
    if ctx.channel.id in active_chains:
        await ctx.send("⚠️ There's already an active chain planned in this channel!")
        return
    
    # Parse the time string
    seconds = parse_time(time_str)
    if seconds is None:
        await ctx.send("❌ Invalid time format! Use something like '5h' for 5 hours or '30m' for 30 minutes.")
        return
    
    # Calculate end time
    end_time = datetime.now() + timedelta(seconds=seconds)
    
    # Create initial embed
    embed = discord.Embed(
        title="⛓️ Upcoming Chain",
        description="A new chain is being organized! React with the emojis below to indicate your participation:",
        color=discord.Color.gold()
    )
    
    embed.add_field(
        name="Chain Start Time",
        value=f"Chain starts in: {format_time_remaining(seconds)}",
        inline=False
    )
    
    embed.add_field(
        name="Participants",
        value="*No participants yet*",
        inline=False
    )
    
    embed.add_field(
        name="Options",
        value="⏭️ = I'll join the chain!\n⏹️ = I can't make it",
        inline=False
    )
    
    embed.set_footer(text=f"Chain organized by {ctx.author.name} • React to join!")
    
    # Send the embed and add reactions
    chain_message = await ctx.send(embed=embed)
    await chain_message.add_reaction("⏭️")
    await chain_message.add_reaction("⏹️")
    
    # Store the chain info
    active_chains[ctx.channel.id] = {
        'message_id': chain_message.id,
        'end_time': end_time,
        'organizer': ctx.author.name
    }
    
    try:
        while datetime.now() < end_time:
            # Wait for 30 seconds between updates
            await asyncio.sleep(30)
            
            # Fetch updated message to get current reactions
            chain_message = await ctx.channel.fetch_message(chain_message.id)
            
            # Get participants
            joiners = []
            cant_make_it = []
            for reaction in chain_message.reactions:
                async for user in reaction.users():
                    if not user.bot:  # Ignore bot reactions
                        if str(reaction.emoji) == "⏭️":
                            joiners.append(user.name)
                        elif str(reaction.emoji) == "⏹️":
                            cant_make_it.append(user.name)
            
            # Calculate remaining time
            remaining = (end_time - datetime.now()).total_seconds()
            if remaining < 0:
                remaining = 0
            
            # Update embed
            embed = discord.Embed(
                title="⛓️ Upcoming Chain",
                description="A new chain is being organized! React with the emojis below to indicate your participation:",
                color=discord.Color.gold()
            )
            
            embed.add_field(
                name="Chain Start Time",
                value=f"Chain starts in: {format_time_remaining(int(remaining))}",
                inline=False
            )
            
            # Update participants field
            joiners_text = "\n".join([f"• {name}" for name in joiners]) if joiners else "*No participants yet*"
            embed.add_field(
                name=f"Participants ({len(joiners)})",
                value=joiners_text,
                inline=False
            )
            
            cant_make_it_text = "\n".join([f"• {name}" for name in cant_make_it]) if cant_make_it else "*None*"
            embed.add_field(
                name=f"Can't Make It ({len(cant_make_it)})",
                value=cant_make_it_text,
                inline=False
            )
            
            embed.add_field(
                name="Options",
                value="⏭️ = I'll join the chain!\n⏹️ = I can't make it",
                inline=False
            )
            
            embed.set_footer(text=f"Chain organized by {ctx.author.name} • React to join!")
            
            await chain_message.edit(embed=embed)
            
            if remaining <= 0:
                break
        
        # Final update when time is up
        final_embed = discord.Embed(
            title="⛓️ Chain Starting!",
            description="Time's up! The chain is starting now!",
            color=discord.Color.green()
        )
        
        final_embed.add_field(
            name=f"Final Participants ({len(joiners)})",
            value=joiners_text,
            inline=False
        )
        
        if joiners:
            mentions = " ".join([f"<@{user.id}>" async for user in chain_message.reactions[0].users() if not user.bot])
            await ctx.send(f"🔔 Chain is starting! {mentions}")
        
        await ctx.send(embed=final_embed)
        
    except Exception as e:
        await ctx.send("An error occurred while managing the chain. Please try again.")
        print(f"Chain error: {e}")
    finally:
        # Clean up active chain data
        if ctx.channel.id in active_chains:
            del active_chains[ctx.channel.id]

bot.run(token, log_handler=handler, log_level=logging.DEBUG)