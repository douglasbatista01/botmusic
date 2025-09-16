# -*- coding: utf-8 -*-

import os
import json
import logging
import discord
from discord import ui
from discord.ext import commands
from datetime import datetime, timedelta

# Pega o logger configurado no bot.py
logger = logging.getLogger('discord_bot.moderation_cog')

# Nome do arquivo para persistir os bans
BANLIST_FILE = "banlist.json"
ITEMS_PER_PAGE = 4 # Usuários por página no menu

class ConfirmMassUnban(ui.View):
    """View de confirmação para a ação de desbanir todos."""
    def __init__(self, author: discord.Member):
        super().__init__(timeout=30)
        self.author = author
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("Apenas o autor do comando pode confirmar esta ação.", ephemeral=True)
            return False
        return True

    @ui.button(label="Confirmar Desbanimento em Massa", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        self.stop()
        await interaction.response.defer()

class ModerationMenu(ui.View):
    """View interativa para gerenciar a lista de banidos."""
    def __init__(self, author: discord.Member, bans: dict, cog: "ModerationCog", page: int = 0):
        super().__init__(timeout=180)
        self.author = author
        self.cog = cog
        self.all_bans = list(bans.items())
        self.page = page
        self.total_pages = max(0, (len(self.all_bans) - 1) // ITEMS_PER_PAGE)
        self.update_view_items()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("Você não tem permissão para usar este menu.", ephemeral=True)
            return False
        return True

    async def _get_page_embed(self) -> discord.Embed:
        """Cria o embed para a página atual da lista de banidos."""
        embed = discord.Embed(title=f"Menu de Moderação - Página {self.page + 1}/{self.total_pages + 1}", color=discord.Color.orange())
        
        start_index = self.page * ITEMS_PER_PAGE
        end_index = start_index + ITEMS_PER_PAGE
        page_bans = self.all_bans[start_index:end_index]
        
        if not page_bans:
            embed.description = "Não há usuários banidos para exibir."
            return embed

        description = ""
        now = datetime.utcnow()
        for i, (member_id_str, ban_info) in enumerate(page_bans):
            try:
                member = await self.cog.bot.fetch_user(int(member_id_str))
                member_display = f"{member.name} (`{member.id}`)"
            except discord.NotFound:
                member_display = f"ID: `{member_id_str}` (usuário desconhecido)"
            
            until = ban_info.get("until")
            reason = ban_info.get("reason", "Nenhum motivo fornecido.") # [NOVO] Pega o motivo
            
            if until:
                until_dt = datetime.fromisoformat(until)
                if now > until_dt:
                    ban_status = "Ban expirado."
                else:
                    remaining = until_dt - now
                    ban_status = f"Expira em: `{str(timedelta(seconds=int(remaining.total_seconds())))}`"
            else:
                ban_status = "Ban **Permanente**"
            
            description += f"**{i + 1 + start_index}. {member_display}**\n- **Motivo:** *{reason}*\n- {ban_status}\n\n"

        embed.description = description
        return embed

    def update_view_items(self):
        """Limpa e recria todos os botões para a página atual."""
        self.clear_items()
        start_index = self.page * ITEMS_PER_PAGE
        end_index = start_index + ITEMS_PER_PAGE
        page_bans = self.all_bans[start_index:end_index]

        # Botões de desbanir individual
        for i, (member_id_str, _) in enumerate(page_bans):
            button = ui.Button(label=f"Desbanir #{i + 1 + start_index}", style=discord.ButtonStyle.secondary, custom_id=f"unban_{member_id_str}", row=0)
            button.callback = self.unban_callback
            self.add_item(button)

        # Botões de navegação e controle
        prev_button = ui.Button(label="Anterior", emoji="◀️", disabled=self.page == 0, row=1)
        prev_button.callback = self.prev_page_callback
        self.add_item(prev_button)

        next_button = ui.Button(label="Próxima", emoji="▶️", disabled=self.page >= self.total_pages, row=1)
        next_button.callback = self.next_page_callback
        self.add_item(next_button)

        mass_unban_button = ui.Button(label="Desbanir Todos", style=discord.ButtonStyle.danger, emoji="💥", disabled=not self.all_bans, row=2)
        mass_unban_button.callback = self.mass_unban_callback
        self.add_item(mass_unban_button)

    async def refresh_menu(self, interaction: discord.Interaction):
        """Atualiza a mensagem com a nova página e botões."""
        self.total_pages = max(0, (len(self.all_bans) - 1) // ITEMS_PER_PAGE)
        if self.page > self.total_pages:
            self.page = self.total_pages
        
        self.update_view_items()
        embed = await self._get_page_embed()
        await interaction.message.edit(embed=embed, view=self)

    async def unban_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        member_id_str = interaction.data['custom_id'].split('_')[1]

        if member_id_str in self.cog.bans:
            del self.cog.bans[member_id_str]
            self.cog._save_bans()
            self.all_bans = list(self.cog.bans.items())
            await self.refresh_menu(interaction)
        else:
            await interaction.followup.send("Este usuário não estava mais na lista.", ephemeral=True, delete_after=5)

    async def prev_page_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.page -= 1
        await self.refresh_menu(interaction)

    async def next_page_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.page += 1
        await self.refresh_menu(interaction)
        
    async def mass_unban_callback(self, interaction: discord.Interaction):
        confirm_view = ConfirmMassUnban(self.author)
        await interaction.response.send_message("**Você tem certeza que deseja desbanir TODOS os usuários?** Esta ação é irreversível.", view=confirm_view, ephemeral=True)
        
        await confirm_view.wait()

        if confirm_view.confirmed:
            self.cog.bans.clear()
            self.cog._save_bans()
            self.all_bans = []
            await interaction.followup.send("💥 Todos os usuários foram desbanidos.", ephemeral=True)
            await self.refresh_menu(interaction)
        else:
            await interaction.followup.send("Ação cancelada.", ephemeral=True)

class ModerationCog(commands.Cog, name="Moderation"):
    """Cog para gerenciar permissões de uso do bot."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bans = self._load_bans()

    def _load_bans(self) -> dict:
        if os.path.exists(BANLIST_FILE):
            try:
                with open(BANLIST_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Erro ao carregar {BANLIST_FILE}: {e}. Criando um novo arquivo.")
                return {}
        return {}

    def _save_bans(self):
        try:
            with open(BANLIST_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.bans, f, indent=4)
        except IOError as e:
            logger.error(f"Não foi possível salvar a banlist em {BANLIST_FILE}: {e}")

    @commands.command(name="ban", help="Proíbe um membro de usar os comandos de música. Uso: !ban @membro [minutos] [motivo]")
    @commands.has_permissions(manage_guild=True)
    async def ban(self, ctx: commands.Context, member: discord.Member, duration_minutes: int = 0, *, reason: str = "Nenhum motivo fornecido."):
        member_id_str = str(member.id)
        
        if member.id == ctx.author.id:
            return await ctx.send("Você não pode banir a si mesmo.")
        if member.guild_permissions.manage_guild:
            return await ctx.send("Você não pode banir outros administradores.")

        ban_until = None
        if duration_minutes > 0:
            ban_until_dt = datetime.utcnow() + timedelta(minutes=duration_minutes)
            ban_until = ban_until_dt.isoformat()
            duration_text = f"por **{duration_minutes} minuto(s)**"
        else:
            duration_text = "**permanentemente**"
        
        # [NOVO] Salva o motivo junto com as outras informações
        self.bans[member_id_str] = {
            "until": ban_until, 
            "banned_by": ctx.author.id,
            "reason": reason
        }
        self._save_bans()
        
        embed = discord.Embed(
            title="🚫 Usuário Banido",
            description=f"{member.mention} foi proibido de usar os comandos de música {duration_text}.",
            color=discord.Color.red()
        )
        embed.add_field(name="Motivo", value=reason, inline=False)
        embed.set_footer(text=f"Banido por: {ctx.author.display_name}")
        await ctx.send(embed=embed)
        logger.info(f"'{member.display_name}' ({member.id}) foi banido por '{ctx.author.display_name}'. Motivo: {reason}")

    @commands.command(name="unban", help="Remove a proibição de um membro.")
    @commands.has_permissions(manage_guild=True)
    async def unban(self, ctx: commands.Context, member: discord.Member):
        member_id_str = str(member.id)
        
        if member_id_str in self.bans:
            del self.bans[member_id_str]
            self._save_bans()
            embed = discord.Embed(
                title="✅ Usuário Desbanido",
                description=f"{member.mention} agora pode usar os comandos de música novamente.",
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)
            logger.info(f"'{member.display_name}' ({member.id}) foi desbanido por '{ctx.author.display_name}'.")
        else:
            await ctx.send("Este membro não está na lista de banidos.")

    @commands.command(name="mod", help="Abre o menu interativo de moderação.")
    @commands.has_permissions(manage_guild=True)
    async def mod(self, ctx: commands.Context):
        self.bans = self._load_bans() # Recarrega para garantir dados atualizados
        if not self.bans:
            return await ctx.send("A lista de banidos está vazia.")
            
        view = ModerationMenu(ctx.author, self.bans, self)
        embed = await view._get_page_embed()
        await ctx.send(embed=embed, view=view)

async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))

