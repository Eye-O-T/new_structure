// 날짜·카메라 선택과 조회 결과를 연결한다. 선택이나 새로고침 알림이 바뀌면 API를 다시 조회한다.
// autoDispose는 더 이상 보지 않는 조회 상태를 해제하며 계정 전환 시 초기화는 main.dart가 맡는다.
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:app/core/network/providers.dart';
import 'package:app/features/events/data/api_event_repository.dart';
import 'package:app/features/events/domain/event.dart';

final camerasProvider = FutureProvider.autoDispose<List<Map<String, dynamic>>>((
  ref,
) {
  return ref.watch(apiClientProvider).listAll('/api/v1/cameras');
});

class SelectedCamera extends Notifier<String?> {
  @override
  String? build() => null;
  void select(String? id) => state = id;
}

final selectedCameraProvider = NotifierProvider<SelectedCamera, String?>(
  SelectedCamera.new,
);

class SelectedDateNotifier extends Notifier<DateTime> {
  @override
  DateTime build() {
    final now = DateTime.now();
    return DateTime(now.year, now.month, now.day);
  }

  void selectDate(DateTime date) =>
      state = DateTime(date.year, date.month, date.day);
}

final selectedDateProvider = NotifierProvider<SelectedDateNotifier, DateTime>(
  SelectedDateNotifier.new,
);

final eventsProvider = FutureProvider.autoDispose<List<Event>>((ref) async {
  final api = ref.watch(apiClientProvider);
  final selected = ref.watch(selectedCameraProvider);
  final date = ref.watch(selectedDateProvider);
  final cameras = await ref.watch(camerasProvider.future);
  final cameraIds = cameras.map((c) => c['camera_id'].toString()).toSet();
  String? camera = cameraIds.contains(selected) ? selected : null;
  if (!api.isAdmin && camera == null) {
    if (cameras.isEmpty) return [];
    camera = cameras.first['camera_id'].toString();
  }
  return ApiEventRepository(
    apiClient: api,
    cameraId: camera,
  ).getEventsByDate(date);
});

final eventDetailProvider = FutureProvider.autoDispose.family<Event, String>((
  ref,
  id,
) async {
  final json = await ref
      .watch(apiClientProvider)
      .request('GET', '/api/v1/events/${Uri.encodeComponent(id)}');
  return Event.fromJson(json);
});
