// 화면이 HTTP 구현을 직접 알지 않고 날짜별 이벤트 목록을 요청할 수 있게 하는 계약이다.
import 'package:app/features/events/domain/event.dart';
import 'package:app/core/network/api_client.dart';

class EventPage {
  const EventPage({required this.items, required this.nextCursor});
  final List<Event> items;
  final String? nextCursor;
  bool get hasMore => nextCursor != null;
}

/// 화면과 조회 구현을 분리해 테스트에서 저장소를 교체할 수 있게 한다.
abstract class EventRepository {
  /// 첫 페이지의 서버 스냅샷을 cursor에 유지하여 추가·삭제와 무관하게 이어 조회한다.
  Future<EventPage> getPage(
    DateTime date, {
    String? cursor,
    ApiCancellation? cancellation,
  });
}
