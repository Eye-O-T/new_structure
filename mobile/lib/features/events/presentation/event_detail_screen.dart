// 이벤트 기본 정보와 인물 사진 자리, 연결 녹화를 표시한다.
// 사진 조회가 연결되기 전까지 인물 이벤트에는 자리표시자를 사용한다.
import 'package:flutter/material.dart';
import 'dart:typed_data';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:app/core/network/providers.dart';
import 'event_history_view_model.dart';

final eventImageProvider = FutureProvider.autoDispose.family<Uint8List, String>((ref, key) {
  final separator = key.indexOf('|');
  final eventId = key.substring(0, separator);
  final kind = key.substring(separator + 1);
  return ref.read(apiClientProvider).getBytes('/api/v1/events/$eventId/$kind');
});

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
        data: (value) {
          final isPersonEvent = const {
            'person_detected',
            'person_appeared',
            'person_disappeared',
          }.contains(value.eventType);
          final globalPersonId = value.globalPersonId?.trim();
          return ListView(
            padding: const EdgeInsets.fromLTRB(20, 16, 20, 32),
            children: [
              Text(
                value.title,
                style: Theme.of(context).textTheme.headlineSmall,
              ),
              const SizedBox(height: 8),
              Text(
                '카메라 ID: ${value.cameraId}',
                style: Theme.of(context).textTheme.bodyMedium,
              ),
              const SizedBox(height: 24),
              if (isPersonEvent) ...[
                if (value.media['crop'] == true)
                  _EventImage(eventId: value.id, kind: 'crop')
                else if (value.media['snapshot'] == true)
                  _EventImage(eventId: value.id, kind: 'snapshot')
                else
                  const Text('Image is not available.'),
                if (value.media['annotated_snapshot'] == true)
                  TextButton(
                    onPressed: () => showDialog<void>(
                      context: context,
                      builder: (_) => Dialog(
                        child: _EventImage(eventId: value.id, kind: 'annotated-snapshot'),
                      ),
                    ),
                    child: const Text('View annotated snapshot'),
                  ),
                const SizedBox(height: 24),
                _DetailField(
                  label: '글로벌 Person ID',
                  value: globalPersonId == null || globalPersonId.isEmpty
                      ? '미등록'
                      : globalPersonId,
                ),
                const SizedBox(height: 20),
              ],
              _DetailField(
                label: '발생 시각',
                value: value.occurredAt.toLocal().toString().split('.').first,
              ),
              const SizedBox(height: 24),
              const Divider(),
              const SizedBox(height: 16),
              Text('연결 녹화', style: Theme.of(context).textTheme.titleMedium),
              const SizedBox(height: 12),
              if (value.recordingIds.isEmpty)
                const Text('연결된 녹화가 없습니다. 녹화 저장 후 다시 확인할 수 있습니다.'),
              for (final id in value.recordingIds)
                Card(
                  margin: const EdgeInsets.only(bottom: 8),
                  child: ListTile(
                    title: Text('연결 녹화 $id'),
                    leading: const Icon(Icons.play_circle_outline),
                    trailing: const Icon(Icons.chevron_right),
                    onTap: () =>
                        context.push('/recordings/${Uri.encodeComponent(id)}'),
                  ),
                ),
              const SizedBox(height: 16),
              TextButton(
                onPressed: () => ref.invalidate(eventDetailProvider(eventId)),
                child: const Text('이벤트 정보 새로고침'),
              ),
            ],
          );
        },
      ),
    );
  }
}

/// 이후 실제 Crop 이미지로 교체할 사진 영역이다. 네트워크 요청은 하지 않는다.
class _PersonPhotoPlaceholder extends StatelessWidget {
  const _PersonPhotoPlaceholder();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Center(
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 280),
        child: AspectRatio(
          aspectRatio: 4 / 5,
          child: DecoratedBox(
            decoration: BoxDecoration(
              color: theme.colorScheme.surfaceContainerHighest,
              borderRadius: BorderRadius.circular(16),
            ),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                Icon(
                  Icons.person_outline,
                  size: 64,
                  color: theme.colorScheme.onSurfaceVariant,
                ),
                const SizedBox(height: 12),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16),
                  child: Text(
                    '인물 사진 준비 중',
                    textAlign: TextAlign.center,
                    style: theme.textTheme.bodyMedium?.copyWith(
                      color: theme.colorScheme.onSurfaceVariant,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _EventImage extends ConsumerWidget {
  const _EventImage({required this.eventId, required this.kind});
  final String eventId;
  final String kind;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final image = ref.watch(eventImageProvider('$eventId|$kind'));
    return image.when(
      loading: () => const Center(child: CircularProgressIndicator()),
      error: (error, _) => Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(_imageError(error)),
          if (error is! ApiException || error.statusCode == 0)
            TextButton(
              onPressed: () => ref.invalidate(eventImageProvider('$eventId|$kind')),
              child: const Text('Retry'),
            ),
        ],
      ),
      data: (bytes) => Image.memory(bytes, fit: BoxFit.contain,
          errorBuilder: (_, __, ___) => const Text('Image format is invalid.')),
    );
  }

  String _imageError(Object error) {
    if (error is ApiException) {
      if (error.statusCode == 403) return 'You do not have permission to view this image.';
      if (error.statusCode == 404) return 'Image is not available.';
      if (error.statusCode == 0) return 'Could not load image. Retry.';
    }
    return 'Could not load image. Retry.';
  }
}

class _DetailField extends StatelessWidget {
  const _DetailField({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Text(label, style: Theme.of(context).textTheme.labelLarge),
      const SizedBox(height: 6),
      SelectableText(value, style: Theme.of(context).textTheme.bodyLarge),
    ],
  );
}
