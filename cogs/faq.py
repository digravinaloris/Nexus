"""
Module auto-help / FAQ de Nexus (Cog discord.py).

Chargé par main.py via `await bot.load_extension("cogs.faq")` dans
setup_hook() -- comme cogs/music.py, ce module ne fait jamais `import main` :
il accède aux quelques dépendances partagées via l'objet `bot` lui-même
(voir setup() en bas de fichier) :
  - bot.db          -> la base MongoDB (exposée par main.py après init_mongo())
  - bot.get_config  -> pour lire la langue configurée du serveur (dashboard)

Stocke ses propres données dans la collection Mongo "faq", un document par
serveur, plutôt que dans la collection "config" de main.py -- évite de
toucher à get_config()/update_config() et garde le module entièrement
autonome, comme demandé pour ce chantier de découpage.
"""
import secrets
import discord
from discord import app_commands
from discord.ext import commands

from utils import looks_like_question, match_faq_entries

# Référence au bot, renseignée par setup() au chargement de l'extension.
_bot = None


def _faq_col():
    """Accès paresseux à la collection Mongo -- bot.db n'existe qu'une fois
    init_mongo() passé (dans on_ready(), après le chargement des cogs), donc
    on ne le lit jamais au niveau module, seulement au moment de la commande."""
    return _bot.db["faq"]


def _get_faq_doc(guild_id):
    doc = _faq_col().find_one({"guild_id": str(guild_id)})
    if not doc:
        doc = {"guild_id": str(guild_id), "enabled": False, "entries": []}
        _faq_col().insert_one(doc)
    return doc


def _save_entries(guild_id, entries):
    _faq_col().update_one(
        {"guild_id": str(guild_id)},
        {"$set": {"entries": entries}},
        upsert=True,
    )


def _new_entry_id(existing_entries):
    """4 caractères hex, en évitant les collisions avec les ids déjà pris sur ce serveur."""
    existing_ids = {e["id"] for e in existing_entries}
    while True:
        candidate = secrets.token_hex(2)
        if candidate not in existing_ids:
            return candidate


async def _require_admin(interaction: discord.Interaction) -> bool:
    """Check local (pas de dépendance à has_admin() de main.py, qui est un
    decorator-factory -- ceux-ci doivent être évalués à la définition de la
    classe, avant que _bot ne soit renseigné par setup())."""
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return False
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You need Administrator permission to use this command.", ephemeral=True)
        return False
    return True


class Faq(commands.Cog):
    faq_group = app_commands.Group(name="faq", description="Manage the auto-help FAQ — admin only")

    def __init__(self, bot):
        self.bot = bot

    # ---------------------------  Commands  ---------------------------

    @faq_group.command(name="add", description="Add a FAQ entry (comma-separated keywords + a response)")
    @app_commands.describe(keywords="Comma-separated trigger words, e.g. 'rules, reglement'", response="The answer the bot should post")
    async def faq_add(self, interaction: discord.Interaction, keywords: str, response: str):
        if not await _require_admin(interaction):
            return
        parsed_keywords = [k.strip() for k in keywords.split(",") if k.strip()]
        if not parsed_keywords:
            await interaction.response.send_message("❌ Give at least one keyword.", ephemeral=True)
            return
        doc = _get_faq_doc(interaction.guild_id)
        entry_id = _new_entry_id(doc["entries"])
        doc["entries"].append({"id": entry_id, "keywords": parsed_keywords, "response": response})
        _save_entries(interaction.guild_id, doc["entries"])
        embed = discord.Embed(title="✅ FAQ Entry Added", color=0x00cc00)
        embed.add_field(name="ID", value=f"`{entry_id}`", inline=True)
        embed.add_field(name="Keywords", value=", ".join(parsed_keywords), inline=True)
        embed.add_field(name="Response", value=response[:1000], inline=False)
        if not doc.get("enabled"):
            embed.set_footer(text="Auto-help is currently OFF — enable it with /faq toggle enabled:True")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @faq_group.command(name="edit", description="Edit a FAQ entry's keywords and/or response")
    @app_commands.describe(entry_id="The FAQ entry ID (see /faq list)", keywords="New comma-separated keywords (optional)", response="New response text (optional)")
    async def faq_edit(self, interaction: discord.Interaction, entry_id: str, keywords: str = None, response: str = None):
        if not await _require_admin(interaction):
            return
        if keywords is None and response is None:
            await interaction.response.send_message("❌ Provide new keywords and/or a new response.", ephemeral=True)
            return
        doc = _get_faq_doc(interaction.guild_id)
        entry = next((e for e in doc["entries"] if e["id"] == entry_id), None)
        if not entry:
            await interaction.response.send_message(f"❌ No FAQ entry with ID `{entry_id}`.", ephemeral=True)
            return
        if keywords is not None:
            parsed_keywords = [k.strip() for k in keywords.split(",") if k.strip()]
            if not parsed_keywords:
                await interaction.response.send_message("❌ Give at least one keyword.", ephemeral=True)
                return
            entry["keywords"] = parsed_keywords
        if response is not None:
            entry["response"] = response
        _save_entries(interaction.guild_id, doc["entries"])
        embed = discord.Embed(title="✅ FAQ Entry Updated", color=0x00cc00)
        embed.add_field(name="ID", value=f"`{entry_id}`", inline=True)
        embed.add_field(name="Keywords", value=", ".join(entry["keywords"]), inline=True)
        embed.add_field(name="Response", value=entry["response"][:1000], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @faq_group.command(name="remove", description="Remove a FAQ entry by its ID")
    @app_commands.describe(entry_id="The FAQ entry ID (see /faq list)")
    async def faq_remove(self, interaction: discord.Interaction, entry_id: str):
        if not await _require_admin(interaction):
            return
        doc = _get_faq_doc(interaction.guild_id)
        remaining = [e for e in doc["entries"] if e["id"] != entry_id]
        if len(remaining) == len(doc["entries"]):
            await interaction.response.send_message(f"❌ No FAQ entry with ID `{entry_id}`.", ephemeral=True)
            return
        _save_entries(interaction.guild_id, remaining)
        await interaction.response.send_message(f"🗑️ FAQ entry `{entry_id}` removed.", ephemeral=True)

    @faq_group.command(name="list", description="List all FAQ entries configured on this server")
    async def faq_list(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return
        doc = _get_faq_doc(interaction.guild_id)
        state = "🟢 On" if doc.get("enabled") else "⚪ Off"
        if not doc["entries"]:
            embed = discord.Embed(description=f"{state} — No FAQ entries yet. Add one with `/faq add`.", color=0x3399ff)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        lines = [f"`{e['id']}` — **{', '.join(e['keywords'])}** → {e['response'][:80]}" for e in doc["entries"]]
        embed = discord.Embed(title=f"📚 FAQ Entries ({state})", description="\n".join(lines)[:4000], color=0x3399ff)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @faq_group.command(name="toggle", description="Turn the auto-help FAQ on or off for this server")
    @app_commands.describe(enabled="True to enable, False to disable")
    async def faq_toggle(self, interaction: discord.Interaction, enabled: bool):
        if not await _require_admin(interaction):
            return
        _faq_col().update_one(
            {"guild_id": str(interaction.guild_id)},
            {"$set": {"enabled": enabled}},
            upsert=True,
        )
        state = "🟢 enabled" if enabled else "⚪ disabled"
        await interaction.response.send_message(f"Auto-help FAQ is now **{state}**.", ephemeral=True)

    # ---------------------------  Detection  ---------------------------

    @commands.Cog.listener("on_message")
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        doc = _get_faq_doc(message.guild.id)
        if not doc.get("enabled") or not doc["entries"]:
            return
        if not looks_like_question(message.content):
            return
        matches = match_faq_entries(message.content, doc["entries"])
        if not matches:
            return

        if len(matches) == 1:
            await message.channel.send(matches[0]["response"][:2000])
            return

        cfg = self.bot.get_config(message.guild.id)
        is_french = cfg.get("language") == "fr"
        header = f"J'ai trouvé {len(matches)} choses qui peuvent t'aider :" if is_french else f"I found {len(matches)} things that might help:"
        lines = [header] + [f"• {m['response']}" for m in matches[:5]]
        await message.channel.send("\n".join(lines)[:2000])


async def setup(bot):
    """Point d'entrée de l'extension, appelé par bot.load_extension()."""
    global _bot
    _bot = bot
    await bot.add_cog(Faq(bot))
