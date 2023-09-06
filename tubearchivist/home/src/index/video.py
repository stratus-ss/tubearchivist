"""
functionality:
- get metadata from youtube for a video
- index and update in es
"""

import os
from datetime import datetime

import requests
from django.conf import settings
from home.src.es.connect import ElasticWrap
from home.src.index import channel as ta_channel
from home.src.index import comments as ta_comments
from home.src.index import playlist as ta_playlist
from home.src.index.generic import YouTubeItem
from home.src.index.subtitle import YoutubeSubtitle
from home.src.index.video_constants import VideoTypeEnum
from home.src.index.video_streams import (
    DurationConverter,
    MediaStreamExtractor,
)
from home.src.ta.helper import randomizor
from home.src.ta.ta_redis import RedisArchivist
from ryd_client import ryd_client


class SponsorBlock:
    """handle sponsor block integration"""

    API = "https://sponsor.ajay.app/api"

    def __init__(self, user_id=False):
        self.user_id = user_id
        self.user_agent = f"{settings.TA_UPSTREAM} {settings.TA_VERSION}"
        self.last_refresh = int(datetime.now().timestamp())

    def get_sb_id(self):
        """get sponsorblock userid or generate if needed"""
        if not self.user_id:
            print("missing request user id")
            raise ValueError

        key = f"{self.user_id}:id_sponsorblock"
        sb_id = RedisArchivist().get_message(key)
        if not sb_id["status"]:
            sb_id = {"status": randomizor(32)}
            RedisArchivist().set_message(key, sb_id)

        return sb_id

    def get_timestamps(self, youtube_id):
        """get timestamps from the API"""
        url = f"{self.API}/skipSegments?videoID={youtube_id}"
        headers = {"User-Agent": self.user_agent}
        print(f"{youtube_id}: get sponsorblock timestamps")
        try:
            response = requests.get(url, headers=headers, timeout=10)
        except requests.ReadTimeout:
            print(f"{youtube_id}: sponsorblock API timeout")
            return False

        if not response.ok:
            print(f"{youtube_id}: sponsorblock failed: {response.status_code}")
            if response.status_code == 503:
                return False

            return {
                "last_refresh": self.last_refresh,
                "is_enabled": True,
                "segments": [],
            }
        else:
            all_segments = response.json()
            return self._get_sponsor_dict(all_segments)

    def _get_sponsor_dict(self, all_segments):
        """format and process response"""
        _ = [i.pop("description", None) for i in all_segments]
        has_unlocked = not any(i.get("locked") for i in all_segments)

        return {
            "last_refresh": self.last_refresh,
            "has_unlocked": has_unlocked,
            "is_enabled": True,
            "segments": all_segments,
        }

    def post_timestamps(self, youtube_id, start_time, end_time):
        """post timestamps to api"""
        user_id = self.get_sb_id().get("status")
        data = {
            "videoID": youtube_id,
            "startTime": start_time,
            "endTime": end_time,
            "category": "sponsor",
            "userID": user_id,
            "userAgent": self.user_agent,
        }
        url = f"{self.API}/skipSegments?videoID={youtube_id}"
        print(f"post: {data}")
        print(f"to: {url}")

        return {"success": True}, 200

    def vote_on_segment(self, uuid, vote):
        """send vote on existing segment"""
        user_id = self.get_sb_id().get("status")
        data = {
            "UUID": uuid,
            "userID": user_id,
            "type": vote,
        }
        url = f"{self.API}/api/voteOnSponsorTime"
        print(f"post: {data}")
        print(f"to: {url}")

        return {"success": True}, 200


class YoutubeVideo(YouTubeItem, YoutubeSubtitle):
    """represents a single youtube video"""

    es_path = False
    index_name = "ta_video"
    yt_base = "https://www.youtube.com/watch?v="

    def __init__(
        self,
        youtube_id,
        video_overwrites=False,
        video_type=VideoTypeEnum.VIDEOS,
    ):
        super().__init__(youtube_id)
        self.channel_id = False
        self.video_overwrites = video_overwrites
        self.video_type = video_type
        self.offline_import = False

    def build_json(self, youtube_meta_overwrite=False, media_path=False):
        """build json dict of video"""
        self.get_from_youtube()
        if not self.youtube_meta:
            if not youtube_meta_overwrite:
                return

            self.youtube_meta = youtube_meta_overwrite
            self.offline_import = True

        self._process_youtube_meta()
        self._add_channel()
        self._add_stats()
        self.add_file_path()
        self.add_player(media_path)
        self.add_streams(media_path)
        if self.config["downloads"]["integrate_ryd"]:
            self._get_ryd_stats()

        if self._check_get_sb():
            self._get_sponsorblock()

        return

    def _check_get_sb(self):
        """check if need to run sponsor block"""
        integrate = self.config["downloads"]["integrate_sponsorblock"]

        if self.video_overwrites:
            single_overwrite = self.video_overwrites.get(self.youtube_id)
            if not single_overwrite:
                return integrate

            if "integrate_sponsorblock" in single_overwrite:
                return single_overwrite.get("integrate_sponsorblock")

        return integrate

    def _process_youtube_meta(self):
        """extract relevant fields from youtube"""
        # extract
        self.channel_id = self.youtube_meta["channel_id"]
        upload_date = self.youtube_meta["upload_date"]
        upload_date_time = datetime.strptime(upload_date, "%Y%m%d")
        published = upload_date_time.strftime("%Y-%m-%d")
        last_refresh = int(datetime.now().timestamp())
        # base64_blur = ThumbManager().get_base64_blur(self.youtube_id)
        base64_blur = False
        # build json_data basics
        self.json_data = {
            "title": self.youtube_meta["title"],
            "description": self.youtube_meta["description"],
            "category": self.youtube_meta["categories"],
            "vid_thumb_url": self.youtube_meta["thumbnail"],
            "vid_thumb_base64": base64_blur,
            "tags": self.youtube_meta["tags"],
            "published": published,
            "vid_last_refresh": last_refresh,
            "date_downloaded": last_refresh,
            "youtube_id": self.youtube_id,
            # Using .value to make json encodable
            "vid_type": self.video_type.value,
            "active": True,
        }

    def _add_channel(self):
        """add channel dict to video json_data"""
        channel = ta_channel.YoutubeChannel(self.channel_id)
        channel.build_json(upload=True, fallback=self.youtube_meta)
        self.json_data.update({"channel": channel.json_data})

    def _add_stats(self):
        """add stats dicst to json_data"""
        # likes
        like_count = self.youtube_meta.get("like_count", 0)
        dislike_count = self.youtube_meta.get("dislike_count", 0)
        average_rating = self.youtube_meta.get("average_rating", 0)
        self.json_data.update(
            {
                "stats": {
                    "view_count": self.youtube_meta["view_count"],
                    "like_count": like_count,
                    "dislike_count": dislike_count,
                    "average_rating": average_rating,
                }
            }
        )

    def build_dl_cache_path(self):
        """find video path in dl cache"""
        cache_dir = self.app_conf["cache_dir"]
        video_id = self.json_data["youtube_id"]
        cache_path = f"{cache_dir}/download/{video_id}.mp4"
        if os.path.exists(cache_path):
            return cache_path

        channel_path = os.path.join(
            self.app_conf["videos"],
            self.json_data["channel"]["channel_id"],
            f"{video_id}.mp4",
        )
        if os.path.exists(channel_path):
            return channel_path

        raise FileNotFoundError

    def add_player(self, media_path=False):
        """add player information for new videos"""
        vid_path = media_path or self.build_dl_cache_path()

        duration_handler = DurationConverter()
        duration = duration_handler.get_sec(vid_path)
        duration_str = duration_handler.get_str(duration)
        self.json_data.update(
            {
                "player": {
                    "watched": False,
                    "duration": duration,
                    "duration_str": duration_str,
                }
            }
        )

    def add_streams(self, media_path=False):
        """add stream metadata"""
        vid_path = media_path or self.build_dl_cache_path()
        media = MediaStreamExtractor(vid_path)
        self.json_data.update(
            {
                "streams": media.extract_metadata(),
                "media_size": media.get_file_size(),
            }
        )

    def add_file_path(self):
        """build media_url for where file will be located"""
        self.json_data["media_url"] = os.path.join(
            self.json_data["channel"]["channel_id"],
            self.json_data["youtube_id"] + ".mp4",
        )

    def delete_media_file(self):
        """delete video file, meta data"""
        print(f"{self.youtube_id}: delete video")
        self.get_from_es()
        if not self.json_data:
            raise FileNotFoundError

        video_base = self.app_conf["videos"]
        media_url = self.json_data.get("media_url")
        file_path = os.path.join(video_base, media_url)
        try:
            os.remove(file_path)
        except FileNotFoundError:
            print(f"{self.youtube_id}: failed {media_url}, continue.")

        self.del_in_playlists()
        self.del_in_es()
        self.delete_subtitles()
        self.delete_comments()

    def del_in_playlists(self):
        """remove downloaded in playlist"""
        all_playlists = self.json_data.get("playlist")
        if not all_playlists:
            return

        for playlist_id in all_playlists:
            print(f"{playlist_id}: delete video {self.youtube_id}")
            playlist = ta_playlist.YoutubePlaylist(playlist_id)
            playlist.get_from_es()
            entries = playlist.json_data["playlist_entries"]
            for idx, entry in enumerate(entries):
                if entry["youtube_id"] == self.youtube_id:
                    playlist.json_data["playlist_entries"][idx].update(
                        {"downloaded": False}
                    )
            playlist.upload_to_es()

    def delete_subtitles(self, subtitles=False):
        """delete indexed subtitles"""
        print(f"{self.youtube_id}: delete subtitles")
        YoutubeSubtitle(self).delete(subtitles=subtitles)

    def delete_comments(self):
        """delete comments from es"""
        comments = ta_comments.Comments(self.youtube_id, config=self.config)
        comments.check_config()
        if comments.is_activated:
            comments.delete_comments()

    def _get_ryd_stats(self):
        """get optional stats from returnyoutubedislikeapi.com"""
        # pylint: disable=broad-except
        try:
            print(f"{self.youtube_id}: get ryd stats")
            result = ryd_client.get(self.youtube_id)
        except Exception as err:
            print(f"{self.youtube_id}: failed to query ryd api {err}")
            return

        if result["status"] == 404:
            return

        dislikes = {
            "dislike_count": result.get("dislikes", 0),
            "average_rating": result.get("rating", 0),
        }
        self.json_data["stats"].update(dislikes)

    def _get_sponsorblock(self):
        """get optional sponsorblock timestamps from sponsor.ajay.app"""
        if sponsorblock := SponsorBlock().get_timestamps(self.youtube_id):
            self.json_data["sponsorblock"] = sponsorblock

    def check_subtitles(self, subtitle_files=False):
        """optionally add subtitles"""
        if self.offline_import and subtitle_files:
            indexed = self._offline_subtitles(subtitle_files)
            self.json_data["subtitles"] = indexed
            return

        handler = YoutubeSubtitle(self)
        if subtitles := handler.get_subtitles():
            indexed = handler.download_subtitles(relevant_subtitles=subtitles)
            self.json_data["subtitles"] = indexed

    def _offline_subtitles(self, subtitle_files):
        """import offline subtitles"""
        base_name, _ = os.path.splitext(self.json_data["media_url"])
        subtitles = []
        for subtitle in subtitle_files:
            lang = subtitle.split(".")[-2]
            subtitle_media_url = f"{base_name}.{lang}.vtt"
            to_add = {
                "ext": "vtt",
                "url": False,
                "name": lang,
                "lang": lang,
                "source": "file",
                "media_url": subtitle_media_url,
            }
            subtitles.append(to_add)

        return subtitles

    def update_media_url(self):
        """update only media_url in es for reindex channel rename"""
        data = {"doc": {"media_url": self.json_data["media_url"]}}
        path = f"{self.index_name}/_update/{self.youtube_id}"
        _, _ = ElasticWrap(path).post(data=data)


def index_new_video(
    youtube_id, video_overwrites=False, video_type=VideoTypeEnum.VIDEOS
):
    """combined classes to create new video in index"""
    video = YoutubeVideo(
        youtube_id, video_overwrites=video_overwrites, video_type=video_type
    )
    video.build_json()
    if not video.json_data:
        raise ValueError(f"failed to get metadata for {youtube_id}")

    video.check_subtitles()
    video.upload_to_es()
    return video.json_data
