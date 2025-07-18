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
from typing import Optional, Dict, Set, Tuple
from discord.ui import Button, View

# Load environment variables and setup logging
load_dotenv()
token = os.getenv("DISCORD_TOKEN")
if not token:
    raise ValueError("DISCORD_TOKEN not found in .env file")

handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")
# Update intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guild_messages = True
intents.guilds = True

class ChainBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        # Store active chains and their timers
        self.active_chains = {}

bot = ChainBot()

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

# Slash Commands
@bot.tree.command(name="hello", description="Say hello!")
@app_commands.guild_only()
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(f"Hello {interaction.user.mention}!")

@bot.tree.command(name="setnick", description="Set your nickname in the format: name [ID]")
@app_commands.describe(
    name="Your name (e.g., batfrog)",
    user_id="Your ID number (e.g., 3636117)"
)
@app_commands.guild_only()
async def setnick(interaction: discord.Interaction, name: str, user_id: str):
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server!", ephemeral=True)
        return

    # Get the member object
    member = interaction.guild.get_member(interaction.user.id)
    if not member:
        await interaction.response.send_message("Could not find your member info", ephemeral=True)
        return

    # Check if bot has permission to manage nicknames
    bot_member = interaction.guild.get_member(bot.user.id)
    if not bot_member or not bot_member.guild_permissions.manage_nicknames:
        await interaction.response.send_message(
            "❌ I need the 'Manage Nicknames' permission to do this! Please ask a server admin to grant me this permission.",
            ephemeral=True
        )
        return

    try:
        # Format the nickname
        new_nickname = f"{name} [{user_id}]"
        
        # Check if nickname is too long (Discord limit is 32 characters)
        if len(new_nickname) > 32:
            await interaction.response.send_message(
                "❌ Nickname is too long! Please use a shorter name or ID.",
                ephemeral=True
            )
            return
        
        # Change the nickname
        await member.edit(nick=new_nickname)
        await interaction.response.send_message(
            f"✅ Your nickname has been set to: {new_nickname}",
            ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I don't have permission to change nicknames! The server role hierarchy might be preventing me.",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            "An error occurred while changing your nickname.",
            ephemeral=True
        )
        logging.error(f"Nickname change error: {e}")

@bot.tree.command(name="dm", description="Send yourself a DM with your message")
@app_commands.describe(message="The message to send to yourself")
async def dm(interaction: discord.Interaction, message: str):
    try:
        await interaction.user.send(f"You said: {message}")
        await interaction.response.send_message("Message sent to your DMs!", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I couldn't send you a DM. Please check if you have DMs enabled.",
            ephemeral=True
        )

@bot.tree.command(name="poll", description="Create a poll with yes/no voting")
@app_commands.describe(question="The question to ask in the poll")
@app_commands.guild_only()
async def poll(interaction: discord.Interaction, question: str):
    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "Polls can only be created in text channels or threads!",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="📊 Poll",
        description=question,
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"Poll started by {interaction.user.name} • React with ✅ or ❌ to vote")
    
    await interaction.response.send_message(embed=embed)
    poll_message = await interaction.original_response()
    await poll_message.add_reaction("✅")
    await poll_message.add_reaction("❌")
    
    try:
        await asyncio.sleep(60)
        poll_message = await interaction.channel.fetch_message(poll_message.id)
        
        yes_votes = next((reaction.count - 1 for reaction in poll_message.reactions if str(reaction.emoji) == "✅"), 0)
        no_votes = next((reaction.count - 1 for reaction in poll_message.reactions if str(reaction.emoji) == "❌"), 0)
        total_votes = yes_votes + no_votes
        
        yes_percentage = (yes_votes / total_votes * 100) if total_votes > 0 else 0
        no_percentage = (no_votes / total_votes * 100) if total_votes > 0 else 0
        
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
        await interaction.followup.send("An error occurred while collecting poll results.")
        logging.error(f"Poll error: {e}")

def parse_time(time_str: str) -> Optional[int]:
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

def format_time_remaining(seconds: int) -> str:
    """Format seconds into hours, minutes, seconds"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    else:
        return f"{minutes}m {seconds}s"

class ChainButton(Button):
    def __init__(self, style: discord.ButtonStyle, label: str, is_join: bool):
        super().__init__(style=style, label=label)
        self.is_join = is_join
        
    async def callback(self, interaction: discord.Interaction):
        assert self.view is not None
        view: ChainView = self.view
        
        if self.is_join:
            view.joiners.add((interaction.user.id, interaction.user.display_name))
            view.cant_make_it.discard((interaction.user.id, interaction.user.display_name))
            await interaction.response.send_message("You've joined the chain!", ephemeral=True)
        else:
            view.cant_make_it.add((interaction.user.id, interaction.user.display_name))
            view.joiners.discard((interaction.user.id, interaction.user.display_name))
            await interaction.response.send_message("You've indicated you can't make it.", ephemeral=True)

class ChainView(View):
    def __init__(self, chain_data: Dict):
        super().__init__(timeout=None)
        self.joiners: Set[Tuple[int, str]] = set()
        self.cant_make_it: Set[Tuple[int, str]] = set()
        self.chain_data = chain_data
        
        # Add the buttons
        self.join_button = ChainButton(
            style=discord.ButtonStyle.success,
            label="I'll join!",
            is_join=True
        )
        self.skip_button = ChainButton(
            style=discord.ButtonStyle.danger,
            label="Can't make it",
            is_join=False
        )
        
        self.add_item(self.join_button)
        self.add_item(self.skip_button)
    
    def disable_all_buttons(self):
        """Disable all buttons in the view"""
        self.join_button.disabled = True
        self.skip_button.disabled = True

@bot.tree.command(name="chain", description="Organize a chain with a countdown timer")
@app_commands.describe(time_str="Time until chain starts (e.g., '5h' for 5 hours, '30m' for 30 minutes)")
@app_commands.guild_only()
async def chain(interaction: discord.Interaction, time_str: str):
    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "Chains can only be created in text channels or threads!",
            ephemeral=True
        )
        return
    
    if interaction.channel.id in bot.active_chains:
        await interaction.response.send_message(
            "⚠️ There's already an active chain planned in this channel!",
            ephemeral=True
        )
        return
    
    seconds = parse_time(time_str)
    if seconds is None:
        await interaction.response.send_message(
            "❌ Invalid time format! Use something like '5h' for 5 hours or '30m' for 30 minutes.",
            ephemeral=True
        )
        return
    
    end_time = datetime.now() + timedelta(seconds=seconds)
    
    embed = discord.Embed(
        title="🔄 Upcoming Chain",
        description="A new chain is being organized! Click the buttons below to indicate your participation:",
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
        value="🟢 = I'll join the chain!\n🔴 = Can't make it",
        inline=False
    )
    
    embed.set_footer(text=f"Chain organized by {interaction.user.name}")
    
    chain_data = {
        'end_time': end_time,
        'organizer': interaction.user.name
    }
    
    view = ChainView(chain_data)
    await interaction.response.send_message(embed=embed, view=view)
    chain_message = await interaction.original_response()
    
    bot.active_chains[interaction.channel.id] = {
        'message_id': chain_message.id,
        'end_time': end_time,
        'organizer': interaction.user.name,
        'view': view
    }
    
    try:
        while datetime.now() < end_time:
            await asyncio.sleep(5)
            
            remaining = (end_time - datetime.now()).total_seconds()
            if remaining < 0:
                remaining = 0
            
            embed = discord.Embed(
                title="🔄 Upcoming Chain",
                description="A new chain is being organized! Click the buttons below to indicate your participation:",
                color=discord.Color.gold()
            )
            
            embed.add_field(
                name="Chain Start Time",
                value=f"Chain starts in: {format_time_remaining(int(remaining))}",
                inline=False
            )
            
            joiners_text = "\n".join([f"• {name}" for _, name in view.joiners]) if view.joiners else "*No participants yet*"
            embed.add_field(
                name=f"Participants ({len(view.joiners)})",
                value=joiners_text,
                inline=False
            )
            
            cant_make_it_text = "\n".join([f"• {name}" for _, name in view.cant_make_it]) if view.cant_make_it else "*None*"
            embed.add_field(
                name=f"Can't Make It ({len(view.cant_make_it)})",
                value=cant_make_it_text,
                inline=False
            )
            
            embed.add_field(
                name="Options",
                value="🟢 = I'll join the chain!\n🔴 = Can't make it",
                inline=False
            )
            
            embed.set_footer(text=f"Chain organized by {interaction.user.name}")
            
            await chain_message.edit(embed=embed, view=view)
            
            if remaining <= 0:
                break
        
        final_embed = discord.Embed(
            title="🎯 Chain Starting!",
            description="Time's up! The chain is starting now!",
            color=discord.Color.green()
        )
        
        joiners_text = "\n".join([f"• {name}" for _, name in view.joiners]) if view.joiners else "*No participants*"
        final_embed.add_field(
            name=f"Final Participants ({len(view.joiners)})",
            value=joiners_text,
            inline=False
        )
        
        if view.joiners:
            mentions = [f"<@{user_id}>" for user_id, _ in view.joiners]
            mentions_text = " ".join(mentions)
            await interaction.followup.send(f"🔔 Chain is starting! {mentions_text}")
        
        # Disable the buttons after chain ends
        view.disable_all_buttons()
        await chain_message.edit(embed=final_embed, view=view)
        
    except Exception as e:
        await interaction.followup.send("An error occurred while managing the chain.")
        logging.error(f"Chain error: {e}")
    finally:
        if interaction.channel.id in bot.active_chains:
            del bot.active_chains[interaction.channel.id]

bot.run(token, log_handler=handler, log_level=logging.DEBUG)