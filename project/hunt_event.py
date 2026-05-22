import asyncio
import io
import re
from collections import defaultdict
from difflib import SequenceMatcher
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import discord
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat
from discord.ext import commands

from project.config import AppConfig
from project.logging_service import LogService


def parse_env_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


class HuntEventService:
    def __init__(self, config: AppConfig, logger: LogService):
        self.config = config
        self.logger = logger
        self._env_lock = asyncio.Lock()
        self._env_path = Path(__file__).resolve().parents[1] / ".env"
        self._start_date = parse_env_date(config.hunt_event_start_date)
        self._end_date = parse_env_date(config.hunt_event_end_date)

        if config.tesseract_cmd.strip():
            pytesseract.pytesseract.tesseract_cmd = config.tesseract_cmd.strip()

    @staticmethod
    def _to_points(value_with_k: str) -> int:
        normalized = value_with_k.lower().replace("k", "").replace(",", ".").strip()
        return int(float(normalized) * 1000)

    @staticmethod
    def _normalize_identity(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", value.lower())

    @staticmethod
    def _is_image_attachment(attachment: discord.Attachment) -> bool:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            return True
        name = (attachment.filename or "").lower()
        return name.endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))

    @staticmethod
    def _quick_screenshot_check(filename: str, image_bytes: bytes) -> tuple[bool, str]:
        """
        Fast heuristic to classify whether an attachment looks like a screenshot.
        Uses filename hints + image quality profile (contrast/edges/entropy).
        Conservative by design: ambiguous images are treated as non-printscreen.
        """
        name = (filename or "").lower()
        score = 0.0
        reasons: list[str] = []

        # Filename hints.
        if any(tag in name for tag in ("screenshot", "screen_shot", "screen-shot", "captura", "snip")):
            score += 3.0
            reasons.append("nome_print")
        if re.fullmatch(r"image(?:-\d+)?\.(png|jpg|jpeg|webp|bmp)", name):
            score += 2.0
            reasons.append("nome_discord_clipboard")
        if re.match(r"^(img|dsc)[-_]?\d{6,}", name):
            score -= 3.0
            reasons.append("nome_camera")

        try:
            with Image.open(io.BytesIO(image_bytes)) as opened:
                raw = opened.copy()
        except Exception:
            return False, "imagem_invalida"

        if raw.mode in ("RGBA", "LA") or (raw.mode == "P" and "transparency" in raw.info):
            bg = Image.new("RGBA", raw.size, (255, 255, 255, 255))
            bg.paste(raw.convert("RGBA"), mask=raw.convert("RGBA").split()[3])
            raw = bg

        gray = raw.convert("L")
        width, height = gray.size
        # Reject only truly tiny images. Narrow leaderboard crops (e.g. 249x524)
        # can still be valid screenshots, so don't fail by min-side alone.
        area = width * height
        if area < 95_000 and max(width, height) < 520:
            return False, f"miniatura={width}x{height}"

        # Screenshots used in ranking usually are at least medium-sized.
        if width >= 700 and height >= 380:
            score += 1.5
            reasons.append("res_media_alta")

        contrast_stddev = float(ImageStat.Stat(gray).stddev[0])
        edge_mean = float(ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0])
        entropy = float(gray.entropy())

        # UI screenshot profile tends to have moderate entropy, readable edges and contrast.
        if 18.0 <= contrast_stddev <= 90.0:
            score += 1.0
        else:
            score -= 0.8

        if 6.0 <= edge_mean <= 45.0:
            score += 1.0
        else:
            score -= 1.0

        if 3.0 <= entropy <= 7.2:
            score += 1.0
        elif entropy > 7.6:
            # Very high entropy is usually camera/photo texture.
            score -= 1.5

        decision = score >= 2.0
        reasons_text = ",".join(reasons) if reasons else "sem_hint"
        return decision, (
            f"score={score:.1f} {reasons_text} "
            f"res={width}x{height} contraste={contrast_stddev:.1f} "
            f"bordas={edge_mean:.1f} entropia={entropy:.2f}"
        )

    @staticmethod
    def _parse_date_input(raw: str) -> date | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    def _has_manager_permission(self, ctx: commands.Context) -> bool:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return False
        if ctx.author.guild_permissions.administrator:
            return True
        return any(role.id in self.config.allowed_command_role_ids for role in ctx.author.roles)

    def _is_bot_log_room(self, ctx: commands.Context) -> bool:
        if not ctx.guild:
            return False
        target_channel_id = (
            self.config.bot_log_channel_id
            if self.config.bot_log_channel_id > 0
            else self.config.default_log_channel_id
        )
        return ctx.channel.id == target_channel_id

    def _participant_matches_user(self, participant_name: str, member: discord.abc.User) -> bool:
        """Check if an OCR-extracted participant name belongs to the given user."""
        # Treat the participant_name as a single-line OCR fragment and check
        # whether any of the user's character aliases appear in it.
        for alias in self._user_aliases(member):
            if self._alias_matches_line(alias, participant_name):
                return True
        return False

    async def _persist_event_date_in_env(self, env_key: str, value: date):
        value_str = value.isoformat()
        async with self._env_lock:
            lines: list[str] = []
            if self._env_path.exists():
                lines = self._env_path.read_text(encoding="utf-8").splitlines()

            target_prefix = f"{env_key}="
            replaced = False
            updated_lines: list[str] = []
            for line in lines:
                if line.startswith(target_prefix):
                    updated_lines.append(f"{env_key}={value_str}")
                    replaced = True
                else:
                    updated_lines.append(line)

            if not replaced:
                if updated_lines and updated_lines[-1].strip() != "":
                    updated_lines.append("")
                updated_lines.append(f"{env_key}={value_str}")

            content = "\n".join(updated_lines).rstrip() + "\n"
            self._env_path.write_text(content, encoding="utf-8")

        if env_key == "HUNT_EVENT_START_DATE":
            self._start_date = value
        elif env_key == "HUNT_EVENT_END_DATE":
            self._end_date = value

    def _preprocess_image(self, image_bytes: bytes) -> tuple["Image.Image", "Image.Image"]:
        """
        Return (enhanced, inverted) variants tuned for game leaderboard screenshots.
        Tesseract works best on dark text on white background, so the inverted
        variant usually gives much cleaner results for dark-bg HUD screenshots.
        PNGs with transparency are composited onto white before processing.
        """
        raw = Image.open(io.BytesIO(image_bytes))
        # Flatten transparency onto white background (common in PNG screenshots).
        if raw.mode in ("RGBA", "LA") or (raw.mode == "P" and "transparency" in raw.info):
            bg = Image.new("RGBA", raw.size, (255, 255, 255, 255))
            bg.paste(raw.convert("RGBA"), mask=raw.convert("RGBA").split()[3])
            raw = bg
        img = raw.convert("L")
        img = ImageOps.autocontrast(img)
        img = ImageEnhance.Contrast(img).enhance(2.5)
        img = ImageEnhance.Sharpness(img).enhance(2.0)
        img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
        return img, ImageOps.invert(img)

    def _extract_text_candidates(self, image_bytes: bytes) -> list[str]:
        """
        Run multiple OCR passes over image variants and PSM configs.
        Returns all non-empty text strings (caller selects the best one).
        """
        enhanced, inverted = self._preprocess_image(image_bytes)
        threshold_dark = enhanced.point(lambda p: 255 if p > 140 else 0)
        threshold_light = inverted.point(lambda p: 255 if p > 140 else 0)

        # PSM 6 = uniform text block
        # PSM 4 = single column of text (best for ranked lists)
        # --oem 1 = LSTM neural-net engine (most accurate)
        configs = [
            "--oem 1 --psm 6",
            "--oem 1 --psm 4",
            "--oem 1 --psm 11",
        ]

        results: list[str] = []
        for image in (enhanced, inverted, threshold_dark, threshold_light):
            for cfg in configs:
                text = pytesseract.image_to_string(image, config=cfg)
                if text and text.strip():
                    results.append(text)
        return results

    def _image_quality_metrics(self, image_bytes: bytes) -> tuple[int, int, float, float]:
        """Return width, height, contrast stddev and edge intensity mean."""
        with Image.open(io.BytesIO(image_bytes)) as opened:
            raw = opened.copy()
        if raw.mode in ("RGBA", "LA") or (raw.mode == "P" and "transparency" in raw.info):
            bg = Image.new("RGBA", raw.size, (255, 255, 255, 255))
            bg.paste(raw.convert("RGBA"), mask=raw.convert("RGBA").split()[3])
            raw = bg

        gray = raw.convert("L")
        width, height = gray.size
        contrast_stddev = float(ImageStat.Stat(gray).stddev[0])
        edges = gray.filter(ImageFilter.FIND_EDGES)
        edge_mean = float(ImageStat.Stat(edges).mean[0])
        return width, height, contrast_stddev, edge_mean

    def _should_skip_low_quality_image(self, image_bytes: bytes) -> tuple[bool, str]:
        """
        Heuristic quality gate to avoid parsing unreadable screenshots.
        Returns (skip, reason).
        """
        width, height, contrast_stddev, edge_mean = self._image_quality_metrics(image_bytes)

        # Very small images are usually unreadable for leaderboard OCR.
        if min(width, height) < 220 or (width * height) < 120_000:
            return True, (
                f"resolucao_baixa={width}x{height} "
                f"contraste={contrast_stddev:.1f} bordas={edge_mean:.1f}"
            )

        # Combined sharpness/contrast score; low values indicate heavy blur/compression.
        quality_score = (0.6 * contrast_stddev) + (1.4 * edge_mean)
        if quality_score < 30.0:
            return True, (
                f"qualidade_baixa score={quality_score:.1f} "
                f"res={width}x{height} contraste={contrast_stddev:.1f} bordas={edge_mean:.1f}"
            )

        return False, (
            f"ok score={quality_score:.1f} "
            f"res={width}x{height} contraste={contrast_stddev:.1f} bordas={edge_mean:.1f}"
        )

    # ------------------------------------------------------------------
    # Leaderboard extraction
    # ------------------------------------------------------------------

    # _RANK_ROW_RE: matches leaderboard rows regardless of how OCR reads the degree
    # symbol (°) after the rank number.  Tesseract commonly outputs it as:
    #   °  º  o  O  .  *  @  ,  or simply drops it.
    # Strategy: after the rank digits, allow up to 5 characters that are NOT a
    # Latin letter — then expect the character name to start.
    _RANK_ROW_RE = re.compile(
        r"^\s*[\dIl\|]+[^A-Za-z]{0,5}([A-Za-z][A-Za-z0-9_\-]{2,})"
    )
    _POINTS_RE = re.compile(r"(\d+(?:[\.,]\d+)?)\s*[kK]\b")

    def _extract_name_before_points(self, line: str, points_match: re.Match[str]) -> str | None:
        """Extract likely character name from the text preceding the first k-value."""
        prefix = line[: points_match.start()]
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", prefix)
        if not tokens:
            return None
        return tokens[-1]

    def _parse_leaderboard_from_text(self, text: str) -> list[tuple[str, int]]:
        """
        Parse OCR text into an ordered [(character_name, points), ...] list.
        Handles OCR rows split across two lines.
        """
        entries: list[tuple[str, int]] = []
        seen_names: set[str] = set()
        lines = [line.strip() for line in text.splitlines() if line.strip()]

        for i, line in enumerate(lines):
            m = self._RANK_ROW_RE.match(line)
            if not m:
                continue
            char_name = m.group(1)
            for j in (i, i + 1):
                if not (0 <= j < len(lines)):
                    continue
                pm = self._POINTS_RE.search(lines[j])
                if pm:
                    norm_name = self._normalize_identity(char_name)
                    if norm_name and norm_name not in seen_names:
                        entries.append((char_name, self._to_points(pm.group(1) + "k")))
                        seen_names.add(norm_name)
                    break

        # Fallback path: include rows where OCR failed to read the rank marker
        # (common on highlighted/self row) but still captured name + points.
        for line in lines:
            pm = self._POINTS_RE.search(line)
            if not pm:
                continue
            char_name = self._extract_name_before_points(line, pm)
            if not char_name:
                continue
            norm_name = self._normalize_identity(char_name)
            if not norm_name or norm_name in seen_names:
                continue
            entries.append((char_name, self._to_points(pm.group(1) + "k")))
            seen_names.add(norm_name)

        return entries

    def _extract_leaderboard_sync(self, image_bytes: bytes) -> list[tuple[str, int]]:
        """
        Run all OCR candidates and return the leaderboard parse with the most entries.
        """
        best: list[tuple[str, int]] = []
        for text in self._extract_text_candidates(image_bytes):
            entries = self._parse_leaderboard_from_text(text)
            if len(entries) > len(best):
                best = entries
        return best

    def _find_user_in_leaderboard(
        self,
        entries: list[tuple[str, int]],
        user: discord.abc.User,
    ) -> tuple[str, int] | None:
        """
        Match the user's character aliases against the extracted leaderboard list.
        """
        aliases = self._user_aliases(user)
        for char_name, points in entries:
            for alias in aliases:
                if self._alias_matches_line(alias, char_name):
                    return char_name, points
        return None

    def _extract_best_from_image_for_user_sync(
        self,
        image_bytes: bytes,
        user: discord.abc.User,
    ) -> tuple[str, int] | None:
        """
        Full pipeline:
          1. OCR image with multiple configs and image variants
          2. Parse the complete leaderboard from the best OCR result
          3. Match the message author's aliases against the leaderboard
        """
        entries = self._extract_leaderboard_sync(image_bytes)
        if not entries:
            return None
        return self._find_user_in_leaderboard(entries, user)

    def _user_aliases(self, user: discord.abc.User) -> list[str]:
        # Prefer guild-local nickname because event chars are usually listed there
        # (e.g. "Costureiro |LEKLAUS|TRESTETAO").
        raw_candidates: list[str] = []
        nick = getattr(user, "nick", None)
        if nick:
            raw_candidates.append(nick)

        display_name = getattr(user, "display_name", "")
        if display_name:
            raw_candidates.append(display_name)

        name = getattr(user, "name", "")
        if name:
            raw_candidates.append(name)

        global_name = getattr(user, "global_name", None)
        if global_name:
            raw_candidates.append(global_name)

        aliases: list[str] = []
        for raw in raw_candidates:
            if not raw:
                continue

            # Strip bracket tags like [AION2] and leading emoji/decorator chars.
            stripped = re.sub(r"\[[^\]]+\]", "", raw)
            stripped = re.sub(r"[^\w\s|/]", " ", stripped).strip()

            # Split on | and / to get individual character names.
            parts = re.split(r"[|/]", stripped)
            for part in parts:
                part = part.strip()
                if len(part) >= 3:
                    aliases.append(part)

            # Keep full stripped string too (without bracket tags).
            if len(stripped) >= 3:
                aliases.append(stripped)

        # Keep unique aliases preserving order.
        unique: list[str] = []
        seen = set()
        for alias in aliases:
            norm = self._normalize_identity(alias)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            unique.append(alias)
        return unique

    @staticmethod
    def _line_tokens(line: str) -> list[str]:
        """Split an OCR line into alpha-numeric word tokens for per-token matching."""
        return [t for t in re.split(r"[\s|/,;:°º.]+", line) if len(t) >= 2]

    def _alias_matches_line(self, alias: str, line: str) -> bool:
        """
        Check whether a character name (alias) appears in an OCR line.
        Strategy (in order):
          1. Raw case-insensitive substring  — most reliable, catches direct OCR reads
          2. Normalized substring            — survives OCR special-char noise
          3. Per-token fuzzy ratio >= 0.72   — handles OCR typos (0→O, l→I, etc.)
        """
        # 1. Raw case-insensitive search.
        if alias.lower() in line.lower():
            return True

        alias_norm = self._normalize_identity(alias)
        if not alias_norm:
            return False

        # 2. Normalized substring (strips punctuation/accents from both sides).
        line_norm = self._normalize_identity(line)
        if alias_norm in line_norm:
            return True

        # 3. Per-token fuzzy: split line into words and compare each against alias.
        for token in self._line_tokens(line):
            token_norm = self._normalize_identity(token)
            if not token_norm:
                continue
            len_diff = abs(len(alias_norm) - len(token_norm))
            if len_diff <= max(2, len(alias_norm) // 2):
                if SequenceMatcher(None, alias_norm, token_norm).ratio() >= 0.72:
                    return True
        return False

    def _extract_for_user_from_text(
        self,
        text: str,
        user: discord.abc.User,
    ) -> tuple[str, int] | None:
        aliases = self._user_aliases(user)
        if not aliases:
            return None

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        points_re = re.compile(r"(\d+(?:[\.,]\d+)?)\s*[kK]\b")

        # Step 1: for each OCR line, check if any character alias appears in it.
        # Step 2: once the alias row is found, extract the k-value from same or adjacent line.
        for i, line in enumerate(lines):
            for alias in aliases:
                if not self._alias_matches_line(alias, line):
                    continue

                # Alias found on this line — search for k-value here or on adjacent lines.
                for j in (i, i + 1, i - 1, i + 2):
                    if not (0 <= j < len(lines)):
                        continue
                    points_match = points_re.search(lines[j])
                    if points_match:
                        points = self._to_points(points_match.group(1) + "k")
                        label = "Alias match" if j == i else f"Alias match (linha+{j - i})"
                        return alias, points

        return None

    def _extract_participant_and_points(self, text: str) -> tuple[str, int] | None:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        ranked_line = re.compile(r"^\d+\D+", re.IGNORECASE)
        points_re = re.compile(r"(\d+(?:[\.,]\d+)?)\s*[kK]\b")

        active_character: str | None = None
        for line in lines:
            if "|" not in line:
                continue
            if points_re.search(line):
                continue

            tokens = [
                token.strip()
                for token in re.split(r"\|", line)
                if re.search(r"[A-Za-z]", token)
            ]
            if len(tokens) >= 2:
                candidate = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", tokens[-1])
                if candidate:
                    active_character = candidate[-1]
                    break

        if active_character:
            for line in lines:
                if active_character.lower() not in line.lower():
                    continue
                points_match = points_re.search(line)
                if not points_match:
                    continue
                points = self._to_points(points_match.group(1) + "k")
                return active_character, points

        candidate_lines = []
        for line in lines:
            if points_re.search(line):
                score = 1
                if ranked_line.search(line):
                    score += 2
                if "|" in line:
                    score += 1
                candidate_lines.append((score, line))

        if not candidate_lines:
            return None

        candidate_lines.sort(key=lambda item: item[0], reverse=True)
        for _, line in candidate_lines:
            points_match = points_re.search(line)
            if not points_match:
                continue
            before_points = line[: points_match.start()]
            tokens = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{2,}", before_points)
            if not tokens:
                continue
            participant = tokens[-1]
            points = self._to_points(points_match.group(1) + "k")
            return participant, points

        return None

    def _build_window(self) -> tuple[datetime, datetime] | None:
        if self._start_date is None or self._end_date is None:
            return None

        start_dt = datetime.combine(self._start_date, time.min).replace(tzinfo=timezone.utc)
        end_dt = datetime.combine(self._end_date + timedelta(days=1), time.min).replace(
            tzinfo=timezone.utc
        )
        return start_dt, end_dt

    async def process_hunt_message(self, message: discord.Message):
        # Ranking is computed on-demand from history. Keep event handler lightweight.
        return

    async def _compute_ranking_from_history(
        self,
        channel: discord.TextChannel | discord.Thread,
    ) -> tuple[list[tuple[str, int, int]], dict[str, int]]:
        window = self._build_window()
        if window is None:
            return [], {
                "scanned_messages": 0,
                "messages_with_images": 0,
                "ocr_extracted": 0,
                "name_matched": 0,
                "accepted_daily": 0,
            }

        start_dt, end_dt = window
        daily_best: dict[tuple[int, str], tuple[str, int]] = {}
        stats = {
            "scanned_messages": 0,
            "messages_with_images": 0,
            "ocr_extracted": 0,
            "name_matched": 0,
            "accepted_daily": 0,
        }

        async for message in channel.history(limit=None, after=start_dt, before=end_dt, oldest_first=True):
            stats["scanned_messages"] += 1
            if message.author.bot:
                continue

            image_attachments = [
                attachment for attachment in message.attachments if self._is_image_attachment(attachment)
            ]
            if not image_attachments:
                continue
            stats["messages_with_images"] += 1

            message_date = message.created_at.date().isoformat()
            print("===============================================================")
            print(
                "[HUNT] [1/4] mensagem | "
                f"data={message_date} autor={message.author}"
            )

            best_in_message: tuple[str, int] | None = None
            parsed_table_for_log: list[tuple[str, int]] = []
            for attachment in image_attachments:
                try:
                    image_bytes = await attachment.read()
                    is_printscreen, printscreen_reason = await asyncio.to_thread(
                        self._quick_screenshot_check,
                        attachment.filename or "",
                        image_bytes,
                    )
                    if not is_printscreen:
                        continue

                    skip_low_quality, quality_reason = await asyncio.to_thread(
                        self._should_skip_low_quality_image,
                        image_bytes,
                    )
                    if skip_low_quality:
                        continue

                    entries = await asyncio.to_thread(
                        self._extract_leaderboard_sync,
                        image_bytes,
                    )
                    if len(entries) > len(parsed_table_for_log):
                        parsed_table_for_log = entries

                    if not entries:
                        continue

                    extracted = self._find_user_in_leaderboard(entries, message.author)
                    if not extracted:
                        continue

                    participant, points = extracted
                    if not self._participant_matches_user(participant, message.author):
                        continue

                    stats["ocr_extracted"] += 1
                    stats["name_matched"] += 1

                    if best_in_message is None or points > best_in_message[1]:
                        best_in_message = (participant, points)
                except Exception:
                    print(
                        "[HUNT] Erro processando anexo | "
                        f"msg={message.id} arquivo={attachment.filename}"
                    )
                    continue

            print(
                "[HUNT] [2/4] imagem+tabela | "
                f"anexo_imagem={bool(image_attachments)} tabela_parseada={parsed_table_for_log}"
            )
            aliases = self._user_aliases(message.author)
            print(
                "[HUNT] [3/4] nomes+match | "
                f"nomes_usuario={aliases} match={best_in_message is not None}"
            )

            if best_in_message is None:
                print("[HUNT] [4/4] pontuacao_dia | nao_aplicavel")
                continue

            day_key = message.created_at.date().isoformat()
            key = (message.author.id, day_key)
            current = daily_best.get(key)
            if current is None or best_in_message[1] > current[1]:
                daily_best[key] = best_in_message
                stats["accepted_daily"] += 1
                print(
                    "[HUNT] [4/4] pontuacao_dia | "
                    f"dia={day_key} autor={message.author} participante={best_in_message[0]} "
                    f"soma_dia={best_in_message[1]}"
                )
            else:
                print(
                    "[HUNT] [4/4] pontuacao_dia | "
                    f"dia={day_key} autor={message.author} participante={current[0]} "
                    f"soma_dia={current[1]}"
                )

        total_by_participant: dict[str, int] = defaultdict(int)
        accepted_by_participant: dict[str, int] = defaultdict(int)

        for participant, points in daily_best.values():
            total_by_participant[participant] += points
            accepted_by_participant[participant] += 1

        ranking = [
            (participant, points, accepted_by_participant[participant])
            for participant, points in total_by_participant.items()
        ]
        ranking.sort(key=lambda item: (-item[1], item[0].lower()))
        return ranking, stats

    async def _get_event_channel(
        self, ctx: commands.Context
    ) -> discord.TextChannel | discord.Thread | None:
        if self.config.hunt_event_channel_id <= 0:
            return None

        channel = ctx.guild.get_channel(self.config.hunt_event_channel_id) if ctx.guild else None
        if channel is None:
            try:
                channel = await ctx.bot.fetch_channel(self.config.hunt_event_channel_id)
            except Exception:
                return None

        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    async def _send_ranking_dm(self, ctx: commands.Context, ranking: list[tuple[str, int, int]]):
        if not ranking:
            await ctx.author.send("Ainda nao ha pontos validos no Hunt Event para o periodo configurado.")
            return

        header = "Ranking Hunt Event (completo):"
        lines = [
            f"{index}. {participant} - {points} pts ({accepted_days} dia(s) validos)"
            for index, (participant, points, accepted_days) in enumerate(ranking, start=1)
        ]

        chunks: list[str] = []
        current = header
        for line in lines:
            candidate = f"{current}\n{line}"
            if len(candidate) > 1900:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)

        for chunk in chunks:
            await ctx.author.send(chunk)

    async def _build_debug_report(
        self,
        channel: discord.TextChannel | discord.Thread,
        sample_size: int,
    ) -> list[str]:
        window = self._build_window()
        if window is None:
            return ["Evento sem periodo configurado (start/end)."]

        start_dt, end_dt = window
        sample_size = max(1, min(20, sample_size))

        report: list[str] = [
            "Hunt Debug Report",
            f"Canal: #{channel.name} ({channel.id})",
            f"Periodo: {self._start_date} -> {self._end_date}",
            f"Amostra maxima: {sample_size} mensagens com imagem",
            "",
        ]

        inspected = 0
        async for message in channel.history(limit=None, after=start_dt, before=end_dt, oldest_first=False):
            if message.author.bot:
                continue

            image_attachments = [
                attachment for attachment in message.attachments if self._is_image_attachment(attachment)
            ]
            if not image_attachments:
                continue

            inspected += 1
            attachment = image_attachments[0]
            aliases = self._user_aliases(message.author)
            report.append(
                f"msg={message.id} author={message.author} date={message.created_at.date()} aliases={aliases}"
            )

            try:
                image_bytes = await attachment.read()
                texts = await asyncio.to_thread(self._extract_text_candidates, image_bytes)
                report.append(f"ocr_candidates={len(texts)} file={attachment.filename}")

                for idx, text in enumerate(texts[:3], start=1):
                    snippet = " ".join(text.splitlines()[:4])[:220]
                    by_user = self._extract_for_user_from_text(text, message.author)
                    generic = self._extract_participant_and_points(text)
                    report.append(
                        f"  cand#{idx}: by_user={by_user} generic={generic} text='{snippet}'"
                    )

                if not texts:
                    report.append("  no_text_from_ocr")
            except Exception as exc:
                report.append(f"  error={exc}")

            report.append("")
            if inspected >= sample_size:
                break

        if inspected == 0:
            report.append("Nenhuma mensagem com imagem encontrada no periodo.")

        return report

    def register_commands(self, bot: commands.Bot):
        @bot.command(name="setstartdate")
        async def setstartdate(ctx: commands.Context, value: str):
            if not self._is_bot_log_room(ctx):
                await ctx.send("Use este comando na sala log-bot.")
                return

            if not self._has_manager_permission(ctx):
                await ctx.send("Sem permissao para definir a data inicial do evento.")
                return

            parsed = self._parse_date_input(value)
            if parsed is None:
                await ctx.send("Data invalida. Use formato YYYY-MM-DD.")
                return

            if self._end_date and parsed > self._end_date:
                await ctx.send("Data inicial nao pode ser maior que a data final.")
                return

            await self._persist_event_date_in_env("HUNT_EVENT_START_DATE", parsed)
            await ctx.send(f"Data inicial do evento definida para {parsed.isoformat()}.")

        @bot.command(name="setenddate")
        async def setenddate(ctx: commands.Context, value: str):
            if not self._is_bot_log_room(ctx):
                await ctx.send("Use este comando na sala log-bot.")
                return

            if not self._has_manager_permission(ctx):
                await ctx.send("Sem permissao para definir a data final do evento.")
                return

            parsed = self._parse_date_input(value)
            if parsed is None:
                await ctx.send("Data invalida. Use formato YYYY-MM-DD.")
                return

            if self._start_date and parsed < self._start_date:
                await ctx.send("Data final nao pode ser menor que a data inicial.")
                return

            await self._persist_event_date_in_env("HUNT_EVENT_END_DATE", parsed)
            await ctx.send(f"Data final do evento definida para {parsed.isoformat()}.")

        @bot.command(name="rank")
        async def rank(ctx: commands.Context):
            if self.config.hunt_event_channel_id > 0 and ctx.channel.id != self.config.hunt_event_channel_id:
                await ctx.send("Use este comando no canal do evento de caca.")
                return

            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

            if self._build_window() is None:
                warning = await ctx.send(
                    "Evento sem periodo configurado. Defina inicio e fim com setstartdate/setenddate."
                )
                try:
                    await warning.delete(delay=8)
                except (discord.Forbidden, discord.HTTPException):
                    pass
                return

            target_channel = await self._get_event_channel(ctx)
            if target_channel is None:
                warning = await ctx.send("Canal do evento nao encontrado/configurado.")
                try:
                    await warning.delete(delay=8)
                except (discord.Forbidden, discord.HTTPException):
                    pass
                return

            print(
                "[HUNT] Comando !rank recebido | "
                f"autor={ctx.author} canal={ctx.channel.id}"
            )
            ranking, stats = await self._compute_ranking_from_history(target_channel)
            try:
                await self._send_ranking_dm(ctx, ranking)
                print(
                    "[HUNT] DM de ranking enviada | "
                    f"autor={ctx.author} participantes={len(ranking)}"
                )
                await self.logger.enviar_log(
                    "Hunt Event: rank solicitado",
                    (
                        f"Solicitante: {ctx.author} ({ctx.author.id})\n"
                        f"Canal analisado: {target_channel.mention}\n"
                        f"Mensagens analisadas: {stats['scanned_messages']}\n"
                        f"Mensagens com imagem: {stats['messages_with_images']}\n"
                        f"OCR extracoes validas: {stats['ocr_extracted']}\n"
                        f"Nome confere com usuario: {stats['name_matched']}\n"
                        f"Melhores diarias aceitas: {stats['accepted_daily']}\n"
                        f"Participantes no ranking: {len(ranking)}"
                    ),
                    discord.Color.blue(),
                    channel_id=self.config.hunt_event_log_channel_id,
                )
            except discord.Forbidden:
                warning = await ctx.send("Nao consegui enviar DM. Ative mensagens privadas do servidor.")
                try:
                    await warning.delete(delay=8)
                except (discord.Forbidden, discord.HTTPException):
                    pass

        @bot.command(name="huntrank")
        async def huntrank(ctx: commands.Context, top: int = 10):
            top = max(1, min(50, top))

            target_channel = await self._get_event_channel(ctx)
            if target_channel is None:
                await ctx.send("Canal do evento nao encontrado/configurado.")
                return

            ranking, _ = await self._compute_ranking_from_history(target_channel)
            if not ranking:
                await ctx.send("Ainda nao ha pontos validos no Hunt Event para o periodo configurado.")
                return

            lines = []
            for index, (participant, points, accepted_days) in enumerate(ranking[:top], start=1):
                lines.append(f"{index}. {participant} - {points} pts ({accepted_days} dia(s) validos)")
            await ctx.send("Ranking Hunt Event:\n" + "\n".join(lines))

        @bot.command(name="huntpoints")
        async def huntpoints(ctx: commands.Context, *, participant_name: str):
            target_channel = await self._get_event_channel(ctx)
            if target_channel is None:
                await ctx.send("Canal do evento nao encontrado/configurado.")
                return

            ranking, _ = await self._compute_ranking_from_history(target_channel)
            for participant, points, accepted_days in ranking:
                if participant.lower() == participant_name.lower():
                    await ctx.send(
                        f"{participant}: {points} pontos em {accepted_days} dia(s) validos."
                    )
                    return

            await ctx.send(f"Nenhum ponto encontrado para {participant_name}.")

        @bot.command(name="huntdebug")
        async def huntdebug(ctx: commands.Context, sample_size: int = 8):
            if not self._is_bot_log_room(ctx):
                await ctx.send("Use este comando na sala log-bot.")
                return

            if not self._has_manager_permission(ctx):
                await ctx.send("Sem permissao para executar debug do Hunt Event.")
                return

            target_channel = await self._get_event_channel(ctx)
            if target_channel is None:
                await ctx.send("Canal do evento nao encontrado/configurado.")
                return

            report_lines = await self._build_debug_report(target_channel, sample_size)
            content = "\n".join(report_lines)

            chunks: list[str] = []
            current = ""
            for line in content.splitlines():
                candidate = (current + "\n" + line).strip() if current else line
                if len(candidate) > 1900:
                    chunks.append(current)
                    current = line
                else:
                    current = candidate
            if current:
                chunks.append(current)

            try:
                for chunk in chunks:
                    await ctx.author.send(chunk)
                await ctx.send("Debug enviado no seu privado.")
            except discord.Forbidden:
                await ctx.send("Nao consegui enviar DM. Ative mensagens privadas do servidor.")
