import asyncio
import functools
import math
import time
from collections import deque

import discord
from discord.ext.commands import Cog, command, guild_only

from ...translation import (InfoMessages, number_to_miau, pt_to_miau,
                            send_with_reaction)
from ...utils import is_int, seconds_human_friendly
from .youtuber import Youtuber


class Deejay(Cog):
    """Akira discotecando"""

    def __init__(self, bot):
        self.bot = bot
        self.setlists = {}
        self.current_songs = {}
        self.youtuber = Youtuber()
        self.stopped_playing_timestamp = None

    @command()
    @guild_only()
    async def toca(self, ctx: discord.ext.commands.Context, *, args):
        """Toca música.
        Se não estiver conectada a um canal de voz, entra no canal de voz do
        invocador. Continua tocando no canal de voz em que estiver.
        Não aceita pedidos de quem não está no canal de voz.

        :param str args: URL ou string de busca no youtube.
        """

        await self.request(ctx, args)

    @command()
    @guild_only()
    async def fila(self, ctx: discord.ext.commands.Context):
        """Mostra a setlist atual."""

        current_song = self.current_songs.get(ctx.guild.id)
        if not current_song:
            meow = pt_to_miau(InfoMessages.EMPTY_QUEUE)
            await send_with_reaction(ctx.send, meow)
        else:
            fila_embed = self.get_fila_embed(ctx.guild.id)
            await ctx.send(embed=fila_embed)

    @command()
    @guild_only()
    async def pula(self, ctx: discord.ext.commands.Context, position=0):
        """Vota para pular uma música da fila.
        Pula com votos de 1/3 dos membros do canal de voz em que Akira está.
        Não aceita votos de quem não está no canal de voz.
        Sem argumentos, pula a música atual.

        :param int position: Posição da música na fila
        """

        current_song = self.current_songs.get(ctx.guild.id)
        # makes sense only if there is a song playing
        if not current_song:
            meow = pt_to_miau(InfoMessages.NOT_PLAYING)
            await send_with_reaction(ctx.send, meow)
            return

        setlist = self.setlists[ctx.guild.id]

        if position < 0 or position > len(setlist):
            meow = pt_to_miau(InfoMessages.INVALID_QUEUE_POSITION)
            await send_with_reaction(ctx.send, meow)
            return

        # only accept requests from members in the same voice channel
        # except if whoever wants to skip requested the song
        if (
            (not ctx.author.voice)
            or (not ctx.author.voice.channel == ctx.voice_client.channel)
        ) and (current_song['requester_id'] != ctx.author.id):
            meow = pt_to_miau(InfoMessages.NOT_MY_VOICE_CHANNEL)
            await send_with_reaction(ctx.send, meow)
            return

        song_to_skip = None
        if position == 0:
            song_to_skip = current_song
        else:
            # position-1 because user will input as 1-indexed list
            song_to_skip = setlist[position - 1]
            song_to_skip['pula_votes'].add(ctx.author.id)

        n_members = len(ctx.voice_client.channel.members)
        required_votes = math.floor(1 / 3 * (n_members - 1))  # 1 is the bot

        if (
            len(song_to_skip['pula_votes']) >= required_votes
            or current_song['requester_id'] == ctx.author.id
        ):
            meow = None
            if position > 0:
                meow = pt_to_miau(InfoMessages.SKIPPED_SPECIFIC)
                del setlist[position - 1]
            else:
                ctx.voice_client.pause()
                self.play_next(ctx.guild)
                meow = pt_to_miau(InfoMessages.SKIPPED)
            await send_with_reaction(ctx.send, meow)

        else:
            n_to_skip = required_votes - len(song_to_skip['pula_votes'])
            # TODO: logic for any number (right now works for 1-9 only)
            meow = pt_to_miau(InfoMessages.NEED_MORE_VOTES, n_to_skip)
            await send_with_reaction(ctx.send, meow)

    @command()
    @guild_only()
    async def limpa(self, ctx: discord.ext.commands.Context):
        """Limpa a fila.
        Este comando limpa a fila e pronto."""
        self.setlists[ctx.guild.id] = []
        await ctx.send('foda-se')

    async def request(self, ctx: discord.ext.commands.Context, song):
        call_play = False
        voice_client = ctx.guild.voice_client
        if not voice_client:
            # the bot does not have a VoiceClient on this guild
            voice_client = await self.connect_to_user_voice_client(ctx.author)
            if not voice_client:
                meow = pt_to_miau(InfoMessages.NO_VOICE_CHANNEL)
                await send_with_reaction(ctx.send, meow)
                return

            # should call play because isn't playing yet
            call_play = True
        elif (not ctx.author.voice) or (
            not ctx.author.voice.channel == ctx.voice_client.channel
        ):
            # only accept requests from members in the same voice channel
            meow = pt_to_miau(InfoMessages.NOT_MY_VOICE_CHANNEL)
            await send_with_reaction(ctx.send, meow)
            return

        try:
            videos = self.youtuber.get_video_info(song)
        except AttributeError:
            meow = pt_to_miau(InfoMessages.INVALID_URL)
            await send_with_reaction(ctx.send, meow)
            return
        except IndexError:
            meow = pt_to_miau(InfoMessages.NO_VIDEO_FOUND)
            await send_with_reaction(ctx.send, meow)
            return

        for video_info in videos:
            self.setlists_append(ctx.author, ctx.guild.id, video_info)
        embed = self.get_toca_embed(ctx.author, videos[0])
        await ctx.send(embed=embed)

        if self.should_start_playing(voice_client):
            self.play_next(ctx.guild)

    def setlists_append(self, author, guild_id, obj):
        obj['requester_id'] = author.id
        obj['pula_votes'] = set()
        if self.setlists.get(guild_id):
            # the guild has a non-empty setlist
            self.setlists[guild_id].append(obj)
        else:
            # the setlist is missing, or empty.
            self.setlists[guild_id] = deque([obj])

    def play_next(self, guild):
        voice_client = guild.voice_client
        if not voice_client:
            print('weirdly, i have no voice_client but I should have')
            self.setlists[guild.id].clear()
            self.current_songs[guild.id] = None
            return
        if len(self.setlists[guild.id]) == 0:
            self.stopped_playing_timestamp = time.monotonic()
            # if the queue is empty, disconnect after 10 minutes
            asyncio.run_coroutine_threadsafe(
                self._trigger_disconnect(voice_client, guild), self.bot.loop
            )
            self.current_songs[guild.id] = None
            return

        # get an AudioSource from next song in setlist
        next_song_info = self.setlists[guild.id].popleft()
        audio_source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                next_song_info['source_url'],
                before_options='-reconnect 1 -reconnect_streamed 1 '
                '-reconnect_delay_max 5',
            )
        )

        try:
            voice_client.play(
                audio_source, after=lambda _: self.play_next(guild)
            )

            self.stopped_playing_timestamp = None

            self.current_songs[guild.id] = next_song_info
        except discord.ClientException:
            if voice_client.is_playing():
                print(
                    f'tried to play {next_song_info}, but i am already playing {current_songs[guild.id]}'
                )

    def is_playing_guild(self, guild):
        """Decides if the Akira is playing an Audio Source in this guild.

        :param discord.Guild guild: Guild to check
        :returns: True if Akira is playing in guild.
        :rtype: bool

        """
        if guild.voice_client:
            return guild.voice_client.is_playing()
        else:
            return False

    async def connect_to_user_voice_client(self, user):
        if user.voice:
            return await user.voice.channel.connect()

    def get_setlist_titles(self, guild_id, current=False, n=None):
        current_song = self.current_songs.get(guild_id)
        if current_song and current:
            titles = [current_song['title']]
        else:
            titles = []

        for song in self.setlists[guild_id]:
            titles.append(song['title'])
            if len(titles) == n:
                # if n is not given, will append all titles
                break
        return titles

    def get_setlist_titles_links_formatted(self, guild_id, current=False):
        """Gets the titles and links of the guild's setlist.

        Formats the links in markdown.

        :param int guild_id: id of a guild
        :param bool current: If should include the current song
        :returns: a list of markdown formatted strings
        :rtype: [str]

        """
        current_song = self.current_songs.get(guild_id)
        if current_song and current:
            titles = [
                f"[{current_song['title']}]({current_song['webpage_url']})"
            ]
        else:
            titles = []

        titles.extend(
            [
                f"{idx+1}. [{s['title']}]({s['webpage_url']})"
                for idx, s in enumerate(self.setlists[guild_id])
            ]
        )
        return titles

    def get_toca_embed(self, author, video_info, n_titles=3):
        title = video_info['title']
        duration = seconds_human_friendly(video_info['duration'])
        thumbnail = video_info['thumbnail']
        webpage_url = video_info['webpage_url']

        titles = self.get_setlist_titles(
            author.guild.id, current=True, n=n_titles
        )
        total_songs = len(self.setlists[author.guild.id]) + 1  # + current song
        footer = ', '.join(titles) + ('...' if total_songs > n_titles else '')

        embed = (
            discord.Embed(
                title=title,
                url=webpage_url,
                description=f'**Duração:** {duration}',
            )
            .set_author(name=author.display_name, icon_url=author.avatar_url)
            .set_thumbnail(url=thumbnail)
            .set_footer(
                text=footer,
                icon_url='https://raw.githubusercontent.com/pedroqueiroga/'
                'discord-akira/master/icons/'
                'playlist_add_check_white_18dp_36.png',
            )
        )

        return embed

    def get_fila_embed(self, guild_id):
        titles_links = self.get_setlist_titles_links_formatted(
            guild_id, current=False
        )
        titles_links_included = []
        total_len = 0
        for tl in titles_links:
            if total_len > 1500:
                break

            total_len += len(tl)
            titles_links_included.append(tl)

        joined_titles_links = '\n'.join(titles_links_included)
        total_duration = seconds_human_friendly(
            self.total_setlist_duration(guild_id)
        )
        total_duration_str = f'Duração total: {total_duration}'
        current_song = self.current_songs.get(guild_id)
        current_song_duration_str = (
            f"**Duração:** {seconds_human_friendly(current_song['duration'])}"
        )

        next_str = (
            f'\n\n**Próximas:**\n{joined_titles_links}'
            if self.setlists[guild_id]
            else ''
        )

        listed_songs_diff = len(titles_links) - len(titles_links_included)

        if listed_songs_diff > 0:
            next_str += f'\n... e mais {listed_songs_diff}'

        description = f'{current_song_duration_str}{next_str}'

        embed = (
            discord.Embed(
                title=current_song['title'],
                url=current_song['webpage_url'],
                description=description,
            )
            .set_author(
                name=total_duration_str,
                icon_url='https://raw.githubusercontent.com/pqueiroga/'
                'discord-terraplanista/master/icons/'
                'queue_music_white_18dp_36.png',
            )
            .set_thumbnail(url=current_song['thumbnail'])
        )
        return embed

    def total_setlist_duration(self, guild_id):
        current_song = self.current_songs.get(guild_id)
        return functools.reduce(
            lambda x, y: {'duration': x['duration'] + y['duration']},
            self.setlists[guild_id],
            current_song,
        )['duration']

    @command()
    @guild_only()
    async def volume(
        self, ctx: discord.ext.commands.Context, requested_volume=None
    ):
        """Dita o volume da discotecagem de Akira.

        Aceita apenas um argumento, o volume, que deve ser de 0 a 11.
        0 muta o bot, 10 coloca no volume original, 11 coloca no dobro do
        volume original. Alternativemente, se for da forma +x, ou -x, aumenta
        ou diminui o volume em x. Vai até 11.

        :param str args: mudança de volume
        """

        if not self.is_playing_guild(ctx.guild):
            miau = pt_to_miau(InfoMessages.NOT_PLAYING)
            return await send_with_reaction(ctx.send, miau)

        audio_source = ctx.voice_client.source
        current_volume = audio_source.volume

        if requested_volume is None:
            # if no volume is requested, print current volume human-friendly
            whole_current_volume = self.from_decimal_volume(current_volume)
            miau = number_to_miau(whole_current_volume)
            return await send_with_reaction(ctx.send, miau)

        if not is_int(requested_volume):
            miau = pt_to_miau(InfoMessages.INVALID_VOLUME)
            return await send_with_reaction(ctx.send, miau)

        new_volume, diff_volume = self.get_new_volume(
            current_volume,
            int(requested_volume),
            self.is_requested_volume_diff(requested_volume),
        )

        # validating new volume against maximum and minimum volumes
        if new_volume > 2:
            miau = pt_to_miau(InfoMessages.VOLUME_TOO_LOUD)
            return await send_with_reaction(ctx.send, miau)
        if new_volume < 0:
            miau = pt_to_miau(InfoMessages.VOLUME_TOO_LOW)
            return await send_with_reaction(ctx.send, miau)

        # new volume ok, finally commit the change
        audio_source.volume = new_volume

        if diff_volume > 0:
            miau = pt_to_miau(InfoMessages.INCREASED_VOLUME, abs(diff_volume))
        elif diff_volume < 0:
            miau = pt_to_miau(InfoMessages.DECREASED_VOLUME, abs(diff_volume))
        else:
            miau = pt_to_miau(InfoMessages.NO_VOLUME_CHANGE)

        # this method is quite long, it is mostly verifications
        return await send_with_reaction(ctx.send, miau)

    def get_new_volume(self, current_volume, volume, diff=False):
        whole_current_volume = self.from_decimal_volume(current_volume)
        if diff:
            new_volume = whole_current_volume + volume
        else:
            new_volume = volume

        diff_volume = new_volume - whole_current_volume

        return (
            self.to_decimal_volume(new_volume),
            diff_volume,
        )

    def is_requested_volume_diff(self, requested_volume):
        return requested_volume.startswith('+') or requested_volume.startswith(
            '-'
        )

    def from_decimal_volume(self, volume):
        whole_volume = round(volume * 10)
        if whole_volume < 20 and whole_volume > 11:
            raise Exception("Invalid Volume")
        return 11 if whole_volume == 20 else whole_volume

    def to_decimal_volume(self, volume):
        return ((volume + 9) if volume > 10 else volume) / 10

    async def _trigger_disconnect(self, voice_client, guild):
        ten_minutes = 600  # seconds

        await asyncio.sleep(ten_minutes)

        if len(self.setlists[guild.id]) > 0 or (
            self.stopped_playing_timestamp is None
        ):
            return

        time_since_stop = time.monotonic() - self.stopped_playing_timestamp

        if time_since_stop >= ten_minutes:
            await voice_client.disconnect()
            self.stopped_playing_timestamp = None

    def should_start_playing(self, voice_client: discord.VoiceClient):
        return not voice_client.is_playing()
