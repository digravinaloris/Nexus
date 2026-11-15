"""
Module musique de Nexus (Cog discord.py).

Chargé par main.py via `await bot.load_extension("cogs.music")`. Le
chargement d'une extension se fait au démarrage du bot, donc après que
main.py soit entièrement initialisé -- c'est ce qui évite les imports
circulaires : ce module ne fait jamais `import main`, il accède aux
quelques dépendances partagées via l'objet `bot` lui-même
(voir `setup()` en bas de fichier).
"""
import os
import sys
import json
import shutil
import re
import time
import asyncio
import datetime
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import requests
from utils import AllStrategiesFailed, build_extraction_strategies, run_extraction_strategies, to_soundcloud_search

# Référence au bot, renseignée par setup() au chargement de l'extension.
# Utilisée par les fonctions module-level qui ont besoin de la boucle
# asyncio (play_next) -- les commandes, elles, passent par self.bot.
_bot = None


def check_access(interaction, command_name, native_permission=None):
    """Proxy vers le check_access de main.py, exposé sur l'objet bot pour
    éviter un import circulaire."""
    return _bot.check_access(interaction, command_name, native_permission)


# =======================  MUSIC  ===========================
# ============================================================

# FFmpeg : Render (plan gratuit/standard) n'autorise pas apt-get (filesystem en lecture seule
# pour les paquets système), et le binaire statique téléchargé manuellement (johnvansickle.com)
# segfaultait (return code -11) -- probablement une incompatibilité d'architecture/glibc avec
# l'environnement Render. imageio-ffmpeg télécharge un binaire FFmpeg empaqueté spécifiquement
# pour fonctionner avec l'environnement Python détecté, ce qui est beaucoup plus fiable.
import imageio_ffmpeg
try:
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    print(f"[FFMPEG] Using imageio-ffmpeg binary at: {FFMPEG_PATH}", flush=True)
    print(f"[FFMPEG] File exists: {os.path.isfile(FFMPEG_PATH)}, executable: {os.access(FFMPEG_PATH, os.X_OK)}", flush=True)
except Exception as e:
    print(f"[FFMPEG] imageio_ffmpeg failed to provide a binary, falling back to system ffmpeg: {e}", flush=True)
    FFMPEG_PATH = "ffmpeg"

# Cookies YouTube : Render bloque souvent les requêtes anonymes ("Sign in to confirm you're not a bot").
# On les fournit via une variable d'env (contenu du fichier cookies.txt exporté du navigateur) et on
# les réécrit sur disque au démarrage, car yt-dlp veut un vrai fichier.
YOUTUBE_COOKIES_CONTENT = os.getenv("YOUTUBE_COOKIES")
YOUTUBE_COOKIES_PATH = "/tmp/youtube_cookies.txt"

if YOUTUBE_COOKIES_CONTENT:
    with open(YOUTUBE_COOKIES_PATH, "w", encoding="utf-8") as f:
        f.write(YOUTUBE_COOKIES_CONTENT)

DOWNLOAD_DIR = "/tmp/music_cache"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

YTDL_BASE_OPTIONS = {
    # bestaudio en priorité, puis n'importe quel format jouable en dernier recours
    # (FFmpeg extraira la piste audio même d'un format vidéo+audio combiné).
    "format": "bestaudio/best/bv*+ba/b",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extract_flat": False,
    "ffmpeg_location": FFMPEG_PATH,
    # On télécharge et convertit en mp3 localement plutôt que de streamer l'URL YouTube en
    # direct : plus stable, évite les soucis de protocole réseau qui faisaient planter FFmpeg
    # pendant le streaming live (segfault -11 observé avec le streaming direct).
    "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
    "postprocessors": [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "192",
    }],
}

# --- Extraction YouTube : chaîne de secours ---------------------------------------------
#
# YouTube change quels "clients" d'extraction marchent presque chaque semaine, et yt-dlp
# met à jour SES clients par défaut à chaque release. On NE FORCE donc PLUS `player_client` :
# l'ancien réglage ['web_embedded', 'tv'] désactivait les bons clients par défaut et
# dépendait de `tv`, cassé depuis août 2026 ("The page needs to be reloaded." --
# github.com/yt-dlp/yt-dlp/issues/17389).
#
# Ce qu'utilise yt-dlp par défaut (lu dans le code de yt-dlp 2026.10.26) :
#   - sans runtime JS : `visionos` (aucun JS ni PO token requis)
#   - avec runtime JS : `tv_samsung`, `visionos`, `web`
# Le runtime JS vient du paquet pip `deno` (voir requirements.txt) -- `yt-dlp[default]`
# installe les scripts solveurs mais PAS de runtime.
#
# Ordre de secours (voir utils.build_extraction_strategies) :
#   default -> embedded -> cookies (si configurés) -> soundcloud (recherche texte seulement)


def _log(event, level="info", **fields):
    """Une ligne JSON par événement (même format que log_json de main.py : les cogs ne
    peuvent pas importer main.py) -- filtrable dans les logs Render."""
    payload = {"level": level, "event": event, "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), **fields}
    print(json.dumps(payload, default=str), flush=True)


def _find_js_runtime():
    """Config `js_runtimes` de yt-dlp, ou {} si aucun runtime trouvé (yt-dlp retombe alors
    sur `visionos`, qui n'en a pas besoin). On cherche d'abord à côté de l'interpréteur
    Python, là où pip installe `deno` : le PATH de Render n'inclut pas forcément le venv."""
    candidates = [
        ("deno", os.path.join(sys.prefix, "bin", "deno")),
        ("deno", os.path.join(sys.prefix, "Scripts", "deno.exe")),  # dev local sous Windows
        ("deno", shutil.which("deno")),
        ("node", shutil.which("node")),
    ]
    for name, path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return {name: {"path": path}}
    return {}


JS_RUNTIMES = _find_js_runtime()
HAS_COOKIES = bool(YOUTUBE_COOKIES_CONTENT)
_log("music_engine_ready", yt_dlp=yt_dlp.version.__version__,
     js_runtime=(list(JS_RUNTIMES.items())[0] if JS_RUNTIMES else None), cookies=HAS_COOKIES)


def _ytdl_options(strategy):
    options = dict(YTDL_BASE_OPTIONS)
    if JS_RUNTIMES:
        options["js_runtimes"] = JS_RUNTIMES
    if strategy == "embedded":
        options["extractor_args"] = {"youtube": {"player_client": ["web_embedded"]}}
    elif strategy == "cookies":
        options["cookiefile"] = YOUTUBE_COOKIES_PATH
    # "default" et "soundcloud" : options de base, AUCUN client forcé
    return options

# Fichier local mp3 déjà téléchargé : pas besoin de -reconnect (plus de réseau pendant la lecture).
FFMPEG_OPTIONS_TEMPLATE = {
    "before_options": "",
    "options": "-vn -af volume={volume}",
}

DEFAULT_VOLUME = 0.5  # 50%, ajustable par /volume (0.0 à 2.0)

_ytdl_instances = {}


def _get_ytdl(strategy):
    """Une instance yt-dlp par stratégie, créée à la demande puis réutilisée (comme avant :
    elle garde en cache le player JS et les données de session entre les appels)."""
    key = "default" if strategy == "soundcloud" else strategy
    if key not in _ytdl_instances:
        _ytdl_instances[key] = yt_dlp.YoutubeDL(_ytdl_options(key))
    return _ytdl_instances[key]

SPOTIFY_TRACK_RE = re.compile(r"open\.spotify\.com/track/([A-Za-z0-9]+)")
SPOTIFY_PLAYLIST_RE = re.compile(r"open\.spotify\.com/(playlist|album)/([A-Za-z0-9]+)")


class Track:
    def __init__(self, title, filepath, webpage_url, duration, requester):
        self.title = title
        self.filepath = filepath  # chemin local du mp3 téléchargé
        self.webpage_url = webpage_url  # lien à afficher
        self.duration = duration
        self.requester = requester


class GuildMusicState:
    """Garde la queue et le lecteur vocal pour un serveur donné."""
    def __init__(self, guild_id):
        self.guild_id = guild_id
        self.queue = []
        self.voice_client = None
        self.current = None
        self.loop_lock = asyncio.Lock()
        self.volume = DEFAULT_VOLUME  # 0.0 à 2.0, ajustable via /volume

    def is_playing(self):
        return self.voice_client is not None and self.voice_client.is_playing()


music_states = {}  # {guild_id: GuildMusicState}


def get_music_state(guild_id):
    if guild_id not in music_states:
        music_states[guild_id] = GuildMusicState(guild_id)
    return music_states[guild_id]


async def resolve_query(query, requester):
    """
    Transforme une recherche texte ou un lien YouTube/SoundCloud/Spotify en Track jouable.
    Pour Spotify, on récupère le titre/artiste via oEmbed (pas besoin de clé API) et on recherche sur YouTube.
    """
    loop = asyncio.get_event_loop()

    spotify_match = SPOTIFY_TRACK_RE.search(query)
    if spotify_match:
        # Spotify ne permet pas le streaming direct (DRM) : on récupère le titre via oEmbed et on recherche sur YouTube
        try:
            import urllib.request
            import json as jsonlib
            oembed_url = f"https://open.spotify.com/oembed?url={query}"
            with urllib.request.urlopen(oembed_url, timeout=5) as resp:
                data = jsonlib.loads(resp.read().decode())
            title = data.get("title", "")
            query = f"ytsearch:{title}"
        except Exception:
            query = f"ytsearch:{query}"
    elif query.startswith("http"):
        pass  # lien direct YouTube/SoundCloud, yt-dlp gère nativement
    else:
        query = f"ytsearch:{query}"

    # Une recherche texte peut se rabattre sur SoundCloud si YouTube bloque tout ; un lien
    # direct non (on ne sait pas quoi chercher ailleurs).
    soundcloud_query = to_soundcloud_search(query)
    strategies = build_extraction_strategies(
        has_cookies=HAS_COOKIES,
        has_js_runtime=bool(JS_RUNTIMES),
        can_use_soundcloud=soundcloud_query is not None,
    )

    def extract_with(strategy):
        # download=True : on télécharge réellement le fichier, le post-processeur le convertit en mp3.
        target = soundcloud_query if strategy == "soundcloud" else query
        info = _get_ytdl(strategy).extract_info(target, download=True)
        if "entries" in info:
            info = info["entries"][0]
        return info

    def extract():
        return run_extraction_strategies(
            strategies, extract_with, lambda exc: isinstance(exc, yt_dlp.utils.DownloadError),
        )

    try:
        info, used_strategy, failed_before = await loop.run_in_executor(None, extract)
    except AllStrategiesFailed as exc:
        _log("music_extraction_failed", level="error", query=query, tried=[name for name, _ in exc.errors],
             errors={name: message[:300] for name, message in exc.errors})
        # Le message de la PREMIÈRE stratégie (YouTube par défaut) est presque toujours la vraie
        # cause ; les suivantes (SoundCloud...) ne font que répéter "introuvable".
        first_name, first_message = exc.errors[0]
        others = [name for name, _ in exc.errors[1:]]
        suffix = f"\n(also tried: {', '.join(others)})" if others else ""
        raise yt_dlp.utils.DownloadError(first_message + suffix) from exc

    if failed_before:
        _log("music_extraction_fallback", query=query, used=used_strategy,
             failed=[name for name, _ in failed_before],
             errors={name: message[:300] for name, message in failed_before})

    video_id = info.get("id")
    expected_mp3_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")

    if not os.path.isfile(expected_mp3_path):
        raise FileNotFoundError(f"Downloaded file not found at expected path: {expected_mp3_path}")

    title = info.get("title", "Unknown title")
    if used_strategy == "soundcloud":
        title = f"{title} (SoundCloud)"  # l'utilisateur voit que ce n'est pas la version YouTube

    return Track(
        title=title,
        filepath=expected_mp3_path,
        webpage_url=info.get("webpage_url"),
        duration=info.get("duration"),
        requester=requester,
    )


def format_duration(seconds):
    if not seconds:
        return "Live/Unknown"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


async def play_next(guild_id):
    state = get_music_state(guild_id)
    async with state.loop_lock:
        if not state.queue:
            state.current = None
            return
        track = state.queue.pop(0)
        state.current = track

        if state.voice_client is None or not state.voice_client.is_connected():
            return

        ffmpeg_options = {
            "before_options": FFMPEG_OPTIONS_TEMPLATE["before_options"],
            "options": FFMPEG_OPTIONS_TEMPLATE["options"].format(volume=state.volume),
        }
        print(f"[FFMPEG] Launching with executable={FFMPEG_PATH}, file={track.filepath}", flush=True)
        print(f"[FFMPEG] File exists: {os.path.isfile(track.filepath)}", flush=True)
        try:
            source = discord.FFmpegPCMAudio(
                track.filepath, executable=FFMPEG_PATH, stderr=sys.stdout, **ffmpeg_options
            )
        except Exception as e:
            print(f"[FFMPEG] Failed to start FFmpeg process: {e}", flush=True)
            state.current = None
            return

        def after_play(error):
            if error:
                print(f"Player error: {error}", flush=True)
            # Nettoie le fichier mp3 seulement s'il n'est pas remis en queue (cas /volume qui relance le morceau courant)
            still_queued = state.queue and state.queue[0] is track
            if not still_queued:
                try:
                    if os.path.isfile(track.filepath):
                        os.remove(track.filepath)
                except Exception as cleanup_error:
                    print(f"Cleanup error: {cleanup_error}", flush=True)
            fut = asyncio.run_coroutine_threadsafe(play_next(guild_id), _bot.loop)
            try:
                fut.result()
            except Exception as e:
                print(f"after_play error: {e}", flush=True)

        state.voice_client.play(source, after=after_play)


async def ensure_voice_connected(interaction: discord.Interaction):
    """Connecte (ou déplace) le bot dans le salon vocal de l'utilisateur. Retourne le GuildMusicState, ou None si erreur déjà gérée."""
    if interaction.user.voice is None or interaction.user.voice.channel is None:
        embed = discord.Embed(description="❌ You need to be in a voice channel.", color=0xff0000)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return None

    state = get_music_state(interaction.guild_id)
    voice_channel = interaction.user.voice.channel

    if state.voice_client is None or not state.voice_client.is_connected():
        state.voice_client = await voice_channel.connect()
    elif state.voice_client.channel != voice_channel:
        await state.voice_client.move_to(voice_channel)

    return state


async def queue_and_play(interaction: discord.Interaction, state: "GuildMusicState", track: "Track"):
    """Ajoute un track déjà résolu à la queue et lance la lecture si rien ne joue."""
    state.queue.append(track)

    if state.voice_client.is_playing() or state.voice_client.is_paused():
        embed = discord.Embed(title="➕ Added to queue", color=0x3399ff)
        embed.add_field(name="Title", value=track.title, inline=False)
        embed.add_field(name="Duration", value=format_duration(track.duration), inline=True)
        embed.add_field(name="Position", value=f"{len(state.queue)}", inline=True)
        await interaction.followup.send(embed=embed)
    else:
        embed = discord.Embed(title="🎵 Now Playing", color=0x00cc00)
        embed.add_field(name="Title", value=track.title, inline=False)
        embed.add_field(name="Duration", value=format_duration(track.duration), inline=True)
        embed.add_field(name="Requested by", value=track.requester.mention, inline=True)
        await interaction.followup.send(embed=embed)
        await play_next(interaction.guild_id)


_autocomplete_cache = {}  # {query_lowercase: (timestamp, [entries])}
AUTOCOMPLETE_CACHE_TTL = 300  # 5 minutes


async def play_autocomplete(interaction: discord.Interaction, current: str):
    """
    Callback d'autocomplete pour /play : propose des titres en live pendant la frappe.
    Discord impose ~3s de timeout et spamme une requête par frappe, donc on cache les résultats
    et on attend un minimum de caractères avant de chercher, comme FlaviBot.
    """
    current = current.strip()
    if len(current) < 2 or current.startswith("http"):
        return []

    cache_key = current.lower()
    cached = _autocomplete_cache.get(cache_key)
    now = time.time()
    if cached and now - cached[0] < AUTOCOMPLETE_CACHE_TTL:
        entries = cached[1]
    else:
        try:
            # Discord coupe la connexion après ~3s ; on se laisse une marge pour répondre à temps
            entries = await asyncio.wait_for(search_youtube(current, max_results=5), timeout=2.5)
        except asyncio.TimeoutError:
            print(f"autocomplete search timeout for query: {current}", flush=True)
            return []
        except Exception as e:
            print(f"autocomplete search error: {e}", flush=True)
            return []
        _autocomplete_cache[cache_key] = (now, entries)

    choices = []
    for entry in entries[:8]:
        title = entry.get("title", "Unknown")
        duration = format_duration(entry.get("duration"))
        label = f"{title} · {duration}"[:100]
        video_id = entry.get("id")
        raw_url = entry.get("url")
        if video_id:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
        elif raw_url and raw_url.startswith("http"):
            video_url = raw_url
        else:
            # dernier recours : certains extracteurs renvoient l'id brut dans "url"
            video_url = f"https://www.youtube.com/watch?v={raw_url}" if raw_url else None
        if not video_url:
            continue
        choices.append(app_commands.Choice(name=label, value=video_url[:100]))
    return choices




class Music(commands.Cog):
    """Commandes de lecture musicale."""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="play", description="Play a song from YouTube, Spotify or SoundCloud")
    @app_commands.autocomplete(query=play_autocomplete)
    async def play(self, interaction: discord.Interaction, query: str):
        if not await check_access(interaction, "play", None):
            return

        await interaction.response.defer()
        state = await ensure_voice_connected(interaction)
        if state is None:
            return

        try:
            track = await resolve_query(query, interaction.user)
        except Exception as e:
            error_detail = str(e)[:200]
            embed = discord.Embed(
                description=f"❌ Couldn't find or load that track.\n```{error_detail}```",
                color=0xff0000,
            )
            await interaction.followup.send(embed=embed)
            print(f"resolve_query error: {e}", flush=True)
            return

        await queue_and_play(interaction, state, track)

    @app_commands.command(name="search", description="Search for a song and choose from a list of results")
    async def search(self, interaction: discord.Interaction, query: str):
        if not await check_access(interaction, "search", None):
            return

        await interaction.response.defer()

        try:
            results = await search_youtube(query, max_results=5)
        except Exception as e:
            embed = discord.Embed(description="❌ Search failed, try again.", color=0xff0000)
            await interaction.followup.send(embed=embed)
            print(f"search_youtube error: {e}", flush=True)
            return

        if not results:
            embed = discord.Embed(description="❌ No results found.", color=0xff0000)
            await interaction.followup.send(embed=embed)
            return

        embed = discord.Embed(title=f"🔎 Search results for \"{query}\"", color=0x3399ff)
        lines = []
        for i, entry in enumerate(results):
            title = entry.get("title", "Unknown")
            duration = format_duration(entry.get("duration"))
            lines.append(f"**{i + 1}.** {title} · {duration}")
        embed.description = "\n".join(lines)
        embed.set_footer(text="Select a track from the menu below")

        view = SearchResultView(results, interaction.user)
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="pause", description="Pause the current song")
    async def pause(self, interaction: discord.Interaction):
        if not await check_access(interaction, "pause", None):
            return
        state = get_music_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_playing():
            state.voice_client.pause()
            embed = discord.Embed(description="⏸️ Paused.", color=0xff6600)
            await interaction.response.send_message(embed=embed)
        else:
            embed = discord.Embed(description="❌ Nothing is playing.", color=0xff0000)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="resume", description="Resume the paused song")
    async def resume(self, interaction: discord.Interaction):
        if not await check_access(interaction, "resume", None):
            return
        state = get_music_state(interaction.guild_id)
        if state.voice_client and state.voice_client.is_paused():
            state.voice_client.resume()
            embed = discord.Embed(description="▶️ Resumed.", color=0x00cc00)
            await interaction.response.send_message(embed=embed)
        else:
            embed = discord.Embed(description="❌ Nothing is paused.", color=0xff0000)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        if not await check_access(interaction, "skip", None):
            return
        state = get_music_state(interaction.guild_id)
        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            embed = discord.Embed(description="⏭️ Skipped.", color=0x3399ff)
            await interaction.response.send_message(embed=embed)
            state.voice_client.stop()  # déclenche after_play -> play_next automatiquement
        else:
            embed = discord.Embed(description="❌ Nothing is playing.", color=0xff0000)
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="volume", description="Set the playback volume (0 to 200%)")
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 200]):
        if not await check_access(interaction, "volume", None):
            return
        state = get_music_state(interaction.guild_id)
        state.volume = percent / 100.0

        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            # Redémarre la source avec le nouveau volume sans perdre la position de la queue.
            # FFmpeg ne permet pas de changer le volume "à chaud" sans relancer le flux ; comme on
            # ne peut pas reprendre exactement où on en était facilement, on relance le morceau courant.
            current_track = state.current
            if current_track:
                state.queue.insert(0, current_track)
            state.voice_client.stop()

        embed = discord.Embed(description=f"🔊 Volume set to **{percent}%**.", color=0x3399ff)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        if not await check_access(interaction, "stop", None):
            return
        state = get_music_state(interaction.guild_id)
        # Nettoie les fichiers mp3 des morceaux encore en attente (espace disque limité sur Render)
        for queued_track in state.queue:
            try:
                if os.path.isfile(queued_track.filepath):
                    os.remove(queued_track.filepath)
            except Exception as e:
                print(f"Cleanup error on stop: {e}", flush=True)
        state.queue.clear()
        state.current = None
        if state.voice_client:
            if state.voice_client.is_playing() or state.voice_client.is_paused():
                state.voice_client.stop()
            await state.voice_client.disconnect()
            state.voice_client = None
        embed = discord.Embed(description="⏹️ Stopped and cleared the queue.", color=0xff0000)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="queue", description="Show the current music queue")
    async def queue_cmd(self, interaction: discord.Interaction):
        state = get_music_state(interaction.guild_id)
        embed = discord.Embed(title="🎶 Music Queue", color=0x3399ff)

        if state.current:
            embed.add_field(
                name="Now Playing",
                value=f"{state.current.title} · {format_duration(state.current.duration)}",
                inline=False,
            )
        else:
            embed.add_field(name="Now Playing", value="Nothing", inline=False)

        if state.queue:
            lines = []
            for i, track in enumerate(state.queue[:10], start=1):
                lines.append(f"{i}. {track.title} · {format_duration(track.duration)}")
            embed.add_field(name="Up Next", value="\n".join(lines), inline=False)
            if len(state.queue) > 10:
                embed.set_footer(text=f"+ {len(state.queue) - 10} more in queue")
        else:
            embed.add_field(name="Up Next", value="Queue is empty", inline=False)

        await interaction.response.send_message(embed=embed)


async def setup(bot):
    """Point d'entrée de l'extension, appelé par bot.load_extension()."""
    global _bot
    _bot = bot
    await bot.add_cog(Music(bot))
