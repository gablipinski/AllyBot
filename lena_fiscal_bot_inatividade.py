import os
import json
import asyncio
import traceback
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import tasks, commands
from dotenv import load_dotenv
import aiosqlite

#######################################################################################################################################################################################################
# CONFIG (.env)
#######################################################################################################################################################################################################
load_dotenv()


def _req_int(name: str) -> int:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Variável obrigatória ausente no .env: {name}")
    return int(v)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Variável obrigatória ausente no .env: DISCORD_TOKEN")

ID_GUILD = _req_int("GUILD_ID")
ID_CARGO_INATIVO = _req_int("INATIVO_ROLE_ID")
ID_CANAL_LOGS = _req_int("LOG_CHANNEL_ID")

# Anti-spy (canal de voz oculto)
ID_CANAL_VOZ_GATILHO = int(os.getenv("TRIGGER_VOICE_CHANNEL_ID", "0"))
ID_CANAL_VOZ_OCULTO = int(os.getenv("HIDDEN_VOICE_CHANNEL_ID", "0"))
ID_CARGO_ACESSO_OCULTO = int(os.getenv("HIDDEN_ACCESS_ROLE_ID", "0"))

DIAS_PARA_INATIVO = int(os.getenv("DIAS_PARA_INATIVO", "7"))
DIAS_PARA_REVISAO = int(os.getenv("DIAS_PARA_REVISAO", "30"))  # relatório p/ kick manual
DB_PATH = os.getenv("DB_PATH", "dados.db")
LEGACY_JSON_PATH = os.getenv("LEGACY_JSON_PATH", "dados.json")
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "20"))

# Remover (patentes)
CARGOS_PARA_REMOVER = [
    1184578793243947160, 1118006053083283557, 1376724256548720720,
    1398728831086497926, 1398730283116789830, 1398728890138099734,
    1437511276535349350, 1398731072199589978, 1398734538863149096,
    930609047584010241, 1091176128842043523, 1091176240624451686,
    930608651545239582, 930608929359142972, 1428394625869283492
]

# Imunes (não rebaixa automaticamente)
CARGOS_IMUNES = [
    969636554643501096, 1398720861204385792, 930609161740361821,
    1398723533013520475, 1424884260082417776
]

# Autorizados a comandos (além de Admin)
CARGOS_AUTORIZADOS_COMANDOS = {
    1398720861204385792,  # Líder de Aliança
    930609161740361821,   # Líder de Guild
    1398723533013520475,  # Organizador Geral
}

def has_admin_or_allowed_role():
    async def predicate(ctx: commands.Context):
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return False
        if ctx.author.guild_permissions.administrator:
            return True
        return any(r.id in CARGOS_AUTORIZADOS_COMANDOS for r in ctx.author.roles)
    return commands.check(predicate)

def _perm_msg():
    return "Sem permissão. Apenas Administrador, Líder de Aliança, Líder de Guild ou Organizador Geral podem usar este comando."

def hidden_guard_enabled() -> bool:
    return all(x > 0 for x in (ID_CANAL_VOZ_GATILHO, ID_CANAL_VOZ_OCULTO, ID_CARGO_ACESSO_OCULTO))

#######################################################################################################################################################################################################
# INTENTS / BOT
#######################################################################################################################################################################################################
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

#######################################################################################################################################################################################################
# UTIL
#######################################################################################################################################################################################################
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()

def parse_iso(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d

async def enviar_log(titulo: str, descricao: str, cor: discord.Color, thumbnail: str | None = None):
    try:
        canal = bot.get_channel(ID_CANAL_LOGS)
        if canal:
            embed = discord.Embed(title=titulo, description=descricao, color=cor)
            embed.set_footer(text=f"Lena Fiscal • {datetime.now().strftime('%d/%m %H:%M')}")
            if thumbnail:
                embed.set_thumbnail(url=thumbnail)
            await canal.send(embed=embed)
        print(f"[LOG] {titulo}: {descricao}")
    except Exception as e:
        print(f"[ERRO] Falha ao enviar_log: {e}")

async def log_exception(contexto: str, e: Exception, extra: str | None = None):
    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))[-1200:]
    desc = f"Contexto: {contexto}\nErro: {e}"
    if extra:
        desc += f"\nExtra: {extra}"
    desc += f"\n```{tb}```"
    await enviar_log("Erro", desc, discord.Color.red())

async def ensure_hidden_voice_defaults(guild: discord.Guild):
    """Garante que o canal oculto fique invisível/inacessível por padrão para @everyone."""
    if not hidden_guard_enabled():
        return

    hidden = guild.get_channel(ID_CANAL_VOZ_OCULTO)
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
        await hidden.set_permissions(everyone, overwrite=overwrite, reason="Anti-spy: esconder canal por padrão.")

async def aplicar_guardiao_canal_oculto(member: discord.Member, before, after):
    """Move para o canal oculto apenas quem entra no gatilho e tem o cargo de acesso."""
    if member.bot or not hidden_guard_enabled():
        return

    guild = member.guild
    trigger = guild.get_channel(ID_CANAL_VOZ_GATILHO)
    hidden = guild.get_channel(ID_CANAL_VOZ_OCULTO)
    if not isinstance(trigger, discord.VoiceChannel) or not isinstance(hidden, discord.VoiceChannel):
        return

    # Reforça as permissões padrão de ocultação.
    await ensure_hidden_voice_defaults(guild)

    # Se saiu do oculto, remove o acesso individual para não deixar rastros.
    if before.channel and before.channel.id == hidden.id and (after.channel is None or after.channel.id != hidden.id):
        await hidden.set_permissions(member, overwrite=None, reason="Anti-spy: remove acesso ao sair do canal oculto.")

    # Só move quando o usuário entra no canal de gatilho.
    if not after.channel or after.channel.id != trigger.id:
        return

    has_access_role = any(r.id == ID_CARGO_ACESSO_OCULTO for r in member.roles)
    if not has_access_role:
        return

    user_overwrite = hidden.overwrites_for(member)
    user_overwrite.view_channel = True
    user_overwrite.connect = True
    user_overwrite.speak = True
    await hidden.set_permissions(member, overwrite=user_overwrite, reason="Anti-spy: acesso temporário ao canal oculto.")
    await member.move_to(hidden, reason="Anti-spy: entrou no canal de gatilho com cargo autorizado.")

@bot.event
async def on_command_error(ctx, error):
    # não poluir logs com comandos inexistentes
    if isinstance(error, commands.CommandNotFound):
        return
    raise error

#######################################################################################################################################################################################################
# DB (SQLite)
#######################################################################################################################################################################################################
_db_lock = asyncio.Lock()
_db_initialized = False

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  last_seen TEXT,
  inactive_since TEXT
);
"""

async def ensure_db():
    global _db_initialized
    if _db_initialized:
        return
    async with _db_lock:
        if _db_initialized:
            return
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(CREATE_SQL)
            await db.commit()
        _db_initialized = True

async def db_get(user_id: int) -> tuple[str | None, str | None]:
    await ensure_db()
    async with _db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT last_seen, inactive_since FROM users WHERE user_id = ?", (user_id,)) as cur:
                row = await cur.fetchone()
                return (row[0], row[1]) if row else (None, None)

async def db_upsert_last_seen(user_id: int, last_seen_iso: str):
    await ensure_db()
    async with _db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO users(user_id, last_seen) VALUES(?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen",
                (user_id, last_seen_iso),
            )
            await db.commit()

async def db_set_inactive_since(user_id: int, inactive_since_iso: str):
    await ensure_db()
    async with _db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO users(user_id, inactive_since) VALUES(?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET inactive_since=excluded.inactive_since",
                (user_id, inactive_since_iso),
            )
            await db.commit()

async def db_clear_inactive_since(user_id: int):
    await ensure_db()
    async with _db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO users(user_id, inactive_since) VALUES(?, NULL) "
                "ON CONFLICT(user_id) DO UPDATE SET inactive_since=NULL",
                (user_id,),
            )
            await db.commit()

async def db_clear_inactive_if_activity_newer(user_id: int):
    last_seen_str, inactive_since_str = await db_get(user_id)
    if not last_seen_str or not inactive_since_str:
        return
    try:
        if parse_iso(last_seen_str) > parse_iso(inactive_since_str):
            await db_clear_inactive_since(user_id)
    except Exception:
        await db_clear_inactive_since(user_id)

async def migrate_legacy_json_if_exists():
    await ensure_db()
    if not os.path.exists(LEGACY_JSON_PATH):
        return
    async with _db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cur:
                row = await cur.fetchone()
                count = int(row[0]) if row else 0
    if count > 0:
        return

    try:
        with open(LEGACY_JSON_PATH, "r", encoding="utf-8") as f:
            legacy = json.load(f)
    except Exception as e:
        await log_exception("migração: leitura do JSON legado", e)
        return

    try:
        async with _db_lock:
            async with aiosqlite.connect(DB_PATH) as db:
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
        await enviar_log("Migração concluída", f"Migrado {LEGACY_JSON_PATH} -> {DB_PATH}.", discord.Color.green())
    except Exception as e:
        await log_exception("migração: gravação no DB", e)

#######################################################################################################################################################################################################
# EVENTOS (atividade)
#######################################################################################################################################################################################################
@bot.event
async def on_ready():
    await ensure_db()
    await migrate_legacy_json_if_exists()
    guild = bot.get_guild(ID_GUILD)
    if guild:
        try:
            await ensure_hidden_voice_defaults(guild)
        except Exception as e:
            await log_exception("on_ready: configurar canal oculto", e)
    if not verificar_inatividade.is_running():
        verificar_inatividade.start()

    await enviar_log(
        "Bot online",
        f"Online como {bot.user}\nInativo após {DIAS_PARA_INATIVO} dias\nRevisão manual após {DIAS_PARA_REVISAO}+ dias no Inativo",
        discord.Color.blue(),
    )

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    try:
        uid = int(message.author.id)
        await db_upsert_last_seen(uid, iso(utcnow()))
        role = message.guild.get_role(ID_CARGO_INATIVO) if message.guild else None
        if role and role in getattr(message.author, "roles", []):
            await db_clear_inactive_since(uid)
    except Exception as e:
        await log_exception("on_message: atualizar last_seen", e, extra=f"author_id={message.author.id}")

@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    if member.bot:
        return
    try:
        if after.channel is not None:
            uid = int(member.id)
            await db_upsert_last_seen(uid, iso(utcnow()))
            role = member.guild.get_role(ID_CARGO_INATIVO) if member.guild else None
            if role and role in member.roles:
                await db_clear_inactive_since(uid)

        await aplicar_guardiao_canal_oculto(member, before, after)
    except Exception as e:
        await log_exception("on_voice_state_update: atividade/guardiao_voz", e, extra=f"member_id={member.id}")

#######################################################################################################################################################################################################
# LÓGICA: varredura
#######################################################################################################################################################################################################
async def executar_verificacao_inatividade(guild: discord.Guild) -> dict:
    role_inativo = guild.get_role(ID_CARGO_INATIVO)
    if not role_inativo:
        return {"ok": False, "erro": "Cargo Inativo não encontrado."}

    agora = utcnow()
    contagem_inativos = 0
    candidatos_kick = []

    for member in guild.members:
        if member.bot:
            continue
        if any(r.id in CARGOS_IMUNES for r in member.roles):
            continue

        last_seen_str, inactive_since_str = await db_get(member.id)
        last_seen = parse_iso(last_seen_str) if last_seen_str else agora  # anistia
        dias_sem_interagir = (agora - last_seen).days

        await db_clear_inactive_if_activity_newer(member.id)
        _, inactive_since_str = await db_get(member.id)

        # Já é Inativo
        if role_inativo in member.roles:
            if not inactive_since_str:
                await db_set_inactive_since(member.id, iso(agora))
                continue
            dias_no_limbo = (agora - parse_iso(inactive_since_str)).days
            if dias_no_limbo >= DIAS_PARA_REVISAO:
                candidatos_kick.append((dias_no_limbo, member.mention, inactive_since_str[:10]))
            continue

        # Ativo -> virar Inativo
        if dias_sem_interagir >= DIAS_PARA_INATIVO:
            try:
                await member.add_roles(role_inativo, reason="Marcado como inativo automaticamente.")
                for cargo_id in CARGOS_PARA_REMOVER:
                    cargo = guild.get_role(cargo_id)
                    if cargo and cargo in member.roles:
                        await member.remove_roles(cargo, reason="Rebaixamento por inatividade.")
                await db_set_inactive_since(member.id, iso(agora))
                contagem_inativos += 1
            except Exception as e:
                await log_exception("marcar como inativo", e, extra=f"member_id={member.id}")

    try:
        if candidatos_kick:
            candidatos_kick.sort(reverse=True)
            for i in range(0, len(candidatos_kick), PAGE_SIZE):
                chunk = candidatos_kick[i:i + PAGE_SIZE]
                texto = "\n".join([f"{m} — {d} dias (desde {dt})" for d, m, dt in chunk])
                await enviar_log("Lista para kick manual", texto, discord.Color.orange())
        else:
            await enviar_log("Lista para kick manual", "Nenhum membro elegível hoje.", discord.Color.green())
    except Exception as e:
        await log_exception("relatório kick manual", e)

    await enviar_log(
        "Relatório final",
        f"Novos inativos: {contagem_inativos}\nElegíveis p/ kick manual: {len(candidatos_kick)}",
        discord.Color.blue(),
    )

    return {"ok": True, "novos_inativos": contagem_inativos, "elegiveis": len(candidatos_kick)}

@tasks.loop(hours=24)
async def verificar_inatividade():
    guild = bot.get_guild(ID_GUILD)
    if not guild:
        return
    await executar_verificacao_inatividade(guild)

@verificar_inatividade.before_loop
async def before_verificar_inatividade():
    await bot.wait_until_ready()
    await asyncio.sleep(30)

#######################################################################################################################################################################################################
# COMANDOS
#######################################################################################################################################################################################################
@bot.command(name="test")
async def test_cmd(ctx):
    await ctx.send("TEST DONE")

@bot.command()
@has_admin_or_allowed_role()
async def radar(ctx):
    """Membros com 4 a (DIAS_PARA_INATIVO-1) dias sem interagir (ex.: 4–6)."""
    try:
        guild = ctx.guild
        role_inativo = guild.get_role(ID_CARGO_INATIVO)
        agora = utcnow()
        risco = []

        for member in guild.members:
            if member.bot:
                continue
            if role_inativo in member.roles:
                continue
            if any(r.id in CARGOS_IMUNES for r in member.roles):
                continue

            last_seen_str, _ = await db_get(member.id)
            last_seen = parse_iso(last_seen_str) if last_seen_str else agora
            dias = (agora - last_seen).days
            if 4 <= dias < DIAS_PARA_INATIVO:
                risco.append((dias, member.mention))

        risco.sort(key=lambda x: x[0], reverse=True)
        if not risco:
            await ctx.send(f"Ninguém na zona de risco (4 a {DIAS_PARA_INATIVO-1} dias).")
            return

        linhas = "\n".join([f"{d} dias: {m}" for d, m in risco[:20]])
        await ctx.send(linhas)
    except Exception as e:
        await log_exception("comando radar", e)
        await ctx.send("Erro ao executar radar (veja o canal de logs).")

@radar.error
async def radar_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send(_perm_msg())

@bot.command()
@has_admin_or_allowed_role()
async def kicklist(ctx, dias: int = DIAS_PARA_REVISAO):
    """Lista membros com cargo Inativo há X+ dias (kick manual)."""
    try:
        guild = ctx.guild
        role_inativo = guild.get_role(ID_CARGO_INATIVO)
        agora = utcnow()
        candidatos = []

        for member in guild.members:
            if member.bot:
                continue
            if any(r.id in CARGOS_IMUNES for r in member.roles):
                continue
            if role_inativo not in member.roles:
                continue

            await db_clear_inactive_if_activity_newer(member.id)
            _, inactive_since_str = await db_get(member.id)
            if not inactive_since_str:
                continue

            dias_no_limbo = (agora - parse_iso(inactive_since_str)).days
            if dias_no_limbo >= dias:
                candidatos.append((dias_no_limbo, member.mention, inactive_since_str[:10]))

        candidatos.sort(reverse=True)
        if not candidatos:
            await ctx.send(f"Ninguém com {dias}+ dias no Inativo.")
            return

        for i in range(0, len(candidatos), PAGE_SIZE):
            chunk = candidatos[i:i + PAGE_SIZE]
            texto = "\n".join([f"{m} — {d} dias (desde {dt})" for d, m, dt in chunk])
            await ctx.send(texto)
    except Exception as e:
        await log_exception("comando kicklist", e)
        await ctx.send("Erro ao gerar kicklist (veja o canal de logs).")

@kicklist.error
async def kicklist_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send(_perm_msg())

@bot.command()
@has_admin_or_allowed_role()
async def runcheck(ctx):
    """Roda a verificação de inatividade imediatamente."""
    await ctx.send("Rodando verificação agora...")
    try:
        resumo = await executar_verificacao_inatividade(ctx.guild)
        if not resumo.get("ok"):
            await ctx.send(f"Falha: {resumo.get('erro', 'erro desconhecido')}")
            return
        await ctx.send(f"Check concluído. Novos inativos: {resumo['novos_inativos']} | Elegíveis: {resumo['elegiveis']}")
    except Exception as e:
        await log_exception("comando runcheck", e)
        await ctx.send("Erro ao rodar runcheck (veja o canal de logs).")

@runcheck.error
async def runcheck_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send(_perm_msg())

@bot.command()
@has_admin_or_allowed_role()
async def diag(ctx):
    """Diagnóstico (inclui quem não tem registro no DB)."""
    try:
        guild = ctx.guild
        role_inativo = guild.get_role(ID_CARGO_INATIVO)
        agora = utcnow()

        total = len(guild.members)
        bots_count = sum(1 for m in guild.members if m.bot)
        imunes_count = sum(1 for m in guild.members if any(r.id in CARGOS_IMUNES for r in m.roles))
        com_cargo_inativo = sum(1 for m in guild.members if (not m.bot) and (role_inativo in m.roles))

        sem_registro = 0
        sem_registro_nao_inativos = 0
        inativos_sem_inactive_since = 0
        elegiveis_para_inativar = 0

        for m in guild.members:
            if m.bot:
                continue
            if any(r.id in CARGOS_IMUNES for r in m.roles):
                continue

            last_seen_str, inactive_since_str = await db_get(m.id)

            if not last_seen_str:
                sem_registro += 1
                if role_inativo not in m.roles:
                    sem_registro_nao_inativos += 1
                if role_inativo in m.roles and not inactive_since_str:
                    inativos_sem_inactive_since += 1
                continue

            dias = (agora - parse_iso(last_seen_str)).days
            if role_inativo not in m.roles and dias >= DIAS_PARA_INATIVO:
                elegiveis_para_inativar += 1

            if role_inativo in m.roles and not inactive_since_str:
                inativos_sem_inactive_since += 1

        resumo_txt = (
            f"Total membros: {total}\n"
            f"Bots: {bots_count}\n"
            f"Imunes (ignorados): {imunes_count}\n"
            f"Com cargo Inativo: {com_cargo_inativo}\n"
            f"Sem registro no DB (last_seen vazio): {sem_registro}\n"
            f"Sem registro e NAO inativos: {sem_registro_nao_inativos}\n"
            f"Inativos sem inactive_since no DB: {inativos_sem_inactive_since}\n"
            f"Elegiveis para virar Inativo agora (>= {DIAS_PARA_INATIVO} dias): {elegiveis_para_inativar}"
        )
        await ctx.send(f"Diagnostico:\n```{resumo_txt}```")
    except Exception as e:
        await log_exception("comando diag", e)
        await ctx.send("Erro ao executar diag (veja o canal de logs).")

@diag.error
async def diag_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send(_perm_msg())

#######################################################################################################################################################################################################
# START
#######################################################################################################################################################################################################
bot.run(TOKEN)
