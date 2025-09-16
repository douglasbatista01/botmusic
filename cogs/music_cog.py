# -*- coding: utf-8 -*-

import asyncio
import logging
import time
import os
import re
from enum import Enum
from typing import Dict, Optional, List
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

import discord
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from discord.ext import commands
from discord import app_commands, ui

# --- Configurações Otimizadas ---
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'default_search': 'auto',
    'quiet': True,
    'no_warnings': True,
    'force_ipv4': True,
    'source_address': '0.0.0.0',
    'cookiefile': 'cookies.txt',
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -nostdin -bufsize 5M',
}

logger = logging.getLogger('discord_bot.music_cog')
SPOTIFY_PLAYLIST_REGEX = re.compile(r"https://open.spotify.com/playlist/([a-zA-Z0-9]+)")
PEER_SIZE = 20
PEER_THRESHOLD = 5
ADMIN_QUEUE_ITEMS_PER_PAGE = 5 # Músicas por página no menu admin

# --- Decorator de Verificação de Ban ---
def is_not_banned():
    async def predicate(ctx_or_interaction: any) -> bool:
        if isinstance(ctx_or_interaction, discord.Interaction):
            author = ctx_or_interaction.user; bot = ctx_or_interaction.client
        else:
            author = ctx_or_interaction.author; bot = ctx_or_interaction.bot
            
        mod_cog = bot.get_cog("Moderation")
        if not mod_cog:
            logging.warning("Cog de Moderação não encontrado."); return True

        member_id_str = str(author.id)
        bans = mod_cog._load_bans()
        
        if member_id_str in bans:
            ban_info = bans[member_id_str]
            until = ban_info.get("until")
            if until:
                if datetime.utcnow() > datetime.fromisoformat(until):
                    del bans[member_id_str]; mod_cog.bans = bans; mod_cog._save_bans()
                    return True
            
            if isinstance(ctx_or_interaction, discord.Interaction):
                await ctx_or_interaction.response.send_message("🚫 Você está proibido de usar os comandos de música.", ephemeral=True)
            else:
                 await ctx_or_interaction.send("🚫 Você está proibido de usar os comandos de música.")
            return False
        return True
    return commands.check(predicate)

# --- Componentes de Classes ---
def search_sync(query: str) -> Optional[dict]:
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        try:
            data = ydl.extract_info(f"ytsearch:{query}", download=False)
            if 'entries' in data and data['entries']: return data['entries'][0]
            return None
        except Exception as e:
            logging.error(f"Erro no processo de busca do YTDL para '{query}': {e}"); return None

class LoopState(Enum):
    NONE = 0; SONG = 1; QUEUE = 2

class Song:
    def __init__(self, data: dict, requester: discord.Member):
        self.source_url: str = data['url']; self.title: str = data.get('title', 'Título Desconhecido')
        self.thumbnail: Optional[str] = data.get('thumbnail'); self.duration: int = int(data.get('duration', 0))
        self.requester: discord.Member = requester; self.webpage_url: str = data.get('webpage_url', '')

class GuildState:
    def __init__(self, loop: asyncio.AbstractEventLoop, cog_instance: 'MusicCog'):
        self.cog_instance = cog_instance; self.loop = loop
        self.song_queue = asyncio.Queue(maxsize=200)
        self.play_next_song = asyncio.Event()
        self.current_song: Optional[Song] = None; self.player_task: Optional[asyncio.Task] = None
        self.menu_message: Optional[discord.WebhookMessage] = None
        self.volume: float = 0.5; self.loop_state: LoopState = LoopState.NONE
        self.song_start_time: Optional[float] = None; self.playlist_mode: bool = False
        self.playlist_requester: Optional[discord.Member] = None
        self.playlist_total_tracks: int = 0; self.playlist_loaded_tracks: int = 0
        self.playlist_tracks_to_search: List[str] = []; self.playlist_loader_task: Optional[asyncio.Task] = None

    def reset_playlist_state(self):
        self.playlist_mode = False; self.playlist_requester = None; self.playlist_total_tracks = 0; self.playlist_loaded_tracks = 0
        self.playlist_tracks_to_search.clear()
        if self.playlist_loader_task and not self.playlist_loader_task.done(): self.playlist_loader_task.cancel()
        while not self.song_queue.empty():
            try: self.song_queue.get_nowait()
            except asyncio.QueueEmpty: continue
        logger.info("Estado da playlist e fila de músicas foram resetados.")

    async def update_menu(self):
        if not self.menu_message: return
        embed = self.cog_instance.build_player_embed(self)
        view = PlayerView(self.cog_instance, self)
        try:
            await self.menu_message.edit(embed=embed, view=view)
        except (discord.NotFound, discord.HTTPException) as e:
            logger.warning(f"Não foi possível editar a mensagem do menu: {e}"); self.menu_message = None

# --- [NOVO] Views Paginadas para o Menu Admin ---
class AdminQueuePaginator(ui.View):
    def __init__(self, author: discord.Member, state: GuildState, cog: 'MusicCog'):
        super().__init__(timeout=180)
        self.author = author; self.state = state; self.cog = cog
        self.queue_list = list(state.song_queue._queue)
        self.page = 0
        self.total_pages = max(0, (len(self.queue_list) - 1) // ADMIN_QUEUE_ITEMS_PER_PAGE)
        self.update_view()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("Você não pode usar este menu.", ephemeral=True); return False
        return True

    def update_view(self):
        self.clear_items()
        start_index = self.page * ADMIN_QUEUE_ITEMS_PER_PAGE
        end_index = start_index + ADMIN_QUEUE_ITEMS_PER_PAGE
        page_songs = self.queue_list[start_index:end_index]
        
        options = [discord.SelectOption(label=f"#{i + 1 + start_index}: {song.title[:80]}", value=str(i + start_index)) for i, song in enumerate(page_songs)]
        if options:
            select = ui.Select(placeholder=f"Página {self.page + 1}/{self.total_pages + 1} - Selecione uma música...", options=options)
            select.callback = self.select_callback
            self.add_item(select)
        
        prev_button = ui.Button(label="Anterior", emoji="◀️", disabled=self.page == 0, row=1)
        prev_button.callback = self.prev_page_callback
        self.add_item(prev_button)

        next_button = ui.Button(label="Próxima", emoji="▶️", disabled=self.page >= self.total_pages, row=1)
        next_button.callback = self.next_page_callback
        self.add_item(next_button)

    async def select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        selected_index = int(interaction.data['values'][0])
        
        song_to_move = self.queue_list.pop(selected_index)
        
        while not self.state.song_queue.empty():
            try: self.state.song_queue.get_nowait()
            except asyncio.QueueEmpty: continue
            
        await self.state.song_queue.put(song_to_move)
        for song in self.queue_list: await self.state.song_queue.put(song)
        
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop() # Força o player a pegar a próxima música da fila
            
        await interaction.followup.send(f"✅ **{song_to_move.title}** foi movida para o topo e será a próxima a tocar.", ephemeral=True, delete_after=10)
        await interaction.message.delete()
        await self.state.update_menu()

    async def prev_page_callback(self, interaction: discord.Interaction):
        self.page -= 1
        self.update_view()
        await interaction.response.edit_message(view=self)

    async def next_page_callback(self, interaction: discord.Interaction):
        self.page += 1
        self.update_view()
        await interaction.response.edit_message(view=self)

# --- Views Principais ---
class StopPlaylistView(ui.View):
    def __init__(self, cog: 'MusicCog'):
        super().__init__(timeout=60); self.cog = cog
    @ui.button(label="Parar Playlist e Limpar Fila", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop_playlist(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.stop_player(interaction)
        await interaction.response.edit_message(content="Playlist parada. Agora você pode adicionar novas músicas.", view=None)

class PlayerView(ui.View):
    def __init__(self, cog: 'MusicCog', state: GuildState):
        super().__init__(timeout=None); self.cog = cog; self.state = state
        self._update_buttons()

    def _update_buttons(self):
        vc = self.state.current_song.requester.guild.voice_client if self.state.current_song else None
        pause_resume_btn = self.children[0]
        if vc and vc.is_paused(): pause_resume_btn.label, pause_resume_btn.emoji, pause_resume_btn.style = "Retomar", "▶️", discord.ButtonStyle.green
        else: pause_resume_btn.label, pause_resume_btn.emoji, pause_resume_btn.style = "Pausar", "⏸️", discord.ButtonStyle.secondary
        loop_btn = self.children[3]
        if self.state.loop_state == LoopState.NONE: loop_btn.label, loop_btn.style = "Loop Off", discord.ButtonStyle.secondary
        elif self.state.loop_state == LoopState.SONG: loop_btn.label, loop_btn.style = "Loop Msc", discord.ButtonStyle.primary
        else: loop_btn.label, loop_btn.style = "Loop Fila", discord.ButtonStyle.primary

    @ui.button(label="Pausar", style=discord.ButtonStyle.secondary, emoji="⏸️", row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: ui.Button):
        vc = interaction.guild.voice_client
        if not vc: return await interaction.response.send_message("O bot não está tocando nada.", ephemeral=True)
        if vc.is_paused(): vc.resume(); await interaction.response.send_message("▶️ Música retomada!", ephemeral=True, delete_after=5)
        else: vc.pause(); await interaction.response.send_message("⏸️ Música pausada!", ephemeral=True, delete_after=5)
        self._update_buttons(); await interaction.message.edit(view=self)

    @ui.button(label="Pular", style=discord.ButtonStyle.secondary, emoji="⏭️", row=0)
    async def skip(self, interaction: discord.Interaction, button: ui.Button):
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()): return await interaction.response.send_message("Não há música para pular.", ephemeral=True)
        vc.stop(); await interaction.response.send_message("⏭️ Música pulada!", ephemeral=True, delete_after=5)

    @ui.button(label="Parar", style=discord.ButtonStyle.danger, emoji="⏹️", row=0)
    async def stop(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.stop_player(interaction)

    @ui.button(label="Loop Off", style=discord.ButtonStyle.secondary, emoji="🔁", row=0)
    async def loop(self, interaction: discord.Interaction, button: ui.Button):
        states = [LoopState.NONE, LoopState.SONG, LoopState.QUEUE]
        messages = ["🔁 Loop desativado.", "🔂 Loop da música ativado.", "🔁 Loop da fila ativado."]
        next_index = (self.state.loop_state.value + 1) % len(states)
        self.state.loop_state = states[next_index]
        await interaction.response.send_message(messages[next_index], ephemeral=True, delete_after=10)
        self._update_buttons(); await self.state.update_menu()
    
    @ui.button(label="Limpar Fila", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def clear_queue(self, interaction: discord.Interaction, button: ui.Button):
        if self.state.song_queue.empty() and not self.state.playlist_tracks_to_search:
            return await interaction.response.send_message("A fila já está vazia.", ephemeral=True)
        if self.state.playlist_mode: self.state.reset_playlist_state()
        else:
             while not self.state.song_queue.empty():
                try: self.state.song_queue.get_nowait()
                except asyncio.QueueEmpty: continue
        await self.state.update_menu()
        await interaction.response.send_message("🗑️ Fila de músicas limpa!", ephemeral=True)

    @ui.button(label="Fila", style=discord.ButtonStyle.primary, emoji="📜", row=1)
    async def queue(self, interaction: discord.Interaction, button: ui.Button):
        await self.cog.show_queue(interaction, ephemeral=True)
    
    @ui.button(label="Admin: Pular Fila", style=discord.ButtonStyle.blurple, emoji="🔀", row=2)
    async def jump_queue(self, interaction: discord.Interaction, button: ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("🚫 Apenas administradores podem usar esta função.", ephemeral=True)
        if self.state.song_queue.empty():
            return await interaction.response.send_message("A fila está vazia, não há músicas para reordenar.", ephemeral=True)
        view = AdminQueuePaginator(interaction.user, self.state, self.cog)
        await interaction.response.send_message("Selecione a música para tocar em seguida:", view=view, ephemeral=True)

# --- Classe Principal do Cog ---
class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot; self.guild_states: Dict[int, GuildState] = {}
        self.process_executor = ProcessPoolExecutor(max_workers=2); self.spotify_client = None
        client_id = os.getenv("SPOTIPY_CLIENT_ID"); client_secret = os.getenv("SPOTIPY_CLIENT_SECRET")
        if client_id and client_secret:
            try:
                auth_manager = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
                self.spotify_client = spotipy.Spotify(auth_manager=auth_manager)
                logger.info("Cliente Spotify inicializado com sucesso.")
            except Exception as e: logger.error(f"Falha ao inicializar o cliente Spotify: {e}")
        else: logger.warning("Credenciais do Spotify não encontradas.")

    def cog_unload(self): self.process_executor.shutdown(wait=True)
    def get_guild_state(self, guild_id: int) -> GuildState:
        if guild_id not in self.guild_states: self.guild_states[guild_id] = GuildState(self.bot.loop, self)
        return self.guild_states[guild_id]

    async def _cleanup(self, guild: discord.Guild):
        state = self.get_guild_state(guild.id)
        if state.player_task: state.player_task.cancel()
        if state.playlist_loader_task: state.playlist_loader_task.cancel()
        if guild.voice_client: await guild.voice_client.disconnect()
        if state.menu_message:
            try:
                embed = discord.Embed(title="Player Desconectado", description="Até a próxima! 👋", color=discord.Color.red())
                embed.set_footer(text="Desenvolvido por: Douglas Batista")
                await state.menu_message.edit(embed=embed, view=None)
            except (discord.NotFound, discord.HTTPException): pass
        if guild.id in self.guild_states: del self.guild_states[guild.id]
        logger.info(f"Estado do servidor '{guild.name}' foi limpo.")

    def _player_finished_callback(self, state: GuildState, error=None):
        if error: logger.error(f"Erro no player: {error}", exc_info=error)
        else: logger.info(f"Reprodução de '{state.current_song.title}' finalizada.")
        state.play_next_song.set()

    async def _player_loop(self, guild_id: int):
        state = self.get_guild_state(guild_id)
        guild = self.bot.get_guild(guild_id)
        if not guild: return await self._cleanup(guild)
        while True:
            state.play_next_song.clear()
            vc = guild.voice_client
            if not vc or not vc.is_connected():
                logger.warning(f"Player loop detectou desconexão. Limpando."); return await self._cleanup(guild)
            try:
                song_to_play = await asyncio.wait_for(state.song_queue.get(), timeout=300.0)
            except asyncio.TimeoutError:
                logger.info(f"Fila vazia por 5 minutos. Desconectando.")
                if vc and not vc.is_playing():
                   if state.menu_message and state.menu_message.channel:
                       await state.menu_message.channel.send("Fila vazia. Desconectando por inatividade.", delete_after=30)
                   return await self._cleanup(guild)
                continue
            state.current_song = song_to_play
            try:
                source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(song_to_play.source_url, **FFMPEG_OPTIONS), volume=state.volume)
                vc.play(source, after=lambda e: self._player_finished_callback(state, e))
                state.song_start_time = time.time()
                logger.info(f"Iniciando reprodução de '{song_to_play.title}'.")
            except Exception as e:
                logger.error(f"Erro CRÍTICO ao iniciar a reprodução: {e}", exc_info=True)
                if state.menu_message and state.menu_message.channel:
                    await state.menu_message.channel.send(f"⚠️ Erro ao tocar `{song_to_play.title}`. Pulando para a próxima.", delete_after=15)
                state.play_next_song.set()
            await state.update_menu()
            await state.play_next_song.wait()

    async def _search_song(self, query: str, requester: discord.Member) -> Optional[Song]:
        try:
            data = await self.bot.loop.run_in_executor(self.process_executor, search_sync, query)
            if data: return Song(data, requester)
            logger.warning(f"Nenhum resultado encontrado para: '{query}'"); return None
        except Exception as e:
            logger.error(f"Erro ao buscar '{query}': {e}"); return None

    def build_player_embed(self, state: GuildState) -> discord.Embed:
        if state.current_song:
            song = state.current_song
            embed = discord.Embed(title="Tocando Agora", color=discord.Color.blue(), description=f"**[{song.title}]({song.webpage_url})**")
            embed.set_thumbnail(url=song.thumbnail)
            m, s = divmod(song.duration, 60)
            embed.add_field(name="Duração", value=f"`{m}:{s:02d}`", inline=True)
            embed.add_field(name="Pedido por", value=song.requester.mention, inline=True)
            embed.add_field(name="Volume", value=f"`{int(state.volume * 100)}%`", inline=True)
            queue_text = f"📜 Fila: {state.song_queue.qsize()}"
            if state.playlist_mode: queue_text += f" (+{len(state.playlist_tracks_to_search)} a buscar)"
            queue_text += f" | Loop: {state.loop_state.name.capitalize()}"
        else:
            embed = discord.Embed(title="Player Parado", description="Use `/play` para adicionar uma música!", color=discord.Color.greyple())
            queue_text = "Aguardando músicas..."
        embed.set_footer(text=f"{queue_text}\nDesenvolvido por: Douglas Batista")
        return embed

    async def _playlist_peer_loader_loop(self, guild_id: int, requester: discord.Member, initial_message: discord.Message):
        state = self.get_guild_state(guild_id)
        logger.info(f"Iniciando carregador de playlist.")
        if state.playlist_tracks_to_search:
            first_query = state.playlist_tracks_to_search.pop(0)
            await initial_message.edit(content=f"▶️ Buscando a primeira música: `{first_query[:50]}...`")
            first_song = await self._search_song(first_query, requester)
            if first_song:
                await state.song_queue.put(first_song); state.playlist_loaded_tracks += 1
                await initial_message.edit(content=f"Tocando `{first_song.title}`. Carregando as outras {len(state.playlist_tracks_to_search) + 1} músicas...")
            else: await initial_message.edit(content=f"Não achei a primeira música. Tentando a próxima...")
        while state.playlist_tracks_to_search:
            try:
                if state.song_queue.qsize() <= PEER_THRESHOLD:
                    queries = [state.playlist_tracks_to_search.pop(0) for _ in range(min(PEER_SIZE, len(state.playlist_tracks_to_search)))]
                    for query in queries:
                        song = await self._search_song(query, requester)
                        if song: await state.song_queue.put(song); state.playlist_loaded_tracks += 1
                        await asyncio.sleep(0.5)
                    await state.update_menu()
                await asyncio.sleep(5)
            except asyncio.CancelledError: logger.info(f"Carregador de playlist cancelado."); break
            except Exception as e: logger.error(f"Erro no carregador de playlist: {e}", exc_info=e); break
        state.playlist_mode = False
        logger.info(f"Carregador de playlist concluído.")

    # --- COMANDOS ---
    @app_commands.command(name="play", description="Toca uma música do YouTube.")
    @is_not_banned()
    async def play(self, interaction: discord.Interaction, busca: str):
        state = self.get_guild_state(interaction.guild_id)
        if state.playlist_mode:
            embed = discord.Embed(title="Playlist em Andamento", description=f"Uma playlist pedida por **{state.playlist_requester.display_name}** está tocando.", color=discord.Color.orange())
            return await interaction.response.send_message(embed=embed, view=StopPlaylistView(self), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.user.voice: return await interaction.followup.send("Você precisa estar em um canal de voz!", ephemeral=True)
        if not interaction.guild.voice_client:
            try: await interaction.user.voice.channel.connect()
            except Exception as e: return await interaction.followup.send(f"Não consegui conectar: {e}", ephemeral=True)
        song = await self._search_song(busca, interaction.user)
        if not song: return await interaction.followup.send(f"Não encontrei a música `{busca}`.", ephemeral=True)
        await state.song_queue.put(song)
        await interaction.followup.send(f"✅ Adicionado à fila: **{song.title}**", ephemeral=True)
        if not state.player_task or state.player_task.done(): state.player_task = self.bot.loop.create_task(self._player_loop(interaction.guild_id))
        if not state.menu_message or not state.menu_message.channel:
            embed = self.build_player_embed(state)
            view = PlayerView(self, state)
            state.menu_message = await interaction.channel.send(embed=embed, view=view)
        else: await state.update_menu()

    @commands.command(name="pl", help="Adiciona uma playlist do Spotify. Uso: !pl <link>")
    @is_not_banned()
    async def pl(self, ctx: commands.Context, *, url: str = None):
        state = self.get_guild_state(ctx.guild.id)
        if state.playlist_mode:
            embed = discord.Embed(title="Playlist em Andamento", description=f"Uma playlist pedida por **{state.playlist_requester.display_name}** está tocando.", color=discord.Color.orange())
            return await ctx.send(embed=embed, view=StopPlaylistView(self))
        if url is None: return await ctx.send("Uso: `!pl <link da playlist do Spotify>`")
        if not self.spotify_client: return await ctx.send("A integração com o Spotify não está configurada.")
        if not SPOTIFY_PLAYLIST_REGEX.match(url): return await ctx.send("URL de playlist do Spotify inválida.")
        if not ctx.author.voice: return await ctx.send("Você precisa estar em um canal de voz.")
        if not ctx.guild.voice_client:
            try: await ctx.author.voice.channel.connect()
            except Exception as e: return await ctx.send(f"Não consegui conectar: {e}")
        initial_message = await ctx.send(f"🔍 Analisando playlist...")
        try:
            items = await self.bot.loop.run_in_executor(None, lambda: self.spotify_client.playlist_tracks(url)['items'])
            if not items: return await initial_message.edit(content="Playlist vazia ou não encontrada.")
            state.reset_playlist_state()
            state.playlist_mode = True
            state.playlist_requester = ctx.author
            state.playlist_tracks_to_search = [f"{item['track']['name']} {item['track']['artists'][0]['name']}" for item in items if item.get('track')]
            state.playlist_total_tracks = len(state.playlist_tracks_to_search)
            if not state.playlist_tracks_to_search: return await initial_message.edit(content="Não extraí músicas válidas da playlist.")
            if not state.player_task or state.player_task.done():
                state.player_task = self.bot.loop.create_task(self._player_loop(ctx.guild.id))
            state.playlist_loader_task = self.bot.loop.create_task(self._playlist_peer_loader_loop(ctx.guild.id, ctx.author, initial_message))
        except Exception as e:
            logger.error(f"Erro ao processar playlist '{url}': {e}", exc_info=e)
            await initial_message.edit(content="Ocorreu um erro ao buscar a playlist.")

    @commands.command(name="status", help="Mostra o status da playlist em andamento.")
    @is_not_banned()
    async def status(self, ctx: commands.Context):
        state = self.get_guild_state(ctx.guild.id)
        if not state.playlist_mode or not state.playlist_requester: return await ctx.send("Nenhuma playlist está em processamento.")
        embed = discord.Embed(title="Status da Playlist", color=discord.Color.blue())
        embed.add_field(name="Status", value="Playlist em processamento", inline=False)
        embed.add_field(name="Pedido por", value=state.playlist_requester.mention, inline=False)
        embed.add_field(name="Progresso", value=f"`{state.playlist_loaded_tracks} / {state.playlist_total_tracks}` músicas carregadas", inline=False)
        embed.set_footer(text=f"Fila atual: {state.song_queue.qsize()} músicas prontas para tocar.\nDesenvolvido por: Douglas Batista")
        await ctx.send(embed=embed)

    @commands.command(name="connect", help="Testa a conexão com a API do Spotify.")
    @commands.is_owner()
    async def connect(self, ctx: commands.Context):
        if not self.spotify_client: return await ctx.send("❌ **Cliente Spotify não inicializado.**")
        async with ctx.typing():
            try:
                await self.bot.loop.run_in_executor(None, lambda: self.spotify_client.artist('1dfeR4HaWDbWqFHLkxsg1d'))
                await ctx.send("✅ **Conexão com a API do Spotify bem-sucedida!**")
            except Exception as e: await ctx.send(f"❌ **Falha ao conectar com a API do Spotify.**\n`Erro: {e}`")

    @app_commands.command(name="nowplaying", description="Mostra informações da música que está tocando.")
    @is_not_banned()
    async def nowplaying(self, interaction: discord.Interaction):
        state = self.get_guild_state(interaction.guild_id)
        if not state.current_song or not state.song_start_time:
            return await interaction.response.send_message("Não há nenhuma música tocando.", ephemeral=True)
        song = state.current_song; elapsed = time.time() - state.song_start_time
        progress_bar_length = 20
        progress_percent = min(elapsed / song.duration, 1.0) if song.duration > 0 else 0
        filled_blocks = int(progress_percent * progress_bar_length)
        progress_bar = '▬' * filled_blocks + '🔵' + '▬' * (progress_bar_length - 1 - filled_blocks) if filled_blocks < progress_bar_length else '▬' * progress_bar_length
        m_elapsed, s_elapsed = divmod(int(elapsed), 60)
        m_total, s_total = divmod(song.duration, 60)
        embed = discord.Embed(title="Tocando Agora", color=discord.Color.green(), description=f"**[{song.title}]({song.webpage_url})**")
        embed.set_thumbnail(url=song.thumbnail)
        embed.add_field(name="Progresso", value=f"`{progress_bar}`\n`{m_elapsed:02d}:{s_elapsed:02d} / {m_total:02d}:{s_total:02d}`", inline=False)
        embed.set_footer(text=f"Pedido por: {song.requester.display_name}")
        await interaction.response.send_message(embed=embed)

    async def show_queue(self, interaction: discord.Interaction, ephemeral: bool = False):
        state = self.get_guild_state(interaction.guild.id)
        if state.song_queue.empty() and not state.current_song and not state.playlist_tracks_to_search:
            return await interaction.response.send_message("A fila está vazia!", ephemeral=ephemeral)
        embed = discord.Embed(title="📜 Fila de Músicas", color=discord.Color.orange())
        desc = ""
        if state.current_song: desc += f"**Tocando Agora:**\n`▶️` {state.current_song.title}\n\n"
        desc += "**Próximas na fila:**\n"
        queue_list = list(state.song_queue._queue)
        if not queue_list: desc += "Nenhuma música na fila.\n"
        else:
            lines = [f"`{i+1}.` {song.title}" for i, song in enumerate(queue_list[:10])]
            desc += "\n".join(lines)
        if state.playlist_mode:
            desc += f"\n\n**Aguardando busca:**\n`+{len(state.playlist_tracks_to_search)}` músicas da playlist."
        embed.description = desc
        if len(queue_list) > 10: embed.set_footer(text=f"... e mais {len(queue_list) - 10} música(s).")
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    @app_commands.command(name="queue", description="Mostra a fila de músicas.")
    @is_not_banned()
    async def queue_command(self, interaction: discord.Interaction):
        await self.show_queue(interaction)

    @app_commands.command(name="volume", description="Ajusta o volume do player (1 a 150%).")
    @is_not_banned()
    async def volume(self, interaction: discord.Interaction, valor: app_commands.Range[int, 1, 150]):
        vc = interaction.guild.voice_client
        if not vc or not vc.source: return await interaction.response.send_message("O bot não está tocando nada.", ephemeral=True)
        state = self.get_guild_state(interaction.guild_id)
        state.volume = valor / 100
        vc.source.volume = state.volume
        await interaction.response.send_message(f"🔊 Volume ajustado para **{valor}%**.", ephemeral=True)
        await state.update_menu()

    async def stop_player(self, interaction: discord.Interaction):
        state = self.get_guild_state(interaction.guild.id)
        state.reset_playlist_state()
        if not interaction.guild.voice_client:
            msg = "O bot não está conectado."
            if interaction.type == discord.InteractionType.component: return await interaction.response.send_message(msg, ephemeral=True)
            return await interaction.response.send_message(msg)
        await self._cleanup(interaction.guild)
        if interaction.type != discord.InteractionType.component:
            await interaction.response.send_message("⏹️ Player parado e desconectado.")

    @app_commands.command(name="stop", description="Para a música, limpa a fila e desconecta.")
    @is_not_banned()
    async def stop_command(self, interaction: discord.Interaction):
        await self.stop_player(interaction)

    @app_commands.command(name="menu", description="Recria o painel de controle interativo do player.")
    @is_not_banned()
    async def menu(self, interaction: discord.Interaction):
        state = self.get_guild_state(interaction.guild.id)
        if state.menu_message:
            try: await state.menu_message.delete()
            except (discord.NotFound, discord.HTTPException): pass
        embed = self.build_player_embed(state)
        view = PlayerView(self, state)
        state.menu_message = await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("Painel recriado!", ephemeral=True, delete_after=5)

async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))

