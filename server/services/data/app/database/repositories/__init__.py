# 분야별 저장 코드를 하나의 저장소 객체로 묶어 공통 DB 관리 객체를 사용하게 한다.
"""이벤트와 후속 알림·객체 작업은 같은 연결에서 함께 커밋한다.
분야별 mixin은 SQL을 나누되 별도 중첩 트랜잭션을 열지 않는다."""

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
