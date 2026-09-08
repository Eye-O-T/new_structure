// 로그인 상태에 따라 접근할 화면을 정한다. 최종 데이터 접근 권한은 서버에서도 검사한다.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:app/core/network/providers.dart';
import 'package:app/features/auth/presentation/login_screen.dart';
import 'package:app/features/events/presentation/history_screen.dart';
import 'package:app/features/events/presentation/event_detail_screen.dart';
import 'package:app/features/live/presentation/live_screen.dart';
import 'package:app/features/live/presentation/camera_screen.dart';
import 'package:app/features/live/presentation/protected_video.dart';
import 'package:app/features/settings/presentation/settings_screen.dart';
import 'home_page.dart';

final appRouterProvider = Provider<GoRouter>((ref) {
  final api = ref.watch(apiClientProvider);
  final router = GoRouter(
    initialLocation: '/live',
    refreshListenable: api,
    redirect: (context, state) {
      final path = state.uri.path;
      // 저장된 세션 복원이 끝나기 전에 로그아웃으로 오인해 로그인 화면으로 이동하지 않는다.
      if (api.restoring) return path == '/loading' ? null : '/loading';
      if (!api.signedIn) return path == '/login' ? null : '/login';
      if (path == '/login' || path == '/loading') return '/live';
      return null;
    },
    routes: [
      GoRoute(
        path: '/loading',
        builder: (_, _) =>
            const Scaffold(body: Center(child: CircularProgressIndicator())),
      ),
      GoRoute(path: '/login', builder: (_, _) => const LoginScreen()),
      ShellRoute(
        builder: (_, _, child) => HomePage(child: child),
        routes: [
          GoRoute(path: '/live', builder: (_, _) => const LiveScreen()),
          GoRoute(path: '/history', builder: (_, _) => const HistoryScreen()),
          GoRoute(path: '/settings', builder: (_, _) => const SettingsScreen()),
        ],
      ),
      GoRoute(
        path: '/events/:id',
        builder: (_, state) =>
            EventDetailScreen(eventId: state.pathParameters['id']!),
      ),
      GoRoute(
        path: '/cameras/:id',
        builder: (_, state) =>
            CameraScreen(cameraId: state.pathParameters['id']!),
      ),
      GoRoute(
        path: '/recordings/:id',
        builder: (_, state) => Scaffold(
          appBar: AppBar(title: const Text('녹화 재생')),
          body: ProtectedVideo(
            endpoint:
                '/api/v1/recordings/${Uri.encodeComponent(state.pathParameters['id']!)}/playback',
            urlField: 'playback_url',
          ),
        ),
      ),
    ],
  );
  ref.onDispose(router.dispose);
  return router;
});
