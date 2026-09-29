// 알림 수신 범위와 로그인 세션을 관리하는 화면이다. 저장·기기 등록은 알림/API 관리자에 맡긴다.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:app/core/network/providers.dart';
import 'package:app/core/notifications/providers.dart';

/// 현재 계정과 서버, 알림 수신 설정 및 기기 등록 상태를 표시한다.
class SettingsScreen extends ConsumerStatefulWidget {
  const SettingsScreen({super.key});
  @override
  ConsumerState<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends ConsumerState<SettingsScreen> {
  bool busy = false;
  Future<Map<String, dynamic>>? featureStatus;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    featureStatus ??= ref.read(apiClientProvider).request(
      'GET',
      '/api/v1/features/status',
    );
  }

  /// 설정 변경·재등록·로그아웃에 공통 대기 표시와 화면이 남아 있을 때의 오류 안내를 적용한다.
  Future<void> perform(Future<void> Function() action) async {
    setState(() => busy = true);
    try {
      await action();
    } catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(error.toString())));
      }
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final api = ref.watch(apiClientProvider);
    final notifications = ref.watch(notificationControllerProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('설정')),
      // Provider는 같은 관리자 객체를 반환하므로 내부 상태 변경은 ChangeNotifier로 구독한다.
      body: ListenableBuilder(
        listenable: notifications,
        builder: (_, _) => ListView(
          children: [
            ListTile(
              title: Text(api.user?['username']?.toString() ?? ''),
              subtitle: Text(api.isAdmin ? '관리자' : '조회 사용자'),
            ),
            ListTile(
              title: const Text('중앙 서버'),
              subtitle: Text(api.origin.toString()),
            ),
            FutureBuilder<Map<String, dynamic>>(
              future: featureStatus,
              builder: (context, snapshot) {
                final values = snapshot.data;
                String label(String key, String title) {
                  final state = values?[key] is Map
                      ? (values![key] as Map)['status']
                      : null;
                  final text = switch (state) {
                    'enabled' => '켜짐',
                    'disabled' => '꺼짐',
                    _ => '확인 불가',
                  };
                  return '$title: $text';
                }

                return ListTile(
                  title: const Text('서버 AI 기능 상태'),
                  subtitle: Text(snapshot.hasError
                      ? '상태를 불러오지 못했습니다.'
                      : snapshot.connectionState != ConnectionState.done
                          ? '조회 중…'
                          : '${label('preprocessing', '사람 감지·식별')}\n${label('analysis', '이벤트 분석')}'),
                  trailing: IconButton(
                    tooltip: '상태 새로고침',
                    onPressed: () => setState(() {
                      featureStatus = api.request('GET', '/api/v1/features/status');
                    }),
                    icon: const Icon(Icons.refresh),
                  ),
                );
              },
            ),
            SwitchListTile(
              title: const Text('푸시 알림'),
              value: notifications.enabled,
              onChanged: busy
                  ? null
                  : (value) => perform(
                      () => notifications.setPreferences(
                        receive: value,
                        everyEvent: notifications.allEvents,
                      ),
                    ),
            ),
            SwitchListTile(
              title: const Text('모든 이벤트 알림'),
              subtitle: const Text('끄면 사람 감지·장애·위험 상태만 알립니다.'),
              value: notifications.allEvents,
              onChanged: busy
                  ? null
                  : (value) => perform(
                      () => notifications.setPreferences(
                        receive: notifications.enabled,
                        everyEvent: value,
                      ),
                    ),
            ),
            ListTile(
              title: const Text('알림 연결 상태'),
              subtitle: Text(notifications.status),
              trailing: IconButton(
                tooltip: '알림 연결 다시 시도',
                onPressed: busy
                    ? null
                    : () => perform(
                        notifications.ready
                            ? notifications.sync
                            : notifications.start,
                      ),
                icon: const Icon(Icons.refresh),
              ),
            ),
            const Divider(),
            Padding(
              padding: const EdgeInsets.all(16),
              child: OutlinedButton(
                onPressed: busy ? null : () => perform(api.logout),
                child: const Text('로그아웃'),
              ),
            ),
            if (busy) const LinearProgressIndicator(),
          ],
        ),
      ),
    );
  }
}
