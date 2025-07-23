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
    
    # Check if this is a war chain by looking at the original message's embed
    is_war_chain = "⚔️" in chain_message.embeds[0].title if chain_message.embeds else False
    
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
                title="⚔️ Upcoming War Chain ⚔️" if is_war_chain else "🔄 Upcoming Chain",
                description="A new war chain is being organized! Click the buttons below to indicate your participation:" if is_war_chain else "A new chain is being organized! Click the buttons below to indicate your participation:",
                color=discord.Color.red() if is_war_chain else discord.Color.gold()
            )
            
            if is_war_chain:
                embed.set_image(url="https://gifdb.com/images/high/theres-a-war-coming-text-7mhdnfq5009q4jg1.webp")
            
            # Format the chain start time field differently based on whether it's today/tomorrow or a future date
            now_utc = datetime.now(timezone.utc)
            if end_time_utc.date() == now_utc.date():
                time_str = "Today"
            elif end_time_utc.date() == (now_utc + timedelta(days=1)).date():
                time_str = "Tomorrow"
            else:
                time_str = end_time_utc.strftime("%d.%m.%Y")
            
            embed.add_field(
                name=f"{'War Chain' if is_war_chain else 'Chain'} Start Time",
                value=f"Countdown: {format_time_remaining(int(remaining))}\n" +
                      f"Date: {time_str}\n" +
                      f"Time: {end_time_utc.strftime('%H:%M')} TC\n" +
                      f"Your local time: <t:{timestamp}:F>",
                inline=False
            )
            
            joiners_text = "\n".join([f"• {name}" for _, name in view.joiners]) if view.joiners else "*No participants yet*"
            embed.add_field(
                name=f"{'Warriors Ready' if is_war_chain else 'Participants'} ({len(view.joiners)})",
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
                value="🟢 = Ready for battle!\n🔴 = Can't make it" if is_war_chain else "🟢 = I'll join the chain!\n🔴 = Can't make it",
                inline=False
            )
            
            embed.set_footer(text=f"{'War chain' if is_war_chain else 'Chain'} organized by {organizer_name}")
            
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
            title="⚔️ War Chain Starting! ⚔️" if is_war_chain else "🎯 Chain Starting!",
            description="Time's up! The war chain is starting now!" if is_war_chain else "Time's up! The chain is starting now!",
            color=discord.Color.red() if is_war_chain else discord.Color.green()
        )
        
        joiners_text = "\n".join([f"• {name}" for _, name in view.joiners]) if view.joiners else "*No participants*"
        final_embed.add_field(
            name=f"Final {'Warriors' if is_war_chain else 'Participants'} ({len(view.joiners)})",
            value=joiners_text,
            inline=False
        )
        
        if view.joiners:
            mentions = [f"<@{user_id}>" for user_id, _ in view.joiners]
            mentions_text = " ".join(mentions)
            if is_war_chain:
                await channel.send("https://tenor.com/view/lets-go-charge-attack-battle-war-gif-21250118")
                await channel.send(f"⚔️ War chain is starting! {mentions_text}")
            else:
                await channel.send(f"🔔 Chain is starting! {mentions_text}")
        
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

@bot.tree.command(name="warstart", description="Organize a war chain with a countdown timer")
@app_commands.describe(
    time_str="Time until chain starts: '5h', '30m', '18:00TC', or '18:00TC at DD.MM.YYYY' (e.g., '18:00TC at 25.12.2024')"
)
@app_commands.guild_only()
async def warstart(interaction: discord.Interaction, time_str: str):
    # Defer the response immediately to prevent timeout
    await interaction.response.defer()

    if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send(
            "War chains can only be created in text channels or threads!",
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
        title="⚔️ Upcoming War Chain ⚔️",
        description="A new war chain is being organized! Click the buttons below to indicate your participation:",
        color=discord.Color.red()
    )
    
    # Add the war GIF
    embed.set_image(url="https://tenor.com/view/lets-go-charge-attack-battle-war-gif-21250118")
    
    # Format the chain start time field differently based on whether it's today/tomorrow or a future date
    now_utc = datetime.now(timezone.utc)
    if end_time_utc.date() == now_utc.date():
        time_str = "Today"
    elif end_time_utc.date() == (now_utc + timedelta(days=1)).date():
        time_str = "Tomorrow"
    else:
        time_str = end_time_utc.strftime("%d.%m.%Y")
    
    embed.add_field(
        name="War Chain Start Time",
        value=f"Countdown: {format_time_remaining(seconds)}\n" +
              f"Date: {time_str}\n" +
              f"Time: {end_time_utc.strftime('%H:%M')} TC\n" +
              f"Your local time: <t:{timestamp}:F>",
        inline=False
    )
    
    embed.add_field(
        name="Warriors Ready",
        value="*No warriors have joined yet*",
        inline=False
    )
    
    embed.add_field(
        name="Options",
        value="🟢 = Ready for battle!\n🔴 = Can't make it",
        inline=False
    )
    
    embed.set_footer(text=f"War chain organized by {interaction.user.name}")
    
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
    logger.info(f"War chain started in channel {interaction.channel.id} by {interaction.user.name}")

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


bot.run(token, log_level=logging.INFO)