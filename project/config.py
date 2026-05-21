import os
import base64
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv(override=True)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _req_int(name: str) -> int:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Variavel obrigatoria ausente no .env: {name}")
    return int(value)


def _req_int_list(name: str) -> list[int]:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Variavel obrigatoria ausente no .env: {name}")
    try:
        return [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise RuntimeError(
            f"Variavel {name} invalida. Use IDs inteiros separados por virgula."
        ) from exc


@dataclass(frozen=True)
class AppConfig:
    token: str
    guild_id: int
    inativo_role_id: int

    default_log_channel_id: int
    bot_log_channel_id: int
    command_log_channel_id: int
    error_log_channel_id: int
    trigger_log_channel_id: int
    report_log_channel_id: int
    migration_log_channel_id: int

    version: str
    command_prefix: str
    enable_message_content_intent: bool
    enable_members_intent: bool

    trigger_voice_channel_id: int
    hidden_voice_channel_id: int
    hidden_access_role_id: int

    dias_para_inativo: int
    dias_para_revisao: int
    db_path: str
    legacy_json_path: str
    page_size: int

    remove_role_ids: list[int]
    immune_role_ids: set[int]
    allowed_command_role_ids: set[int]

    @property
    def hidden_guard_enabled(self) -> bool:
        return all(
            value > 0
            for value in (
                self.trigger_voice_channel_id,
                self.hidden_voice_channel_id,
                self.hidden_access_role_id,
            )
        )


def load_config() -> AppConfig:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Variavel obrigatoria ausente no .env: DISCORD_TOKEN")

    return AppConfig(
        token=token,
        guild_id=_req_int("GUILD_ID"),
        inativo_role_id=_req_int("INATIVO_ROLE_ID"),
        default_log_channel_id=_req_int("LOG_CHANNEL_ID"),
        bot_log_channel_id=int(os.getenv("BOT_LOG_CHANNEL_ID", "0")),
        command_log_channel_id=int(os.getenv("COMMAND_LOG_CHANNEL_ID", "0")),
        error_log_channel_id=int(os.getenv("ERROR_LOG_CHANNEL_ID", "0")),
        trigger_log_channel_id=int(os.getenv("TRIGGER_LOG_CHANNEL_ID", "0")),
        report_log_channel_id=int(os.getenv("REPORT_LOG_CHANNEL_ID", "0")),
        migration_log_channel_id=int(os.getenv("MIGRATION_LOG_CHANNEL_ID", "0")),
        version=os.getenv("BOT_VERSION", "0.0.2"),
        command_prefix=os.getenv("COMMAND_PREFIX", "!"),
        enable_message_content_intent=_env_bool("ENABLE_MESSAGE_CONTENT_INTENT", True),
        enable_members_intent=_env_bool("ENABLE_MEMBERS_INTENT", True),
        trigger_voice_channel_id=int(os.getenv("TRIGGER_VOICE_CHANNEL_ID", "0")),
        hidden_voice_channel_id=int(os.getenv("HIDDEN_VOICE_CHANNEL_ID", "0")),
        hidden_access_role_id=int(os.getenv("HIDDEN_ACCESS_ROLE_ID", "0")),
        dias_para_inativo=int(os.getenv("DIAS_PARA_INATIVO", "7")),
        dias_para_revisao=int(os.getenv("DIAS_PARA_REVISAO", "30")),
        db_path=os.getenv("DB_PATH", "dados.db"),
        legacy_json_path=os.getenv("LEGACY_JSON_PATH", "dados.json"),
        page_size=int(os.getenv("PAGE_SIZE", "20")),
        remove_role_ids=_req_int_list("REMOVE_ROLE_IDS"),
        immune_role_ids=set(_req_int_list("IMMUNE_ROLE_IDS")),
        allowed_command_role_ids=set(_req_int_list("ALLOWED_COMMAND_ROLE_IDS")),
    )


def token_bot_id(token: str) -> str:
    """Extracts bot user ID encoded in the first token segment for diagnostics."""
    try:
        first = token.split(".")[0]
        padding = "=" * (-len(first) % 4)
        decoded = base64.urlsafe_b64decode(first + padding).decode("utf-8")
        return decoded if decoded.isdigit() else "unknown"
    except Exception:
        return "unknown"
