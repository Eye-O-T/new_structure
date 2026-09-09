// 서버 주소·계정을 입력받아 공용 API 세션을 만든다. 성공 후 화면 이동은 라우터가 처리한다.
// 비밀번호 입력값은 성공 시 비우며 로그인 유지에는 비밀번호 대신 토큰을 사용한다.
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:app/core/config/api_config.dart';
import 'package:app/core/network/providers.dart';

/// 서버와 계정 입력을 받아 공용 세션의 로그인을 시작하는 화면이다.
class LoginScreen extends ConsumerStatefulWidget {
  const LoginScreen({super.key});
  @override
  ConsumerState<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends ConsumerState<LoginScreen> {
  final server = TextEditingController(text: ApiConfig.initialOrigin);
  final username = TextEditingController();
  final password = TextEditingController();
  bool busy = false;
  String? error;

  @override
  void initState() {
    super.initState();
    final previous = ref.read(apiClientProvider).origin;
    if (previous != null) server.text = previous.toString();
  }

  /// 중복 제출을 막고 비동기 로그인 결과를 화면이 남아 있을 때만 오류 상태로 반영한다.
  Future<void> submit() async {
    if (busy) return;
    setState(() {
      busy = true;
      error = null;
    });
    try {
      await ref
          .read(apiClientProvider)
          .login(server.text, username.text, password.text);
      password.clear();
    } catch (failure) {
      if (mounted) {
        setState(
          () => error = failure is FormatException
              ? failure.message
              : failure.toString(),
        );
      }
    } finally {
      // 성공한 로그인은 라우터가 이 화면을 제거할 수 있어 완료 시 mounted를 확인한다.
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('AI CCTV 로그인')),
    body: Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 440),
          child: AutofillGroup(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                TextField(
                  controller: server,
                  enabled: !busy,
                  keyboardType: TextInputType.url,
                  autocorrect: false,
                  decoration: const InputDecoration(
                    labelText: '중앙 서버 주소',
                    hintText: 'https://cctv.example.com',
                  ),
                ),
                const SizedBox(height: 16),
                TextField(
                  controller: username,
                  enabled: !busy,
                  autofillHints: const [AutofillHints.username],
                  autocorrect: false,
                  decoration: const InputDecoration(labelText: '계정'),
                ),
                const SizedBox(height: 16),
                TextField(
                  controller: password,
                  enabled: !busy,
                  obscureText: true,
                  autofillHints: const [AutofillHints.password],
                  decoration: const InputDecoration(labelText: '비밀번호'),
                  onSubmitted: (_) => submit(),
                ),
                const SizedBox(height: 24),
                if (error != null)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 16),
                    child: Text(error!),
                  ),
                FilledButton(
                  onPressed: busy ? null : submit,
                  child: Text(busy ? '로그인 중…' : '로그인'),
                ),
              ],
            ),
          ),
        ),
      ),
    ),
  );

  /// 화면이 소유한 입력 컨트롤러를 함께 해제한다.
  @override
  void dispose() {
    server.dispose();
    username.dispose();
    password.dispose();
    super.dispose();
  }
}
