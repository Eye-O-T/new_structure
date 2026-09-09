// 앱의 시작점: 공용 API·라우터·알림 관리자를 연결한 뒤 저장된 로그인 정보를 복원한다.
import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:app/core/network/providers.dart';
import 'package:app/core/notifications/notification_controller.dart';
import 'package:app/core/notifications/providers.dart';
import 'package:app/core/router/app_router.dart';
import 'package:app/features/events/presentation/event_history_view_model.dart';
import 'package:app/features/live/presentation/camera_screen.dart';

/// 앱 전체가 공유하는 의존성을 만들고 세션 복원과 알림 초기화를 병렬로 시작한다.
void main() {
  WidgetsFlutterBinding.ensureInitialized();
  // 알림 콜백에서 라우터를 사용하므로 컨테이너를 먼저 만들고 관리자를 나중에 연결한다.
  late final NotificationController notifications;
  final container = ProviderContainer(
    overrides: [
      notificationControllerProvider.overrideWith((ref) => notifications),
    ],
  );
  final api = container.read(apiClientProvider);
  final router = container.read(appRouterProvider);
  // 앱 복귀나 계정 변경 때 서버에서 달라졌을 수 있는 조회 결과를 모두 무효화한다.
  void refresh() {
    container.invalidate(camerasProvider);
    container.invalidate(eventsProvider);
    container.invalidate(eventDetailProvider);
    container.invalidate(cameraStatusProvider);
    container.invalidate(videoProfileProvider);
  }

  notifications = NotificationController(
    api,
    onResume: refresh,
    onEvent: (payload, open) {
      // 알림에는 이동에 필요한 ID만 있으므로 상세 내용은 로그인된 API로 다시 조회한다.
      // 탭으로 열 때는 해당 카메라와 발생일을 선택해 히스토리 화면의 조건도 일치시킨다.
      if (open) {
        container
            .read(selectedDateProvider.notifier)
            .selectDate(payload.occurredAt.toLocal());
        container
            .read(selectedCameraProvider.notifier)
            .select(payload.cameraId);
        container.invalidate(eventDetailProvider(payload.eventId));
        router.go('/events/${Uri.encodeComponent(payload.eventId)}');
      } else if (payload.matchesLocalDate(
        container.read(selectedDateProvider),
      )) {
        container.invalidate(eventsProvider);
      }
      container.invalidate(cameraStatusProvider(payload.cameraId));
    },
  );
  api.addListener(() {
    refresh();
    if (!api.signedIn) {
      container.invalidate(selectedCameraProvider);
      container.invalidate(selectedDateProvider);
    }
  });
  // 화면을 먼저 띄우고 초기화를 진행한다. 라우터는 세션 복원 중 로딩 화면을 보여준다.
  runApp(UncontrolledProviderScope(container: container, child: const MyApp()));
  unawaited(api.restore());
  unawaited(notifications.start());
}

/// 공용 테마와 인증 상태를 따르는 라우터를 Flutter 앱에 연결한다.
class MyApp extends ConsumerWidget {
  const MyApp({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) => MaterialApp.router(
    title: 'AI CCTV',
    debugShowCheckedModeBanner: false,
    theme: ThemeData(
      colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFF254D71)),
      useMaterial3: true,
    ),
    routerConfig: ref.watch(appRouterProvider),
  );
}
