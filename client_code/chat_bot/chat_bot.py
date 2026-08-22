# chat_bot.py
#
# 역할:
# - VLM 분석 결과를 즉시 Discord로 보내지 않고 내부 작업 큐에 먼저 등록합니다.
# - 별도 daemon thread가 큐에서 메시지를 하나씩 꺼내 discord_bot.py에 전송을 위임합니다.
# - VLMWorker 쪽에서는 chatbot.send_msg(result) 한 줄만 호출하면 됩니다.

from __future__ import annotations

import atexit
import queue
import threading
from typing import Any

# 같은 chat_bot 디렉터리 안의 discord_bot.py를 import합니다.
# "from chat_bot import chat_bot as chatbot" 형태로 이 모듈을 불러올 예정이므로,
# 패키지 기준 import를 사용합니다.
from chat_bot import discord_bot


# Discord로 보낼 메시지를 저장하는 FIFO 큐입니다.
# Queue는 thread-safe이므로 VLMWorker thread와 알림 worker thread 사이에서 안전하게 사용할 수 있습니다.
_message_queue: queue.Queue[object] = queue.Queue()

# worker thread 종료를 알리기 위한 sentinel 객체입니다.
# 일반 문자열 메시지와 구분하기 위해 object() 하나를 전용 종료 신호로 사용합니다.
_STOP_SIGNAL = object()

# worker thread가 중복 생성되지 않도록 보호하는 lock입니다.
_worker_lock = threading.Lock()

# 실제 큐 소비를 담당하는 worker thread입니다.
# 최초 send_msg() 호출 시 lazy-start 방식으로 생성됩니다.
_worker_thread: threading.Thread | None = None


def send_msg(message: Any) -> None:
    """VLM 분석 결과를 Discord 알림 큐에 등록합니다.

    이 함수는 VLMWorker에서 호출되는 공개 함수입니다.
    Discord 전송은 별도 worker thread에서 처리되므로,
    이 함수는 네트워크 I/O를 기다리지 않고 빠르게 반환합니다.

    Args:
        message: Discord로 보낼 내용입니다. str이 아니어도 str(message)로 변환합니다.
    """
    # None 또는 빈 문자열도 예외로 처리하지 않고,
    # 운영 로그로 확인 가능한 메시지로 변환합니다.
    text = _normalize_message(message)

    # worker thread가 아직 없으면 시작합니다.
    # send_msg()를 여러 thread에서 동시에 호출해도 _worker_lock 덕분에 worker는 하나만 생성됩니다.
    _ensure_worker_started()

    # 메시지를 큐 뒤에 추가합니다.
    # worker thread는 이 큐를 FIFO 순서로 소비하므로 메시지는 등록 순서대로 전송됩니다.
    _message_queue.put(text)


def stop(wait: bool = True) -> None:
    """알림 worker thread를 종료합니다.

    일반적으로 직접 호출하지 않아도 됩니다.
    atexit에 등록되어 프로세스 종료 시 자동으로 호출됩니다.

    Args:
        wait: True이면 worker thread가 종료될 때까지 최대 10초 기다립니다.
    """
    global _worker_thread

    # 현재 worker thread 참조를 lock 안에서 안전하게 가져옵니다.
    with _worker_lock:
        thread = _worker_thread

        # 아직 worker가 시작되지 않았거나 이미 종료된 경우에는 할 일이 없습니다.
        if thread is None or not thread.is_alive():
            _worker_thread = None
            return

        # worker loop가 종료될 수 있도록 sentinel을 큐에 넣습니다.
        # 이미 큐에 쌓인 메시지를 모두 처리한 뒤 이 신호를 만나 종료됩니다.
        _message_queue.put(_STOP_SIGNAL)

    # join은 lock 밖에서 수행합니다.
    # lock을 잡고 join하면 worker 쪽 정리 코드와 교착될 가능성이 생깁니다.
    if wait:
        thread.join(timeout=10)

    # thread가 정상 종료되었으면 참조를 비워 다음 실행 때 새로 시작할 수 있게 합니다.
    with _worker_lock:
        if _worker_thread is thread and not thread.is_alive():
            _worker_thread = None


def _ensure_worker_started() -> None:
    """알림 worker thread를 lazy-start 방식으로 시작합니다."""
    global _worker_thread

    with _worker_lock:
        # 이미 살아 있는 worker가 있으면 새로 만들지 않습니다.
        if _worker_thread is not None and _worker_thread.is_alive():
            return

        # daemon=True이므로 프로그램 종료를 막지는 않습니다.
        # 단, 정상 종료 시에는 atexit의 stop()이 큐 처리 후 종료를 시도합니다.
        _worker_thread = threading.Thread(
            target=_worker_loop,
            name="ChatBotNotificationWorker",
            daemon=True,
        )
        _worker_thread.start()


def _worker_loop() -> None:
    """큐에서 메시지를 하나씩 꺼내 Discord 전송 함수로 넘깁니다."""
    try:
        while True:
            # get()은 새 메시지가 들어올 때까지 대기합니다.
            item = _message_queue.get()

            try:
                # stop()에서 넣은 종료 신호를 받으면 loop를 종료합니다.
                if item is _STOP_SIGNAL:
                    return

                # 큐에는 문자열만 들어오도록 send_msg()에서 정규화하지만,
                # 방어적으로 str()을 한 번 더 적용합니다.
                text = str(item)

                # 실제 Discord API 호출은 discord_bot.py에 위임합니다.
                # 이 호출은 네트워크 I/O가 끝날 때까지 worker thread 안에서만 대기합니다.
                discord_bot.send_message(text)

            except Exception as exc:
                # 알림 실패가 VLM 분석 thread를 죽이지 않도록 여기서 예외를 흡수합니다.
                # 운영 중에는 이 print를 logging 모듈로 교체해도 됩니다.
                print(f"Discord 알림 전송 실패: {exc}")

            finally:
                # get()으로 꺼낸 작업 하나가 끝났음을 Queue에 알려줍니다.
                # _STOP_SIGNAL도 get()으로 꺼낸 항목이므로 task_done() 대상입니다.
                _message_queue.task_done()

    finally:
        # worker가 종료될 때 Discord client도 닫습니다.
        # 네트워크 연결과 event loop thread를 정리하기 위한 호출입니다.
        discord_bot.close()


def _normalize_message(message: Any) -> str:
    """VLM 결과를 Discord에 보낼 수 있는 문자열로 변환합니다."""
    if message is None:
        return "VLM 분석 결과가 없습니다."

    text = str(message).strip()

    if not text:
        return "VLM 분석 결과가 비어 있습니다."

    return text


# Python 프로세스가 종료될 때 worker thread와 Discord client를 정리합니다.
# GUI 종료, Ctrl+C, 정상 프로세스 종료 등 대부분의 정상 종료 경로에서 호출됩니다.
atexit.register(stop)