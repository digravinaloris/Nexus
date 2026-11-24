"""Calendar Cog: guild events with role mentions and centralized reminders."""
import asyncio
import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import discord
from discord import app_commands
from discord.ext import commands

UTC = dt.timezone.utc
DEFAULT_TZ = "Europe/Paris"
REMINDERS = (("24h", dt.timedelta(hours=24)), ("1h", dt.timedelta(hours=1)))


def validate_timezone(name):
    try:
        ZoneInfo((name or "").strip())
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("Invalid IANA timezone (example: Europe/Paris).") from None
    return name.strip()


def parse_event_datetime(date_text, time_text, timezone_name, now=None):
    zone = ZoneInfo(validate_timezone(timezone_name))
    try:
        day = dt.date.fromisoformat(date_text.strip())
    except (ValueError, AttributeError):
        raise ValueError("Date must be YYYY-MM-DD.") from None
    hour, minute = 0, 0
    if time_text:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_text.strip()):
            raise ValueError("Time must be HH:MM in 24-hour format.")
        hour, minute = map(int, time_text.strip().split(":"))
    local = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=zone)
    if local.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
        raise ValueError("This local time does not exist due to daylight-saving time.")
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if local.astimezone(UTC) <= now.astimezone(UTC):
        raise ValueError("Events must be in the future.")
    return local.astimezone(UTC)


def local_when(event):
    local = event["starts_at"].astimezone(ZoneInfo(event.get("timezone", DEFAULT_TZ)))
    return local.strftime("%Y-%m-%d %H:%M %Z") if event.get("time_set") else local.strftime("%Y-%m-%d (time unspecified)")


def make_embed(event, reminder=None):
    e = discord.Embed(title="📅 " + event["title"][:100], description=(event.get("description") or "No description.")[:1000], color=0x5865F2)
    e.add_field(name="When", value=local_when(event), inline=True)
    e.add_field(name="Role", value=f"<@&{event['role_id']}>", inline=True)
    if reminder:
        e.set_footer(text="Reminder: " + reminder)
    return e


class Calendar(commands.Cog):
    calendar = app_commands.Group(name="calendar", description="Manage server calendar events")

    def __init__(self, bot):
        self.bot = bot
        self.task = None
        self._indexes_ready = False

    def col(self):
        collection = self.bot.db["calendar_events"]
        if not self._indexes_ready:
            collection.create_index([("guild_id", 1), ("starts_at", 1)])
            collection.create_index([("guild_id", 1), ("title", 1), ("starts_at", 1)], unique=True)
            self._indexes_ready = True
        return collection

    def timezone(self, gid):
        return self.bot.get_config(gid).get("calendar_timezone") or DEFAULT_TZ

    async def allowed(self, i):
        if not i.guild or not isinstance(i.user, discord.Member):
            await i.response.send_message("Server-only command.", ephemeral=True)
            return False
        if i.user.guild_permissions.administrator:
            return True
        cfg = self.bot.get_config(i.guild_id)
        ids = {int(x) for x in cfg.get("calendar_manager_role_ids", []) if str(x).isdigit()}
        if ids.intersection(r.id for r in i.user.roles):
            return True
        return await self.bot.check_access(i, "calendar", "manage_guild")

    def role_ok(self, guild, role):
        role = guild.get_role(role.id)
        if role is None or role.is_default():
            raise ValueError("Select a valid role, not @everyone.")
        return role

    def channel(self, guild, event):
        configured = self.bot.get_config(guild.id).get("calendar_channel_id")
        return (guild.get_channel(int(configured)) if configured else None) or guild.get_channel(int(event["channel_id"]))

    async def post(self, channel, event, reminder=None):
        role = channel.guild.get_role(int(event["role_id"]))
        if role is None:
            return False
        try:
            await channel.send(content=role.mention, embed=make_embed(event, reminder),
                              allowed_mentions=discord.AllowedMentions(roles=[role], users=False, everyone=False))
            return True
        except discord.HTTPException:
            return False

    @calendar.command(name="add", description="Create and announce an event")
    @app_commands.describe(title="Event name", role="Role to notify", date="YYYY-MM-DD", time="Optional HH:MM", description="Optional description")
    async def add(self, i: discord.Interaction, title: str, role: discord.Role, date: str, time: str = None, description: str = None):
        if not await self.allowed(i): return
        title, desc = title.strip(), (description or "").strip()
        if not title or len(title) > 100 or len(desc) > 1000:
            await i.response.send_message("Title must be 1–100 chars and description max 1000.", ephemeral=True); return
        try:
            role = self.role_ok(i.guild, role)
            zone = validate_timezone(self.timezone(i.guild_id))
            start = parse_event_datetime(date, time, zone)
        except ValueError as exc:
            await i.response.send_message(str(exc), ephemeral=True); return
        event = {"guild_id": str(i.guild_id), "title": title, "role_id": str(role.id), "description": desc,
                 "starts_at": start, "timezone": zone, "time_set": bool(time), "channel_id": str(i.channel_id),
                 "reminders_sent": [], "created_by": str(i.user.id), "created_at": dt.datetime.now(UTC)}
        if self.col().find_one({"guild_id": event["guild_id"], "title": title, "starts_at": start}):
            await i.response.send_message("Duplicate event (same title and date).", ephemeral=True); return
        event["_id"] = self.col().insert_one(event).inserted_id
        ch = self.channel(i.guild, event)
        sent = bool(ch and await self.post(ch, event))
        await i.response.send_message("Event saved. Announcement " + ("sent." if sent else "not sent (channel unavailable)."), ephemeral=True)

    @calendar.command(name="list", description="Show upcoming events")
    async def list_events(self, i: discord.Interaction):
        if not await self.allowed(i): return
        items = list(self.col().find({"guild_id": str(i.guild_id), "starts_at": {"$gte": dt.datetime.now(UTC)}}).sort("starts_at", 1).limit(15))
        if not items:
            await i.response.send_message("No upcoming events.", ephemeral=True); return
        lines = [f"**{e['title']}** — {local_when(e)} · <@&{e['role_id']}>" for e in items]
        await i.response.send_message(embed=discord.Embed(title="Upcoming events", description="\n".join(lines)[:4000]),
                                    ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @calendar.command(name="edit", description="Edit an event")
    @app_commands.describe(event_id="MongoDB event ID", title="New title", role="New role", date="New date YYYY-MM-DD", time="New time HH:MM", description="New description (empty clears)")
    async def edit(self, i: discord.Interaction, event_id: str, title: str = None, role: discord.Role = None, date: str = None, time: str = None, description: str = None):
        if not await self.allowed(i): return
        if all(x is None for x in (title, role, date, time, description)):
            await i.response.send_message("Provide at least one field to edit.", ephemeral=True); return
        from bson import ObjectId
        try: oid = ObjectId(event_id)
        except Exception:
            await i.response.send_message("Invalid event ID.", ephemeral=True); return
        event = self.col().find_one({"_id": oid, "guild_id": str(i.guild_id)})
        if not event:
            await i.response.send_message("Event not found.", ephemeral=True); return
        changes = {}
        if title is not None:
            title = title.strip()
            if not title or len(title) > 100:
                await i.response.send_message("Title must be 1–100 characters.", ephemeral=True); return
            changes["title"] = title
        if role is not None:
            try: changes["role_id"] = str(self.role_ok(i.guild, role).id)
            except ValueError as exc:
                await i.response.send_message(str(exc), ephemeral=True); return
        if date is not None or time is not None:
            zone = event.get("timezone", DEFAULT_TZ); old = event["starts_at"].astimezone(ZoneInfo(zone))
            try: changes["starts_at"] = parse_event_datetime(date or old.date().isoformat(), time if time is not None else (old.strftime("%H:%M") if event.get("time_set") else None), zone)
            except ValueError as exc:
                await i.response.send_message(str(exc), ephemeral=True); return
            changes["time_set"] = bool(time) if time is not None else event.get("time_set", False)
            changes["reminders_sent"] = []
        if description is not None:
            if len(description) > 1000:
                await i.response.send_message("Description max 1000 characters.", ephemeral=True); return
            changes["description"] = description.strip()
        self.col().update_one({"_id": oid, "guild_id": str(i.guild_id)}, {"$set": changes})
        await i.response.send_message("Event updated.", ephemeral=True)

    @calendar.command(name="delete", description="Delete an event")
    async def delete(self, i: discord.Interaction, event_id: str):
        if not await self.allowed(i): return
        from bson import ObjectId
        try: oid = ObjectId(event_id)
        except Exception:
            await i.response.send_message("Invalid event ID.", ephemeral=True); return
        result = self.col().delete_one({"_id": oid, "guild_id": str(i.guild_id)})
        await i.response.send_message("Event deleted." if result.deleted_count else "Event not found.", ephemeral=True)

    @calendar.command(name="timezone", description="Set the server calendar timezone")
    async def set_timezone(self, i: discord.Interaction, timezone: str):
        if not await self.allowed(i): return
        try: name = validate_timezone(timezone)
        except ValueError as exc:
            await i.response.send_message(str(exc), ephemeral=True); return
        from config_store import update_config
        update_config(i.guild_id, "calendar_timezone", name)
        await i.response.send_message("Timezone set: " + name, ephemeral=True)

    @calendar.command(name="channel", description="Set or clear the calendar announcements channel")
    async def set_channel(self, i: discord.Interaction, channel: discord.TextChannel = None):
        if not await self.allowed(i): return
        from config_store import update_config
        update_config(i.guild_id, "calendar_channel_id", str(channel.id) if channel else None)
        await i.response.send_message("Calendar announcement channel updated.", ephemeral=True)

    @calendar.command(name="manager", description="Add/remove calendar manager roles")
    @app_commands.choices(action=[app_commands.Choice(name="add", value="add"), app_commands.Choice(name="remove", value="remove")])
    async def manager(self, i: discord.Interaction, role: discord.Role, action: str):
        if not await self.allowed(i): return
        if not i.user.guild_permissions.administrator or role.is_default():
            await i.response.send_message("Admins only; @everyone cannot be a manager role.", ephemeral=True); return
        from config_store import get_config, update_config
        roles = set(map(str, get_config(i.guild_id).get("calendar_manager_role_ids", [])))
        roles.add(str(role.id)) if action == "add" else roles.discard(str(role.id))
        update_config(i.guild_id, "calendar_manager_role_ids", sorted(roles))
        await i.response.send_message("Calendar manager roles saved.", ephemeral=True)

    async def reminder_pass(self):
        now = dt.datetime.now(UTC)
        due = list(self.col().find({"starts_at": {"$gt": now, "$lte": now + dt.timedelta(hours=25)}}).limit(100))
        for event in due:
            guild = self.bot.get_guild(int(event["guild_id"]))
            ch = self.channel(guild, event) if guild else None
            if not ch: continue
            remaining = event["starts_at"] - now
            for key, offset in REMINDERS:
                if remaining <= offset and key not in event.get("reminders_sent", []):
                    if await self.post(ch, event, key + " before"):
                        self.col().update_one({"_id": event["_id"], "reminders_sent": {"$ne": key}}, {"$addToSet": {"reminders_sent": key}})
                        event.setdefault("reminders_sent", []).append(key)

    async def scheduler(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try: await self.reminder_pass()
            except Exception as exc: print("[CALENDAR] scheduler pass failed: " + str(exc), flush=True)
            await asyncio.sleep(60)

    async def cog_load(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.scheduler(), name="calendar-reminders")

    async def cog_unload(self):
        if self.task: self.task.cancel()


async def setup(bot):
    await bot.add_cog(Calendar(bot))
