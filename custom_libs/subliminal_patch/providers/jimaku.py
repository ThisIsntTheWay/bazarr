from __future__ import absolute_import

import io
import logging
import re
import time
import traceback
import urllib.parse
import rarfile
import zipfile
import requests

from requests import Session
from subliminal import __short_version__
from subliminal.exceptions import ConfigurationError, AuthenticationError
from subliminal.utils import sanitize
from subliminal.video import Episode, Movie
from subliminal_patch.providers import Provider
from subliminal_patch.subtitle import Subtitle
from subliminal_patch.exceptions import APIThrottled
from subliminal_patch.providers.utils import get_subtitle_from_archive
from guessit import guessit
from functools import cache
from subzero.language import Language

logger = logging.getLogger(__name__)

class JimakuSubtitle(Subtitle):
    '''Jimaku Subtitle.'''
    provider_name = 'jimaku'
    
    hash_verifiable = False

    def __init__(self, video, subtitle_id, subtitle_url, subtitle_filename):
        # Override param 'language' as it could only ever be "ja"
        super(JimakuSubtitle, self).__init__(Language("jpn"))
        
        self.video = video
        self.subtitle_id = subtitle_id
        self.subtitle_url = subtitle_url
        self.subtitle_filename = subtitle_filename
        self.release_info = subtitle_filename
        
    @property
    def id(self):
        return self.subtitle_id

    def get_matches(self, video):
        matches = set()
        
        # Episode/Movie specific matches
        if isinstance(video, Episode):
            if sanitize(video.series) and sanitize(self.video.series) in (
                    sanitize(name) for name in [video.series] + video.alternative_series):
                matches.add('series')
            
            if video.season and self.video.season is None or video.season and video.season == self.video.season:
                matches.add('season')
        elif isinstance(video, Movie):
            if sanitize(video.title) and sanitize(self.video.title) in (
                    sanitize(name) for name in [video.title] + video.alternative_titles):
                matches.add('title')
        else:
            raise ValueError(f"Unhandled instance of argument 'video': {type(video)}")

        # General matches
        if video.year and video.year == self.video.year:
            matches.add('year')

        video_type = 'movie' if isinstance(video, Movie) else 'episode'
        matches.add(video_type)
        
        guess = guessit(self.subtitle_filename, {'type': video_type})
        logger.debug(f"Guessit: {guess}")
        for g in guess:
            if g[0] == "release_group" or "source":
                if video.release_group == g[1]:
                    matches.add('release_group')
                    break

        return matches

class JimakuProvider(Provider):
    '''Jimaku Provider.'''
    video_types = (Episode, Movie)
    
    api_url = 'https://jimaku.cc/api'
    api_ratelimit_max_delay_seconds = 5
    api_ratelimit_backoff_limit = 3
    
    corrupted_file_size_threshold = 500
    
    episode_number_override = False
    
    languages = {Language.fromietf("ja")}

    def __init__(self, enable_archives, enable_ai_subs, api_key):
        if api_key:
            self.api_key = api_key
        else:
            raise ConfigurationError('Missing api_key.')

        self.enable_archives = enable_archives
        self.enable_ai_subs = enable_ai_subs
        self.session = None

    def initialize(self):
        self.session = Session()
        self.session.headers['User-Agent'] = 'Subliminal/%s' % __short_version__
        self.session.headers['Content-Type'] = 'application/json'
        self.session.headers['Authorization'] = self.api_key

    def terminate(self):
        self.session.close()

    def _query(self, video):
        if isinstance(video, Movie):
            media_name = video.title.lower()
        elif isinstance(video, Episode):
            media_name = video.series.lower()
            
            # With entries that have a season larger than 1, Jimaku appends the corresponding season number to the name.
            # We'll reassemble media_name here to account for cases where we can only search by name alone.
            season_addendum = str(video.season) if video.season > 1 else None
            media_name = f"{media_name} {season_addendum}" if season_addendum else media_name

        # Search for entry
        url = self._assemble_jimaku_search_url(video, media_name)
        data = self._get_jimaku_response(url)
        if not data:
            return None

        # We only go for the first entry
        entry = data[0]
        
        entry_id = entry.get('id')
        anilist_id = entry.get('anilist_id', None)
        entry_name = entry.get('name')
        
        logger.info(f"Matched entry: ID: '{entry_id}', anilist_id: '{anilist_id}', name: '{entry_name}', english_name: '{entry.get('english_name')}'")
        if entry.get("flags").get("unverified"):
            logger.warning(f"This entry '{entry_id}' is unverified, subtitles might be incomplete or have quality issues!")    
        
        # Get a list of subtitles for entry
        if isinstance(video, Episode):
            episode_number = video.episode
        
        retry_count = 0
        while retry_count <= 1:
            retry_count += 1
            
            if isinstance(video, Episode):
                url = f"entries/{entry_id}/files?episode={episode_number}"
            else:
                url = f"entries/{entry_id}/files"
            data = self._get_jimaku_response(url)
            
            # Edge case: When dealing with a cour, episodes could be uploaded with their episode numbers having an offset applied
            if not data and isinstance(video, Episode) and self.episode_number_override and retry_count < 1:
                logger.warning(f"Found no subtitles for {episode_number}, but will retry with offset-adjusted episode number {video.series_anidb_episode_no}.")
                episode_number = video.series_anidb_episode_no
            elif not data:
                return None
        
        # Filter subtitles
        list_of_subtitles = []

        archive_formats_blacklist = (".7z",) # Unhandled format
        if not self.enable_archives:
            disabled_archives = (".zip", ".rar")
            
            # Handle shows that only have archives uploaded
            filter = [item for item in data if not item['name'].endswith(disabled_archives)]
            if len(filter) == 0:
                logger.warning("Archives are disabled, but only archived subtitles have been returned. Will therefore download anyway.")
            else:
                archive_formats_blacklist += disabled_archives

        for item in data:
            subtitle_filename = item.get('name')
            subtitle_url = item.get('url')

            if not self.enable_ai_subs:
                if "whisperai" in subtitle_filename.lower():
                    logger.warning(f"Skipping AI generated subtitle '{subtitle_filename}'")
                    continue
            
            # Check if file is obviously corrupt. If no size is returned, assume OK
            subtitle_filesize = item.get('size', self.corrupted_file_size_threshold)
            if subtitle_filesize < self.corrupted_file_size_threshold:
                logger.warning(f"Skipping possibly corrupt file '{subtitle_filename}': Filesize is just {subtitle_filesize} bytes.")
                continue
            
            if not subtitle_filename.endswith(archive_formats_blacklist):
                number = episode_number if isinstance(video, Episode) else 0
                subtitle_id = f"{str(anilist_id)}_{number}_{video.release_group}"
                
                list_of_subtitles.append(JimakuSubtitle(video, subtitle_id, subtitle_url, subtitle_filename))
            else:
                logger.debug(f"> Skipping subtitle of name '{subtitle_filename}' due to archive blacklist. (enable_archives: {self.enable_archives})")
        
        return list_of_subtitles

    # As we'll only ever handle "ja", we'll just ignore the parameter "languages"
    def list_subtitles(self, video, languages=None):
        subtitles = self._query(video)
        if not subtitles:
            return []
        
        return [s for s in subtitles]

    def download_subtitle(self, subtitle: JimakuSubtitle):
        target_url = subtitle.subtitle_url
        response = self.session.get(target_url, timeout=10)
        response.raise_for_status()
        
        archive = self._is_archive(response.content)
        if archive:
            subtitle.content = get_subtitle_from_archive(archive, subtitle.video.episode)
        elif not archive and subtitle.subtitle_url.endswith(('.zip', '.rar')):
            logger.warning("Archive seems not to be an archive! File possibly corrupt? Skipping.")
            return None
        else:
            subtitle.content = response.content
    
    @staticmethod
    def _is_archive(archive: bytes):
        archive_stream = io.BytesIO(archive)
        
        if rarfile.is_rarfile(archive_stream):
            logger.debug("Identified rar archive")
            return rarfile.RarFile(archive_stream)
        elif zipfile.is_zipfile(archive_stream):
            logger.debug("Identified zip archive")
            return zipfile.ZipFile(archive_stream)
        else:
            logger.debug("Doesn't seem like an archive")
            return None
    
    @cache
    def _get_jimaku_response(self, url_path):
        url = f"{self.api_url}/{url_path}"
        
        retry_count = 0
        while retry_count < self.api_ratelimit_backoff_limit:
            retry_count += 1
            response = self.session.get(url, timeout=10)
            
            if response.status_code == 429:
                api_reset_time = float(response.headers.get("x-ratelimit-reset-after", 5))
                reset_time = self.api_ratelimit_max_delay_seconds if api_reset_time > self.api_ratelimit_max_delay_seconds else api_reset_time
                
                logger.warning(f"Jimaku ratelimit hit, waiting for '{reset_time}' seconds ({retry_count}/{self.api_ratelimit_backoff_limit} tries)")
                time.sleep(reset_time)
            elif response.status_code == 401:
                raise AuthenticationError("Unauthorized. API key possibly invalid")
            
            response.raise_for_status()
            
            data = response.json()
            logger.debug(f"Length of response on {url}: {len(data)}")
            if len(data) == 0:
                logger.error(f"Jimaku returned no items for our our query: {url_path}")
                return None
            elif 'error' in data:
                logger.error(f"Jimaku returned an error for our query.\nMessage: '{data.get('error')}', Code: '{data.get('code')}'")
                return None
            else:
                return data
            
        # Max retries exceeded
        raise APIThrottled(f"Jimaku ratelimit max backoff limit of {self.api_ratelimit_backoff_limit} reached, aborting.")
    
    @staticmethod
    @cache
    def _webrequest_with_cache(url, headers=None):
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        try:
            return response.json()
        except:
            return response.content
    
    @cache
    def _assemble_jimaku_search_url(self, video, media_name):
        """
        Return a search URL for the Jimaku API.
        Will first try to determine an Anilist ID based on properties of the video object.
        If that fails, will simply assemble a query URL that relies on Jimakus fuzzy search instead.
        """
                       
        url = "entries/search"
        anilist_id = video.anilist_id
        if anilist_id:
            logger.info(f"Will search for entry based on anilist_id: {anilist_id}")
            url = f"{url}?anilist_id={anilist_id}"
        else:
            logger.info(f"Will search for entry based on media_name: {media_name}")
            url = f"{url}?query={urllib.parse.quote_plus(media_name)}"
            
        return url