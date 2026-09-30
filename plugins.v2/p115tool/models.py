from __future__ import annotations
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional


class StorageType(str, Enum):
    NORMAL = "NORMAL"
    SHARE = "SHARE"
    CACHE = "CACHE"


class Status(str, Enum):
    DISCOVERED = "DISCOVERED"
    ORGANIZED = "ORGANIZED"
    SHARE_CREATING = "SHARE_CREATING"
    SHARED = "SHARED"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    STRM_CREATED = "STRM_CREATED"
    SOURCE_DELETING = "SOURCE_DELETING"
    READY = "READY"
    FAILED_SHARE = "FAILED_SHARE"
    FAILED_VERIFY = "FAILED_VERIFY"
    FAILED_STRM = "FAILED_STRM"
    FAILED_DELETE = "FAILED_DELETE"
    BROKEN = "BROKEN"


@dataclass(frozen=True)
class RemoteFile:
    file_id: str
    name: str
    size: int
    sha1: str
    parent_id: str = "0"
    pickcode: str = ""
    path: str = ""
    is_dir: bool = False

    def matches(self, other: RemoteFile) -> bool:
        return (not self.is_dir and not other.is_dir and self.name == other.name
                and self.size == other.size and bool(self.sha1)
                and self.sha1.upper() == other.sha1.upper())


@dataclass(frozen=True)
class DownloadLink:
    url: str
    headers: dict


@dataclass
class MediaObject:
    id: int
    token: str
    title: str
    file_name: str
    virtual_path: str
    size: int
    sha1: str
    storage_type: str
    status: str
    strm_path: Optional[str]
    source_deleted: int
    created_at: float
    updated_at: float
    error: Optional[str]
    tmdb_id: Optional[int]

    def public(self) -> dict:
        data = asdict(self)
        data.pop("token")
        return data


class ToolError(RuntimeError):
    pass


class SafetyError(ToolError):
    pass


class RemoteError(ToolError):
    pass


class MissingFile(RemoteError):
    pass
