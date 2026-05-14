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
from typing import Any, List, Optional, Dict, Set, Tuple
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
OC_STATE_FILE = "oc_monitor_state.json"

DEFAULT_OC_FACTION_IDS = [53180]
DEFAULT_OC_REMINDER_HOURS = [12, 24]
OC_ACTIVE_STATUSES = {"recruiting", "planning", "available"}
OC_FINISHED_STATUSES = {"successful", "failure", "failed", "expired", "completed"}

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
        self.config = {}
        self.chain_checker_started = False
        self.faction_role_sync_started = False
        self.oc_monitor_started = False
        self.oc_known_statuses: Dict[str, str] = {}
        self.oc_reminders_sent: Set[str] = set()
        self.oc_first_seen: Dict[str, int] = {}
        self.oc_active_counts: Dict[int, Optional[int]] = {}
        logger.info("ChainBot initialized")

bot = ChainBot()

CONFIG_FILE = "config.json"

async def load_config():
    """Loads configuration from a JSON file."""
    try:
        with open(CONFIG_FILE, 'r') as f:
            bot.config = json.load(f)
            # Ensure new keys are present
            bot.config.setdefault("chain_notification_channel_id", None)
            bot.config.setdefault("oc_notification_channel_id", None)
            bot.config.setdefault("oc_faction_ids", DEFAULT_OC_FACTION_IDS)
            bot.config.setdefault("oc_reminder_hours", DEFAULT_OC_REMINDER_HOURS)
            logger.info("Configuration loaded from config.json.")
    except FileNotFoundError:
        logger.info("config.json not found, starting with default configuration.")
        bot.config = {
            "chain_notification_channel_id": None,
            "oc_notification_channel_id": None,
            "oc_faction_ids": DEFAULT_OC_FACTION_IDS,
            "oc_reminder_hours": DEFAULT_OC_REMINDER_HOURS,
        }
    except json.JSONDecodeError:
        logger.error("Could not decode config.json. Starting with default configuration.")
        bot.config = {
            "chain_notification_channel_id": None,
            "oc_notification_channel_id": None,
            "oc_faction_ids": DEFAULT_OC_FACTION_IDS,
            "oc_reminder_hours": DEFAULT_OC_REMINDER_HOURS,
        }

async def save_config():
    """Saves the current configuration to a JSON file."""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(bot.config, f, indent=4)
        logger.info("Configuration saved to config.json.")
    except Exception as e:
        logger.error(f"Failed to save configuration: {e}")


async def load_oc_state():
    """Loads persisted organized crime monitor state."""
    try:
        with open(OC_STATE_FILE, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.info("No oc_monitor_state.json file found. Starting fresh.")
        return
    except json.JSONDecodeError:
        logger.error("Could not decode oc_monitor_state.json. Starting fresh.")
        return

    bot.oc_known_statuses = {
        str(crime_id): str(status).lower()
        for crime_id, status in data.get("known_statuses", {}).items()
    }
    bot.oc_reminders_sent = set(data.get("reminders_sent", []))
    bot.oc_first_seen = {
        str(crime_id): int(timestamp)
        for crime_id, timestamp in data.get("first_seen", {}).items()
    }
    logger.info("Organized crime monitor state loaded.")


async def save_oc_state():
    """Persists organized crime monitor state."""
    try:
        with open(OC_STATE_FILE, 'w') as f:
            json.dump(
                {
                    "known_statuses": bot.oc_known_statuses,
                    "reminders_sent": sorted(bot.oc_reminders_sent),
                    "first_seen": bot.oc_first_seen,
                },
                f,
                indent=4
            )
    except Exception as e:
        logger.error(f"Failed to save organized crime monitor state: {e}")


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
    await load_config()  # Load configuration
    await load_oc_state()
    if not bot.persistent_views_loaded:
        await load_and_resume_chains()
        bot.persistent_views_loaded = True
    
    if not bot.chain_checker_started:
        asyncio.create_task(check_chain_status_periodically())
        bot.chain_checker_started = True
        
    if not bot.faction_role_sync_started:
        asyncio.create_task(sync_faction_roles_periodically())
        bot.faction_role_sync_started = True

    if not bot.oc_monitor_started:
        asyncio.create_task(check_organized_crimes_periodically())
        bot.oc_monitor_started = True
        
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

    faction_id, error_message = await validate_and_get_faction(name, user_id)
    
    if error_message:
        await interaction.followup.send(error_message, ephemeral=False)
        return

    # Faction validation
    allowed_factions = {
        53180: "faction -I-",
        55332: "faction -II-"
    }
    
    if faction_id not in allowed_factions:
        await interaction.followup.send(
            f"❌ You must be a member of 'faction -I-' or 'faction -II-' to set your nickname here.",
            ephemeral=False
        )
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
        
        # --- Role Assignment ---
        
        # 1. Soldier Role
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

        # 2. Faction Roles
        faction_role_message = ""
        target_role_name = allowed_factions[faction_id]
        role_i = discord.utils.get(interaction.guild.roles, name="faction -I-")
        role_ii = discord.utils.get(interaction.guild.roles, name="faction -II-")
        
        roles_to_add = []
        roles_to_remove = []

        if target_role_name == "faction -I-":
            if role_i and role_i not in member.roles: roles_to_add.append(role_i)
            if role_ii and role_ii in member.roles: roles_to_remove.append(role_ii)
        elif target_role_name == "faction -II-":
            if role_ii and role_ii not in member.roles: roles_to_add.append(role_ii)
            if role_i and role_i in member.roles: roles_to_remove.append(role_i)

        try:
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason="/setnick command")
                faction_role_message += f" and the {roles_to_add[0].mention} role"
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="/setnick command")
        except discord.Forbidden:
            faction_role_message += f" (couldn't update faction roles - insufficient permissions)"
        except Exception as e:
            logging.error(f"Faction role assignment error in /setnick: {e}")
            faction_role_message += f" (error updating faction roles)"

        await interaction.followup.send(
            f"✅ Your nickname has been set to: {new_nickname}{role_message}{faction_role_message}.",
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
        await interaction.user.send(f"You said🗣️ {interaction.user.mention}: {message}")
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
    Convert time string like '5h', '30m', '18:00TC', or '18:00TC at 25.12.2024' to seconds and target UTC time
    Returns (seconds_until_target, target_utc_datetime)
    """
    # Check for TC time with date format (e.g., 18:00TC at 25.12.2024)
    date_match = re.match(r'(\d{1,2}):(\d{2})tc at (\d{1,2})\.(\d{1,2})\.(\d{4})', time_str.lower())
    if date_match:
        tc_hour = int(date_match.group(1))
        tc_minute = int(date_match.group(2))
        day = int(date_match.group(3))
        month = int(date_match.group(4))
        year = int(date_match.group(5))
        
        if not (0 <= tc_hour <= 23) or not (0 <= tc_minute <= 59):
            return None, None
            
        try:
            # Create target datetime in UTC
            target_time = datetime(year, month, day, tc_hour, tc_minute, 0, tzinfo=timezone.utc)
            
            # Check if date is in the past
            now_utc = datetime.now(timezone.utc)
            if target_time <= now_utc:
                return None, None
                
            # Calculate seconds until target time
            time_diff = target_time - now_utc
            return int(time_diff.total_seconds()), target_time
            
        except ValueError:  # Invalid date
            return None, None
    
    # Check for TC time format (e.g., 18:00TC or 12:30TC)
    tc_match = re.match(r'(\d{1,2}):(\d{2})tc', time_str.lower())
    if tc_match:
        tc_hour = int(tc_match.group(1))
        tc_minute = int(tc_match.group(2))
        
        if not (0 <= tc_hour <= 23) or not (0 <= tc_minute <= 59):
            return None, None
        
        # Convert TC time to target time
        # TC time is UTC, so we calculate the exact target time
        now_utc = datetime.now(timezone.utc)
        target_time = now_utc.replace(hour=tc_hour, minute=tc_minute, second=0, microsecond=0)
        
        # If the target time has already passed today, set it for tomorrow
        if target_time <= now_utc:
            target_time += timedelta(days=1)
        
        # Calculate seconds until target time
        time_diff = target_time - now_utc
        return int(time_diff.total_seconds()), target_time
    
    # Check for regular time format (5h, 30m)
    regular_match = re.match(r'(\d+)([hm])', time_str.lower())
    if regular_match:
        amount, unit = regular_match.groups()
        amount = int(amount)
        
        seconds = 0
        if unit == 'h':
            seconds = amount * 3600  # hours to seconds
        elif unit == 'm':
            seconds = amount * 60    # minutes to seconds
        
        # For duration-based times, calculate target time
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

async def validate_and_get_faction(name: str, user_id: str) -> Tuple[Optional[int], str]:
    """
    Validates a Torn user's name and ID, and returns their faction ID.
    Returns (faction_id, error_message)
    """
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.torn.com/user/{user_id}?selections=profile&key={torn_api_key}"
            async with session.get(url) as response:
                if response.status != 200:
                    return None, "❌ Failed to connect to Torn API. Please try again later."
                
                data = await response.json()
                
                if 'error' in data:
                    if data['error']['code'] == 2: # "User not found"
                         return None, f"❌ User ID {user_id} not found in Torn."
                    return None, f"❌ API Error: {data['error']['error']}"
                
                # Validate name
                api_name = data.get('name', '').lower()
                if api_name != name.lower():
                    actual_name = data.get('name', 'Unknown')
                    return None, f"❌ Name mismatch! User ID {user_id} belongs to '{actual_name}', not '{name}'."

                # Get faction ID
                faction_info = data.get('faction', {})
                faction_id = faction_info.get('faction_id')
                
                return int(faction_id) if faction_id and faction_id != 0 else None, ""
                
    except Exception as e:
        logging.error(f"Error during user validation for {user_id}: {e}")
        return None, "❌ An unexpected error occurred during validation."

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

async def get_user_faction(user_id: str) -> Tuple[Optional[int], bool]:
    """
    Gets the faction ID for a given Torn user ID.
    Returns (faction_id, api_success) where:
    - faction_id: The faction ID if user is in a faction, None if not in a faction, None if API error
    - api_success: True if API call succeeded, False if there was an error
    """
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.torn.com/user/{user_id}?selections=profile&key={torn_api_key}"
            async with session.get(url) as response:
                if response.status != 200:
                    logger.error(f"Failed to get user data for {user_id}. Status: {response.status}")
                    return None, False
                
                data = await response.json()
                
                if 'error' in data:
                    # Don't log "User not found" as an error, it's expected for old IDs
                    if data['error']['code'] != 2:
                         logger.error(f"Torn API error for user {user_id}: {data['error']['error']}")
                    return None, False
                
                faction_info = data.get('faction', {})
                faction_id = faction_info.get('faction_id')
                # If we got valid data, api_success is True, even if user has no faction
                return (int(faction_id) if faction_id and faction_id != 0 else None), True
                
    except Exception as e:
        logger.error(f"Error getting user faction for {user_id}: {e}")
        return None, False


def get_configured_oc_faction_ids() -> List[int]:
    """Returns configured faction IDs for organized crime monitoring."""
    raw_faction_ids = bot.config.get("oc_faction_ids", DEFAULT_OC_FACTION_IDS)
    faction_ids: List[int] = []

    if isinstance(raw_faction_ids, list):
        for faction_id in raw_faction_ids:
            try:
                faction_ids.append(int(faction_id))
            except (TypeError, ValueError):
                logger.warning(f"Ignoring invalid OC faction ID in config: {faction_id}")

    return faction_ids or DEFAULT_OC_FACTION_IDS


def get_configured_oc_reminder_hours() -> List[int]:
    """Returns configured reminder hours for organized crime monitoring."""
    raw_hours = bot.config.get("oc_reminder_hours", DEFAULT_OC_REMINDER_HOURS)
    reminder_hours: List[int] = []

    if isinstance(raw_hours, list):
        for hour in raw_hours:
            try:
                parsed_hour = int(hour)
            except (TypeError, ValueError):
                logger.warning(f"Ignoring invalid OC reminder hour in config: {hour}")
                continue

            if parsed_hour > 0:
                reminder_hours.append(parsed_hour)

    return sorted(set(reminder_hours)) or DEFAULT_OC_REMINDER_HOURS


def normalize_oc_status(status: Any) -> str:
    return str(status or "unknown").strip().lower()


async def get_organized_crimes(faction_id: int) -> Optional[List[Dict[str, Any]]]:
    """Get organized crime data from Torn API.

    Torn has exposed crimes through multiple faction API shapes, so this tries
    the v2 crimes endpoint first and then falls back to the legacy selection.
    """
    urls = [
        f"https://api.torn.com/v2/faction/{faction_id}/crimes?cat=all&sort=DESC&key={torn_api_key}",
        f"https://api.torn.com/v2/faction/crimes?cat=all&sort=DESC&key={torn_api_key}",
        f"https://api.torn.com/v2/faction/{faction_id}?selections=crimes&cat=all&sort=DESC&key={torn_api_key}",
        f"https://api.torn.com/faction/{faction_id}?selections=crimes&key={torn_api_key}",
    ]

    async with aiohttp.ClientSession() as session:
        for url in urls:
            try:
                logger.info(f"Making organized crime API request to: {url[:90]}...")
                async with session.get(url) as response:
                    if response.status == 404:
                        continue
                    if response.status != 200:
                        logger.error(f"Organized crime API request failed with status {response.status}")
                        continue

                    data = await response.json()
                    if 'error' in data:
                        logger.error(f"Organized crime API error: {data['error'].get('error', data['error'])}")
                        continue

                    crimes = data.get("crimes")
                    if crimes is None:
                        continue
                    if isinstance(crimes, dict):
                        return [crime for crime in crimes.values() if isinstance(crime, dict)]
                    if isinstance(crimes, list):
                        return [crime for crime in crimes if isinstance(crime, dict)]
            except Exception as e:
                logger.error(f"Organized crime data error for faction {faction_id}: {e}")

    return None


def get_crime_id(crime: Dict[str, Any]) -> str:
    return str(crime.get("id") or crime.get("crime_id") or crime.get("previous_crime_id") or "unknown")


def get_crime_display_name(crime: Dict[str, Any]) -> str:
    return str(crime.get("name") or crime.get("crime") or "Organized Crime")


def get_crime_age_seconds(crime_key: str, crime: Dict[str, Any]) -> int:
    now_ts = int(datetime.now(timezone.utc).timestamp())
    created_at = crime.get("created_at") or crime.get("created")

    try:
        if created_at:
            return max(0, now_ts - int(created_at))
    except (TypeError, ValueError):
        pass

    first_seen = bot.oc_first_seen.setdefault(crime_key, now_ts)
    return max(0, now_ts - int(first_seen))


def get_member_by_torn_id(guild: discord.Guild, torn_id: str) -> Optional[discord.Member]:
    id_pattern = re.compile(r'\[(\d+)\]$')

    for member in guild.members:
        if member.bot:
            continue

        for name in (member.nick, member.display_name):
            if not name:
                continue
            match = id_pattern.search(name)
            if match and match.group(1) == torn_id:
                return member

    return None


def get_oc_unjoined_slots(crime: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Returns assigned users who have not joined and any unfilled slot labels."""
    missing_users: List[Dict[str, Any]] = []
    open_slots: List[str] = []

    for slot in crime.get("slots", []) or []:
        if not isinstance(slot, dict):
            continue

        position = str(
            slot.get("position")
            or slot.get("position_name")
            or slot.get("name")
            or "Open slot"
        )
        user = slot.get("user")

        if not user:
            open_slots.append(position)
            continue

        if not isinstance(user, dict):
            continue

        joined_at = user.get("joined_at")
        user_id = user.get("id") or user.get("user_id")
        if user_id and not joined_at:
            missing_users.append({
                "id": str(user_id),
                "position": position,
            })

    return missing_users, open_slots


def build_oc_summary(crime: Dict[str, Any]) -> str:
    status = normalize_oc_status(crime.get("status"))
    parts = [
        f"Crime: {get_crime_display_name(crime)}",
        f"Status: {status}",
    ]

    for field_name, label in (
        ("created_at", "Created"),
        ("ready_at", "Ready"),
        ("planning_at", "Planning"),
        ("executed_at", "Executed"),
        ("expired_at", "Expires"),
    ):
        timestamp = crime.get(field_name)
        if timestamp:
            try:
                parts.append(f"{label}: <t:{int(timestamp)}:R>")
            except (TypeError, ValueError):
                pass

    return "\n".join(parts)


async def send_oc_dm(member: discord.Member, crime: Dict[str, Any], hour: int):
    try:
        await member.send(
            f"OC reminder: {get_crime_display_name(crime)} still needs you to join. "
            f"This is the {hour} hour reminder."
        )
    except discord.Forbidden:
        logger.info(f"Could not DM OC reminder to {member.display_name} ({member.id}).")
    except Exception as e:
        logger.error(f"Failed to send OC reminder DM to {member.display_name}: {e}")


async def send_oc_message(channel: discord.abc.Messageable, title: str, description: str, color: discord.Color):
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    await channel.send(embed=embed)


async def handle_oc_reminders(channel: discord.abc.Messageable, faction_id: int, crime: Dict[str, Any]):
    crime_id = get_crime_id(crime)
    crime_key = f"{faction_id}:{crime_id}"
    missing_users, open_slots = get_oc_unjoined_slots(crime)

    if not missing_users and not open_slots:
        return

    age_seconds = get_crime_age_seconds(crime_key, crime)
    guild = channel.guild if isinstance(channel, (discord.TextChannel, discord.Thread)) else None

    for reminder_hour in get_configured_oc_reminder_hours():
        if age_seconds < reminder_hour * 3600:
            continue

        reminder_key = f"{crime_key}:{reminder_hour}"
        if reminder_key in bot.oc_reminders_sent:
            continue

        mentions: List[str] = []
        missing_lines: List[str] = []
        for missing_user in missing_users:
            torn_id = missing_user["id"]
            member = get_member_by_torn_id(guild, torn_id) if guild else None
            mention = member.mention if member else f"Torn ID {torn_id}"
            mentions.append(mention)
            missing_lines.append(f"- {mention}: {missing_user['position']}")
            if member:
                await send_oc_dm(member, crime, reminder_hour)

        open_slot_lines = [f"- {slot}" for slot in open_slots[:10]]
        description_parts = [
            f"Faction {faction_id} has an OC waiting after {reminder_hour} hours.",
            build_oc_summary(crime),
        ]

        if missing_lines:
            description_parts.append("Missing assigned members:\n" + "\n".join(missing_lines[:20]))
        if open_slot_lines:
            description_parts.append("Open slots:\n" + "\n".join(open_slot_lines))

        content = " ".join(mentions[:20])
        try:
            if content:
                await channel.send(content)
            await send_oc_message(
                channel,
                f"OC Reminder - {reminder_hour}h",
                "\n\n".join(description_parts),
                discord.Color.orange()
            )
            bot.oc_reminders_sent.add(reminder_key)
            await save_oc_state()
        except discord.Forbidden:
            logger.error("Missing permissions to send organized crime reminder.")
        except Exception as e:
            logger.error(f"Failed to send organized crime reminder: {e}")


async def check_organized_crimes_periodically():
    """Periodically checks organized crimes and sends alerts/reminders."""
    await bot.wait_until_ready()
    logger.info("Starting organized crime monitoring.")

    while not bot.is_closed():
        channel_id = bot.config.get("oc_notification_channel_id")
        channel = bot.get_channel(channel_id) if channel_id else None
        if not channel:
            logger.warning("OC monitor is enabled, but no OC notification channel is set.")
            await asyncio.sleep(600)
            continue

        state_changed = False

        for faction_id in get_configured_oc_faction_ids():
            crimes = await get_organized_crimes(faction_id)
            if crimes is None:
                continue

            active_crimes = [
                crime for crime in crimes
                if normalize_oc_status(crime.get("status")) in OC_ACTIVE_STATUSES
            ]
            current_active_count = len(active_crimes)
            previous_active_count = bot.oc_active_counts.get(faction_id)
            bot.oc_active_counts[faction_id] = current_active_count

            if previous_active_count is not None and previous_active_count > 0 and current_active_count == 0:
                try:
                    await send_oc_message(
                        channel,
                        "No Organized Crimes Left",
                        f"Faction {faction_id} has no active, recruiting, planning, or available OCs left.",
                        discord.Color.red()
                    )
                except Exception as e:
                    logger.error(f"Failed to send empty organized crime alert: {e}")

            current_keys: Set[str] = set()
            for crime in crimes:
                crime_id = get_crime_id(crime)
                if crime_id == "unknown":
                    continue

                status = normalize_oc_status(crime.get("status"))
                crime_key = f"{faction_id}:{crime_id}"
                current_keys.add(crime_key)
                bot.oc_first_seen.setdefault(crime_key, int(datetime.now(timezone.utc).timestamp()))

                previous_status = bot.oc_known_statuses.get(crime_key)
                if previous_status != status:
                    bot.oc_known_statuses[crime_key] = status
                    state_changed = True

                is_new_crime = previous_status is None
                became_finished = (
                    previous_status is not None
                    and previous_status not in OC_FINISHED_STATUSES
                    and status in OC_FINISHED_STATUSES
                )

                try:
                    if is_new_crime and status in OC_ACTIVE_STATUSES:
                        await send_oc_message(
                            channel,
                            "Organized Crime Needs Members",
                            f"Faction {faction_id} has an OC to join.\n\n{build_oc_summary(crime)}",
                            discord.Color.blue()
                        )
                    elif became_finished:
                        await send_oc_message(
                            channel,
                            "Organized Crime Finished",
                            f"Faction {faction_id} finished an OC. Check for the next one to join.\n\n{build_oc_summary(crime)}",
                            discord.Color.green()
                        )
                except Exception as e:
                    logger.error(f"Failed to send organized crime status alert: {e}")

                if status in OC_ACTIVE_STATUSES:
                    await handle_oc_reminders(channel, faction_id, crime)

            # Keep remembered state bounded to current API data plus recent known history.
            if current_keys:
                stale_keys = [
                    key for key in bot.oc_first_seen
                    if key.startswith(f"{faction_id}:") and key not in current_keys
                ]
                for stale_key in stale_keys[:100]:
                    bot.oc_first_seen.pop(stale_key, None)

        if state_changed:
            await save_oc_state()

        await asyncio.sleep(600)

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
        super().__init__(style=discord.ButtonStyle.primary, label="Cancel Chain", custom_id="cancel_chain")
        
    async def callback(self, interaction: discord.Interaction):
        assert self.view is not None
        view: ChainView = self.view
        
        # Check if user is authorized to cancel
        is_admin = any(role.name.lower() == "admin" for role in interaction.user.roles)
        is_organizer = interaction.user.name == view.chain_data['organizer']
        
        if not (is_admin or is_organizer):
            await interaction.response.send_message(
                "❌ Only the chain organizer or admins can cancel the chain!",
                ephemeral=True
            )
            return
            
        # Cancel the chain
        channel_id = interaction.channel.id
        if channel_id in view.bot.active_chains:
            del view.bot.active_chains[channel_id]
            
            # Disable all buttons
            view.disable_all_buttons()
            
            # Update the message
            cancel_embed = discord.Embed(
                title="❌ Chain Cancelled",
                description=f"Chain was cancelled by {interaction.user.name}",
                color=discord.Color.red()
            )
            await interaction.message.edit(embed=cancel_embed, view=view)
            
            await interaction.response.send_message("Chain has been cancelled!", ephemeral=True)
            await save_active_chains()
        else:
            await interaction.response.send_message(
                "This chain has already ended or been cancelled.",
                ephemeral=True
            )

class ChainView(View):
    def __init__(self, bot_instance: ChainBot, chain_data: Dict):
        super().__init__(timeout=None)
        self.bot = bot_instance
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
        self.cancel_button = CancelButton()
        
        self.add_item(self.join_button)
        self.add_item(self.skip_button)
        self.add_item(self.cancel_button)
    
    def disable_all_buttons(self):
        """Disable all buttons in the view"""
        self.join_button.disabled = True
        self.skip_button.disabled = True
        self.cancel_button.disabled = True


async def manage_chain_lifecycle(channel_id: int):
    """Manages the lifecycle of a chain countdown in the background."""
    chain_info = bot.active_chains.get(channel_id)
    if not chain_info:
        logging.warning(f"manage_chain_lifecycle called for channel {channel_id} but no active chain found.")
        return

    channel = bot.get_channel(channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        logging.error(f"Could not find channel or invalid channel type for ID {channel_id}.")
        if channel_id in bot.active_chains:
            del bot.active_chains[channel_id]
        return
        
    try:
        chain_message = await channel.fetch_message(chain_info['message_id'])
    except (discord.NotFound, discord.Forbidden):
        logging.error(f"Could not fetch message {chain_info['message_id']} in channel {channel_id}.")
        if channel_id in bot.active_chains:
            del bot.active_chains[channel_id]
        return

    view = chain_info['view']
    end_time_utc = chain_info['end_time_utc']
    timestamp = chain_info['timestamp']
    organizer_name = chain_info['organizer']
    
    try:
        while datetime.now(timezone.utc) < end_time_utc:
            if channel_id not in bot.active_chains:
                logging.info(f"Chain in channel {channel_id} was cancelled. Stopping countdown.")
                return

            await asyncio.sleep(25)
            
            remaining = (end_time_utc - datetime.now(timezone.utc)).total_seconds()
            if remaining < 0:
                remaining = 0
            
            embed = discord.Embed(
                title="🔄 Upcoming Chain",
                description="A new chain is being organized! Click the buttons below to indicate your participation:",
                color=discord.Color.gold()
            )
            
            # Format the chain start time field differently based on whether it's today/tomorrow or a future date
            now_utc = datetime.now(timezone.utc)
            if end_time_utc.date() == now_utc.date():
                time_str = "Today"
            elif end_time_utc.date() == (now_utc + timedelta(days=1)).date():
                time_str = "Tomorrow"
            else:
                time_str = end_time_utc.strftime("%d.%m.%Y")
            
            embed.add_field(
                name="Chain Start Time",
                value=f"Countdown: {format_time_remaining(int(remaining))}\n" +
                      f"Date: {time_str}\n" +
                      f"Time: {end_time_utc.strftime('%H:%M')} TC\n" +
                      f"Your local time: <t:{timestamp}:F>",
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
            
            embed.set_footer(text=f"Chain organized by {organizer_name}")
            
            try:
                await chain_message.edit(embed=embed, view=view)
            except discord.NotFound:
                logging.warning(f"Chain message {chain_info['message_id']} not found during update. Stopping task.")
                break
            
            if remaining <= 0:
                break
        
        if channel_id not in bot.active_chains:
             logging.info(f"Chain in channel {channel_id} was cancelled or ended prematurely before starting.")
             return

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
            await channel.send(f"🔔 @everyone Chain is starting! {mentions_text}")
        
        view.disable_all_buttons()
        await chain_message.edit(embed=final_embed, view=view)
        
    except Exception as e:
        logging.error(f"Chain lifecycle management error: {e}")
        try:
            await channel.send("An error occurred while managing the chain.")
        except discord.Forbidden:
            logging.error(f"Could not send error message to channel {channel_id}.")
    finally:
        if channel_id in bot.active_chains:
            del bot.active_chains[channel_id]
        await save_active_chains()

@bot.tree.command(name="chain", description="Organize a chain with a countdown timer")
@app_commands.describe(
    time_str="Time until chain starts: '5h', '30m', '18:00TC', or '18:00TC at DD.MM.YYYY' (e.g., '18:00TC at 25.12.2024')"
)
@app_commands.guild_only()
async def chain(interaction: discord.Interaction, time_str: str):
    # Defer the response immediately to prevent timeout
    await interaction.response.defer()

    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send(
            "Chains can only be created in text channels or threads!",
            ephemeral=True
        )
        return
    
    if interaction.channel.id in bot.active_chains:
        await interaction.followup.send(
            "⚠️ There's already an active chain planned in this channel!",
            ephemeral=True
        )
        return
    
    seconds, end_time_utc = parse_time(time_str)
    if seconds is None or end_time_utc is None:
        await interaction.followup.send(
            "❌ Invalid time format! Use one of these formats:\n" +
            "• '5h' for 5 hours\n" +
            "• '30m' for 30 minutes\n" +
            "• '18:00TC' for TC time today/tomorrow\n" +
            "• '18:00TC at 25.12.2024' for a specific date",
            ephemeral=True
        )
        return
    
    timestamp = int(end_time_utc.timestamp())
    
    embed = discord.Embed(
        title="🔄 Upcoming Chain",
        description="A new chain is being organized! Click the buttons below to indicate your participation:",
        color=discord.Color.gold()
    )
    
    # Format the chain start time field differently based on whether it's today/tomorrow or a future date
    now_utc = datetime.now(timezone.utc)
    if end_time_utc.date() == now_utc.date():
        time_str = "Today"
    elif end_time_utc.date() == (now_utc + timedelta(days=1)).date():
        time_str = "Tomorrow"
    else:
        time_str = end_time_utc.strftime("%d.%m.%Y")
    
    embed.add_field(
        name="Chain Start Time",
        value=f"Countdown: {format_time_remaining(seconds)}\n" +
              f"Date: {time_str}\n" +
              f"Time: {end_time_utc.strftime('%H:%M')} TC\n" +
              f"Your local time: <t:{timestamp}:F>",
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
        'organizer': interaction.user.name
    }
    
    view = ChainView(bot, chain_data)
    await interaction.followup.send(embed=embed, view=view)
    chain_message = await interaction.original_response()
    
    bot.active_chains[interaction.channel.id] = {
        'message_id': chain_message.id,
        'end_time_utc': end_time_utc,
        'timestamp': timestamp,
        'organizer': interaction.user.name,
        'view': view
    }
    
    await save_active_chains()
    asyncio.create_task(manage_chain_lifecycle(interaction.channel.id))
    logger.info(f"Chain started in channel {interaction.channel.id} by {interaction.user.name}")

 

async def get_chain_leaderboard(faction_id: str = "53180") -> Optional[Dict]:
    """
    Get current chain leaderboard data from Torn API
    Returns chain data or None if failed
    """
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.torn.com/faction/{faction_id}?selections=chain&key={torn_api_key}"
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                
                data = await response.json()
                
                if 'error' in data:
                    logging.error(f"Chain API Error: {data['error']['error']}")
                    return None
                
                return data.get("chain", {})
                
    except Exception as e:
        logging.error(f"Chain leaderboard error: {e}")
        return None

def process_chain_data(chain_data: Dict) -> Tuple[Dict, int, bool]:
    """
    Process chain data and return leaderboard, current hits, and if chain is active
    Returns (leaderboard_dict, current_hits, is_active)
    """
    chain_log = chain_data.get("log", {})
    current_hits = chain_data.get("current", 0)
    
    # Check if chain is active (has recent activity)
    is_active = current_hits > 0
    
    if not chain_log:
        return {}, current_hits, is_active
    
    # Aggregate hits per player
    leaderboard = {}
    
    for hit in chain_log.values():
        attacker = hit.get("initiator_name", "Unknown")
        result = hit.get("result", "").lower()
        
        if attacker not in leaderboard:
            leaderboard[attacker] = {"hits": 0, "mugs": 0, "leaves": 0, "others": 0}
        
        leaderboard[attacker]["hits"] += 1
        
        if "mug" in result:
            leaderboard[attacker]["mugs"] += 1
        elif "leave" in result:
            leaderboard[attacker]["leaves"] += 1
        else:
            leaderboard[attacker]["others"] += 1
    
    return leaderboard, current_hits, is_active

def create_leaderboard_embed(leaderboard: Dict, current_hits: int, is_final: bool = False) -> discord.Embed:
    """Create Discord embed for chain leaderboard"""
    title = "🔗 Final Chain Leaderboard" if is_final else f"🔗 Chain Leaderboard - {current_hits} hits"
    color = discord.Color.gold() if is_final else discord.Color.blue()
    
    embed = discord.Embed(
        title=title,
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    
    if not leaderboard:
        embed.description = "No chain data available yet."
        return embed
    
    # Sort by total hits
    sorted_leaderboard = sorted(leaderboard.items(), key=lambda x: x[1]["hits"], reverse=True)
    
    # Show top 10 players to avoid embed limits
    for i, (name, stats) in enumerate(sorted_leaderboard[:10]):
        position = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"{i+1}."
        value = (
            f"🎯 Hits: `{stats['hits']}`\n"
            f"💰 Mugs: `{stats['mugs']}`\n"
            f"🚪 Leaves: `{stats['leaves']}`\n"
            f"🧍 Others: `{stats['others']}`"
        )
        embed.add_field(name=f"{position} {name}", value=value, inline=True)
    
    if len(sorted_leaderboard) > 10:
        embed.set_footer(text=f"Showing top 10 of {len(sorted_leaderboard)} participants")
    
    return embed

async def track_chain_progress(channel, initial_hits: int = 0):
    """
    Track chain progress and update leaderboard every 30 seconds
    Stop if chain is inactive for more than 5 minutes
    """
    last_hits = initial_hits
    inactive_time = 0
    max_inactive_time = 300  # 5 minutes in seconds
    update_interval = 30  # 30 seconds
    
    leaderboard_message = None
    
    try:
        while inactive_time < max_inactive_time:
            await asyncio.sleep(update_interval)
            
            # Get current chain data
            chain_data = await get_chain_leaderboard()
            if not chain_data:
                continue
            
            leaderboard, current_hits, is_active = process_chain_data(chain_data)
            
            # Check if chain has new activity
            if current_hits > last_hits:
                last_hits = current_hits
                inactive_time = 0  # Reset inactive timer
            else:
                inactive_time += update_interval
            
            # Create/update leaderboard embed
            embed = create_leaderboard_embed(leaderboard, current_hits)
            
            if leaderboard_message is None:
                # Send initial leaderboard message
                leaderboard_message = await channel.send(embed=embed)
            else:
                # Update existing message
                try:
                    await leaderboard_message.edit(embed=embed)
                except discord.NotFound:
                    # Message was deleted, send a new one
                    leaderboard_message = await channel.send(embed=embed)
            
            # If chain is not active, break early
            if not is_active:
                break
        
        # Send final leaderboard
        if chain_data:
            leaderboard, current_hits, _ = process_chain_data(chain_data)
            final_embed = create_leaderboard_embed(leaderboard, current_hits, is_final=True)
            final_embed.description = "🔒 Chain tracking ended - No activity for 5+ minutes"
            
            if leaderboard_message:
                try:
                    await leaderboard_message.edit(embed=final_embed)
                except discord.NotFound:
                    await channel.send(embed=final_embed)
            else:
                await channel.send(embed=final_embed)
                
    except Exception as e:
        logging.error(f"Chain tracking error: {e}")
        if leaderboard_message:
            try:
                error_embed = discord.Embed(
                    title="❌ Chain Tracking Error",
                    description="An error occurred while tracking the chain.",
                    color=discord.Color.red()
                )
                await leaderboard_message.edit(embed=error_embed)
            except:
                pass

@bot.tree.command(name="chainboard", description="Show current chain leaderboard")
@app_commands.guild_only()
async def chainboard(interaction: discord.Interaction):
    await interaction.response.defer()
    
    chain_data = await get_chain_leaderboard()
    if not chain_data:
        await interaction.followup.send(
            "❌ Failed to retrieve chain data from Torn API.",
            ephemeral=True
        )
        return
    
    leaderboard, current_hits, is_active = process_chain_data(chain_data)
    embed = create_leaderboard_embed(leaderboard, current_hits)
    
    if not is_active:
        embed.description = "⚠️ No active chain found."
    
    await interaction.followup.send(embed=embed)

 

async def check_chain_status_periodically(faction_id: str = "53180"):
    """Periodically checks for an active chain and sends a notification if one starts."""
    notification_sent_for_current_chain = False
    
    while True:
        await asyncio.sleep(600)  # Check every 10 minutes
        
        chain_data = await get_chain_leaderboard(faction_id)
        if not chain_data:
            continue
            
        is_active = chain_data.get("current", 0) > 0
        
        if is_active and not notification_sent_for_current_chain:
            channel_id = bot.config.get("chain_notification_channel_id")
            if not channel_id:
                logger.warning("Chain detected, but no notification channel is set.")
                continue
                
            channel = bot.get_channel(channel_id)
            if not channel:
                logger.error(f"Could not find notification channel with ID {channel_id}.")
                continue
            
            embed = discord.Embed(
                title="🚨 Chain Started!",
                description="A faction chain has started! Time to attack!",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text="Powered by your friendly bot")
            
            try:
                await channel.send(embed=embed)
                notification_sent_for_current_chain = True
                logger.info(f"Sent chain start notification to channel {channel_id}.")
            except discord.Forbidden:
                logger.error(f"Missing permissions to send message in channel {channel_id}.")
            except Exception as e:
                logger.error(f"Failed to send chain notification: {e}")
                
        elif not is_active:
            notification_sent_for_current_chain = False
            
@bot.tree.command(name="show-config", description="Display the current bot configuration.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def show_config(interaction: discord.Interaction):
    """Shows the current bot configuration."""
    await interaction.response.defer(ephemeral=True)
    
    chain_channel_id = bot.config.get("chain_notification_channel_id")
    oc_channel_id = bot.config.get("oc_notification_channel_id")
    
    chain_channel = bot.get_channel(chain_channel_id) if chain_channel_id else None
    oc_channel = bot.get_channel(oc_channel_id) if oc_channel_id else None
    
    embed = discord.Embed(
        title="Bot Configuration",
        description="Current notification channel settings.",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="Chain Notification Channel",
        value=f"{chain_channel.mention if chain_channel else 'Not Set'}\nID: `{chain_channel_id}`",
        inline=False
    )

    embed.add_field(
        name="OC Notification Channel",
        value=f"{oc_channel.mention if oc_channel else 'Not Set'}\nID: `{oc_channel_id}`",
        inline=False
    )

    embed.add_field(
        name="OC Factions",
        value=", ".join(str(faction_id) for faction_id in get_configured_oc_faction_ids()),
        inline=False
    )

    embed.add_field(
        name="OC Reminder Hours",
        value=", ".join(str(hour) for hour in get_configured_oc_reminder_hours()),
        inline=False
    )
    
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="set-oc-channel", description="Set the organized crime notification channel.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def set_oc_channel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    await interaction.response.defer(ephemeral=True)

    target_channel = channel or interaction.channel
    if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send(
            "OC notifications must be sent to a text channel or thread.",
            ephemeral=True
        )
        return

    bot.config["oc_notification_channel_id"] = target_channel.id
    await save_config()
    await interaction.followup.send(
        f"OC notification channel set to {target_channel.mention}.",
        ephemeral=True
    )


@bot.tree.command(name="set-oc-factions", description="Set faction IDs monitored for organized crimes.")
@app_commands.describe(faction_ids="Comma-separated faction IDs, for example: 53180,55332")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def set_oc_factions(interaction: discord.Interaction, faction_ids: str):
    await interaction.response.defer(ephemeral=True)

    parsed_faction_ids: List[int] = []
    for raw_id in faction_ids.split(","):
        raw_id = raw_id.strip()
        if not raw_id:
            continue
        try:
            parsed_faction_ids.append(int(raw_id))
        except ValueError:
            await interaction.followup.send(
                f"Invalid faction ID: {raw_id}",
                ephemeral=True
            )
            return

    if not parsed_faction_ids:
        await interaction.followup.send(
            "Please provide at least one faction ID.",
            ephemeral=True
        )
        return

    bot.config["oc_faction_ids"] = sorted(set(parsed_faction_ids))
    await save_config()
    await interaction.followup.send(
        "OC factions set to: " + ", ".join(str(faction_id) for faction_id in bot.config["oc_faction_ids"]),
        ephemeral=True
    )


@bot.tree.command(name="set-oc-reminders", description="Set organized crime reminder hours.")
@app_commands.describe(hours="Comma-separated hours, for example: 12,24")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def set_oc_reminders(interaction: discord.Interaction, hours: str):
    await interaction.response.defer(ephemeral=True)

    parsed_hours: List[int] = []
    for raw_hour in hours.split(","):
        raw_hour = raw_hour.strip()
        if not raw_hour:
            continue
        try:
            parsed_hour = int(raw_hour)
        except ValueError:
            await interaction.followup.send(
                f"Invalid reminder hour: {raw_hour}",
                ephemeral=True
            )
            return

        if parsed_hour <= 0 or parsed_hour > 168:
            await interaction.followup.send(
                "Reminder hours must be between 1 and 168.",
                ephemeral=True
            )
            return
        parsed_hours.append(parsed_hour)

    if not parsed_hours:
        await interaction.followup.send(
            "Please provide at least one reminder hour.",
            ephemeral=True
        )
        return

    bot.config["oc_reminder_hours"] = sorted(set(parsed_hours))
    await save_config()
    await interaction.followup.send(
        "OC reminders set to: " + ", ".join(str(hour) for hour in bot.config["oc_reminder_hours"]) + " hours",
        ephemeral=True
    )


@bot.tree.command(name="oc-status", description="Show current organized crime monitor status.")
@app_commands.guild_only()
async def oc_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    faction_ids = get_configured_oc_faction_ids()
    embed = discord.Embed(
        title="Organized Crime Status",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )

    for faction_id in faction_ids:
        crimes = await get_organized_crimes(faction_id)
        if crimes is None:
            embed.add_field(
                name=f"Faction {faction_id}",
                value="Could not retrieve OC data.",
                inline=False
            )
            continue

        active_crimes = [
            crime for crime in crimes
            if normalize_oc_status(crime.get("status")) in OC_ACTIVE_STATUSES
        ]
        if not active_crimes:
            embed.add_field(
                name=f"Faction {faction_id}",
                value="No active, recruiting, planning, or available OCs.",
                inline=False
            )
            continue

        lines = []
        for crime in active_crimes[:5]:
            missing_users, open_slots = get_oc_unjoined_slots(crime)
            lines.append(
                f"{get_crime_display_name(crime)} - {normalize_oc_status(crime.get('status'))} "
                f"({len(missing_users)} missing, {len(open_slots)} open)"
            )

        embed.add_field(
            name=f"Faction {faction_id}",
            value="\n".join(lines),
            inline=False
        )

    await interaction.followup.send(embed=embed, ephemeral=True)

async def sync_faction_roles_periodically():
    """
    Periodically synchronizes faction roles for all members in all servers the bot is in.
    Runs every 30 minutes.
    """
    await bot.wait_until_ready()
    while not bot.is_closed():
        logger.info("Starting periodic faction role synchronization...")
        
        faction_map = {
            53180: "faction -I-",
            55332: "faction -II-"
        }
        
        id_pattern = re.compile(r'\[(\d+)\]$')

        for guild in bot.guilds:
            logger.info(f"Syncing roles for guild: {guild.name} ({guild.id})")
            
            role_i = discord.utils.get(guild.roles, name="faction -I-")
            role_ii = discord.utils.get(guild.roles, name="faction -II-")

            if not role_i or not role_ii:
                logger.warning(f"Skipping guild {guild.name} because faction roles ('faction -I-', 'faction -II-') were not found.")
                continue

            updated_members = 0
            
            for member in guild.members:
                if member.bot or not member.nick:
                    continue
                    
                match = id_pattern.search(member.nick)
                if not match:
                    continue
                
                torn_id = match.group(1)
                faction_id, api_success = await get_user_faction(torn_id)
                
                # Skip if API call failed (don't remove roles due to temporary API errors)
                if not api_success:
                    continue
                
                await asyncio.sleep(0.6) # API rate limit

                target_role_name = faction_map.get(faction_id) if faction_id else None
                roles_changed = False
                
                try:
                    if target_role_name == "faction -I-":
                        if role_ii in member.roles:
                            await member.remove_roles(role_ii, reason="Auto faction sync")
                            roles_changed = True
                        if role_i not in member.roles:
                            await member.add_roles(role_i, reason="Auto faction sync")
                            roles_changed = True
                    
                    elif target_role_name == "faction -II-":
                        if role_i in member.roles:
                            await member.remove_roles(role_i, reason="Auto faction sync")
                            roles_changed = True
                        if role_ii not in member.roles:
                            await member.add_roles(role_ii, reason="Auto faction sync")
                            roles_changed = True
                    
                    else: # Not in either faction (faction_id is None but API call succeeded)
                        if role_i in member.roles:
                            await member.remove_roles(role_i, reason="Auto faction sync")
                            roles_changed = True
                        if role_ii in member.roles:
                            await member.remove_roles(role_ii, reason="Auto faction sync")
                            roles_changed = True

                    if roles_changed:
                        updated_members += 1
                        logger.info(f"Updated roles for {member.display_name} ({member.id}) in {guild.name}.")
                        
                except discord.Forbidden:
                    logger.error(f"Permission error updating roles for {member.display_name} in {guild.name}.")
                except Exception as e:
                    logger.error(f"An unexpected error occurred while updating roles for {member.display_name}: {e}")

            logger.info(f"Faction role sync complete for {guild.name}. Updated {updated_members} members.")

        await asyncio.sleep(1800) # Wait 30 minutes

async def get_ranked_war_data(faction_id: str = "53180") -> Optional[Dict]:
    """Get ranked war data from Torn API."""
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.torn.com/faction/{faction_id}?selections=rankedwars&key={torn_api_key}"
            logger.info(f"Making API request to: {url[:80]}...")  # Don't log the full API key
            async with session.get(url) as response:
                logger.info(f"API response status: {response.status}")
                if response.status != 200:
                    logger.error(f"Ranked war API request failed with status {response.status}")
                    return None
                data = await response.json()
                logger.info(f"Raw API response: {json.dumps(data, indent=2)}")
                if 'error' in data:
                    logger.error(f"Ranked war API Error: {data['error']['error']}")
                    return None
                wars = data.get("rankedwars", {})
                logger.info(f"Found {len(wars)} ranked wars in API response")
                return wars
    except Exception as e:
        logger.error(f"Ranked war data error: {e}")
        return None

async def check_ranked_war_status_periodically(faction_id: str = "53180"):
    """Periodically checks for upcoming ranked wars and announces them."""
    logger.info(f"Starting ranked war monitoring for faction {faction_id}")
    logger.info(f"Current configuration: {bot.config}")
    
    while True:
        await asyncio.sleep(60)  # Check every 1 minute
        
        logger.info("=== Checking for ranked wars ===")
        war_data = await get_ranked_war_data(faction_id)
        if not war_data:
            logger.info("No war data returned from API. This is normal if no wars are scheduled.")
            pass

        logger.info(f"Processing {len(war_data)} wars from API. Current announced IDs: {bot.announced_war_ids}")
        
        relevant_war_ids = set()

        for war_id, war in war_data.items():
            war_details = war.get('war', {})
            war_start_timestamp = war_details.get('start', 0)
            war_end_timestamp = war_details.get('end', 0)
            current_timestamp = datetime.now(timezone.utc).timestamp()

            logger.info(f"Processing War ID: {war_id} | Start: {datetime.fromtimestamp(war_start_timestamp, tz=timezone.utc)} | End: {datetime.fromtimestamp(war_end_timestamp, tz=timezone.utc)}")

            # A war is relevant if it hasn't ended yet.
            if war_end_timestamp > current_timestamp:
                relevant_war_ids.add(war_id)

                # Announce if it's an UPCOMING war that hasn't been announced yet.
                if war_start_timestamp > current_timestamp:
                    if war_id not in bot.announced_war_ids:
                        logger.info(f"Found new UPCOMING war: {war_id}. Announcing...")
                        
                        channel_id = bot.config.get("war_notification_channel_id")
                        if not channel_id:
                            logger.warning(f"Upcoming war {war_id} detected, but no notification channel is set.")
                            continue

                        channel = bot.get_channel(channel_id)
                        if not channel:
                            logger.error(f"Could not find war notification channel with ID {channel_id}.")
                            continue

                        start_time_utc = datetime.fromtimestamp(war_start_timestamp, tz=timezone.utc)
                        seconds_until_start = max(0, (start_time_utc - datetime.now(timezone.utc)).total_seconds())

                        embed = discord.Embed(
                            title="⚔️ Upcoming Ranked War! ⚔️",
                            description="A new ranked war is on the horizon! Prepare for battle!",
                            color=discord.Color.orange()
                        )
                        embed.set_image(url="https://tenor.com/view/lets-go-charge-attack-battle-war-gif-21250118")
                        
                        factions = war.get('factions', {})
                        enemy_faction_name = "Unknown Faction"
                        for f_id, f_details in factions.items():
                            if f_id != faction_id:
                                enemy_faction_name = f_details.get('name', 'Unknown Faction')
                                break
                        
                        embed.add_field(name="Opponent", value=enemy_faction_name, inline=False)
                        embed.add_field(
                            name="War Starts In",
                            value=f"Countdown: {format_time_remaining(int(seconds_until_start))}\n" +
                                  f"Start Time: <t:{int(start_time_utc.timestamp())}:F>",
                            inline=False
                        )
                        
                        chain_data = {'organizer': 'Auto-Announced'}
                        view = ChainView(bot, chain_data)

                        try:
                            await channel.send(embed=embed, view=view)
                            bot.announced_war_ids.add(war_id)
                            logger.info(f"Successfully announced upcoming war {war_id} in channel {channel_id}.")
                        except Exception as e:
                            logger.error(f"Failed to send war announcement for war {war_id}: {e}", exc_info=True)
                    else:
                        logger.info(f"Upcoming war {war_id} has already been announced.")
                else:
                    logger.info(f"War {war_id} is already active, not announcing as 'upcoming'.")
            else:
                logger.info(f"War {war_id} has already ended.")

        # Clean up announced_war_ids for wars that are no longer relevant.
        obsolete_ids = bot.announced_war_ids - relevant_war_ids
        if obsolete_ids:
            logger.info(f"Clearing obsolete war IDs from announced set: {obsolete_ids}")
            bot.announced_war_ids -= obsolete_ids




bot.run(token, log_level=logging.INFO)
