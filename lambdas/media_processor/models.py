from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["image", "video", "audio", "other"]


class MediaRecord(BaseModel):
    """One item's metadata sidecar.

    Written by three producers that must not clobber each other: the uploader
    sets identity and category before the object lands, the processor adds what
    it can read from the file, and the API records archive and category
    decisions. Unknown fields are ignored so an older sidecar still loads.
    """

    model_config = ConfigDict(extra="ignore")

    key: str
    name: str
    kind: Kind = "other"
    size: int = 0
    source_path: str | None = None
    category: str | None = None

    modified_at: str | None = None
    source_mtime: str | None = None
    taken_at: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    title: str | None = None
    artist: str | None = None

    thumb: str | None = None
    renderable: bool = False
    reason: str | None = None

    archived: bool = False
    archived_by: str | None = None
    archived_at: str | None = None
    category_by: str | None = None
    category_at: str | None = None

    def sort_key(self) -> str:
        """Best available date: when it was taken, else when the file was last
        written on disk, and only then when it happened to be uploaded.

        Anything before 1990 is a clockless camera writing an epoch-relative
        stamp, and is worse than no date at all since it buries the item.
        """
        for candidate in (self.taken_at, self.source_mtime, self.modified_at):
            if candidate and candidate >= "1990":
                return candidate
        return self.modified_at or ""

    def to_json(self) -> bytes:
        return self.model_dump_json(exclude_none=True).encode("utf-8")


class ArchiveEntry(BaseModel):
    """A row in the archive section: either an object under archive/, or an
    item archived in place, which keeps its media/ key and stays viewable."""

    model_config = ConfigDict(extra="ignore")

    key: str
    name: str
    size: int = 0
    storage_class: str = "STANDARD"
    kind: Kind = "other"
    category: str | None = None
    thumb: str | None = None
    taken_at: str | None = None
    modified_at: str | None = None
    duration: float | None = None
    playable: bool = False


class Manifest(BaseModel):
    generated_at: str
    items: list[MediaRecord] = Field(default_factory=list)
    archive: list[ArchiveEntry] = Field(default_factory=list)

    def to_json(self) -> bytes:
        return self.model_dump_json(exclude_none=True).encode("utf-8")
