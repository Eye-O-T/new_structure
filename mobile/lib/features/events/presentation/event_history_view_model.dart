// 날짜·카메라 선택과 조회 결과를 연결한다. 선택이나 새로고침 알림이 바뀌면 API를 다시 조회한다.
// autoDispose는 더 이상 보지 않는 조회 상태를 해제하며 계정 전환 시 초기화는 main.dart가 맡는다.
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:app/core/network/providers.dart';
import 'package:app/core/network/api_client.dart';
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

class EventHistory {
  const EventHistory({
    required this.items,
    this.nextCursor,
    this.loadingMore = false,
    this.error,
  });
  final List<Event> items;
  final String? nextCursor;
  final bool loadingMore;
  final String? error;
}

/// 첫 50건부터 표시하고 추가 조회 실패 시 이미 받은 목록과 재시도 cursor를 유지한다.
class EventHistoryNotifier extends AsyncNotifier<EventHistory> {
  ApiCancellation? _cancellation;
  ApiEventRepository? _repository;
  DateTime? _date;

  @override
  Future<EventHistory> build() async {
    final api = ref.watch(apiClientProvider);
    final selected = ref.watch(selectedCameraProvider);
    final date = ref.watch(selectedDateProvider);
    _repository = null;
    _date = null;
    final cancellation = ApiCancellation();
    _cancellation?.cancel();
    _cancellation = cancellation;
    ref.onDispose(cancellation.cancel);
    final cameras = await ref.watch(camerasProvider.future);
    cancellation.check();
    final cameraIds = cameras.map((c) => c['camera_id'].toString()).toSet();
    String? camera = cameraIds.contains(selected) ? selected : null;
    if (!api.isAdmin && camera == null) {
      if (cameras.isEmpty) return const EventHistory(items: []);
      camera = cameras.first['camera_id'].toString();
    }
    final repository = ApiEventRepository(apiClient: api, cameraId: camera);
    _repository = repository;
    _date = date;
    final page = await repository.getPage(date, cancellation: cancellation);
    cancellation.check();
    return EventHistory(items: page.items, nextCursor: page.nextCursor);
  }

  Future<void> loadMore() async {
    if (state.isLoading) return;
    final history = state.asData?.value;
    final cancellation = _cancellation;
    final repository = _repository;
    final date = _date;
    if (history == null ||
        history.loadingMore ||
        history.nextCursor == null ||
        cancellation == null ||
        cancellation.isCancelled ||
        repository == null ||
        date == null) {
      return;
    }
    state = AsyncData(
      EventHistory(
        items: history.items,
        nextCursor: history.nextCursor,
        loadingMore: true,
      ),
    );
    try {
      final page = await repository.getPage(
        date,
        cursor: history.nextCursor,
        cancellation: cancellation,
      );
      if (cancellation.isCancelled ||
          !ref.mounted ||
          _cancellation != cancellation) {
        return;
      }
      final seen = history.items.map((event) => event.id).toSet();
      state = AsyncData(
        EventHistory(
          items: [
            ...history.items,
            ...page.items.where((event) => seen.add(event.id)),
          ],
          nextCursor: page.nextCursor,
        ),
      );
    } catch (error) {
      if (cancellation.isCancelled ||
          !ref.mounted ||
          _cancellation != cancellation) {
        return;
      }
      state = AsyncData(
        EventHistory(
          items: history.items,
          nextCursor: history.nextCursor,
          error: error.toString(),
        ),
      );
    }
  }
}

final eventsProvider =
    AsyncNotifierProvider.autoDispose<EventHistoryNotifier, EventHistory>(
      EventHistoryNotifier.new,
    );

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
