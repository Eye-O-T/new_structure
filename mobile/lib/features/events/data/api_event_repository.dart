// 사용자가 고른 현지 날짜의 자정~다음 자정을 UTC로 바꾸어 이벤트 API를 조회한다.
// 서버 cursor로 고정한 조회 범위를 한 페이지씩 최신순으로 전달한다.
import 'package:app/core/network/api_client.dart';
import '../domain/event.dart';
import 'event_repository.dart';

/// 현지 날짜와 선택 카메라를 이벤트 API의 시간 범위·카메라 필터로 변환한다.
class ApiEventRepository implements EventRepository {
  ApiEventRepository({required this.apiClient, this.cameraId});
  final ApiClient apiClient;
  final String? cameraId;

  /// 현지 자정 이상 다음 날 자정 미만의 이벤트를 받아 최신순으로 반환한다.
  @override
  Future<EventPage> getPage(
    DateTime date, {
    String? cursor,
    ApiCancellation? cancellation,
  }) async {
    final from = DateTime(date.year, date.month, date.day);
    // 현지 달력에서 다음 날을 만들어 일광절약시간제 전환일도 같은 날짜 경계를 사용한다.
    final to = DateTime(date.year, date.month, date.day + 1);
    final result = await apiClient.request(
      'GET',
      '/api/v1/events',
      query: {
        'from': from.toUtc().toIso8601String(),
        'to': to.toUtc().toIso8601String(),
        'camera_id': ?cameraId,
        'order': 'desc',
        'limit': '50',
        'offset': '0',
        'cursor': ?cursor,
      },
      cancellation: cancellation,
    );
    final rows = (result['items'] as List).cast<Map<String, dynamic>>();
    final events = rows.map(Event.fromJson).toList();
    // 발생 시각이 같으면 ID 비교로 순서를 정해 동일 응답의 표시 순서를 일관되게 유지한다.
    events.sort((a, b) {
      final time = b.occurredAt.compareTo(a.occurredAt);
      return time != 0
          ? time
          : BigInt.parse(b.id).compareTo(BigInt.parse(a.id));
    });
    return EventPage(
      items: events,
      nextCursor: result['next_cursor'] as String?,
    );
  }
}
