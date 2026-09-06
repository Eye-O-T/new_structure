"""Domain repositories sharing one database and transaction boundary.

Events enqueue push and object jobs using the same connection before commit.
The mixins split domain SQL without opening nested transactions for those jobs.
"""

from ..connection import Database
from .base import CameraHasHistory, CameraLimitReached
from .cameras import CamerasRepositoryMixin
from .events import EventsRepositoryMixin
from .notifications import PushRepositoryMixin
from .objects import ObjectRepositoryMixin
from .recordings import RecordingsRepositoryMixin
from .recovery import RecoveryRepositoryMixin
from .sessions import SessionsRepositoryMixin
from .users import UsersRepositoryMixin

__all__ = ["DataRepository", "CameraHasHistory", "CameraLimitReached"]


class DataRepository(
    UsersRepositoryMixin,
    CamerasRepositoryMixin,
    RecordingsRepositoryMixin,
    EventsRepositoryMixin,
    SessionsRepositoryMixin,
    RecoveryRepositoryMixin,
    PushRepositoryMixin,
    ObjectRepositoryMixin,
):
    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        self.database.initialize()
