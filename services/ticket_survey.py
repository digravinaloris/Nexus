"""Ticket satisfaction feedback UI sent after ticket closure."""
import discord

from audit_services.audit import record_audit


class SatisfactionSurveyView(discord.ui.View):
    """DM envoyé au propriétaire du ticket une fois celui-ci fermé."""
    def __init__(self):
        super().__init__(timeout=None)

    async def _record(self, interaction: discord.Interaction, satisfied: bool):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        record_audit("global", str(interaction.user.id), str(interaction.user),
                      "Ticket satisfaction feedback", "👍 satisfied" if satisfied else "👎 not satisfied")
        await interaction.followup.send("Thanks for the feedback! 🙏", ephemeral=True)

    @discord.ui.button(label="Good experience", style=discord.ButtonStyle.success, emoji="👍", custom_id="nexus_satisfaction_up")
    async def satisfied(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, True)

    @discord.ui.button(label="Could be better", style=discord.ButtonStyle.secondary, emoji="👎", custom_id="nexus_satisfaction_down")
    async def unsatisfied(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, False)

