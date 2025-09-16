# -*- coding: utf-8 -*-

import os
import logging
import logging.handlers
from typing import Literal, Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv

# Carrega as variáveis de ambiente do arquivo .env
load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_TOKEN")

# --- Sistema de Logs Profissional ---
# Cria um logger principal para o bot.
logger = logging.getLogger('discord_bot')
logger.setLevel(logging.INFO) # Define o nível mínimo de logs a serem capturados

# Formato do log: Data/Hora, Nível do Log, Nome do Módulo, Mensagem
log_format = logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s')

# Handler para salvar os logs em um arquivo com rotação automática
# Cria um novo arquivo quando o atual atinge 10MB, mantendo até 5 arquivos antigos.
file_handler = logging.handlers.RotatingFileHandler(
    filename='discord_bot.log',
    encoding='utf-8',
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,
)
file_handler.setFormatter(log_format)

# Handler para mostrar os logs no console (terminal)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_format)

# Adiciona os handlers ao logger principal
logger.addHandler(file_handler)
logger.addHandler(console_handler)
# --- Fim do Sistema de Logs ---

# Define as intenções (Intents) do bot, permissões necessárias para ele funcionar
intents = discord.Intents.default()
intents.message_content = True  # Para ler comandos de texto como !pl
intents.voice_states = True     # Para gerenciar estados de voz (entrar/sair de canais)

class MusicBot(commands.Bot):
    """Classe principal do Bot para uma melhor organização e escalabilidade."""
    def __init__(self):
        # O prefixo '!' é usado para comandos de texto
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        """
        Este método é chamado uma vez quando o bot é iniciado.
        É o local ideal para carregar as extensões (Cogs).
        """
        logger.info("Carregando extensões (Cogs)...")
        await self.load_extension("cogs.music_cog")
        logger.info("Cog de música carregado com sucesso.")
        
        await self.load_extension("cogs.moderation_cog")
        logger.info("Cog de moderação carregado com sucesso.")

# Cria a instância principal do bot
bot = MusicBot()

@bot.event
async def on_ready():
    """Evento disparado quando o bot está online e pronto para uso."""
    logger.info(f'Bot {bot.user.name} está online e pronto.')
    logger.info(f'Use !sync para gerenciar os comandos de barra.')

# --- Comandos de Sincronização (Apenas para o Dono do Bot) ---
@bot.command()
@commands.is_owner()
async def sync(ctx: commands.Context, guild: Optional[discord.Guild] = None):
    """
    Sincroniza os comandos de barra com o Discord.
    Pode ser global ou para um servidor específico.
    """
    if guild:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        await ctx.send(f"Sincronizados {len(synced)} comandos para o servidor `{guild.name}`.")
        logger.info(f"Comandos sincronizados para o servidor '{guild.name}' por '{ctx.author.name}'.")
    else:
        synced = await bot.tree.sync()
        await ctx.send(f"Sincronizados {len(synced)} comandos globalmente.")
        logger.info(f"Comandos sincronizados globalmente por '{ctx.author.name}'.")

@bot.command()
@commands.is_owner()
async def unsync(ctx: commands.Context, guild: Optional[discord.Guild] = None):
    """Remove os comandos de barra do Discord."""
    bot.tree.clear_commands(guild=guild)
    await bot.tree.sync(guild=guild)
    await ctx.send(f"Comandos de barra removidos.")
    logger.warning(f"Comandos de barra removidos por '{ctx.author.name}'.")

@bot.command()
@commands.is_owner()
async def resync(ctx: commands.Context, guild: Optional[discord.Guild] = None):
    """Executa um unsync seguido de um sync para forçar a atualização."""
    await unsync(ctx, guild)
    await sync(ctx, guild)

# --- Tratamento de Erros de Comando ---
@bot.event
async def on_command_error(ctx: commands.Context, error):
    """Tratador de erros global para comandos de texto."""
    if isinstance(error, commands.CommandNotFound):
        return # Ignora comandos que não existem
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("Você não tem permissão para usar este comando.")
    elif isinstance(error, commands.NotOwner):
        await ctx.send("Este comando é restrito ao dono do bot.")
    else:
        logger.error(f"Erro ao executar o comando '{ctx.command}': {error}", exc_info=error)
        await ctx.send("Ocorreu um erro ao executar este comando. Verifique os logs para mais detalhes.")

# --- Comando de Log (Apenas para o Dono do Bot) ---
@bot.command()
@commands.is_owner()
async def log(ctx: commands.Context, lines: int = 25):
    """Mostra as últimas N linhas do arquivo de log."""
    log_file = 'discord_bot.log'
    if not os.path.exists(log_file):
        return await ctx.send("Arquivo de log não encontrado.")
    
    with open(log_file, 'r', encoding='utf-8') as f:
        log_content = f.readlines()
    
    last_lines = "".join(log_content[-lines:])
    
    if len(last_lines) > 1990:
        # Se for muito grande, envia como arquivo para não exceder o limite do Discord
        with open("log_export.txt", "w", encoding="utf-8") as f:
            f.write(last_lines)
        await ctx.send("O log é muito grande. Enviando como arquivo.", file=discord.File("log_export.txt"))
        os.remove("log_export.txt")
    else:
        await ctx.send(f"```\n{last_lines}\n```")

# --- Ponto de Entrada Principal ---
if __name__ == "__main__":
    if not BOT_TOKEN:
        logger.critical("O TOKEN do Discord não foi encontrado! Verifique seu arquivo .env e se o nome é DISCORD_TOKEN.")
    else:
        # Inicia o bot. O log_handler=None impede que a biblioteca discord.py configure seu próprio logger.
        # Nós já configuramos o nosso, que é mais completo.
        bot.run(BOT_TOKEN, log_handler=None)

