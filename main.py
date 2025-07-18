import discord
from discord.ext import commands
from discord import app_commands
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
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

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

# /hello
@bot.tree.command(name="hello", description="Say hello!")
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(f"Hello {interaction.user.mention}!")

@bot.tree.command(name="assign", description="Assign yourself the test role")
async def assign(interaction: discord.Interaction):
    role = discord.utils.get(interaction.guild.roles, name="test")
    if role:
        await interaction.user.add_roles(role)
        await interaction.response.send_message(f"You have been assigned the {role.name} role {interaction.user.mention}!")
    else:
        await interaction.response.send_message("Role not found")

@bot.tree.command(name="remove", description="Remove yourself from the test role")
async def remove(interaction: discord.Interaction):
    role = discord.utils.get(interaction.guild.roles, name="test")
    if role:
        await interaction.user.remove_roles(role)
        await interaction.response.send_message(f"You have been removed from the {role.name} role {interaction.user.mention}!")
    else:
        await interaction.response.send_message("Role not found")

@bot.tree.command(name="test", description="Test command for users with test role")
async def test(interaction: discord.Interaction):
    role = discord.utils.get(interaction.guild.roles, name="test")
    if role in interaction.user.roles:
        await interaction.response.send_message(f"Hello {interaction.user.mention}!")
    else:
        await interaction.response.send_message(f"You do not have permission to use this command {interaction.user.mention}!", ephemeral=True)

@bot.tree.command(name="dm", description="Send yourself a DM with your message")
@app_commands.describe(message="The message to send to yourself")
async def dm(interaction: discord.Interaction, message: str):
    await interaction.user.send(f"you said {message}")
    await interaction.response.send_message("Message sent to your DMs!", ephemeral=True)

@bot.tree.command(name="reply", description="Bot sends a reply message")
async def reply(interaction: discord.Interaction):
    await interaction.response.send_message("this is a reply")

@bot.tree.command(name="poll", description="Create a poll with yes/no voting")
@app_commands.describe(question="The question to ask in the poll")
async def poll(interaction: discord.Interaction, question: str):
    # Create an embed for the poll
    embed = discord.Embed(
        title="📊 Poll",
        description=question,
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"Poll started by {interaction.user.name} • React with ✅ or ❌ to vote")
    
    # Send the embed and add reactions
    await interaction.response.send_message(embed=embed)
    poll_message = await interaction.original_response()
    await poll_message.add_reaction("✅")
    await poll_message.add_reaction("❌")
    
    try:
        # Wait for 60 seconds and then fetch the message again to get final reactions
        await asyncio.sleep(60)
        poll_message = await interaction.channel.fetch_message(poll_message.id)
        
        # Count reactions (subtract 1 from each to exclude bot's reactions)
        yes_votes = next((reaction.count - 1 for reaction in poll_message.reactions if str(reaction.emoji) == "✅"), 0)
        no_votes = next((reaction.count - 1 for reaction in poll_message.reactions if str(reaction.emoji) == "❌"), 0)
        total_votes = yes_votes + no_votes
        
        # Calculate percentages
        yes_percentage = (yes_votes / total_votes * 100) if total_votes > 0 else 0
        no_percentage = (no_votes / total_votes * 100) if total_votes > 0 else 0
        
        # Create results embed
        results_embed = discord.Embed(
            title="📊 Poll Results",
            description=question,
            color=discord.Color.gold()
        )
        results_embed.add_field(
            name="Results",
            value=f"✅ Yes: {yes_votes} votes ({yes_percentage:.1f}%)\n❌ No: {no_votes} votes ({no_percentage:.1f}%)",
            inline=False
        )
        results_embed.set_footer(text=f"Total votes: {total_votes}")
        
        await interaction.followup.send(embed=results_embed)
        
    except Exception as e:
        await interaction.followup.send("An error occurred while collecting poll results. Please try again.")
        print(f"Poll error: {e}")

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

@bot.tree.command(name="chain", description="Organize a chain with a countdown timer")
@app_commands.describe(time_str="Time until chain starts (e.g., '5h' for 5 hours, '30m' for 30 minutes)")
async def chain(interaction: discord.Interaction, time_str: str):
    global active_chains
    
    # Check if there's already an active chain in this channel
    if interaction.channel.id in active_chains:
        await interaction.response.send_message("⚠️ There's already an active chain planned in this channel!")
        return
    
    # Parse the time string
    seconds = parse_time(time_str)
    if seconds is None:
        await interaction.response.send_message("❌ Invalid time format! Use something like '5h' for 5 hours or '30m' for 30 minutes.")
        return
    
    # Calculate end time
    end_time = datetime.now() + timedelta(seconds=seconds)
    
    # Create initial embed
    embed = discord.Embed(
        title="🔄 Upcoming Chain",
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
        value="✅ = I'll join the chain!\n❌ = I can't make it",
        inline=False
    )
    
    embed.set_footer(text=f"Chain organized by {interaction.user.name} • React to join!")
    
    # Send the embed and add reactions
    await interaction.response.send_message(embed=embed)
    chain_message = await interaction.original_response()
    await chain_message.add_reaction("✅")
    await chain_message.add_reaction("❌")
    
    # Store the chain info
    active_chains[interaction.channel.id] = {
        'message_id': chain_message.id,
        'end_time': end_time,
        'organizer': interaction.user.name
    }
    
    try:
        while datetime.now() < end_time:
            # Wait for 5 seconds between updates
            await asyncio.sleep(5)
            
            # Fetch updated message to get current reactions
            chain_message = await interaction.channel.fetch_message(chain_message.id)
            
            # Get participants (ensure each user only appears in one list)
            joiners = []
            cant_make_it = []
            user_reactions = {}  # Track what each user has reacted with
            
            # First, collect all user reactions
            for reaction in chain_message.reactions:
                async for user in reaction.users():
                    if not user.bot:  # Ignore bot reactions
                        if user.display_name not in user_reactions:
                            user_reactions[user.display_name] = []
                        user_reactions[user.display_name].append(str(reaction.emoji))
            
            # Now assign users to lists with priority (✅ takes precedence over ❌)
            for user_name, emojis in user_reactions.items():
                if "✅" in emojis:
                    joiners.append(user_name)
                elif "❌" in emojis:
                    cant_make_it.append(user_name)
            
            # Calculate remaining time
            remaining = (end_time - datetime.now()).total_seconds()
            if remaining < 0:
                remaining = 0
            
            # Update embed
            embed = discord.Embed(
                title="🔄 Upcoming Chain",
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
                value="✅ = I'll join the chain!\n❌ = I can't make it",
                inline=False
            )
            
            embed.set_footer(text=f"Chain organized by {interaction.user.name} • React to join!")
            
            await chain_message.edit(embed=embed)
            
            if remaining <= 0:
                break
        
        # Final update when time is up
        final_embed = discord.Embed(
            title="🎯 Chain Starting!",
            description="Time's up! The chain is starting now!",
            color=discord.Color.green()
        )
        
        final_embed.add_field(
            name=f"Final Participants ({len(joiners)})",
            value=joiners_text,
            inline=False
        )
        
        if joiners:
            # Get mentions for users who reacted with ✅
            mentions = []
            for reaction in chain_message.reactions:
                if str(reaction.emoji) == "✅":
                    async for user in reaction.users():
                        if not user.bot:
                            mentions.append(f"<@{user.id}>")
                    break
            mentions_text = " ".join(mentions)
            await interaction.followup.send(f"🔔 Chain is starting! {mentions_text}")
        
        await interaction.followup.send(embed=final_embed)
        
    except Exception as e:
        await interaction.followup.send("An error occurred while managing the chain. Please try again.")
        print(f"Chain error: {e}")
    finally:
        # Clean up active chain data
        if interaction.channel.id in active_chains:
            del active_chains[interaction.channel.id]

bot.run(token, log_handler=handler, log_level=logging.DEBUG)