from __future__ import annotations
from dataclasses import dataclass

class ToolError(RuntimeError): pass
class SafetyError(ToolError): pass
class RemoteError(ToolError): pass
class MissingFile(RemoteError): pass

@dataclass(frozen=True)
class RemoteFile:
    file_id: str
    name: str
    size: int
    sha1: str
    parent_id: str = '0'
    pickcode: str = ''
    path: str = ''
    is_dir: bool = False

@dataclass(frozen=True)
class DownloadLink:
    url: str
    headers: dict
