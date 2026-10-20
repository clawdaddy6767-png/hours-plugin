from __future__ import annotations
from typing import Any, Iterator, List, Optional, Tuple
from ..model import Session
from . import cline
SOURCE = "roo_code"
NEVER_READ = cline.NEVER_READ
def task_files(home: Optional[str] = None) -> List[Tuple[str, str]]:
    return cline.task_files(home, SOURCE)
def parse_roo_task(path: str, **kwargs: Any) -> Session:
    return cline.parse_task(path, source=SOURCE, **kwargs)
def iter_sessions(home: Optional[str] = None, **kwargs: Any) -> Iterator[Session]:
    return cline.iter_sessions(home, SOURCE, **kwargs)
__all__ = ["parse_roo_task", "task_files", "iter_sessions", "SOURCE", "NEVER_READ"]
