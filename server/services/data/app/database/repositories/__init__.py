# 분야별 저장 코드를 하나의 저장소 객체로 묶어 공통 DB 관리 객체를 사용하게 한다.
"""이벤트와 후속 알림·객체 작업은 같은 연결에서 함께 커밋한다.
분야별 mixin은 SQL을 나누되 별도 중첩 트랜잭션을 열지 않는다."""

from ..connection import Database
from .base import CameraHasHistory, CameraLimitReached
from .cameras import CamerasRepositoryMixin
from .events import EventsRepositoryMixin
from .identity import MATCH_MARGIN, MATCH_THRESHOLD, validate_match_policy
from .notifications import PushRepositoryMixin
from .objects import ObjectRepositoryMixin
from .recordings import RecordingsRepositoryMixin
from .recovery import RecoveryRepositoryMixin
from .retention import RetentionRepositoryMixin
from .sessions import SessionsRepositoryMixin
from .users import UsersRepositoryMixin

__all__ = ["DataRepository", "CameraHasHistory", "CameraLimitReached"]


# 도메인별 SQL 구현을 동일한 Database 인스턴스 위에 합쳐 서비스에 제공한다.
class DataRepository(
    UsersRepositoryMixin,
    CamerasRepositoryMixin,
    RecordingsRepositoryMixin,
    EventsRepositoryMixin,
    SessionsRepositoryMixin,
    RecoveryRepositoryMixin,
    PushRepositoryMixin,
    ObjectRepositoryMixin,
    RetentionRepositoryMixin,
):
    def __init__(
        self,
        database: Database,
        *,
        identity_match_threshold: float = MATCH_THRESHOLD,
        identity_match_margin: float = MATCH_MARGIN,
    ) -> None:
        validate_match_policy(identity_match_threshold, identity_match_margin)
        self.database = database
        self.identity_match_threshold = identity_match_threshold
        self.identity_match_margin = identity_match_margin

    def initialize(self) -> None:
        self.database.initialize()
