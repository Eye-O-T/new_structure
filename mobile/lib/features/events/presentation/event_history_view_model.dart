// 날짜·카메라 선택과 조회 결과를 연결한다. 선택이나 새로고침 알림이 바뀌면 API를 다시 조회한다.
// autoDispose는 더 이상 보지 않는 조회 상태를 해제하며 계정 전환 시 초기화는 main.dart가 맡는다.
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:app/core/network/providers.dart';
import 'package:app/features/events/data/api_event_repository.dart';
import 'package:app/features/events/domain/event.dart';

/// 서버가 현재 사용자에게 허용한 카메라를 페이지 끝까지 조회한다.
final camerasProvider = FutureProvider.autoDispose<List<Map<String, dynamic>>>((
  ref,
) {
  return ref.watch(apiClientProvider).listAll('/api/v1/cameras');
});

/// 카메라 선택을 공유한다. null의 실제 조회 범위는 사용자 역할에 따라 결정한다.
class SelectedCamera extends Notifier<String?> {
  @override
  String? build() => null;
  void select(String? id) => state = id;
}

/// 히스토리 선택과 알림에서 지정한 카메라가 같은 상태를 사용하게 한다.
final selectedCameraProvider = NotifierProvider<SelectedCamera, String?>(
  SelectedCamera.new,
);

/// 선택한 날짜의 시·분·초를 제거해 현지 달력 날짜만 상태에 남긴다.
class SelectedDateNotifier extends Notifier<DateTime> {
  @override
  DateTime build() {
    final now = DateTime.now();
    return DateTime(now.year, now.month, now.day);
  }

  void selectDate(DateTime date) =>
      state = DateTime(date.year, date.month, date.day);
}

/// 달력 탐색과 알림 상세 진입에서 같은 조회 날짜를 유지한다.
final selectedDateProvider = NotifierProvider<SelectedDateNotifier, DateTime>(
  SelectedDateNotifier.new,
);

/// 날짜·카메라 목록·선택 변경에 따라 허용된 카메라 범위의 이벤트를 다시 조회한다.
final eventsProvider = FutureProvider.autoDispose<List<Event>>((ref) async {
  final api = ref.watch(apiClientProvider);
  final selected = ref.watch(selectedCameraProvider);
  final date = ref.watch(selectedDateProvider);
  final cameras = await ref.watch(camerasProvider.future);
  final cameraIds = cameras.map((c) => c['camera_id'].toString()).toSet();
  String? camera = cameraIds.contains(selected) ? selected : null;
  // 잘못되거나 없는 선택은 관리자의 전체 조회 또는 일반 사용자의 첫 카메라로 보정한다.
  if (!api.isAdmin && camera == null) {
    if (cameras.isEmpty) return [];
    camera = cameras.first['camera_id'].toString();
  }
  return ApiEventRepository(
    apiClient: api,
    cameraId: camera,
  ).getEventsByDate(date);
});

/// 이벤트 ID마다 상세 결과를 따로 보관하며 새로고침 시 최신 녹화·분석 정보를 받는다.
final eventDetailProvider = FutureProvider.autoDispose.family<Event, String>((
  ref,
  id,
) async {
  final json = await ref
      .watch(apiClientProvider)
      .request('GET', '/api/v1/events/${Uri.encodeComponent(id)}');
  return Event.fromJson(json);
});
