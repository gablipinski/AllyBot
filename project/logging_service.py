import traceback
from datetime import datetime

import discord
from discord.ext import commands

from project.config import AppConfig


class LogService:
    def __init__(self, bot: commands.Bot, config: AppConfig):
        self.bot = bot
        self.config = config

    async def enviar_log(
        self,
        titulo: str,
        descricao: str,
        cor: discord.Color,
        thumbnail: str | None = None,
        channel_id: int | None = None,
    ):
        target_channel_id = (
            channel_id if channel_id and channel_id > 0 else self.config.default_log_channel_id
        )

        # Try requested channel first, then fallback to default channel.
        channel_candidates = [target_channel_id]
        if target_channel_id != self.config.default_log_channel_id:
            channel_candidates.append(self.config.default_log_channel_id)

        last_error: Exception | None = None
        for candidate_channel_id in channel_candidates:
            try:
                canal = self.bot.get_channel(candidate_channel_id)
                if canal is None:
                    canal = await self.bot.fetch_channel(candidate_channel_id)

                if not isinstance(canal, (discord.TextChannel, discord.Thread)):
                    raise RuntimeError(
                        f"Canal {candidate_channel_id} nao e um canal de texto/thread."
                    )

                embed = discord.Embed(title=titulo, description=descricao, color=cor)
                embed.set_footer(text=f"Tarabot (creditos ao autor) • {datetime.now().strftime('%d/%m %H:%M')}")
                if thumbnail:
                    embed.set_thumbnail(url=thumbnail)
                await canal.send(embed=embed)

                print(f"[LOG] ({candidate_channel_id}) {titulo}: {descricao}")
                return
            except Exception as exc:
                last_error = exc
                print(
                    f"[ERRO] Falha ao enviar_log no canal {candidate_channel_id}: {exc}"
                )

        print(
            "[ERRO] Nenhum canal de log disponivel. "
            f"Ultimo erro: {last_error}"
        )

    async def log_exception(self, contexto: str, exc: Exception, extra: str | None = None):
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1200:]
        desc = f"Contexto: {contexto}\nErro: {exc}"
        if extra:
            desc += f"\nExtra: {extra}"
        desc += f"\n```{tb}```"
        await self.enviar_log(
            "Erro",
            desc,
            discord.Color.red(),
            channel_id=self.config.error_log_channel_id,
        )

    async def print_startup_command_scope(self, guild: discord.Guild | None):
        command_names = sorted(
            [
                cmd.qualified_name
                for cmd in self.bot.commands
                if not getattr(cmd, "hidden", False)
            ]
        )

        print("[BOOT] Comandos registrados:")
        if not command_names:
            print("  - Nenhum comando registrado")
        else:
            for name in command_names:
                print(f"  - {self.config.command_prefix}{name}")

        print("[BOOT] Canais de texto onde o bot consegue ler mensagens:")
        if not self.bot.user:
            print("  - Usuario do bot nao disponivel no momento do startup")
            return

        guilds_to_check: list[discord.Guild] = []
        if guild is not None:
            guilds_to_check.append(guild)
        else:
            guilds_to_check.extend(self.bot.guilds)

        if not guilds_to_check:
            print("  - Nenhuma guild disponivel no momento do startup")
            return

        allowed_channels = []
        for current_guild in guilds_to_check:
            me = current_guild.get_member(self.bot.user.id)
            if not me:
                continue

            channels = list(current_guild.text_channels)
            if not channels:
                try:
                    fetched_channels = await current_guild.fetch_channels()
                    channels = [
                        c for c in fetched_channels if isinstance(c, discord.TextChannel)
                    ]
                except Exception:
                    channels = []

            for channel in channels:
                perms = channel.permissions_for(me)
                if perms.read_messages:
                    allowed_channels.append(
                        f"{current_guild.name}: #{channel.name} ({channel.id})"
                    )

        if not allowed_channels:
            print("  - Nenhum canal com permissao de leitura")
            return

        for channel_info in allowed_channels:
            print(f"  - {channel_info}")
