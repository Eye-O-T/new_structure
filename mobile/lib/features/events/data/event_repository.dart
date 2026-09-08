// 화면이 HTTP 구현을 직접 알지 않고 날짜별 이벤트 목록을 요청할 수 있게 하는 계약이다.
import 'package:app/features/events/domain/event.dart';

abstract class EventRepository {
  Future<List<Event>> getEventsByDate(DateTime date);
}
