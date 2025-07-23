import discord
from discord.ext import commands
from discord import app_commands
import logging
import sys
from dotenv import load_dotenv
import os
import asyncio
import datetime
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Set, Tuple
from discord.ui import Button, View
import aiohttp
import json

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),  # Log to stdout for CapRover
        logging.FileHandler(filename="discord.log", encoding="utf-8", mode="a")  # Also keep file logging
    ]
)
logger = logging.getLogger('discord')  # Get Discord's logger
logger.setLevel(logging.INFO)  # Set Discord logger level

# --- Persistence Setup ---
CHAIN_DATA_FILE = "active_chains.json"

# Load environment variables
load_dotenv()
token = os.getenv("DISCORD_TOKEN")
torn_api_key = os.getenv("TORN_API_KEY")
if not token:
    logger.error("DISCORD_TOKEN not found in .env file")
    raise ValueError("DISCORD_TOKEN not found in .env file")
if not torn_api_key:
    logger.error("TORN_API_KEY not found in .env file")
    raise ValueError("TORN_API_KEY not found in .env file")

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
        self.persistent_views_loaded = False
        logger.info("ChainBot initialized")

    async def setup_hook(self):
        """Called when the bot is done preparing data"""
        # Create background task for checking chain tasks
        self.bg_task = self.loop.create_task(self.check_chain_tasks())
        logger.info("Setup hook completed")

    async def check_single_chain(self, channel_id, chain_data):
        try:
            task = self.chain_tasks.get(channel_id)
            if task and not task.done():
                return  # Task is running, do nothing

            channel = self.get_channel(channel_id)
            if not channel:
                logger.warning(f"check_chain_tasks: Channel {channel_id} not found. Cleaning up.")
                if channel_id in self.active_chains:
                    del self.active_chains[channel_id]
                if channel_id in self.chain_tasks:
                    del self.chain_tasks[channel_id]
                return

            end_time_utc = chain_data['end_time_utc']
            now_utc = datetime.now(timezone.utc)

            if now_utc < end_time_utc:
                # It's in countdown phase, restart lifecycle manager
                logger.info(f"Task for channel {channel_id} is not running. Restarting chain lifecycle.")
                self.chain_tasks[channel_id] = self.loop.create_task(manage_chain_lifecycle(channel_id))
            else:
                # Chain has finished, clean up
                logger.info(f"Chain {channel_id} has ended. Cleaning up.")
                if channel_id in self.active_chains:
                    del self.active_chains[channel_id]
                if channel_id in self.chain_tasks:
                    del self.chain_tasks[channel_id]

        except Exception as e:
            logger.error(f"Error checking single chain {channel_id}: {e}")

    async def check_chain_tasks(self):
        """Periodically check and restart chain tasks if needed"""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                # Create tasks for each chain check to run them concurrently
                check_tasks = [
                    self.check_single_chain(channel_id, chain_data)
                    for channel_id, chain_data in list(self.active_chains.items())
                ]
                if check_tasks:
                    await asyncio.gather(*check_tasks)

                await asyncio.sleep(60)  # Check every minute
            except Exception as e:
                logger.error(f"Error in check_chain_tasks main loop: {e}")
                await asyncio.sleep(60)

bot = ChainBot()

async def save_active_chains():
    """Saves the current state of active chains to a JSON file."""
    serializable_chains = {}
    for channel_id, chain_info in bot.active_chains.items():
        view = chain_info.get('view')
        if not view:
            continue
            
        serializable_chains[channel_id] = {
            'message_id': chain_info['message_id'],
            'end_time_utc': chain_info['end_time_utc'].isoformat(),
            'timestamp': chain_info['timestamp'],
            'organizer': chain_info['organizer'],
            'joiners': [list(item) for item in view.joiners],
            'cant_make_it': [list(item) for item in view.cant_make_it],
        }

    try:
        with open(CHAIN_DATA_FILE, 'w') as f:
            json.dump(serializable_chains, f, indent=4)
        logger.info("Successfully saved active chains to disk.")
    except Exception as e:
        logger.error(f"Failed to save active chains to disk: {e}")

async def load_and_resume_chains():
    """Loads chains from disk and resumes their lifecycle tasks."""
    logger.info("Attempting to load and resume chains from disk...")
    try:
        with open(CHAIN_DATA_FILE, 'r') as f:
            chains_to_load = json.load(f)
    except FileNotFoundError:
        logger.info("No active_chains.json file found. Starting fresh.")
        return
    except json.JSONDecodeError:
        logger.error("Could not decode active_chains.json. File might be corrupt. Starting fresh.")
        return

    now_utc = datetime.now(timezone.utc)
    for channel_id_str, chain_data in chains_to_load.items():
        channel_id = int(channel_id_str)
        end_time_utc = datetime.fromisoformat(chain_data['end_time_utc'])

        if end_time_utc < now_utc:
            logger.info(f"Skipping expired chain in channel {channel_id}.")
            continue
        
        try:
            # Recreate the view and restore its state
            view = ChainView(bot, {'organizer': chain_data['organizer']})
            view.joiners = {tuple(item) for item in chain_data.get('joiners', [])}
            view.cant_make_it = {tuple(item) for item in chain_data.get('cant_make_it', [])}
            
            # Re-register the view with the bot so it can receive interactions
            bot.add_view(view, message_id=chain_data['message_id'])

            bot.active_chains[channel_id] = {
                'message_id': chain_data['message_id'],
                'end_time_utc': end_time_utc,
                'timestamp': chain_data['timestamp'],
                'organizer': chain_data['organizer'],
                'view': view
            }
            
            # Relaunch the lifecycle task
            asyncio.create_task(manage_chain_lifecycle(channel_id))
            logger.info(f"Successfully resumed chain in channel {channel_id}.")
            
        except Exception as e:
            logger.error(f"Failed to resume chain for channel {channel_id}: {e}")

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")
    if not bot.persistent_views_loaded:
        await load_and_resume_chains()
        bot.persistent_views_loaded = True
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

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

    # Defer the response since API validation might take some time
    await interaction.response.defer(ephemeral=False)

    # Validate that the name and ID exist in faction 53180
    is_valid, error_message = await validate_faction_member(name, user_id)
    if not is_valid:
        await interaction.followup.send(error_message, ephemeral=False)
        return

    # Get the member object
    member = interaction.guild.get_member(interaction.user.id)
    if not member:
        await interaction.followup.send("Could not find your member info", ephemeral=False)
        return

    # Check if bot has permission to manage nicknames
    bot_member = interaction.guild.get_member(bot.user.id)
    if not bot_member or not bot_member.guild_permissions.manage_nicknames:
        await interaction.followup.send(
            "❌ I need the 'Manage Nicknames' permission to do this! Please ask a server admin to grant me this permission.",
            ephemeral=False
        )
        return

    try:
        # Format the nickname
        new_nickname = f"{name} [{user_id}]"
        
        # Check if nickname is too long (Discord limit is 32 characters)
        if len(new_nickname) > 32:
            await interaction.followup.send(
                "❌ Nickname is too long! Please use a shorter name or ID.",
                ephemeral=False
            )
            return
        
        # Check for duplicate nicknames
        is_duplicate, existing_member = check_duplicate_nickname(interaction.guild, new_nickname, interaction.user.id)
        if is_duplicate:
            # Find admin role or mention @everyone if no admin role exists
            admin_role = discord.utils.get(interaction.guild.roles, name="admin") or discord.utils.get(interaction.guild.roles, name="Admin")
            admin_mention = admin_role.mention if admin_role else "@admin"
            
            await interaction.followup.send(
                f"❌ Nickname '{new_nickname}' is already in use by {existing_member.mention}. {admin_mention} - duplicate nickname detected!",
                ephemeral=False  # Make this visible to admins
            )
            return

        # Change the nickname
        await member.edit(nick=new_nickname)
        
        # Give the Soldier role if they don't have it
        soldier_role = discord.utils.get(interaction.guild.roles, name="💂‍♀️Soldier💂‍♀️")
        role_message = ""
        
        if soldier_role:
            if soldier_role not in member.roles:
                try:
                    await member.add_roles(soldier_role)
                    role_message = f" and assigned the {soldier_role.mention} role"
                except discord.Forbidden:
                    role_message = f" (couldn't assign {soldier_role.mention} role - insufficient permissions)"
                except Exception as e:
                    logging.error(f"Role assignment error: {e}")
                    role_message = f" (error assigning {soldier_role.mention} role)"
            else:
                role_message = f" (you already have the {soldier_role.mention} role)"
        else:
            role_message = " (💂‍♀️Soldier💂‍♀️ role not found on this server)"
        
        await interaction.followup.send(
            f"✅ Your nickname has been set to: {new_nickname}{role_message}",
            ephemeral=False
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ I don't have permission to change nicknames! The server role hierarchy might be preventing me.",
            ephemeral=False
        )
    except Exception as e:
        await interaction.followup.send(
            "An error occurred while changing your nickname.",
            ephemeral=False
        )
        logging.error(f"Nickname change error: {e}")

@bot.tree.command(name="dm", description="Send yourself a DM with your message")
@app_commands.describe(message="The message to send to yourself")
async def dm(interaction: discord.Interaction, message: str):
    try:
        await interaction.user.send(f"You said🗣️: {message}")
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

def parse_time(time_str: str) -> Tuple[Optional[int], Optional[datetime]]:
    """
    Convert time string like '5h', '30m', '18:00TC', or '18:00TC at dd.mm.yyyy' to seconds and target UTC time
    Returns (seconds_until_target, target_utc_datetime)
    """
    # Check for TC time with a specific date (e.g., 18:00TC at 27.7.2025)
    tc_date_match = re.match(r'(\d{1,2}):(\d{2})tc\s+at\s+(\d{1,2})\.(\d{1,2})\.(\d{4})', time_str.lower())
    if tc_date_match:
        tc_hour, tc_minute, day, month, year = map(int, tc_date_match.groups())

        if not (0 <= tc_hour <= 23) or not (0 <= tc_minute <= 59):
            return None, None
        
        try:
            # Construct the target datetime object with UTC timezone
            target_time = datetime(year, month, day, tc_hour, tc_minute, tzinfo=timezone.utc)
        except ValueError:
            # Handles invalid dates like 31.2.2025
            return None, None

        now_utc = datetime.now(timezone.utc)
        if target_time < now_utc:
            # The specified date and time are in the past
            return None, None

        # Calculate the difference in seconds
        time_diff = target_time - now_utc
        return int(time_diff.total_seconds()), target_time

    # Check for TC time format for today/tomorrow (e.g., 18:00TC or 12:30TC)
    tc_match = re.match(r'(\d{1,2}):(\d{2})tc', time_str.lower())
    if tc_match:
        tc_hour = int(tc_match.group(1))
        tc_minute = int(tc_match.group(2))
        
        if not (0 <= tc_hour <= 23) or not (0 <= tc_minute <= 59):
            return None, None
        
        # TC time is UTC, so we calculate the exact target time
        now_utc = datetime.now(timezone.utc)
        target_time = now_utc.replace(hour=tc_hour, minute=tc_minute, second=0, microsecond=0)
        
        # If the target time has already passed today, set it for tomorrow
        if target_time <= now_utc:
            target_time += timedelta(days=1)
        
        # Calculate seconds until target time
        time_diff = target_time - now_utc
        return int(time_diff.total_seconds()), target_time
    
    # Check for relative time format (5h, 30m)
    regular_match = re.match(r'(\d+)([hm])', time_str.lower())
    if regular_match:
        amount, unit = regular_match.groups()
        amount = int(amount)
        
        seconds = 0
        if unit == 'h':
            seconds = amount * 3600  # hours to seconds
        elif unit == 'm':
            seconds = amount * 60    # minutes to seconds
        
        # For duration-based times, calculate target time from now
        target_time = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        return seconds, target_time
    
    return None, None

def format_time_remaining(seconds: int) -> str:
    """Format seconds into hours, minutes, seconds"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    else:
        return f"{minutes}m {seconds}s"

async def validate_faction_member(name: str, user_id: str) -> Tuple[bool, str]:
    """
    Validate if a user with given name and ID exists in faction 53180
    Returns (is_valid, error_message)
    """
    try:
        async with aiohttp.ClientSession() as session:
            # Get faction members from Torn API
            url = f"https://api.torn.com/faction/53180?selections=basic&key={torn_api_key}"
            async with session.get(url) as response:
                if response.status != 200:
                    return False, "❌ Failed to connect to Torn API. Please try again later."
                
                data = await response.json()
                
                if 'error' in data:
                    return False, f"❌ API Error: {data['error']['error']}"
                
                members = data.get('members', {})
                
                # Check if user_id exists in faction
                if user_id not in members:
                    return False, f"❌ User ID {user_id} is not found in faction ."
                
                # Check if the name matches
                member_data = members[user_id]
                member_name = member_data.get('name', '').lower()
                
                if member_name != name.lower():
                    actual_name = member_data.get('name', 'Unknown')
                    return False, f"❌ Name mismatch! User ID {user_id} belongs to '{actual_name}', not '{name}'."
                
                return True, ""
                
    except aiohttp.ClientError:
        return False, "❌ Network error while connecting to Torn API. Please try again later."
    except json.JSONDecodeError:
        return False, "❌ Invalid response from Torn API. Please try again later."
    except Exception as e:
        logging.error(f"Faction validation error: {e}")
        return False, "❌ An unexpected error occurred during validation."

def check_duplicate_nickname(guild: discord.Guild, new_nickname: str, current_user_id: int) -> Tuple[bool, Optional[discord.Member]]:
    """
    Check if the nickname already exists in the server
    Returns (is_duplicate, existing_member)
    """
    for member in guild.members:
        # Skip the current user (they can keep their own nickname)
        if member.id == current_user_id:
            continue
            
        # Check if any other member has the same nickname
        if member.nick and member.nick.lower() == new_nickname.lower():
            return True, member
            
        # Also check display name in case they don't have a nickname set
        if member.display_name.lower() == new_nickname.lower():
            return True, member
    
    return False, None

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
        
        # Save the updated state
        await save_active_chains()

class CancelButton(Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.secondary, label="Cancel Chain", custom_id="cancel_chain")

    async def callback(self, interaction: discord.Interaction):
        assert self.view is not None
        view: ChainView = self.view
        
        organizer_id = view.chain_data['organizer_id']
        admin_role = discord.utils.get(interaction.guild.roles, name="admin") or discord.utils.get(interaction.guild.roles, name="Admin")
        
        is_organizer = (interaction.user.id == organizer_id)
        is_admin = (admin_role in interaction.user.roles) if isinstance(interaction.user, discord.Member) and admin_role else False

        if not is_organizer and not is_admin:
            await interaction.response.send_message("Only the organizer or an admin can cancel the chain.", ephemeral=True)
            return

        await interaction.response.defer()

        view.disable_all_buttons()
        
        cancel_embed = discord.Embed(
            title="❌ Chain Canceled",
            description=f"This chain was canceled by {interaction.user.mention}.",
            color=discord.Color.red()
        )
        cancel_embed.set_footer(text=f"Chain originally organized by {view.chain_data['organizer']}")
        
        if interaction.message:
            await interaction.message.edit(embed=cancel_embed, view=view)
        
        channel_id = interaction.channel.id
        if bot and channel_id in bot.active_chains:
            del bot.active_chains[channel_id]
            logger.info(f"Chain in channel {channel_id} removed from active_chains.")

        if bot and channel_id in bot.chain_tasks:
            if not bot.chain_tasks[channel_id].done():
                bot.chain_tasks[channel_id].cancel()
            del bot.chain_tasks[channel_id]
            logger.info(f"Chain task for channel {channel_id} cancelled.")

        await interaction.followup.send("Chain has been canceled.", ephemeral=True)

class ChainView(View):
    def __init__(self, bot, chain_data: Dict):
        super().__init__(timeout=None)
        self.joiners: Set[Tuple[int, str]] = set()
        self.cant_make_it: Set[Tuple[int, str]] = set()
        self.chain_data = chain_data
        
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
        self.cancel_button = CancelButton()
        
        self.add_item(self.join_button)
        self.add_item(self.skip_button)
        self.add_item(self.cancel_button)
    
    def disable_all_buttons(self):
        self.join_button.disabled = True
        self.skip_button.disabled = True
        self.cancel_button.disabled = True

async def manage_chain_lifecycle(channel_id: int):
    try:
        chain_info = bot.active_chains.get(channel_id)
        if not chain_info:
            logger.warning(f"manage_chain_lifecycle: No active chain found for channel {channel_id}")
            return

        channel = bot.get_channel(channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            logger.warning(f"manage_chain_lifecycle: Channel {channel_id} not found or not a text channel.")
            return

        message = await channel.fetch_message(chain_info['message_id'])
        view = chain_info['view']
        end_time_utc = chain_info['end_time_utc']
        organizer_name = chain_info['organizer']
        timestamp = chain_info['timestamp']

        while datetime.now(timezone.utc) < end_time_utc:
            await asyncio.sleep(5) # Update every 5 seconds
            
            if channel_id not in bot.active_chains:
                 logger.info(f"Chain in {channel_id} was cancelled. Stopping countdown.")
                 return

            remaining_seconds = (end_time_utc - datetime.now(timezone.utc)).total_seconds()
            if remaining_seconds < 0:
                remaining_seconds = 0
            
            # Format the start time string to include date if not today
            starts_at_str = end_time_utc.strftime('%H:%M TC')
            if end_time_utc.date() != datetime.now(timezone.utc).date():
                starts_at_str += end_time_utc.strftime(' on %d.%m.%Y')

            new_embed = discord.Embed(
                title="🔄 Upcoming Chain",
                description="A new chain is being organized! Click the buttons below to indicate your participation:",
                color=discord.Color.gold()
            )
            
            new_embed.add_field(
                name="Chain Start Time",
                value=f"Countdown: {format_time_remaining(int(remaining_seconds))}\nStarts at: {starts_at_str}\nYour local time: <t:{timestamp}:t>",
                inline=False
            )
            
            joiners_text = "\n".join([f"• {name}" for _, name in view.joiners]) if view.joiners else "*No participants yet*"
            new_embed.add_field(
                name=f"Participants ({len(view.joiners)})",
                value=joiners_text,
                inline=False
            )
            
            cant_make_it_text = "\n".join([f"• {name}" for _, name in view.cant_make_it]) if view.cant_make_it else "*None*"
            new_embed.add_field(
                name=f"Can't Make It ({len(view.cant_make_it)})",
                value=cant_make_it_text,
                inline=False
            )
            
            new_embed.add_field(
                name="Options",
                value="🟢 = I'll join the chain!\n🔴 = Can't make it",
                inline=False
            )
            
            new_embed.set_footer(text=f"Chain organized by {organizer_name}")
            
            await message.edit(embed=new_embed, view=view)

        # Countdown finished
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
        
        view.disable_all_buttons()
        await message.edit(embed=final_embed, view=view)
        
        if view.joiners:
            mentions = [f"<@{user_id}>" for user_id, _ in view.joiners]
            mentions_text = " ".join(mentions)
            await channel.send(f"🔔 Chain is starting! {mentions_text}")
        
        logger.info(f"Countdown finished for channel {channel_id}. Chain is starting.")
        
    except asyncio.CancelledError:
        logger.info(f"manage_chain_lifecycle for channel {channel_id} was cancelled.")
    except discord.NotFound:
        logger.warning(f"Message for chain in channel {channel_id} not found. It might have been deleted.")
    except Exception as e:
        logger.error(f"Error in manage_chain_lifecycle for channel {channel_id}: {e}")
    finally:
        # Clean up the chain since it's over
        if channel_id in bot.active_chains:
            del bot.active_chains[channel_id]
        if channel_id in bot.chain_tasks:
            del bot.chain_tasks[channel_id]
        logger.info(f"Cleaned up chain for channel {channel_id}.")


bot.run(token, log_level=logging.INFO)