// 날짜와 접근 가능한 카메라를 선택해 이벤트를 살펴보는 화면이다. 실제 조회는 ViewModel이 맡는다.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:app/core/network/providers.dart';
import 'event_card.dart';
import 'event_history_view_model.dart';
import 'history_date_selector.dart';

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
              data: (rows) => RefreshIndicator(
                onRefresh: () async {
                  ref.invalidate(eventsProvider);
                  await ref.read(eventsProvider.future);
                },
                child: rows.isEmpty
                    ? ListView(
                        physics: const AlwaysScrollableScrollPhysics(),
                        children: const [
                          SizedBox(height: 80),
                          Center(child: Text('이벤트가 없습니다.')),
                        ],
                      )
                    : ListView.builder(
                        physics: const AlwaysScrollableScrollPhysics(),
                        itemCount: rows.length,
                        itemBuilder: (_, index) =>
                            EventCard(event: rows[index]),
                      ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
