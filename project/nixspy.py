import discord

from project.config import AppConfig
from project.logging_service import LogService


class NixSpyService:
    def __init__(self, config: AppConfig, logger: LogService):
        self.config = config
        self.logger = logger

    def _target_log_channel_id(self) -> int:
        if self.config.bot_log_channel_id > 0:
            return self.config.bot_log_channel_id
        return self.config.trigger_log_channel_id

    async def ensure_hidden_voice_defaults(self, guild: discord.Guild):
        """Garante que o canal oculto fique invisivel/inacessivel por padrao para @everyone."""
        if not self.config.hidden_guard_enabled:
            return

        hidden = guild.get_channel(self.config.hidden_voice_channel_id)
        if not isinstance(hidden, discord.VoiceChannel):
            return

        everyone = guild.default_role
        overwrite = hidden.overwrites_for(everyone)
        changed = False

        if overwrite.view_channel is not False:
            overwrite.view_channel = False
            changed = True
        if overwrite.connect is not False:
            overwrite.connect = False
            changed = True

        if changed:
            await hidden.set_permissions(
                everyone,
                overwrite=overwrite,
                reason="Anti-spy: esconder canal por padrao.",
            )

    async def handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        """Move para o canal oculto apenas quem entra no gatilho e tem o cargo de acesso."""
        if member.bot or not self.config.hidden_guard_enabled:
            return

        guild = member.guild
        trigger = guild.get_channel(self.config.trigger_voice_channel_id)
        hidden = guild.get_channel(self.config.hidden_voice_channel_id)
        if not isinstance(trigger, discord.VoiceChannel) or not isinstance(
            hidden, discord.VoiceChannel
        ):
            return

        await self.ensure_hidden_voice_defaults(guild)

        if (
            before.channel
            and before.channel.id == hidden.id
            and (after.channel is None or after.channel.id != hidden.id)
        ):
            await hidden.set_permissions(
                member,
                overwrite=None,
                reason="Anti-spy: remove acesso ao sair do canal oculto.",
            )

        entered_trigger = (
            after.channel
            and after.channel.id == trigger.id
            and (before.channel is None or before.channel.id != trigger.id)
        )
        if not entered_trigger:
            return

        has_access_role = any(
            role.id == self.config.hidden_access_role_id for role in member.roles
        )
        status = "Possui tag de acesso" if has_access_role else "Nao possui tag de acesso"
        color = discord.Color.green() if has_access_role else discord.Color.red()

        await self.logger.enviar_log(
            "Entrada no canal gatilho",
            (
                f"Usuario: {member.mention} ({member.id})\n"
                f"Canal gatilho: {trigger.mention}\n"
                f"Servidor: {guild.name}\n"
                f"Status: {status}"
            ),
            color,
            thumbnail=member.display_avatar.url,
            channel_id=self._target_log_channel_id(),
        )

        if not has_access_role:
            return

        user_overwrite = hidden.overwrites_for(member)
        user_overwrite.view_channel = True
        user_overwrite.connect = True
        user_overwrite.speak = True
        await hidden.set_permissions(
            member,
            overwrite=user_overwrite,
            reason="Anti-spy: acesso temporario ao canal oculto.",
        )
        await member.move_to(
            hidden,
            reason="Anti-spy: entrou no canal de gatilho com cargo autorizado.",
        )
        await self.logger.enviar_log(
            "Trigger acionado: usuario movido",
            (
                f"Usuario: {member.mention} ({member.id})\n"
                f"Origem: {trigger.mention}\n"
                f"Destino: {hidden.mention}\n"
                f"Servidor: {guild.name}"
            ),
            discord.Color.green(),
            thumbnail=member.display_avatar.url,
            channel_id=self._target_log_channel_id(),
        )
