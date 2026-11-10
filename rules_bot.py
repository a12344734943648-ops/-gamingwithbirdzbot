import os
import re
import time
import sqlite3
import unicodedata
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord import app_commands

try:
    from langdetect import detect_langs  # pip install langdetect
except ImportError:
    detect_langs = None

# ============================ CONFIG ============================
BOT_NAME = "gamingwithbirdz bot"
TOKEN = os.environ["DISCORD_TOKEN"]          # set as an environment variable
LOG_CHANNEL_ID = 0                           # mod-log channel ID (also receives appeals)
ANNOUNCEMENT_CHANNEL_IDS = set()             # channels only staff may post in
ENGLISH_ONLY_CHANNEL_IDS = set()             # empty = check every channel

# Fill in with words you want blocked (hate speech, slurs, etc.)
BLOCKED_WORDS = ["exampleword1", "exampleword2"]

# Racial slurs: the n-word and its variations are built in. Add other slurs to
# EXTRA_SLURS (plain words); they get the same filter that catches leetspeak,
# spacing, repeated letters and look-alike characters. See also /setup_automod.
SLUR_ACTION = "ban"      # "ban" = instant ban, "strike" = normal strike ladder
EXTRA_SLURS = []

SCAM_PATTERNS = [r"free\s+nitro", r"steamcommunity\.com/gift", r"claim\s+your\s+gift"]
INVITE_RE = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite)/\w+", re.I)

STRIKE_EXPIRY_DAYS = 30
# strikes -> timeout length (None = warning only). 4+ uses the last value.
LADDER = {1: None, 2: timedelta(minutes=10), 3: timedelta(hours=1), 4: timedelta(hours=24)}

RULES = [
    "**1. Follow Discord's ToS and Community Guidelines.** Breaking Discord's rules can get the whole server in trouble.",
    "**2. Be respectful.** No harassment, hate speech, racism, sexism or personal attacks. Treat everyone with basic decency.",
    "**3. Use the right channels.** Off-topic stuff belongs in general/off-topic, not in game or announcement channels.",
    "**4. English is the main language.** Other languages are fine in small amounts, but don't exclude people.",
    "**5. Staff decisions are final.** Don't argue with staff in public. If you think a decision was wrong, use /appeal.",
    "**6. Have fun, but don't be a problem.** Light roasting is fine; being a consistent jerk is not.",
]
# ================================================================

db = sqlite3.connect("strikes.db")
db.execute(
    "CREATE TABLE IF NOT EXISTS strikes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "guild_id INTEGER, user_id INTEGER, rule INTEGER, reason TEXT, ts REAL)"
)
db.commit()

intents = discord.Intents.default()
intents.message_content = True
intents.auto_moderation_execution = True


class RulesBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


bot = RulesBot()
recent = defaultdict(lambda: deque(maxlen=5))   # spam tracking
lang_cooldown = {}                              # language reminder cooldown


# ---------------------- slur detection ----------------------
LEET = str.maketrans({"1": "i", "!": "i", "|": "i", "3": "e", "4": "a", "@": "a",
                      "0": "o", "$": "s", "5": "s", "7": "t"})

# n-word variants (n1gg3r, n i g g a, niqqa, ...). Needs 2+ g's so "Niger" is safe,
# the leading boundary keeps "snigger" safe, and "niggardly" is excluded.
NWORD_RE = re.compile(r"(?<![a-z])n+[\W_]*(?:i+[\W_]*)?[gq]+[\W_]*[gq]+[\W_]*(?:e|u|a(?!rd))")


def normalize(text):
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub("[\u200b-\u200f\u2060\ufeff\u00ad]", "", text)  # zero-width chars
    return text.lower().translate(LEET)


def loose_pattern(word):
    letters = [re.escape(c) + "+" for c in word.lower() if c.isalpha()]
    return re.compile(r"(?<![a-z])" + r"[\W_]*".join(letters) + r"(?:s|z)?(?![a-z])")


EXTRA_SLUR_PATTERNS = [loose_pattern(w) for w in EXTRA_SLURS]


def is_slur(text):
    t = normalize(text)
    return bool(NWORD_RE.search(t)) or any(p.search(t) for p in EXTRA_SLUR_PATTERNS)


# ------------------------- helpers -------------------------
def active_strikes(guild_id, user_id):
    cutoff = time.time() - STRIKE_EXPIRY_DAYS * 86400
    return db.execute(
        "SELECT rule, reason, ts FROM strikes WHERE guild_id=? AND user_id=? AND ts>? ORDER BY ts",
        (guild_id, user_id, cutoff),
    ).fetchall()


def add_strike(guild_id, user_id, rule, reason):
    db.execute(
        "INSERT INTO strikes (guild_id, user_id, rule, reason, ts) VALUES (?,?,?,?,?)",
        (guild_id, user_id, rule, reason, time.time()),
    )
    db.commit()
    return len(active_strikes(guild_id, user_id))


async def log(guild, embed):
    ch = guild.get_channel(LOG_CHANNEL_ID)
    if ch:
        await ch.send(embed=embed)


async def punish(member, rule, reason, content=None):
    count = add_strike(member.guild.id, member.id, rule, reason)
    duration = LADDER[min(count, max(LADDER))]
    action = "Warning"
    if duration:
        try:
            await member.timeout(duration, reason=f"Rule {rule}: {reason}")
            action = f"Timeout ({duration})"
        except discord.Forbidden:
            action = "Timeout failed (missing permissions)"

    try:
        await member.send(
            f"You received a strike in **{member.guild.name}**.\n"
            f"Rule {rule}: {reason}\nStrikes: {count}. Action: {action}.\n"
            f"Think it's a mistake? Use /appeal in the server.\n- {BOT_NAME}"
        )
    except discord.HTTPException:
        pass

    embed = discord.Embed(title="Auto-moderation", color=discord.Color.orange())
    embed.add_field(name="User", value=f"{member.mention} ({member.id})")
    embed.add_field(name="Rule", value=str(rule))
    embed.add_field(name="Strikes", value=str(count))
    embed.add_field(name="Action", value=action, inline=False)
    embed.add_field(name="Reason", value=reason, inline=False)
    if content:
        embed.add_field(name="Message", value=content[:1000], inline=False)
    embed.set_footer(text=BOT_NAME)
    await log(member.guild, embed)


async def slur_action(member, content):
    if SLUR_ACTION == "ban":
        try:
            await member.send(
                f"You were banned from **{member.guild.name}** for using a racial slur (Rule 2). "
                f"If you think this was a mistake, contact a moderator.\n- {BOT_NAME}"
            )
        except discord.HTTPException:
            pass
        try:
            await member.ban(reason="Racial slur (Rule 2)", delete_message_seconds=3600)
        except discord.HTTPException:
            pass  # missing permission or role order: falls back to a strike below
        else:
            embed = discord.Embed(title="Auto-ban: racial slur", color=discord.Color.red())
            embed.add_field(name="User", value=f"{member} ({member.id})")
            embed.add_field(name="Message", value=f"||{content[:900]}||", inline=False)
            embed.set_footer(text=BOT_NAME)
            await log(member.guild, embed)
            return
    await punish(member, 2, "Racial slur", content)


def check_hard(message):
    """Violations that earn a strike. Returns (rule, reason) or None."""
    text = message.content
    low = text.lower()

    if INVITE_RE.search(text):
        return 1, "Posting server invite links"
    if any(re.search(p, low) for p in SCAM_PATTERNS):
        return 1, "Scam or phishing content"

    for w in BLOCKED_WORDS:
        if re.search(rf"\b{re.escape(w.lower())}\b", low):
            return 2, "Prohibited language"

    if len(message.mentions) >= 5:
        return 6, "Mass pinging"

    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 15 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        return 6, "Excessive caps"

    q = recent[(message.guild.id, message.author.id)]
    now = time.time()
    q.append(now)
    if len(q) == q.maxlen and now - q[0] < 6:
        q.clear()
        return 6, "Spamming"
    return None


def looks_non_english(text):
    if not detect_langs:
        return False
    clean = re.sub(r"https?://\S+|<[@#:][^>]+>|[^\w\s]", "", text)
    if len(clean) < 40:
        return False
    try:
        top = detect_langs(clean)[0]
    except Exception:
        return False
    return top.lang != "en" and top.prob > 0.95


# ------------------------- events -------------------------
async def handle(message):
    if message.author.bot or not message.guild:
        return
    if message.author.guild_permissions.moderate_members:
        return  # staff are exempt

    # Rule 2: racial slurs get an instant ban (see SLUR_ACTION)
    if is_slur(message.content):
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        await slur_action(message.author, message.content)
        return

    # Rule 3: staff-only channels (no strike, just removal)
    if message.channel.id in ANNOUNCEMENT_CHANNEL_IDS:
        await message.delete()
        await message.channel.send(
            f"{message.author.mention} that channel is staff-only. (Rule 3)", delete_after=8
        )
        return

    hit = check_hard(message)
    if hit:
        rule, reason = hit
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        await punish(message.author, rule, reason, message.content)
        return

    # Rule 4: soft reminder only, with a 10-minute cooldown per user
    in_scope = not ENGLISH_ONLY_CHANNEL_IDS or message.channel.id in ENGLISH_ONLY_CHANNEL_IDS
    if in_scope and looks_non_english(message.content):
        last = lang_cooldown.get(message.author.id, 0)
        if time.time() - last > 600:
            lang_cooldown[message.author.id] = time.time()
            await message.reply(
                "Friendly reminder: English is the main language here so everyone can follow along. (Rule 4)",
                delete_after=15,
            )


@bot.event
async def on_ready():
    print(f"{BOT_NAME} is online as {bot.user}")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="the rules | /rules")
    )


@bot.event
async def on_message(message):
    await handle(message)


@bot.event
async def on_message_edit(before, after):
    if before.content != after.content:
        await handle(after)


# ------------------------- commands -------------------------
@bot.tree.command(name="rules", description="Show the server rules")
async def rules(interaction: discord.Interaction):
    embed = discord.Embed(title="Server Rules", description="\n\n".join(RULES), color=discord.Color.blurple())
    embed.set_footer(text=BOT_NAME)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="appeal", description="Appeal a staff decision or strike (Rule 5)")
@app_commands.describe(reason="What happened and why you think it was a mistake")
async def appeal(interaction: discord.Interaction, reason: str):
    embed = discord.Embed(title="Appeal", color=discord.Color.blue())
    embed.add_field(name="User", value=f"{interaction.user.mention} ({interaction.user.id})")
    embed.add_field(name="Strikes", value=str(len(active_strikes(interaction.guild.id, interaction.user.id))))
    embed.add_field(name="Reason", value=reason[:1000], inline=False)
    embed.set_footer(text=BOT_NAME)
    await log(interaction.guild, embed)
    await interaction.response.send_message("Your appeal was sent to the mods.", ephemeral=True)


@bot.tree.command(name="warn", description="Give a member a strike (staff only)")
@app_commands.describe(member="Who to warn", rule="Rule number (1-6)", reason="Why")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, member: discord.Member, rule: app_commands.Range[int, 1, 6], reason: str):
    await punish(member, rule, reason)
    await interaction.response.send_message(f"Strike added for {member.mention}.", ephemeral=True)


@bot.tree.command(name="strikes", description="View a member's active strikes (staff only)")
@app_commands.checks.has_permissions(moderate_members=True)
async def strikes(interaction: discord.Interaction, member: discord.Member):
    rows = active_strikes(interaction.guild.id, member.id)
    if not rows:
        await interaction.response.send_message(f"{member.mention} has no active strikes.", ephemeral=True)
        return
    lines = [f"<t:{int(ts)}:d> Rule {rule}: {reason}" for rule, reason, ts in rows]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="clearstrikes", description="Clear a member's strikes (staff only)")
@app_commands.checks.has_permissions(moderate_members=True)
async def clearstrikes(interaction: discord.Interaction, member: discord.Member):
    db.execute("DELETE FROM strikes WHERE guild_id=? AND user_id=?", (interaction.guild.id, member.id))
    db.commit()
    await interaction.response.send_message(f"Cleared strikes for {member.mention}.", ephemeral=True)


@bot.tree.command(name="setup_automod", description="Turn on Discord's built-in slur filter (admin only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_automod(interaction: discord.Interaction):
    try:
        await interaction.guild.create_automod_rule(
            name=f"{BOT_NAME} slur filter",
            event_type=discord.AutoModRuleEventType.message_send,
            trigger=discord.AutoModTrigger(
                type=discord.AutoModRuleTriggerType.keyword_preset,
                presets=discord.AutoModPresets(slurs=True),
            ),
            actions=[discord.AutoModRuleAction(
                type=discord.AutoModRuleActionType.block_message,
                custom_message="Slurs aren't allowed here.",
            )],
            enabled=True,
            reason="Slur filter",
        )
    except discord.HTTPException as e:
        await interaction.response.send_message(f"Couldn't create the rule: {e}", ephemeral=True)
        return
    await interaction.response.send_message("Slur filter enabled.", ephemeral=True)


@bot.event
async def on_automod_action_execution(execution):
    # Discord's slur filter blocked a message: apply the same punishment as our own filter
    if execution.rule_trigger_type != discord.AutoModRuleTriggerType.keyword_preset:
        return
    if execution.action.type != discord.AutoModRuleActionType.block_message:
        return
    member = execution.member
    if member is None or member.bot or member.guild_permissions.moderate_members:
        return
    try:
        rule = await execution.guild.fetch_automod_rule(execution.rule_id)
    except discord.HTTPException:
        return
    if rule.trigger.presets and rule.trigger.presets.slurs:
        await slur_action(member, execution.content or execution.matched_content or "")


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("Staff only.", ephemeral=True)
    else:
        raise error


bot.run(TOKEN)
