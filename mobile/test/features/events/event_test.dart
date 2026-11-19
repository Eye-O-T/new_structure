import 'package:app/features/events/domain/event.dart';
import 'package:flutter_test/flutter_test.dart';

Event eventWithMedia(Map<String, bool> media) => Event(
      id: 'event-1',
      cameraId: 'cam-1',
      eventType: 'person_detected',
      occurredAt: DateTime.utc(2026, 1, 1),
      media: media,
    );

void main() {
  test('prefers person crop over snapshot', () {
    expect(preferredEventImageKind(eventWithMedia({'crop': true, 'snapshot': true})), 'crop');
  });

  test('falls back to snapshot when no crop exists', () {
    expect(preferredEventImageKind(eventWithMedia({'crop': false, 'snapshot': true})), 'snapshot');
  });

  test('does not request an image when media is unavailable', () {
    expect(preferredEventImageKind(eventWithMedia({'crop': false, 'snapshot': false})), isNull);
  });

  test('preserves annotated media availability from the public event payload', () {
    final event = Event.fromJson({
      'id': 'event-1',
      'camera_id': 'cam-1',
      'event_type': 'person_detected',
      'occurred_at': '2026-01-01T00:00:00Z',
      'media': {'annotated_snapshot': true},
    });
    expect(event.media['annotated_snapshot'], isTrue);
  });

  test('formats skipped analysis with current PID and GPID', () {
    final event = Event.fromJson({
      'id': 'event-1',
      'camera_id': 'cam-1',
      'event_type': 'person_appeared',
      'occurred_at': '2026-01-01T00:00:00Z',
      'person_id': '12',
      'global_person_id': 307,
      'metadata': {
        'analysis': {
          'status': 'skipped',
          'result': {'reason_code': 'NOT_FULL_BODY'},
        },
      },
    });
    expect(event.displayTitle, 'pid:12/gpid:307 is not full body');
    expect(event.isNotFullBody, isTrue);
  });

  test('does not label historical events without skipped metadata', () {
    final event = Event.fromJson({
      'id': 'event-1',
      'camera_id': 'cam-1',
      'event_type': 'person_appeared',
      'occurred_at': '2026-01-01T00:00:00Z',
    });
    expect(event.displayTitle, event.title);
  });
}
