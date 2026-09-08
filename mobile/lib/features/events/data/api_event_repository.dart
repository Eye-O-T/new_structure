// 사용자가 고른 현지 날짜의 자정~다음 자정을 UTC로 바꾸어 이벤트 API를 조회한다.
// 서버 응답은 페이지 끝까지 모은 뒤 최신 발생 순서로 정렬해 화면에 전달한다.
import 'package:app/core/network/api_client.dart';
import '../domain/event.dart';
import 'event_repository.dart';

class ApiEventRepository implements EventRepository {
  ApiEventRepository({required this.apiClient, this.cameraId});
  final ApiClient apiClient;
  final String? cameraId;

  @override
  Future<List<Event>> getEventsByDate(DateTime date) async {
    final from = DateTime(date.year, date.month, date.day);
    final to = DateTime(date.year, date.month, date.day + 1);
    final rows = await apiClient.listAll(
      '/api/v1/events',
      query: {
        'from': from.toUtc().toIso8601String(),
        'to': to.toUtc().toIso8601String(),
        'camera_id': ?cameraId,
      },
    );
    final events = rows.map(Event.fromJson).toList();
    events.sort((a, b) {
      final time = b.occurredAt.compareTo(a.occurredAt);
      return time != 0 ? time : b.id.compareTo(a.id);
    });
    return events;
  }
}
