// 화면이 HTTP 구현을 직접 알지 않고 날짜별 이벤트 목록을 요청할 수 있게 하는 계약이다.
import 'package:app/features/events/domain/event.dart';

/// 화면과 조회 구현을 분리해 테스트에서 저장소를 교체할 수 있게 한다.
abstract class EventRepository {
  /// 전달한 날짜의 현지 달력 하루에 해당하는 이벤트를 반환한다.
  Future<List<Event>> getEventsByDate(DateTime date);
}
