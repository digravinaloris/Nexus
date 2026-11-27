"""Interactive first-run configuration wizard for Discord servers."""
import discord

from stores.config_store import update_config


class SetupWizardView(discord.ui.View):
    """Assistant de configuration guidé : évite d'avoir à connaître chaque
    commande /config une par une. Chaque sélecteur applique son réglage
    immédiatement, pour que l'assistant reste utile même si on le quitte
    en cours de route."""
    def __init__(self, guild_id, author_id):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who started the setup can use it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="1. Pick your logs channel",
        min_values=1,
        max_values=1,
    )
    async def pick_logs(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel = select.values[0]
        update_config(self.guild_id, "logs_channel", channel.name)
        await interaction.response.send_message(f"✅ Logs channel set to **#{channel.name}**.", ephemeral=True)

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="2. Pick an auto-role for new members (optional)",
        min_values=0,
        max_values=1,
    )
    async def pick_autorole(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if not select.values:
            update_config(self.guild_id, "autorole", None)
            await interaction.response.send_message("✅ Auto-role cleared.", ephemeral=True)
            return
        role = select.values[0]
        risky = ("administrator", "manage_guild", "ban_members", "kick_members", "manage_roles")
        update_config(self.guild_id, "autorole", role.id)
        warning = ""
        if any(getattr(role.permissions, p) for p in risky):
            warning = "\n⚠️ Careful: this role has sensitive permissions and will be given to **every** new member."
        await interaction.response.send_message(f"✅ Auto-role set to **{role.name}**.{warning}", ephemeral=True)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="3. Pick a channel for sanction appeals (optional)",
        min_values=0,
        max_values=1,
    )
    async def pick_appeals(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        if not select.values:
            update_config(self.guild_id, "appeal_channel_id", None)
            await interaction.response.send_message("✅ Appeals disabled.", ephemeral=True)
            return
        channel = select.values[0]
        update_config(self.guild_id, "appeal_channel_id", channel.id)
        await interaction.response.send_message(
            f"✅ Sanction appeals will be reviewed in **#{channel.name}**. Sanction DMs now include an appeal button.",
            ephemeral=True,
        )

    @discord.ui.button(label="Enable anti-nuke", style=discord.ButtonStyle.success, emoji="🛡️", row=3)
    async def enable_antinuke(self, interaction: discord.Interaction, button: discord.ui.Button):
        update_config(self.guild_id, "antinuke_enabled", True)
        button.disabled = True
        button.label = "Anti-nuke enabled"
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            "✅ Anti-nuke enabled. Make sure the bot has the **View Audit Log** permission for it to work.",
            ephemeral=True,
        )

    @discord.ui.button(label="Finish", style=discord.ButtonStyle.primary, emoji="✅", row=3)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="✅ **Setup complete.** Run `/config view` any time to check your settings, or `/setup` again to change them.",
            embed=None,
            view=self,
        )
        self.stop()

