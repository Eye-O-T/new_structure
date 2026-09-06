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
