"""Job dataclass — holds everything needed to process one movie."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..ai.name_parser import MovieInfo
from ..media.audio import AudioOutputTrack
from ..media.streams import ProbeResult


@dataclass
class Job:
    # Source
    source_path:  Path          # folder or file in Torrents
    video_file:   Path          # actual .mkv/.mp4 inside source_path

    # Parsed identity
    movie_info:   MovieInfo

    # Stream analysis
    probe_result: ProbeResult
    audio_tracks: list[AudioOutputTrack] = field(default_factory=list)

    @property
    def output_name(self) -> str:
        """The standardized folder/file name, e.g. 'Tarzan (1999) [1080p] AC3'."""
        info = self.movie_info
        res  = info.resolution or self.probe_result.resolution
        year = f" ({info.year})" if info.year else ""
        suffix = self.probe_result.folder_suffix
        return f"{info.title}{year} [{res}]{suffix}"

    @property
    def output_dir(self) -> Path:
        from .. import config
        return config.OUTPUT_DIR / self.output_name

    @property
    def output_file(self) -> Path:
        return self.output_dir / f"{self.output_name}.mkv"
