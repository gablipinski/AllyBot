import asyncio
import json
import os
from datetime import datetime, timezone

import aiosqlite
import discord
from discord.ext import commands, tasks

from project.config import AppConfig
from project.logging_service import LogService


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class InactivityService:
    def __init__(self, config: AppConfig, logger: LogService):
        self.config = config
        self.logger = logger
        self._db_lock = asyncio.Lock()
        self._db_initialized = False
        self._check_loop: tasks.Loop | None = None

    @staticmethod
    def _perm_msg() -> str:
        return (
            "Sem permissao. Apenas Administrador, Lider de Alianca, "
            "Lider de Guild ou Organizador Geral podem usar este comando."
        )

    def has_admin_or_allowed_role(self):
        async def predicate(ctx: commands.Context):
            if not ctx.guild or not isinstance(ctx.author, discord.Member):
                return False
            if ctx.author.guild_permissions.administrator:
                return True
            return any(
                role.id in self.config.allowed_command_role_ids
                for role in ctx.author.roles
            )

        return commands.check(predicate)

    async def ensure_db(self):
        if self._db_initialized:
            return
        async with self._db_lock:
            if self._db_initialized:
                return
            async with aiosqlite.connect(self.config.db_path) as db:
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                      user_id INTEGER PRIMARY KEY,
                      last_seen TEXT,
                      inactive_since TEXT
                    );
                    """
                )
                await db.commit()
            self._db_initialized = True

    async def db_get(self, user_id: int) -> tuple[str | None, str | None]:
        await self.ensure_db()
        async with self._db_lock:
            async with aiosqlite.connect(self.config.db_path) as db:
                async with db.execute(
                    "SELECT last_seen, inactive_since FROM users WHERE user_id = ?",
                    (user_id,),
                ) as cur:
                    row = await cur.fetchone()
                    return (row[0], row[1]) if row else (None, None)

    async def db_upsert_last_seen(self, user_id: int, last_seen_iso: str):
        await self.ensure_db()
        async with self._db_lock:
            async with aiosqlite.connect(self.config.db_path) as db:
                await db.execute(
                    "INSERT INTO users(user_id, last_seen) VALUES(?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen",
                    (user_id, last_seen_iso),
                )
                await db.commit()

    async def db_set_inactive_since(self, user_id: int, inactive_since_iso: str):
        await self.ensure_db()
        async with self._db_lock:
            async with aiosqlite.connect(self.config.db_path) as db:
                await db.execute(
                    "INSERT INTO users(user_id, inactive_since) VALUES(?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET inactive_since=excluded.inactive_since",
                    (user_id, inactive_since_iso),
                )
                await db.commit()

    async def db_clear_inactive_since(self, user_id: int):
        await self.ensure_db()
        async with self._db_lock:
            async with aiosqlite.connect(self.config.db_path) as db:
                await db.execute(
                    "INSERT INTO users(user_id, inactive_since) VALUES(?, NULL) "
                    "ON CONFLICT(user_id) DO UPDATE SET inactive_since=NULL",
                    (user_id,),
                )
                await db.commit()

    async def db_clear_inactive_if_activity_newer(self, user_id: int):
        last_seen_str, inactive_since_str = await self.db_get(user_id)
        if not last_seen_str or not inactive_since_str:
            return
        try:
            if parse_iso(last_seen_str) > parse_iso(inactive_since_str):
                await self.db_clear_inactive_since(user_id)
        except Exception:
            await self.db_clear_inactive_since(user_id)

    async def migrate_legacy_json_if_exists(self):
        await self.ensure_db()
        if not os.path.exists(self.config.legacy_json_path):
            return

        async with self._db_lock:
            async with aiosqlite.connect(self.config.db_path) as db:
                async with db.execute("SELECT COUNT(*) FROM users") as cur:
                    row = await cur.fetchone()
                    count = int(row[0]) if row else 0
        if count > 0:
            return

        try:
            with open(self.config.legacy_json_path, "r", encoding="utf-8") as file_handle:
                legacy = json.load(file_handle)
        except Exception as exc:
            await self.logger.log_exception("migracao: leitura do JSON legado", exc)
            return

        try:
            async with self._db_lock:
                async with aiosqlite.connect(self.config.db_path) as db:
                    for uid_str, payload in legacy.items():
                        try:
                            uid = int(uid_str)
                        except ValueError:
                            continue
                        await db.execute(
                            "INSERT INTO users(user_id, last_seen, inactive_since) VALUES(?, ?, ?) "
                            "ON CONFLICT(user_id) DO UPDATE SET "
                            "last_seen=COALESCE(excluded.last_seen, users.last_seen), "
                            "inactive_since=COALESCE(excluded.inactive_since, users.inactive_since)",
                            (uid, payload.get("last_seen"), payload.get("inactive_since")),
                        )
                    await db.commit()

            await self.logger.enviar_log(
                "Migracao concluida",
                f"Migrado {self.config.legacy_json_path} -> {self.config.db_path}.",
                discord.Color.green(),
                channel_id=self.config.migration_log_channel_id,
            )
        except Exception as exc:
            await self.logger.log_exception("migracao: gravacao no DB", exc)

    async def handle_message_activity(self, message: discord.Message):
        uid = int(message.author.id)
        await self.db_upsert_last_seen(uid, iso(utcnow()))
        role = message.guild.get_role(self.config.inativo_role_id) if message.guild else None
        if role and role in getattr(message.author, "roles", []):
            await self.db_clear_inactive_since(uid)

    async def handle_voice_activity(
        self,
        member: discord.Member,
        after: discord.VoiceState,
    ):
        if after.channel is None:
            return

        uid = int(member.id)
        await self.db_upsert_last_seen(uid, iso(utcnow()))
        role = member.guild.get_role(self.config.inativo_role_id) if member.guild else None
        if role and role in member.roles:
            await self.db_clear_inactive_since(uid)

    async def executar_verificacao_inatividade(self, guild: discord.Guild) -> dict:
        role_inativo = guild.get_role(self.config.inativo_role_id)
        if not role_inativo:
            return {"ok": False, "erro": "Cargo Inativo nao encontrado."}

        agora = utcnow()
        contagem_inativos = 0
        candidatos_kick = []

        for member in guild.members:
            if member.bot:
                continue
            if any(role.id in self.config.immune_role_ids for role in member.roles):
                continue

            last_seen_str, inactive_since_str = await self.db_get(member.id)
            last_seen = parse_iso(last_seen_str) if last_seen_str else agora
            dias_sem_interagir = (agora - last_seen).days

            await self.db_clear_inactive_if_activity_newer(member.id)
            _, inactive_since_str = await self.db_get(member.id)

            if role_inativo in member.roles:
                if not inactive_since_str:
                    await self.db_set_inactive_since(member.id, iso(agora))
                    continue
                dias_no_limbo = (agora - parse_iso(inactive_since_str)).days
                if dias_no_limbo >= self.config.dias_para_revisao:
                    candidatos_kick.append(
                        (dias_no_limbo, member.mention, inactive_since_str[:10])
                    )
                continue

            if dias_sem_interagir >= self.config.dias_para_inativo:
                try:
                    await member.add_roles(
                        role_inativo, reason="Marcado como inativo automaticamente."
                    )
                    for cargo_id in self.config.remove_role_ids:
                        cargo = guild.get_role(cargo_id)
                        if cargo and cargo in member.roles:
                            await member.remove_roles(
                                cargo, reason="Rebaixamento por inatividade."
                            )
                    await self.db_set_inactive_since(member.id, iso(agora))
                    contagem_inativos += 1
                except Exception as exc:
                    await self.logger.log_exception(
                        "marcar como inativo", exc, extra=f"member_id={member.id}"
                    )

        try:
            if candidatos_kick:
                candidatos_kick.sort(reverse=True)
                for index in range(0, len(candidatos_kick), self.config.page_size):
                    chunk = candidatos_kick[index : index + self.config.page_size]
                    texto = "\n".join(
                        [f"{mention} - {dias} dias (desde {dt})" for dias, mention, dt in chunk]
                    )
                    await self.logger.enviar_log(
                        "Lista para kick manual",
                        texto,
                        discord.Color.orange(),
                        channel_id=self.config.report_log_channel_id,
                    )
            else:
                await self.logger.enviar_log(
                    "Lista para kick manual",
                    "Nenhum membro elegivel hoje.",
                    discord.Color.green(),
                    channel_id=self.config.report_log_channel_id,
                )
        except Exception as exc:
            await self.logger.log_exception("relatorio kick manual", exc)

        await self.logger.enviar_log(
            "Relatorio final",
            (
                f"Novos inativos: {contagem_inativos}\n"
                f"Elegiveis p/ kick manual: {len(candidatos_kick)}"
            ),
            discord.Color.blue(),
            channel_id=self.config.report_log_channel_id,
        )

        return {
            "ok": True,
            "novos_inativos": contagem_inativos,
            "elegiveis": len(candidatos_kick),
        }

    def start_scheduler(self, bot: commands.Bot):
        if self._check_loop and self._check_loop.is_running():
            return
        if self._check_loop is None:

            @tasks.loop(hours=24)
            async def check_loop():
                guild = bot.get_guild(self.config.guild_id)
                if guild:
                    await self.executar_verificacao_inatividade(guild)

            @check_loop.before_loop
            async def before_check_loop():
                await bot.wait_until_ready()
                await asyncio.sleep(30)

            self._check_loop = check_loop

        self._check_loop.start()

    def register_commands(self, bot: commands.Bot):
        @bot.command(name="radar")
        @self.has_admin_or_allowed_role()
        async def radar(ctx: commands.Context):
            """Membros com 4 a (DIAS_PARA_INATIVO-1) dias sem interagir (ex.: 4-6)."""
            try:
                guild = ctx.guild
                role_inativo = guild.get_role(self.config.inativo_role_id)
                agora = utcnow()
                risco = []

                for member in guild.members:
                    if member.bot:
                        continue
                    if role_inativo in member.roles:
                        continue
                    if any(role.id in self.config.immune_role_ids for role in member.roles):
                        continue

                    last_seen_str, _ = await self.db_get(member.id)
                    last_seen = parse_iso(last_seen_str) if last_seen_str else agora
                    dias = (agora - last_seen).days
                    if 4 <= dias < self.config.dias_para_inativo:
                        risco.append((dias, member.mention))

                risco.sort(key=lambda item: item[0], reverse=True)
                if not risco:
                    await ctx.send(
                        f"Ninguem na zona de risco (4 a {self.config.dias_para_inativo - 1} dias)."
                    )
                    return

                linhas = "\n".join([f"{dias} dias: {mention}" for dias, mention in risco[:20]])
                await ctx.send(linhas)
            except Exception as exc:
                await self.logger.log_exception("comando radar", exc)
                await ctx.send("Erro ao executar radar (veja o canal de logs).")

        @radar.error
        async def radar_error(ctx: commands.Context, error: Exception):
            if isinstance(error, commands.CheckFailure):
                await ctx.send(self._perm_msg())

        @bot.command(name="kicklist")
        @self.has_admin_or_allowed_role()
        async def kicklist(ctx: commands.Context, dias: int = 0):
            """Lista membros com cargo Inativo ha X+ dias (kick manual)."""
            try:
                min_dias = dias or self.config.dias_para_revisao
                guild = ctx.guild
                role_inativo = guild.get_role(self.config.inativo_role_id)
                agora = utcnow()
                candidatos = []

                for member in guild.members:
                    if member.bot:
                        continue
                    if any(role.id in self.config.immune_role_ids for role in member.roles):
                        continue
                    if role_inativo not in member.roles:
                        continue

                    await self.db_clear_inactive_if_activity_newer(member.id)
                    _, inactive_since_str = await self.db_get(member.id)
                    if not inactive_since_str:
                        continue

                    dias_no_limbo = (agora - parse_iso(inactive_since_str)).days
                    if dias_no_limbo >= min_dias:
                        candidatos.append(
                            (dias_no_limbo, member.mention, inactive_since_str[:10])
                        )

                candidatos.sort(reverse=True)
                if not candidatos:
                    await ctx.send(f"Ninguem com {min_dias}+ dias no Inativo.")
                    return

                for index in range(0, len(candidatos), self.config.page_size):
                    chunk = candidatos[index : index + self.config.page_size]
                    texto = "\n".join(
                        [f"{mention} - {dias_count} dias (desde {dt})" for dias_count, mention, dt in chunk]
                    )
                    await ctx.send(texto)
            except Exception as exc:
                await self.logger.log_exception("comando kicklist", exc)
                await ctx.send("Erro ao gerar kicklist (veja o canal de logs).")

        @kicklist.error
        async def kicklist_error(ctx: commands.Context, error: Exception):
            if isinstance(error, commands.CheckFailure):
                await ctx.send(self._perm_msg())

        @bot.command(name="runcheck")
        @self.has_admin_or_allowed_role()
        async def runcheck(ctx: commands.Context):
            """Roda a verificacao de inatividade imediatamente."""
            await ctx.send("Rodando verificacao agora...")
            try:
                resumo = await self.executar_verificacao_inatividade(ctx.guild)
                if not resumo.get("ok"):
                    await ctx.send(f"Falha: {resumo.get('erro', 'erro desconhecido')}")
                    return
                await ctx.send(
                    "Check concluido. "
                    f"Novos inativos: {resumo['novos_inativos']} | "
                    f"Elegiveis: {resumo['elegiveis']}"
                )
            except Exception as exc:
                await self.logger.log_exception("comando runcheck", exc)
                await ctx.send("Erro ao rodar runcheck (veja o canal de logs).")

        @runcheck.error
        async def runcheck_error(ctx: commands.Context, error: Exception):
            if isinstance(error, commands.CheckFailure):
                await ctx.send(self._perm_msg())

        @bot.command(name="diag")
        @self.has_admin_or_allowed_role()
        async def diag(ctx: commands.Context):
            """Diagnostico (inclui quem nao tem registro no DB)."""
            try:
                guild = ctx.guild
                role_inativo = guild.get_role(self.config.inativo_role_id)
                agora = utcnow()

                total = len(guild.members)
                bots_count = sum(1 for member in guild.members if member.bot)
                imunes_count = sum(
                    1
                    for member in guild.members
                    if any(role.id in self.config.immune_role_ids for role in member.roles)
                )
                com_cargo_inativo = sum(
                    1
                    for member in guild.members
                    if (not member.bot) and (role_inativo in member.roles)
                )

                sem_registro = 0
                sem_registro_nao_inativos = 0
                inativos_sem_inactive_since = 0
                elegiveis_para_inativar = 0

                for member in guild.members:
                    if member.bot:
                        continue
                    if any(role.id in self.config.immune_role_ids for role in member.roles):
                        continue

                    last_seen_str, inactive_since_str = await self.db_get(member.id)

                    if not last_seen_str:
                        sem_registro += 1
                        if role_inativo not in member.roles:
                            sem_registro_nao_inativos += 1
                        if role_inativo in member.roles and not inactive_since_str:
                            inativos_sem_inactive_since += 1
                        continue

                    dias = (agora - parse_iso(last_seen_str)).days
                    if role_inativo not in member.roles and dias >= self.config.dias_para_inativo:
                        elegiveis_para_inativar += 1

                    if role_inativo in member.roles and not inactive_since_str:
                        inativos_sem_inactive_since += 1

                resumo_txt = (
                    f"Total membros: {total}\n"
                    f"Bots: {bots_count}\n"
                    f"Imunes (ignorados): {imunes_count}\n"
                    f"Com cargo Inativo: {com_cargo_inativo}\n"
                    f"Sem registro no DB (last_seen vazio): {sem_registro}\n"
                    f"Sem registro e NAO inativos: {sem_registro_nao_inativos}\n"
                    f"Inativos sem inactive_since no DB: {inativos_sem_inactive_since}\n"
                    "Elegiveis para virar Inativo agora "
                    f"(>= {self.config.dias_para_inativo} dias): {elegiveis_para_inativar}"
                )
                await ctx.send(f"Diagnostico:\n```{resumo_txt}```")
            except Exception as exc:
                await self.logger.log_exception("comando diag", exc)
                await ctx.send("Erro ao executar diag (veja o canal de logs).")

        @diag.error
        async def diag_error(ctx: commands.Context, error: Exception):
            if isinstance(error, commands.CheckFailure):
                await ctx.send(self._perm_msg())
