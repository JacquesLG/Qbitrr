from __future__ import annotations

import contextlib
import pathlib
import re
import shutil
import sys
import time
from collections import defaultdict
from configparser import NoOptionError, NoSectionError
from copy import copy
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, NoReturn, Set, TYPE_CHECKING, Tuple, Type, Union

import ffmpeg
import logbook
import pathos
import qbittorrentapi
import requests
from peewee import Model, SqliteDatabase
from pyarr import RadarrAPI, SonarrAPI
from qbittorrentapi import TorrentDictionary, TorrentStates

from arr_tables import CommandsModel, EpisodesModel, MoviesModel
from config import (
    APPDATA_FOLDER,
    COMPLETED_DOWNLOAD_FOLDER,
    CONFIG,
    FAILED_CATEGORY,
    LOOP_SLEEP_TIMER,
    NO_INTERNET_SLEEP_TIMER,
    RECHECK_CATEGORY,
)
from errors import DelayLoopException, NoConnectionrException, SkipException
from tables import EpisodeFilesModel, EpisodeQueueModel, MovieQueueModel, MoviesFilesModel
from utils import ExpiringSet, absolute_file_paths, has_internet, validate_and_return_torrent_file

if TYPE_CHECKING:
    from .main import qBitManager

logger = logbook.Logger("ArrManager")


class Arr:
    def __init__(
        self,
        name: str,
        manager: ArrManager,
        client_cls: Type[Callable | RadarrAPI | SonarrAPI],
    ):
        if name in manager.groups:
            raise EnvironmentError("Group '{name}' has already been registered.")
        self._name = name
        self.managed = CONFIG.getboolean(name, "Managed")
        if not self.managed:
            raise SkipException
        self.uri = CONFIG.get(name, "URI")
        if self.uri in manager.uris:
            raise EnvironmentError(
                "Group '{name}' is trying to manage Radarr instance: '{uri}' which has already been registered."
            )

        self.category = CONFIG.get(name, "Category", fallback=self._name)
        self.completed_folder = pathlib.Path(COMPLETED_DOWNLOAD_FOLDER).joinpath(self.category)
        if not self.completed_folder.exists():
            raise EnvironmentError(
                f"{self._name} completed folder is a requirement, The specified folder does not exist '{self.completed_folder}'"
            )
        self.apikey = CONFIG.get(name, "APIKey")

        self.re_search = CONFIG.getboolean(name, "Research")
        self.import_mode = CONFIG.get(name, "importMode", fallback="Move")
        self.refresh_downloads_timer = CONFIG.getint(name, "RefreshDownloadsTimer", fallback=1)
        self.rss_sync_timer = CONFIG.getint(name, "RssSyncTimer", fallback=15)

        self.case_sensitive_matches = CONFIG.getboolean(name, "CaseSensitiveMatches")
        self.folder_exclusion_regex = CONFIG.getlist(name, "FolderExclusionRegex")
        self.file_name_exclusion_regex = CONFIG.getlist(name, "FileNameExclusionRegex")
        self.file_extension_allowlist = CONFIG.getlist(name, "FileExtensionAllowlist")
        self.auto_delete = CONFIG.getboolean(name, "AutoDelete", fallback=False)
        self.ignore_torrents_younger_than = CONFIG.getint(
            name, "IgnoreTorrentsYoungerThan", fallback=600
        )
        self.maximum_eta = CONFIG.getint(name, "MaximumETA", fallback=86400)
        self.maximum_deletable_percentage = CONFIG.getfloat(
            name, "MaximumDeletablePercentage", fallback=0.95
        )

        self.search_missing = CONFIG.getboolean(name, "SearchMissing")
        self.search_specials = CONFIG.getboolean(name, "AlsoSearchSpecials")
        self.search_by_year = CONFIG.getboolean(name, "SearchByYear")
        self.search_in_reverse = CONFIG.getboolean(name, "SearchInReverse")

        self.search_starting_year = CONFIG.getyear(name, "StartYear")
        self.search_ending_year = CONFIG.getyear(name, "LastYear")
        self.search_command_limit = CONFIG.getint(name, "SearchLimit", fallback=5)

        if self.search_in_reverse:
            self.search_current_year = self.search_ending_year
            self._delta = 1
        else:
            self.search_current_year = self.search_starting_year
            self._delta = -1

        self.arr_db_file = pathlib.Path(CONFIG.get(name, "DatabaseFile"))

        self._app_data_folder = APPDATA_FOLDER

        self.search_db_file = self._app_data_folder.joinpath(f"{self._name}.db")

        if self.case_sensitive_matches:
            self.folder_exclusion_regex_re = re.compile(
                "|".join(self.folder_exclusion_regex), re.DOTALL
            )
            self.file_name_exclusion_regex_re = re.compile(
                "|".join(self.file_name_exclusion_regex), re.DOTALL
            )
        else:
            self.folder_exclusion_regex_re = re.compile(
                "|".join(self.folder_exclusion_regex), re.IGNORECASE | re.DOTALL
            )
            self.file_name_exclusion_regex_re = re.compile(
                "|".join(self.file_name_exclusion_regex), re.IGNORECASE | re.DOTALL
            )
        self.client = client_cls(host_url=self.uri, api_key=self.apikey)
        if isinstance(self.client, SonarrAPI):
            self.type = "sonarr"
        else:
            self.type = "radarr"
        self.manager = manager
        if self.rss_sync_timer > 0:
            self.rss_sync_timer_last_checked = datetime(1970, 1, 1)
        else:
            self.rss_sync_timer_last_checked = None
        if self.refresh_downloads_timer > 0:
            self.refresh_downloads_timer_last_checked = datetime(1970, 1, 1)
        else:
            self.refresh_downloads_timer_last_checked = None

        self.queue = []
        self.cache = {}
        self.requeue_cache = {}
        self.sent_to_scan = set()
        self.sent_to_scan_hashes = set()
        self.files_probed = set()
        self.import_torrents = []
        self.change_priority = dict()
        self.recheck = set()
        self.pause = set()
        self.skip_blacklist = set()
        self.delete = set()
        self.resume = set()
        self.needs_cleanup = False

        self.timed_ignore_cache = ExpiringSet(max_age_seconds=self.ignore_torrents_younger_than)
        self.timed_skip = ExpiringSet(max_age_seconds=self.ignore_torrents_younger_than)

        self.session = requests.Session()

        self.manager.completed_folders.add(self.completed_folder)
        self.manager.category_allowlist.add(self.category)
        self.logger = logbook.Logger(self._name)
        self.logger.debug(
            "{group} Config: "
            "Managed: {managed}, "
            "Re-search: {search}, "
            "ImportMode: {import_mode}, "
            "Category: {category}, "
            "URI: {uri}, "
            "API Key: {apikey}, "
            "RefreshDownloadsTimer={refresh_downloads_timer}, "
            "RssSyncTimer={rss_sync_timer}",
            group=self._name,
            import_mode=self.import_mode,
            managed=self.managed,
            search=self.re_search,
            category=self.category,
            uri=self.uri,
            apikey=self.apikey,
            refresh_downloads_timer=self.refresh_downloads_timer,
            rss_sync_timer=self.rss_sync_timer,
        )
        self.logger.info(
            "Script Config:  CaseSensitiveMatches={CaseSensitiveMatches}",
            CaseSensitiveMatches=self.case_sensitive_matches,
        )
        self.logger.info(
            "Script Config:  FolderExclusionRegex={FolderExclusionRegex}",
            FolderExclusionRegex=self.folder_exclusion_regex,
        )
        self.logger.info(
            "Script Config:  FileNameExclusionRegex={FileNameExclusionRegex}",
            FileNameExclusionRegex=self.file_name_exclusion_regex,
        )
        self.logger.info(
            "Script Config:  FileExtensionAllowlist={FileExtensionAllowlist}",
            FileExtensionAllowlist=self.file_extension_allowlist,
        )
        self.logger.info("Script Config:  AutoDelete={AutoDelete}", AutoDelete=self.auto_delete)

        self.logger.info(
            "Script Config:  IgnoreTorrentsYoungerThan={IgnoreTorrentsYoungerThan}",
            IgnoreTorrentsYoungerThan=self.ignore_torrents_younger_than,
        )
        self.logger.info("Script Config:  MaximumETA={MaximumETA}", MaximumETA=self.maximum_eta)
        self.logger.info(
            "Script Config:  MaximumDeletablePercentage={MaximumDeletablePercentage}",
            MaximumDeletablePercentage=self.maximum_deletable_percentage,
        )

        if self.search_missing:
            self.logger.info(
                "Script Config:  SearchMissing={SearchMissing}",
                SearchMissing=self.search_missing,
            )
            self.logger.info(
                "Script Config:  AlsoSearchSpecials={AlsoSearchSpecials}",
                AlsoSearchSpecials=self.search_specials,
            )
            self.logger.info(
                "Script Config:  SearchByYear={SearchByYear}",
                SearchByYear=self.search_by_year,
            )
            self.logger.info(
                "Script Config:  SearchInReverse={SearchInReverse}",
                SearchInReverse=self.search_in_reverse,
            )
            self.logger.info(
                "Script Config:  StartYear={StartYear}",
                StartYear=self.search_starting_year,
            )
            self.logger.info(
                "Script Config:  LastYear={LastYear}",
                LastYear=self.search_ending_year,
            )
            self.logger.info(
                "Script Config:  StartYear={StartYear}",
                StartYear=self.search_command_limit,
            )
            self.logger.info(
                "Script Config:  DatabaseFile={DatabaseFile}",
                DatabaseFile=self.arr_db_file,
            )
        self.search_setup_completed = False
        self.model_arr_file: Union[EpisodesModel, MoviesModel] = None
        self.model_arr_command: CommandsModel = None
        self.model_file: Union[EpisodeFilesModel, MoviesFilesModel] = None
        self.model_queue: Union[EpisodeQueueModel, MovieQueueModel] = None

    @staticmethod
    def is_ignored_state(torrent: TorrentDictionary) -> bool:
        return torrent.state_enum in (
            TorrentStates.FORCED_DOWNLOAD,
            TorrentStates.FORCED_UPLOAD,
            TorrentStates.CHECKING_UPLOAD,
            TorrentStates.CHECKING_DOWNLOAD,
            TorrentStates.CHECKING_RESUME_DATA,
            TorrentStates.ALLOCATING,
            TorrentStates.MOVING,
        )

    @staticmethod
    def is_uploading_state(torrent: TorrentDictionary) -> bool:
        return torrent.state_enum in (
            TorrentStates.UPLOADING,
            TorrentStates.STALLED_UPLOAD,
            TorrentStates.QUEUED_UPLOAD,
        )

    @staticmethod
    def is_complete_state(torrent: TorrentDictionary):
        """Returns True if the State is categorized as Complete."""
        return torrent.state_enum in (
            TorrentStates.UPLOADING,
            TorrentStates.STALLED_UPLOAD,
            TorrentStates.PAUSED_UPLOAD,
            TorrentStates.QUEUED_UPLOAD,
        )

    @staticmethod
    def is_downloading_state(torrent: TorrentDictionary):
        """Returns True if the State is categorized as Downloading."""
        return torrent.state_enum in (
            TorrentStates.DOWNLOADING,
            TorrentStates.PAUSED_DOWNLOAD,
        )

    def _get_arr_modes(self):
        if self.type == "sonarr":
            return EpisodesModel, CommandsModel
        elif self.type == "radarr":
            return MoviesModel, CommandsModel

    def _get_models(self):
        if self.type == "sonarr":
            return EpisodeFilesModel, EpisodeQueueModel
        elif self.type == "radarr":
            return MoviesFilesModel, MovieQueueModel

    def register_search_mode(self):
        if self.search_setup_completed:
            return
        if self.search_missing is False:
            self.search_setup_completed = True
            return
        if not self.arr_db_file.exists():
            self.search_missing = False
            return
        else:
            self.arr_db = SqliteDatabase(None)
            self.arr_db.init(str(self.arr_db_file))
            self.arr_db.connect()

        self.db = SqliteDatabase(None)
        self.db.init(
            str(self.search_db_file),
            pragmas={
                "journal_mode": "wal",
                "cache_size": -1 * 64000,  # 64MB
                "foreign_keys": 1,
                "ignore_check_constraints": 0,
                "synchronous": 0,
            },
        )

        db1, db2 = self._get_models()

        class Files(db1):
            class Meta:
                database = self.db

        class Queue(db2):
            class Meta:
                database = self.db

        self.db.connect()
        self.db.create_tables([Files, Queue])

        self.model_file = Files
        self.model_queue = Queue

        db1, db2 = self._get_arr_modes()

        class Files(db1):
            class Meta:
                database = self.arr_db
                if self.type == "sonarr":
                    table_name = "Episodes"
                elif self.type == "radarr":
                    table_name = "Movies"

        class Commands(db2):
            class Meta:
                database = self.arr_db
                table_name = "Commands"

        self.model_arr_file = Files
        self.model_arr_command = Commands
        self.search_setup_completed = True

    def arr_db_query_commands_count(self) -> int:
        if not self.search_missing:
            return 0
        search_commands = (
            self.model_arr_command.select()
            .where(
                (self.model_arr_command.EndedAt == None)
                & (self.model_arr_command.Name.endswith("Search"))
            )
            .execute()
        )
        return len(list(search_commands))

    def db_update(self):
        if not self.search_missing:
            return
        self.logger.trace(f"Started updating database")
        with self.db.atomic():
            if self.type == "sonarr":
                for series in self.model_arr_file.select().where(
                    (self.model_arr_file.AirDateUtc != None)
                    & (self.model_arr_file.AirDateUtc < datetime.now(timezone.utc))
                    & (
                        self.model_arr_file.AirDateUtc
                        > datetime(month=1, day=1, year=self.search_current_year)
                    )
                    & (
                        self.model_arr_file.AirDateUtc
                        < datetime(month=12, day=31, year=self.search_current_year)
                    )
                ):
                    self.db_update_single_series(db_entry=series)
            elif self.type == "radarr":
                for series in (
                    self.model_arr_file.select()
                    .where((self.model_arr_file.Year == self.search_current_year))
                    .order_by(self.model_arr_file.Added.desc())
                ):
                    self.db_update_single_series(db_entry=series)
        self.logger.trace(f"Finished updating database")
        self._update_current_year()

    def db_update_single_series(self, db_entry: Union[EpisodesModel, MoviesModel] = None):
        if self.search_missing is False:
            return
        try:
            searched = False
            if self.type == "sonarr":
                db_entry: EpisodesModel
                if db_entry.EpisodeFileId != 0:
                    self.model_queue.update(completed=True).where(
                        (self.model_queue.EntryId == db_entry.Id)
                    ).execute()
                EntryId = db_entry.Id
                metadata = self.client.get_episode_by_episode_id(EntryId)
                SeriesTitle = metadata.get("series", {}).get("title")
                SeasonNumber = db_entry.SeasonNumber
                Title = db_entry.Title
                SeriesId = db_entry.SeriesId
                EpisodeFileId = db_entry.EpisodeFileId
                EpisodeNumber = db_entry.EpisodeNumber
                AbsoluteEpisodeNumber = db_entry.AbsoluteEpisodeNumber
                SceneAbsoluteEpisodeNumber = db_entry.SceneAbsoluteEpisodeNumber
                LastSearchTime = db_entry.LastSearchTime
                AirDateUtc = db_entry.AirDateUtc
                Monitored = db_entry.Monitored
                searched = searched

                self.logger.trace(
                    "Updating database entry - {SeriesTitle} - S{SeasonNumber:02d}E{EpisodeNumber:03d} - {Title}",
                    SeriesTitle=SeriesTitle,
                    SeasonNumber=SeasonNumber,
                    EpisodeNumber=EpisodeNumber,
                )

                db_commands = self.model_file.insert(
                    EntryId=EntryId,
                    Title=Title,
                    SeriesId=SeriesId,
                    EpisodeFileId=EpisodeFileId,
                    EpisodeNumber=EpisodeNumber,
                    AbsoluteEpisodeNumber=AbsoluteEpisodeNumber,
                    SceneAbsoluteEpisodeNumber=SceneAbsoluteEpisodeNumber,
                    LastSearchTime=LastSearchTime,
                    AirDateUtc=AirDateUtc,
                    Monitored=Monitored,
                    SeriesTitle=SeriesTitle,
                    SeasonNumber=SeasonNumber,
                    searched=searched,
                ).on_conflict(
                    conflict_target=[self.model_file.EntryId],
                    update={
                        self.model_file.Monitored: Monitored,
                        self.model_file.Title: Title,
                        self.model_file.AirDateUtc: AirDateUtc,
                        self.model_file.LastSearchTime: LastSearchTime,
                        self.model_file.SceneAbsoluteEpisodeNumber: SceneAbsoluteEpisodeNumber,
                        self.model_file.AbsoluteEpisodeNumber: AbsoluteEpisodeNumber,
                        self.model_file.EpisodeNumber: EpisodeNumber,
                        self.model_file.EpisodeFileId: EpisodeFileId,
                        self.model_file.SeriesId: SeriesId,
                        self.model_file.SeriesTitle: SeriesTitle,
                        self.model_file.SeasonNumber: SeasonNumber,
                        self.model_file.searched: searched or self.model_file.searched,
                    },
                )
            elif self.type == "radarr":
                db_entry: MoviesModel
                if db_entry.MovieFileId != 0:
                    searched = True
                    self.model_queue.update(completed=True).where(
                        (self.model_queue.EntryId == db_entry.Id)
                    ).execute()
                title = db_entry.Title
                monitored = db_entry.Monitored
                tmdbId = db_entry.TmdbId
                year = db_entry.Year
                EntryId = db_entry.Id
                MovieFileId = db_entry.MovieFileId
                self.logger.trace(
                    "Updating database entry - {title} ({tmdbId})", title=title, tmdbId=tmdbId
                )
                db_commands = self.model_file.insert(
                    title=title,
                    monitored=monitored,
                    TmdbId=tmdbId,
                    year=year,
                    EntryId=EntryId,
                    searched=searched,
                    MovieFileId=MovieFileId,
                ).on_conflict(
                    conflict_target=[self.model_file.EntryId],
                    update={
                        self.model_file.MovieFileId: MovieFileId,
                        self.model_file.monitored: monitored,
                        self.model_file.searched: searched or self.model_file.searched,
                    },
                )
            db_commands.execute()

        except Exception as e:
            self.logger.error(e, exc_info=sys.exc_info())

    def _update_current_year(self):
        if not self.search_missing:
            return
        if not len(list(self.db_get_files())):
            self.search_current_year += self._delta

    def db_get_files(self):
        if not self.search_missing:
            yield None
        elif self.type == "sonarr":
            condition = self.model_file.EpisodeFileId == 0
            if not self.search_specials:
                condition &= self.model_file.SeasonNumber != 0
            condition &= self.model_file.EpisodeFileId == 0
            condition &= self.model_file.AirDateUtc != None
            condition &= self.model_file.AirDateUtc < datetime.now(timezone.utc)
            condition &= self.model_file.AirDateUtc > datetime(
                month=1, day=1, year=self.search_current_year
            )
            condition &= self.model_file.AirDateUtc < datetime(
                month=12, day=31, year=self.search_current_year
            )

            for entry in (
                self.model_file.select()
                .where(condition)
                .order_by(
                    self.model_file.SeriesTitle,
                    self.model_file.SeasonNumber,
                    self.model_file.AirDateUtc.desc(),
                )
                .execute()
            ):
                yield entry
        elif self.type == "radarr":
            for entry in (
                self.model_file.select()
                .where(
                    (self.model_file.MovieFileId == 0)
                    & (self.model_file.year == self.search_current_year)
                )
                .order_by(self.model_file.title.asc())
                .execute()
            ):
                yield entry

    def maybe_do_search(self, file_model: Union[EpisodeFilesModel, MoviesFilesModel]):
        if not self.search_missing:
            return None
        elif self.type == "sonarr":
            queue = (
                self.model_queue.select()
                .where(
                    (self.model_queue.completed == False)
                    & (self.model_queue.EntryId == file_model.EntryId)
                )
                .execute()
            )
            if queue:
                self.logger.debug(
                    "Skipping: Already in queue : {file_model.SeriesTitle} - S{file_model.SeasonNumber:02d}E{file_model.EpisodeNumber:03d} - {file_model.Title} ",
                    file_model=file_model,
                )
                return True
            active_commands = self.arr_db_query_commands_count()
            self.logger.debug(
                "Sonarr: {active_commands} active search commands", active_commands=active_commands
            )
            if active_commands >= self.search_command_limit:
                self.logger.trace(
                    "Idle: Too many commands in queue : {file_model.SeriesTitle} - S{file_model.SeasonNumber:02d}E{file_model.EpisodeNumber:03d} - {file_model.Title}",
                    file_model=file_model,
                )
                return False
            self.model_queue.insert(
                completed=False,
                EntryId=file_model.EntryId,
            ).execute()
            self.client.post_command("EpisodeSearch", episodeIds=[file_model.EntryId])
            self.logger.notice(
                "Searching for : {file_model.SeriesTitle} - S{file_model.SeasonNumber:02d}E{file_model.EpisodeNumber:03d} - {file_model.Title}",
                file_model=file_model,
            )
            return True
        elif self.type == "radarr":
            queue = (
                self.model_queue.select()
                .where(
                    (self.model_queue.completed == False)
                    & (self.model_queue.EntryId == file_model.EntryId)
                )
                .execute()
            )
            active_commands = self.arr_db_query_commands_count()
            if queue:
                self.logger.debug(
                    "Skipping: Already in queue : {model.title} ({model.year})", model=file_model
                )
                return True
            self.logger.debug(
                "{active_commands} active search commands", active_commands=active_commands
            )
            if active_commands >= self.search_command_limit:
                self.logger.trace(
                    "Skipping: Too many in queue : {model.title} ({model.year})", model=file_model
                )
                return False
            self.model_queue.insert(
                completed=False,
                EntryId=file_model.EntryId,
            ).execute()
            self.client.post_command("MoviesSearch", movieIds=[file_model.EntryId])
            self.logger.notice("Searching for : {model.title} ({model.year})", model=file_model)
            return True

    def delete_from_queue(self, id_, remove_from_client=True, blacklist=True):
        params = {"removeFromClient": remove_from_client, "blocklist": blacklist}
        path = f"/api/v3/queue/{id_}"
        res = self.client.request_del(path, params=params)
        return res

    def post_command(self, name, **kwargs):
        data = {
            "name": name,
            **kwargs,
        }
        path = "/api/v3/command"
        res = self.client.request_post(path, data=data)
        return res

    def refresh_download_queue(self):
        if self.type == "sonarr":
            self.queue = self.client.get_queue()
        else:
            self.queue = self.client.get_queue(page_size=10000).get("records", [])

        self.cache = {
            entry["downloadId"]: entry["id"] for entry in self.queue if entry.get("downloadId")
        }
        if self.type == "sonarr":
            self.requeue_cache = defaultdict(list)
            for entry in self.queue:
                if "episode" in entry:
                    self.requeue_cache[entry["id"]].append(entry["episode"]["id"])
        else:
            self.requeue_cache = {
                entry["id"]: entry["movieId"] for entry in self.queue if entry.get("movieId")
            }

    def _remove_empty_folders(self) -> None:
        new_sent_to_scan = set()
        for path in absolute_file_paths(self.completed_folder):
            if path.is_dir() and not len(list(absolute_file_paths(path))):
                path.rmdir()
                self.logger.trace("Removing empty folder: {path}", path=path)
                if path in self.sent_to_scan:
                    self.sent_to_scan.discard(path)
                else:
                    new_sent_to_scan.add(path)
        self.sent_to_scan = new_sent_to_scan
        if not len(list(absolute_file_paths(self.completed_folder))):
            self.sent_to_scan = set()
            self.sent_to_scan_hashes = set()

    def process_entries(self, hashes: Set[str]) -> Tuple[List[Tuple[int, str]], Set[str]]:
        payload = [
            (_id, h.upper())
            for h in hashes
            if (_id := self.cache.get(h.upper())) is not None
            and not self.logger.debug(
                "Blocklisting: {name} ({hash})",
                hash=h,
                name=self.manager.qbit_manager.name_cache.get(h, "Deleted"),
            )
        ]
        hashes = {h for h in hashes if (_id := self.cache.get(h.upper())) is not None}

        return payload, hashes

    def folder_cleanup(self) -> None:
        if self.auto_delete is False:
            return
        if self.needs_cleanup is False:
            return
        folder = self.completed_folder
        self.logger.debug("Folder Cleanup: {folder}", folder=folder)
        for file in absolute_file_paths(folder):
            if file.name in {"desktop.ini", ".DS_Store"}:
                continue
            if file.is_dir():
                self.logger.trace("Folder Cleanup: File is a folder:  {file}", file=file)
                continue
            if file.suffix in self.file_extension_allowlist:
                self.logger.trace(
                    "Folder Cleanup: File has an allowed extension: {file}", file=file
                )
                if self.file_is_probeable(file):
                    self.logger.trace(
                        "Folder Cleanup: File is a valid media type: {file}", file=file
                    )
                    continue
            try:
                file.unlink(missing_ok=True)
                self.logger.debug("File removed: {path}", path=file)
            except PermissionError:
                self.logger.debug("File in use: Failed to remove file: {path}", path=file)
        self._remove_empty_folders()
        self.needs_cleanup = False

    def file_is_probeable(self, file: pathlib.Path) -> bool:
        if not self.manager.ffprobe_available:
            return True  # ffprobe is not in PATH, so we say every file is acceptable.
        try:
            if file in self.files_probed:
                self.logger.trace("Probeable: File has already been probed: {file}", file=file)
                return True
            if file.is_dir():
                self.logger.trace("Not Probeable: File is a directory: {file}", file=file)
                return False
            output = ffmpeg.probe(str(file.absolute()))
            if not output:
                self.logger.trace("Not Probeable: Probe returned no output: {file}", file=file)
                return False
            self.files_probed.add(file)
            return True
        except ffmpeg.Error as e:
            error = e.stderr.decode()
            self.logger.trace(
                "Not Probeable: Probe returned an error: {file}:\n{e.stderr}",
                e=e,
                file=file,
                exc_info=sys.exc_info(),
            )
            if "Invalid data found when processing input" in error:
                return False
            return False

    @property
    def is_alive(self) -> bool:
        try:
            req = self.session.get(
                f"{self.uri}/api/v3/system/status", timeout=0.5, headers={"X-Api-Key": self.apikey}
            )
            req.raise_for_status()
            self.logger.trace("Successfully connected to {url}", url=self.uri)
            return True
        except requests.RequestException:
            self.logger.warning("Could not connect to {url}", url=self.uri)
        return False

    def api_calls(self):
        if not self.is_alive:
            raise NoConnectionrException(
                "Service: %s did not respond on %s" % (self._name, self.uri)
            )
        now = datetime.now()
        if (
            self.rss_sync_timer_last_checked is not None
            and self.rss_sync_timer_last_checked < now - timedelta(minutes=self.rss_sync_timer)
        ):
            self.post_command("RssSync")
            self.rss_sync_timer_last_checked = now

        if (
            self.refresh_downloads_timer_last_checked is not None
            and self.refresh_downloads_timer_last_checked
            < now - timedelta(minutes=self.refresh_downloads_timer)
        ):
            self.post_command("RefreshMonitoredDownloads")
            self.refresh_downloads_timer_last_checked = now

    def _process_paused(self):
        # Bulks pause all torrents flagged for pausing.
        if self.pause:
            self.needs_cleanup = True
            self.logger.debug("Pausing {count} completed torrents", count=len(self.pause))
            for i in self.pause:
                self.logger.debug(
                    "Pausing {name} ({hash})",
                    hash=i,
                    name=self.manager.qbit_manager.name_cache.get(i),
                )
            self.manager.qbit.torrents_pause(torrent_hashes=self.pause)
            self.pause.clear()

    def _process_imports(self):
        if self.import_torrents:
            self.needs_cleanup = True
            for torrent in self.import_torrents:
                if torrent.hash in self.sent_to_scan:
                    continue
                path = validate_and_return_torrent_file(torrent.content_path)
                if not path.exists():
                    self.skip_blacklist.add(torrent.hash.upper())
                    self.logger.info(
                        "Deleting Missing Torrent: " "{torrent.name} ({torrent.hash})",
                        torrent=torrent,
                    )
                    continue
                if path in self.sent_to_scan:
                    continue
                self.sent_to_scan_hashes.add(torrent.hash)
                self.logger.notice(
                    "DownloadedEpisodesScan: {path}",
                    torrent=torrent,
                    path=path,
                )
                self.post_command(
                    "DownloadedEpisodesScan",
                    path=str(path),
                    downloadClientId=torrent.hash.upper(),
                    importMode=self.import_mode,
                )
                self.sent_to_scan.add(path)
            self.import_torrents.clear()

    def _process_failed_individual(self, hash_: str, entry: int, skip_blacklist: Set[str]):
        with contextlib.suppress(Exception):
            if hash_ not in skip_blacklist:
                self.delete_from_queue(id_=entry, blacklist=True)
            else:
                self.delete_from_queue(id_=entry, blacklist=False)
        object_id = self.requeue_cache.get(entry)
        if object_id:
            if self.type == "sonarr":
                data = self.client.get_episode_by_episode_id(object_id[0])
                name = data.get("title")
                if name:
                    episodeNumber = data.get("episodeNumber", 0)
                    absoluteEpisodeNumber = data.get("absoluteEpisodeNumber", 0)
                    seasonNumber = data.get("seasonNumber", 0)
                    seriesTitle = data.get("series", {}).get("title")
                    year = data.get("series", {}).get("year", 0)
                    tvdbId = data.get("series", {}).get("tvdbId", 0)
                    self.logger.notice(
                        "Re-Searching episode: {seriesTitle} ({year}) | "
                        "S{seasonNumber:02d}E{episodeNumber:03d} "
                        "({absoluteEpisodeNumber:04d}) | "
                        "{title} | "
                        "[tvdbId={tvdbId}|id={episode_ids}]",
                        episode_ids=object_id[0],
                        title=name,
                        year=year,
                        tvdbId=tvdbId,
                        seriesTitle=seriesTitle,
                        seasonNumber=seasonNumber,
                        absoluteEpisodeNumber=absoluteEpisodeNumber,
                        episodeNumber=episodeNumber,
                    )
                else:
                    self.logger.notice(
                        f"Re-Searching episodes: {' '.join([f'{i}' for i in object_id])}"
                    )
                self.post_command("EpisodeSearch", episodeIds=object_id)
            else:
                data = self.client.get_movie_by_movie_id(object_id)
                name = data.get("title")
                if name:
                    year = data.get("year", 0)
                    tmdbId = data.get("tmdbId", 0)
                    self.logger.notice(
                        "Re-Searching movie: {name} ({year}) | " "[tmdbId={tmdbId}|id={movie_id}]",
                        movie_id=object_id,
                        name=name,
                        year=year,
                        tmdbId=tmdbId,
                    )
                else:
                    self.logger.notice(
                        "Re-Searching movie: {movie_id}",
                        movie_id=object_id,
                    )
                self.post_command("MoviesSearch", movieIds=[object_id])

    def _process_failed(self):
        to_delete_all = self.delete.union(self.skip_blacklist)
        skip_blacklist = {i.upper() for i in self.skip_blacklist}
        if to_delete_all:
            self.needs_cleanup = True
            payload, hashes = self.process_entries(to_delete_all)
            if payload:
                for entry, hash_ in payload:
                    self._process_failed_individual(
                        hash_=hash_, entry=entry, skip_blacklist=skip_blacklist
                    )
            # Remove all bad torrents from the Client.
            self.manager.qbit.torrents_delete(hashes=to_delete_all, delete_files=True)
            for h in to_delete_all:
                if h in self.manager.qbit_manager.name_cache:
                    del self.manager.qbit_manager.name_cache[h]
                if h in self.manager.qbit_manager.cache:
                    del self.manager.qbit_manager.cache[h]
        self.skip_blacklist.clear()
        self.delete.clear()

    def _process_errored(self):
        # Recheck all torrents marked for rechecking.
        if self.recheck:
            self.needs_cleanup = True
            updated_recheck = [r for r in self.recheck]
            self.manager.qbit.torrents_recheck(torrent_hashes=updated_recheck)
            for k in updated_recheck:
                self.timed_ignore_cache.add(k)
            self.recheck.clear()

    def _process_resume(self):
        if self.resume:
            self.needs_cleanup = True
            self.manager.qbit.torrents_resume(torrent_hashes=self.resume)
            for k in self.resume:
                self.timed_ignore_cache.add(k)
            self.resume.clear()

    def _process_file_priority(self):
        # Set all files marked as "Do not download" to not download.
        for hash_, files in self.change_priority.copy().items():
            self.needs_cleanup = True
            name = self.manager.qbit_manager.name_cache.get(hash_)
            if name:
                self.logger.debug(
                    "Updating file priority on torrent: {name} ({hash})",
                    name=name,
                    hash=hash_,
                )
                self.manager.qbit.torrents_file_priority(
                    torrent_hash=hash_, file_ids=files, priority=0
                )
            else:
                self.logger.error("Torrent does not exist? {hash}", hash=hash_)
            del self.change_priority[hash_]

    def process(self):
        self._process_paused()
        self._process_errored()
        self._process_file_priority()
        self._process_imports()
        self._process_failed()
        self.folder_cleanup()

    def process_torrents(self):
        if has_internet() is False:
            self.manager.qbit_manager.should_delay_torrent_scan = True
            raise DelayLoopException(length=NO_INTERNET_SLEEP_TIMER, type="internet")
        if self.manager.qbit_manager.should_delay_torrent_scan:
            raise DelayLoopException(length=NO_INTERNET_SLEEP_TIMER, type="delay")
        try:
            self.api_calls()
            self.refresh_download_queue()
            time_now = time.time()
            torrents = self.manager.qbit_manager.client.torrents.info.all(
                category=self.category, sort="added_on", reverse=False
            )
            for torrent in torrents:
                if torrent.category != RECHECK_CATEGORY:
                    self.manager.qbit_manager.cache[torrent.hash] = torrent.category
                self.manager.qbit_manager.name_cache[torrent.hash] = torrent.name
                # Bypass everything if manually marked as failed
                if torrent.category == FAILED_CATEGORY:
                    self.logger.notice(
                        "Deleting manually failed torrent: "
                        "[Progress: {progress}%][Time Left: {timedelta}] | "
                        "{torrent.name} ({torrent.hash})",
                        torrent=torrent,
                        timedelta=timedelta(seconds=torrent.eta),
                        progress=round(torrent.progress * 100, 2),
                    )
                    self.delete.add(torrent.hash)
                # Bypass everything else if manually marked for rechecking
                elif torrent.category == RECHECK_CATEGORY:
                    self.logger.notice(
                        "Re-cheking manually set torrent: "
                        "[Progress: {progress}%][Time Left: {timedelta}] | "
                        "{torrent.name} ({torrent.hash})",
                        torrent=torrent,
                        timedelta=timedelta(seconds=torrent.eta),
                        progress=round(torrent.progress * 100, 2),
                    )
                    self.recheck.add(torrent.hash)
                # Do not touch torrents that are currently "Checking".
                elif self.is_ignored_state(torrent):
                    continue
                # Do not touch torrents recently resumed/reched (A torrent can temporarely stall after being resumed from a paused state).
                elif (torrent.hash in self.timed_ignore_cache) or (
                    torrent.hash in self.timed_skip
                ):
                    continue
                # Ignore torrents who have reached maximum percentage as long as the last activity is within the MaximumETA set for this category
                # For example if you set MaximumETA to 5 mines, this will ignore all torrets that have stalled at a higher percentage as long as there is activity
                # And the window of activity is determined by the current time - MaximumETA, if the last active was after this value ignore this torrent
                # the idea here is that if a torrent isn't completely dead some leecher/seeder may contribute towards your progress.
                # However if its completely dead and no activity is observed, then lets remove it and requeue a new torrent.
                elif (
                    torrent.progress >= self.maximum_deletable_percentage
                    and self.is_complete_state(torrent) is False
                ):
                    if torrent.last_activity < time_now - self.maximum_eta:
                        self.logger.info(
                            "Deleting Stale torrent: "
                            "[Progress: {progress}%] | ({torrent.hash}) {torrent.name}",
                            torrent=torrent,
                            progress=round(torrent.progress * 100, 2),
                        )
                        self.delete.add(torrent.hash)
                    else:
                        continue
                # Ignore torrents which have been submitted to their respective Arr instance for import.
                elif (
                    torrent.hash
                    in self.manager.managed_objects[torrent.category].sent_to_scan_hashes
                ):
                    continue
                # Some times torrents will error, this causes them to be rechecked so they complete downloading.
                elif torrent.state_enum == TorrentStates.ERROR:
                    self.logger.info(
                        "Rechecking Erroed torrent: " "{torrent.name} ({torrent.hash})",
                        torrent=torrent,
                    )
                    self.recheck.add(torrent.hash)
                # If a torrent was not just added, and the amount left to download is 0 and the torrent is Paused tell the Arr tools to process it.
                elif (
                    torrent.added_on > 0
                    and torrent.amount_left == 0
                    and self.is_complete_state(torrent)
                    and torrent.content_path
                    and torrent.completion_on < time_now - 30
                ):
                    self.logger.info(
                        "Pausing Completed torrent: "
                        "{torrent.name} ({torrent.hash}) | {torrent.state_enum}",
                        torrent=torrent,
                    )
                    self.pause.add(torrent.hash)
                    self.import_torrents.append(torrent)
                # Sometimes Sonarr/Radarr does not automatically remove the torrent for some reason,
                # this ensures that we can safelly remove it if the client is reporting the status of the client as "Missing files"
                elif torrent.state_enum == TorrentStates.MISSING_FILES:
                    self.logger.info(
                        "Deleting torrent with missing files: " "{torrent.name} ({torrent.hash})",
                        torrent=torrent,
                    )
                    # We do not want to blacklist these!!
                    self.skip_blacklist.add(torrent.hash)
                # Resume monitored downloads which have been paused.
                elif torrent.state_enum == TorrentStates.PAUSED_DOWNLOAD and torrent.progress < 1:
                    self.resume.add(torrent.hash)
                # Process torrents who have stalled at this point, only mark from for deletion if they have been added more than "IgnoreTorrentsYoungerThan" seconds ago
                elif torrent.state_enum in (
                    TorrentStates.METADATA_DOWNLOAD,
                    TorrentStates.STALLED_DOWNLOAD,
                ):
                    self.timed_skip.add(torrent.hash)
                    if torrent.added_on < time_now - self.ignore_torrents_younger_than:
                        self.logger.info(
                            "Deleting Stale torrent: "
                            "[Progress: {progress}%] | {torrent.name} ({torrent.hash})",
                            torrent=torrent,
                            progress=round(torrent.progress * 100, 2),
                        )
                        self.delete.add(torrent.hash)
                # If a torrent is Uploading Pause it, as long as its for being Forced Uploaded.
                elif (
                    self.is_uploading_state(torrent)
                    and torrent.seeding_time > 1
                    and torrent.amount_left == 0
                    and torrent.added_on > 0
                    and torrent.content_path
                ):
                    self.logger.info(
                        "Pausing uploading torrent: "
                        "{torrent.name} ({torrent.hash}) | {torrent.state_enum}",
                        torrent=torrent,
                    )
                    self.pause.add(torrent.hash)
                # Mark a torrent for deletion
                elif (
                    torrent.state_enum != TorrentStates.PAUSED_DOWNLOAD
                    and torrent.state_enum.is_downloading
                    and torrent.added_on < time_now - self.ignore_torrents_younger_than
                    and torrent.eta > self.maximum_eta
                ):
                    self.logger.info(
                        "Deleting slow torrent: "
                        "[Progress: {progress}%][Time Left: {timedelta}] | "
                        "{torrent.name} ({torrent.hash})",
                        torrent=torrent,
                        timedelta=timedelta(seconds=torrent.eta),
                        progress=round(torrent.progress * 100, 2),
                    )
                    self.delete.add(torrent.hash)
                # Process uncompleted torrents
                elif torrent.state_enum.is_downloading:
                    # If a torrent availability hasn't reached 100% or more within the configurable "IgnoreTorrentsYoungerThan" variable, mark it for deletion.
                    if (
                        torrent.added_on < time_now - self.ignore_torrents_younger_than
                        and torrent.availability < 1
                    ):
                        self.logger.info(
                            "Deleting Stale torrent: "
                            "[Progress: {progress}%][Availability: {availability}%]"
                            "[Last active: {last_activity}] | {torrent.name} ({torrent.hash})",
                            torrent=torrent,
                            progress=round(torrent.progress * 100, 2),
                            availability=round(torrent.availability * 100, 2),
                            last_activity=torrent.last_activity,
                        )
                        self.delete.add(torrent.hash)

                    else:
                        # A downloading torrent is not stalled, parse its contents.
                        _remove_files = set()
                        total = len(torrent.files)
                        for file in torrent.files:
                            file_path = pathlib.Path(file.name)
                            # Acknowledge files that already been marked as "Don't download"
                            if file.priority == 0:
                                total -= 1
                                continue
                            # A file in the torrent does not have the allowlisted extensions, mark it for exclusion.
                            if file_path.suffix not in self.file_extension_allowlist:
                                self.logger.debug(
                                    "Removing File: Not allowed - Extension: "
                                    "{suffix}  | {torrent.name} ({torrent.hash}) | {file.name} ",
                                    torrent=torrent,
                                    file=file,
                                    suffix=file_path.suffix,
                                )
                                _remove_files.add(file.id)
                                total -= 1
                            # A folder within the folder tree matched the terms in FolderExclusionRegex, mark it for exclusion.
                            elif any(
                                self.folder_exclusion_regex_re.match(p.name.lower())
                                for p in file_path.parents
                                if (folder_match := p.name)
                            ):
                                self.logger.debug(
                                    "Removing File: Not allowed - Parent: "
                                    "{folder_match} | {torrent.name} ({torrent.hash}) | {file.name} ",
                                    torrent=torrent,
                                    file=file,
                                    folder_match=folder_match,
                                )
                                _remove_files.add(file.id)
                                total -= 1
                            # A file matched and entry in FileNameExclusionRegex, mark it for exclusion.
                            elif match := self.file_name_exclusion_regex_re.search(file_path.name):
                                self.logger.debug(
                                    "Removing File: Not allowed - Name: "
                                    "{match} | {torrent.name} ({torrent.hash}) | {file.name}",
                                    torrent=torrent,
                                    file=file,
                                    match=match.group(),
                                )
                                _remove_files.add(file.id)
                                total -= 1
                            # If all files in the torrent are marked for exlusion then delete the torrent.
                            if total == 0:
                                self.logger.info(
                                    "Deleting All files ignored: "
                                    "{torrent.name} ({torrent.hash})",
                                    torrent=torrent,
                                )
                                self.delete.add(torrent.hash)
                            # Mark all bad files and folder for exclusion.
                            elif _remove_files and torrent.hash not in self.change_priority:
                                self.change_priority[torrent.hash] = list(_remove_files)
            self.process()
        except NoConnectionrException as e:
            self.logger.error(e.message)
        except Exception as e:
            self.logger.error(e, exc_info=sys.exc_info())

    def run_torrent_loop(self) -> NoReturn:
        while True:
            try:
                try:
                    if not self.manager.qbit_manager.is_alive:
                        raise NoConnectionrException("Could not connect to qBit client.")
                    self.process_torrents()
                except NoConnectionrException as e:
                    self.logger.error(e.message)
                    self.manager.qbit_manager.should_delay_torrent_scan = True
                    raise DelayLoopException(length=300, type="qbit")
                except DelayLoopException:
                    raise
                except Exception as e:
                    self.logger.error(e, exc_info=sys.exc_info())
                time.sleep(LOOP_SLEEP_TIMER)
            except DelayLoopException as e:
                if e.type == "qbit":
                    self.logger.critical(
                        "Failed to connected to qBit client, sleeping for %s."
                        % timedelta(seconds=e.length)
                    )
                elif e.type == "internet":
                    self.logger.critical(
                        "Failed to connected to the internet, sleeping for %s."
                        % timedelta(seconds=e.length)
                    )
                elif e.type == "delay":
                    self.logger.critical(
                        "Forced delay due to temporary issue with environment, sleeping for %s."
                        % timedelta(seconds=e.length)
                    )
                time.sleep(e.length)
                self.manager.qbit_manager.should_delay_torrent_scan = False

    def run_search_loop(self) -> NoReturn:
        self.register_search_mode()
        if not self.search_missing:
            return None
        count_start = self.search_current_year
        stopping_year = datetime.now().year if self.search_in_reverse else 1900
        while True:
            self.db_update()
            try:
                for entry in self.db_get_files():
                    while self.maybe_do_search(entry) is False:
                        time.sleep(30)
                self.search_current_year += self._delta
                if self.search_in_reverse:
                    if self.search_current_year > stopping_year:
                        self.search_current_year = copy(count_start)
                        time.sleep(60)
                else:
                    if self.search_current_year < stopping_year:
                        self.search_current_year = copy(count_start)
                        time.sleep(60)
            except Exception as e:
                self.logger.exception(e, exc_info=sys.exc_info())

    def spawn_child_processes(self):
        _temp = []
        if self.search_missing:
            self.process_search_loop = pathos.helpers.mp.Process(
                target=self.run_search_loop, daemon=True
            )
            self.manager.qbit_manager.child_processes.append(self.process_search_loop)
            _temp.append(self.process_search_loop)
        self.process_torrent_loop = pathos.helpers.mp.Process(
            target=self.run_torrent_loop, daemon=True
        )
        self.manager.qbit_manager.child_processes.append(self.process_torrent_loop)
        _temp.append(self.process_torrent_loop)

        [p.start() for p in _temp]


class PlaceHolderArr(Arr):
    def __init__(
        self,
        name: str,
        manager: ArrManager,
    ):
        if name in manager.groups:
            raise EnvironmentError("Group '{name}' has already been registered.")
        self._name = name
        self.category = name
        self.manager = manager
        self.queue = []
        self.cache = {}
        self.requeue_cache = {}
        self.sent_to_scan = set()
        self.sent_to_scan_hashes = set()
        self.files_probed = set()
        self.import_torrents = []
        self.change_priority = dict()
        self.recheck = set()
        self.pause = set()
        self.skip_blacklist = set()
        self.delete = set()
        self.resume = set()
        self.IGNORE_TORRENTS_YOUNGER_THAN = CONFIG.getint(
            "Settings", "IgnoreTorrentsYoungerThan", fallback=600
        )
        self.timed_ignore_cache = ExpiringSet(max_age_seconds=self.IGNORE_TORRENTS_YOUNGER_THAN)
        self.timed_skip = ExpiringSet(max_age_seconds=self.IGNORE_TORRENTS_YOUNGER_THAN)
        self.logger = logbook.Logger(self._name)
        self.search_missing = False

    def _process_failed(self):
        if not (self.delete or self.skip_blacklist):
            return
        to_delete_all = self.delete.union(self.skip_blacklist)
        skip_blacklist = {i.upper() for i in self.skip_blacklist}
        if to_delete_all:
            for arr in self.manager.managed_objects.values():
                payload, hashes = arr.process_entries(to_delete_all)
                if payload:
                    for entry, hash_ in payload:
                        if hash_ in arr.cache:
                            arr._process_failed_individual(
                                hash_=hash_, entry=entry, skip_blacklist=skip_blacklist
                            )

            # Remove all bad torrents from the Client.
            self.manager.qbit.torrents_delete(hashes=to_delete_all, delete_files=True)
            for h in to_delete_all:
                if h in self.manager.qbit_manager.name_cache:
                    del self.manager.qbit_manager.name_cache[h]
                if h in self.manager.qbit_manager.cache:
                    del self.manager.qbit_manager.cache[h]
        self.skip_blacklist.clear()
        self.delete.clear()

    def _process_errored(self):
        # Recheck all torrents marked for rechecking.
        if self.recheck:
            temp = defaultdict(list)
            updated_recheck = []
            for h in self.recheck:

                updated_recheck.append(h)
                if c := self.manager.qbit_manager.cache.get(h):
                    temp[c].append(h)
            self.manager.qbit.torrents_recheck(torrent_hashes=updated_recheck)
            for k, v in temp.items():
                self.manager.qbit.torrents_set_category(torrent_hashes=v, category=k)

            for k in updated_recheck:
                self.timed_ignore_cache.add(k)
            self.recheck.clear()

    def process(self):
        self._process_errored()
        self._process_failed()

    def process_torrents(self):
        if has_internet() is False:
            self.manager.qbit_manager.should_delay_torrent_scan = True
            raise DelayLoopException(length=NO_INTERNET_SLEEP_TIMER, type="internet")
        if self.manager.qbit_manager.should_delay_torrent_scan:
            raise DelayLoopException(length=NO_INTERNET_SLEEP_TIMER, type="delay")
        try:
            torrents = self.manager.qbit_manager.client.torrents.info.all(
                category=self.category, sort="added_on", reverse=False
            )
            for torrent in torrents:
                if torrent.category != RECHECK_CATEGORY:
                    self.manager.qbit_manager.cache[torrent.hash] = torrent.category
                self.manager.qbit_manager.name_cache[torrent.hash] = torrent.name
                # Bypass everything if manually marked as failed
                if torrent.category == FAILED_CATEGORY:
                    self.logger.notice(
                        "Deleting manually failed torrent: "
                        "[Progress: {progress}%][Time Left: {timedelta}] | "
                        "{torrent.name} ({torrent.hash})",
                        torrent=torrent,
                        timedelta=timedelta(seconds=torrent.eta),
                        progress=round(torrent.progress * 100, 2),
                    )
                    self.delete.add(torrent.hash)
                # Bypass everything else if manually marked for rechecking
                elif torrent.category == RECHECK_CATEGORY:
                    self.logger.notice(
                        "Re-cheking manually set torrent: "
                        "[Progress: {progress}%][Time Left: {timedelta}] | "
                        "{torrent.name} ({torrent.hash})",
                        torrent=torrent,
                        timedelta=timedelta(seconds=torrent.eta),
                        progress=round(torrent.progress * 100, 2),
                    )
                    self.recheck.add(torrent.hash)
            self.process()
        except NoConnectionrException as e:
            self.logger.error(e.message)
        except Exception as e:
            self.logger.error(e, exc_info=sys.exc_info())

    def run_search_loop(self):
        return


class ArrManager:
    def __init__(self, qbitmanager: qBitManager):
        self.groups: Set[str] = set()
        self.uris: Set[str] = set()
        self.special_categories: Set[str] = {FAILED_CATEGORY, RECHECK_CATEGORY}
        self.category_allowlist: Set[str] = self.special_categories.copy()

        self.completed_folders: Set[pathlib.Path] = set()
        self.managed_objects: Dict[str, Arr] = {}
        self.ffprobe_available: bool = bool(shutil.which("ffprobe"))
        self.qbit: qbittorrentapi.Client = qbitmanager.client
        self.qbit_manager: qBitManager = qbitmanager
        self.logger = logger
        if not self.ffprobe_available:
            self.logger.error(
                "ffprobe was not found in your PATH, disabling all functionality dependant on it."
            )

    def build_arr_instances(self):
        for key in CONFIG.sections():
            if search := re.match("(rad|son)arr.*", key, re.IGNORECASE):
                name = search.group(0)
                match = search.group(1)
                if match.lower() == "son":
                    call_cls = SonarrAPI
                elif match.lower() == "rad":
                    call_cls = RadarrAPI
                else:
                    call_cls = None
                try:
                    managed_object = Arr(name, self, client_cls=call_cls)
                    self.groups.add(name)
                    self.uris.add(managed_object.uri)
                    self.managed_objects[managed_object.category] = managed_object
                except (NoSectionError, NoOptionError) as e:
                    logger.exception(e.message)
                except SkipException:
                    continue
                except EnvironmentError as e:
                    logger.exception(e)
        for cat in self.special_categories:
            managed_object = PlaceHolderArr(cat, self)
            self.managed_objects[cat] = managed_object
        return self
