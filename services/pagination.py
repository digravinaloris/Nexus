"""Reusable Discord embed pagination view."""
import discord


class PaginatedEmbedView(discord.ui.View):
    """Vue générique de pagination par boutons — réutilisée par toutes les
    commandes qui listent potentiellement beaucoup d'entrées (mutelist,
    warnlist, banlist, history...), pour ne jamais dépasser les limites
    d'un embed Discord."""
    def __init__(self, title, lines, color=0x3399ff, per_page=10, author_id=None):
        super().__init__(timeout=120)
        self.title = title
        self.lines = lines
        self.color = color
        self.per_page = per_page
        self.page = 0
        self.author_id = author_id
        self.max_page = max(0, (len(lines) - 1) // per_page)
        self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran this command can navigate pages.", ephemeral=True
            )
            return False
        return True

    def _update_buttons(self):
        self.previous_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.max_page

    def build_embed(self):
        start = self.page * self.per_page
        chunk = self.lines[start:start + self.per_page]
        embed = discord.Embed(title=self.title, description="\n".join(chunk) or "—", color=self.color)
        if self.max_page > 0:
            embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1}")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)





