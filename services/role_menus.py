"""Discord UI components for persistent self-service role menus."""
import discord

from stores.role_menu_store import get_role_menu


class RoleMenuSelect(discord.ui.Select):
    """Menu déroulant persistant : les options réellement affichées sont
    déjà figées dans le message Discord une fois posté, donc un placeholder
    suffit ici pour l'enregistrement persistant au démarrage — la vraie
    liste de rôles est relue depuis Mongo (par message_id) à chaque clic,
    ce qui marche même après un redémarrage du bot."""
    def __init__(self, options=None):
        super().__init__(
            placeholder="Select your roles...",
            min_values=0,
            max_values=len(options) if options else 1,
            options=options or [discord.SelectOption(label="placeholder", value="0")],
            custom_id="nexus_role_menu_select",
        )

    async def callback(self, interaction: discord.Interaction):
        menu = get_role_menu(interaction.message.id)
        if not menu:
            await interaction.response.send_message("This role menu is no longer configured.", ephemeral=True)
            return
        all_ids = {item["role_id"] for item in menu["items"]}
        selected_ids = {int(v) for v in self.values}
        member = interaction.user
        to_add = [interaction.guild.get_role(rid) for rid in selected_ids if interaction.guild.get_role(rid)]
        to_remove = [interaction.guild.get_role(rid) for rid in (all_ids - selected_ids) if interaction.guild.get_role(rid)]
        try:
            if to_add:
                await member.add_roles(*to_add, reason="Role menu")
            if to_remove:
                await member.remove_roles(*to_remove, reason="Role menu")
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Couldn't update your roles: {e}", ephemeral=True)
            return
        await interaction.response.send_message("✅ Your roles have been updated.", ephemeral=True)


class RoleMenuView(discord.ui.View):
    def __init__(self, options=None):
        super().__init__(timeout=None)
        self.add_item(RoleMenuSelect(options))

