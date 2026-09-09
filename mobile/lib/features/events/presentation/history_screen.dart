// 날짜와 접근 가능한 카메라를 선택해 이벤트를 살펴보는 화면이다. 실제 조회는 ViewModel이 맡는다.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:app/core/network/providers.dart';
import 'event_card.dart';
import 'event_history_view_model.dart';
import 'history_date_selector.dart';

/// 날짜·카메라 필터와 조회 상태를 묶어 보여주고 수동 재조회 동작을 제공한다.
class HistoryScreen extends ConsumerWidget {
  const HistoryScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final events = ref.watch(eventsProvider);
    final cameras = ref.watch(camerasProvider);
    final selected = ref.watch(selectedCameraProvider);
    final admin = ref.watch(apiClientProvider).isAdmin;
    return SafeArea(
      child: Column(
        children: [
          const HistoryDateSelector(),
          cameras.when(
            loading: () => const LinearProgressIndicator(),
            error: (_, _) => TextButton(
              onPressed: () => ref.invalidate(camerasProvider),
              child: const Text('카메라 목록 다시 불러오기'),
            ),
            data: (rows) {
              final ids = rows.map((c) => c['camera_id'].toString()).toSet();
              // 접근 목록에서 빠진 카메라를 선택값으로 남기지 않도록 조회 로직과 같이 보정한다.
              final effective = ids.contains(selected)
                  ? selected
                  : admin
                  ? null
                  : ids.firstOrNull;
              return Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                child: DropdownButton<String>(
                  isExpanded: true,
                  value: effective ?? '',
                  items: [
                    if (admin || rows.isEmpty)
                      const DropdownMenuItem(value: '', child: Text('전체 카메라')),
                    ...rows.map(
                      (c) => DropdownMenuItem(
                        value: c['camera_id'].toString(),
                        child: Text((c['name'] ?? c['camera_id']).toString()),
                      ),
                    ),
                  ],
                  onChanged: (value) => ref
                      .read(selectedCameraProvider.notifier)
                      .select(value == '' ? null : value),
                ),
              );
            },
          ),
          Row(
            mainAxisAlignment: MainAxisAlignment.end,
            children: [
              TextButton(
                onPressed: () => ref
                    .read(selectedDateProvider.notifier)
                    .selectDate(DateTime.now()),
                child: const Text('오늘'),
              ),
              IconButton(
                tooltip: '새로고침',
                onPressed: () => ref.invalidate(eventsProvider),
                icon: const Icon(Icons.refresh),
              ),
            ],
          ),
          Expanded(
            child: events.when(
              loading: () => const Center(child: CircularProgressIndicator()),
              error: (error, _) => Center(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(error.toString()),
                    TextButton(
                      onPressed: () => ref.invalidate(eventsProvider),
                      child: const Text('다시 시도'),
                    ),
                  ],
                ),
              ),
              data: (history) => RefreshIndicator(
                // 다시 조회한 Future가 끝날 때까지 당겨서 새로고침 표시를 유지한다.
                onRefresh: () async {
                  ref.invalidate(eventsProvider);
                  await ref.read(eventsProvider.future);
                },
                child: history.items.isEmpty
                    ? ListView(
                        physics: const AlwaysScrollableScrollPhysics(),
                        children: const [
                          SizedBox(height: 80),
                          Center(child: Text('이벤트가 없습니다.')),
                        ],
                      )
                    : ListView.builder(
                        physics: const AlwaysScrollableScrollPhysics(),
                        itemCount:
                            history.items.length +
                            (history.nextCursor == null ? 0 : 1),
                        itemBuilder: (_, index) => index < history.items.length
                            ? EventCard(event: history.items[index])
                            : Padding(
                                padding: const EdgeInsets.all(16),
                                child: Column(
                                  children: [
                                    if (history.error != null)
                                      Text(history.error!),
                                    if (history.loadingMore)
                                      const LinearProgressIndicator()
                                    else
                                      OutlinedButton(
                                        onPressed: () => ref
                                            .read(eventsProvider.notifier)
                                            .loadMore(),
                                        child: Text(
                                          history.error == null
                                              ? '이전 이벤트 더 보기'
                                              : '추가 조회 다시 시도',
                                        ),
                                      ),
                                  ],
                                ),
                              ),
                      ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
