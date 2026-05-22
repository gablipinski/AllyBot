import discord
from discord.ext import commands

from project.config import load_config, token_bot_id
from project.hunt_event import HuntEventService
from project.logging_service import LogService
from project.nixspy import NixSpyService


config = load_config()

intents = discord.Intents.default()
intents.message_content = config.enable_message_content_intent
intents.members = config.enable_members_intent
intents.voice_states = True

bot = commands.Bot(command_prefix=config.command_prefix, intents=intents, help_command=None)
logger = LogService(bot, config)
nixspy = NixSpyService(config, logger)
hunt_event = HuntEventService(config, logger)


def _bot_log_room_id() -> int:
    return config.bot_log_channel_id if config.bot_log_channel_id > 0 else config.default_log_channel_id


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandNotFound):
        return
    raise error


@bot.event
async def on_ready():
    print(
        "[BOOT] Intents efetivos: "
        f"message_content={intents.message_content}, "
        f"members={intents.members}, voice_states={intents.voice_states}"
    )

    guild = bot.get_guild(config.guild_id)
    if not guild:
        for current_guild in bot.guilds:
            if current_guild.id == config.guild_id:
                guild = current_guild
                break

    if guild:
        try:
            await nixspy.ensure_hidden_voice_defaults(guild)
        except Exception as exc:
            await logger.log_exception("on_ready: configurar canal oculto", exc)

    await logger.print_startup_command_scope(guild)

    await logger.enviar_log(
        "Bot connected",
        f"Online como {bot.user}",
        discord.Color.blue(),
        channel_id=config.bot_log_channel_id,
    )


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    try:
        await hunt_event.process_hunt_message(message)
    except Exception as exc:
        await logger.log_exception(
            "on_message: processar hunt_event",
            exc,
            extra=f"author_id={message.author.id}",
        )


@bot.event
async def on_command(ctx: commands.Context):
    try:
        comando = ctx.command.qualified_name if ctx.command else "desconhecido"
        prefixo = ctx.prefix or config.command_prefix
        autor = f"{ctx.author} ({ctx.author.id})"
        canal = f"#{ctx.channel}" if ctx.guild else "DM"
        servidor = ctx.guild.name if ctx.guild else "DM"
        avatar = (
            ctx.author.display_avatar.url
            if isinstance(ctx.author, discord.Member)
            else None
        )

        await logger.enviar_log(
            "Comando reconhecido",
            (
                f"Usuario: {autor}\n"
                f"Comando: {prefixo}{comando}\n"
                f"Canal: {canal}\n"
                f"Servidor: {servidor}"
            ),
            discord.Color.gold(),
            thumbnail=avatar,
            channel_id=config.command_log_channel_id,
        )
    except Exception as exc:
        await logger.log_exception("on_command: log de reconhecimento", exc)


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    if member.bot:
        return

    try:
        await nixspy.handle_voice_state_update(member, before, after)
    except Exception as exc:
        await logger.log_exception(
            "on_voice_state_update: guardiao_voz",
            exc,
            extra=f"member_id={member.id}",
        )


@bot.command(name="version")
async def version_cmd(ctx: commands.Context):
    await ctx.send(f"Version: {config.version}")


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    if not ctx.guild or ctx.channel.id != _bot_log_room_id():
        await ctx.send("Use este comando na sala log-bot.")
        return

    lines = [
        "Comandos disponiveis (organizados por funcionalidade):",
        "",
        "[Core]",
        f"- {config.command_prefix}help",
        f"- {config.command_prefix}version",
        "",
        "[Hunt Event]",
        f"- {config.command_prefix}setstartdate YYYY-MM-DD (log-bot, admin/lider)",
        f"- {config.command_prefix}setenddate YYYY-MM-DD (log-bot, admin/lider)",
        f"- {config.command_prefix}rank (usar no canal do evento; resposta via DM)",
        f"- {config.command_prefix}huntrank [top]",
        f"- {config.command_prefix}huntpoints <nome_personagem>",
        "",
        "[BattleNix-AntiSpy]",
        "- Sem comando manual (funciona automaticamente por evento de voz).",
    ]

    await ctx.send("\n".join(lines))


hunt_event.register_commands(bot)

print(
    "[BOOT] Intentos solicitados: "
    f"message_content={intents.message_content}, "
    f"members={intents.members}, voice_states={intents.voice_states}"
)
print(f"[BOOT] Bot ID no token: {token_bot_id(config.token)}")

try:
    bot.run(config.token)
except discord.errors.PrivilegedIntentsRequired:
    raise RuntimeError(
        "Privileged intents nao habilitadas para este bot. "
        "No Discord Developer Portal, ative 'Server Members Intent' e 'Message Content Intent' "
        "na aba Bot, ou desative no .env: ENABLE_MEMBERS_INTENT/ENABLE_MESSAGE_CONTENT_INTENT."
    )
