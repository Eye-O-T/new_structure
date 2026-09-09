// 이벤트 ID로 상세 정보를 다시 조회하고 이후 연결된 녹화·재식별·분석 상태를 보여준다.
// unconfigured는 담당 모델이 아직 연결되지 않았다는 뜻이며 분석 완료로 표시하지 않는다.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'event_history_view_model.dart';

/// 이벤트 ID별 조회 상태를 표시하고 연결 녹화의 인증된 재생 화면으로 이동한다.
class EventDetailScreen extends ConsumerWidget {
  const EventDetailScreen({super.key, required this.eventId});
  final String eventId;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final event = ref.watch(eventDetailProvider(eventId));
    return Scaffold(
      appBar: AppBar(
        title: const Text('이벤트 상세'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () {
            // 알림으로 직접 진입한 경우 이전 화면이 없을 수 있어 히스토리로 돌아간다.
            if (context.canPop()) {
              context.pop();
            } else {
              context.go('/history');
            }
          },
        ),
      ),
      body: event.when(
        loading: () => const Center(child: CircularProgressIndicator()),
        error: (error, _) => Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(error.toString()),
              TextButton(
                onPressed: () => ref.invalidate(eventDetailProvider(eventId)),
                child: const Text('다시 시도'),
              ),
            ],
          ),
        ),
        data: (value) => ListView(
          padding: const EdgeInsets.all(16),
          children: [
            Text(value.title, style: Theme.of(context).textTheme.headlineSmall),
            Text('카메라: ${value.cameraId}'),
            if (value.personId != null) Text('카메라 내 인물 ID: ${value.personId}'),
            if (value.globalPersonId != null)
              Text('통합 인물 ID: ${value.globalPersonId}'),
            if (value.metadata['identity'] is Map)
              Text('인물 연결: ${value.metadata['identity']['status']}'),
            if (value.metadata['analysis'] is Map) ...[
              Text('객체 분석: ${value.metadata['analysis']['status']}'),
              if (value.metadata['analysis']['result'] != null)
                SelectableText('${value.metadata['analysis']['result']}'),
            ],
            Text(
              '발생: ${value.occurredAt.toLocal().toString().split('.').first}',
            ),
            if (value.confidence != null)
              Text('신뢰도: ${(value.confidence! * 100).toStringAsFixed(1)}%'),
            const SizedBox(height: 16),
            if (value.recordingIds.isEmpty)
              const Text('연결된 녹화가 없습니다. 녹화 저장 후 다시 확인할 수 있습니다.'),
            for (final id in value.recordingIds)
              ListTile(
                title: Text('연결 녹화 $id'),
                leading: const Icon(Icons.play_circle_outline),
                onTap: () =>
                    context.push('/recordings/${Uri.encodeComponent(id)}'),
              ),
            TextButton(
              onPressed: () => ref.invalidate(eventDetailProvider(eventId)),
              child: const Text('이벤트 정보 새로고침'),
            ),
          ],
        ),
      ),
    );
  }
}
