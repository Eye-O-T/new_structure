# 모델 준비와 배포 기록

기본 재식별기는 OSNet x0.25의 **MSMT17 combineall 재식별 학습 가중치**를 사용한다.
ImageNet 사전학습 가중치만 사용하는 구성이 아니다. 서비스는 OpenCV CPU로 ONNX를
읽으며 인터넷에서 모델을 받지 않는다. 아래 준비 도구만 다운로드와 PyTorch를 사용한다.

## 준비 및 설치

저장소 루트에서 Python 3.11 가상환경을 만들고 실행한다. Windows PowerShell 예시:

```powershell
py -3.11 -m venv .venv-osnet
.venv-osnet/Scripts/python.exe -m pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/cpu
.venv-osnet/Scripts/python.exe -m pip install -r server/tools/requirements-osnet.txt
.venv-osnet/Scripts/python.exe server/tools/prepare_osnet.py
```

Linux에서는 `python3.11 -m venv .venv-osnet`으로 만들고 Python 경로를
`.venv-osnet/bin/python`으로 바꾼다. 변환 환경의 torch/onnx를 설치 도우미나 Data 서비스에
설치할 필요는 없다.

생성 파일:

- `server/runtime/models/osnet_x0_25_msmt17.onnx`: 운영 추론 파일.
- 같은 이름의 `.onnx.json`: 소스·가중치·ONNX 해시, 변환 버전, 수치 비교 결과.
- `osnet_x0_25_msmt17.LICENSE.txt`: 공식 소스의 MIT 라이선스.

설치 도우미의 인물 식별 모델 선택 또는 CLI의 `--identity-model`에 ONNX 파일을 지정한다.
기본 파일을 사용하면 설치 도우미가 자동으로 찾는다. 기존 설치의 모델 폴더에 직접 준비할 때:

```powershell
.venv-osnet/Scripts/python.exe server/tools/prepare_osnet.py --output D:/AI_CCTV/models/osnet_x0_25_msmt17.onnx
```

기존 파일은 기본적으로 덮어쓰지 않는다. 교체가 필요하면 `--force`를 지정한다. 새 파일의
검증이 끝난 뒤 교체하므로 다운로드·추론 검증 실패로 기존 ONNX를 지우지 않는다.
모델·manifest·라이선스를 모두 임시 파일로 준비하고 기존 세 파일을 백업한 뒤 교체한다.
일반 파일 오류가 나면 이미 바뀐 파일만 복원한다. 복원에도 실패하면 `.osnet-backup-*`
폴더를 삭제하지 않고 오류에 경로를 표시하므로, 그 원본으로 복구한 뒤 다시 실행한다.
세 파일 전체의 전원 장애·동시 실행 원자성을 보장하는 절차는 아니다.
모델 파일은 Git에 포함하지 않으며 다른 서버에도 준비 도구 실행 또는 파일 복사가 필요하다.

네트워크가 없는 곳에서는 온라인 환경에서 생성한 세 파일을 옮기는 방법을 권장한다.
변환을 다시 해야 한다면 `server/runtime/osnet-cache`를 복사하고
`--cache-dir <캐시폴더> --offline`으로 실행한다. 캐시에 있는 파일도 고정 SHA-256을 검사한다.

## 모델 계약과 검증 범위

- 공식 소스 커밋 `f8cd150fdf77e8d9e1ed143b7f308c2c609ded50`을 고정한다.
- 공식 가중치 리비전 `a5c5cc037c24235cda3b21085b93ad77c9616224`를 고정한다.
- 가중치 SHA-256은 `cf55163d78fc44c62c82f85ab62d39f10438679b5abe8c698ae08cfa84aa6e18`이다.
- 가중치는 `weights_only=True`, 모든 층의 `strict=True`로 읽는다. 누락된 층을 무작위 값으로
  남기거나 분류기 출력으로 변환하지 않는다.
- ONNX opset 12, float32 `1×3×256×128` 입력과 단일 `1×512` 출력이다.
- 서비스에서 BGR crop을 RGB로 바꾸고 128×256으로 보간한다. 255로 나눈 뒤
  ImageNet mean `(0.485, 0.456, 0.406)`과 std `(0.229, 0.224, 0.225)`로 정규화한다.
- 출력은 서비스에서 L2 정규화하며 공간명에 ONNX SHA-256과 전처리 버전을 포함한다.
- 준비 도구는 ONNX 그래프를 검사하고 3개 합성 입력의 PyTorch/OpenCV 결과를 비교한다.
  원 출력 허용 오차는 `rtol=1e-3, atol=1e-3`, 단위 벡터는 `rtol=1e-3, atol=1e-4`이다.
  이 검사는 변환·추론 연결 검증이며 CCTV에서 동일인을 구분하는 정확도 측정이 아니다.

현재 로컬 검증 환경(Python 3.11, torch 2.8.0 CPU, ONNX 1.17.0, OpenCV 4.11.0)에서
실제 공식 가중치의 변환 및 위 수치 비교가 통과했다. 다른 OS/CPU에서 실행할 때도 준비
도구가 같은 검사를 수행한다. 생성된 manifest의 `validation`에 해당 실행 결과가 남는다.

동일인 연결은 Data 서비스의 `IDENTITY_MATCH_THRESHOLD`와 `IDENTITY_MATCH_MARGIN`으로
조정한다. 기본값 0.97/0.05는 기존의 보수적 값을 유지한 것이며 OSNet에 교정된 값이 아니다.
서로 다른 카메라의 동일인 앞·뒤 모습과 비슷한 옷을 입은 다른 사람의 crop을 모아,
오연결과 연결 누락을 함께 측정한 뒤 값을 정해야 한다. 임계값을 낮추면 연결 수와 오연결이
함께 증가할 수 있다. 기존 한 장 관측 방식과 확정 ID 유지 정책도 그대로 적용된다.

공식 출처: [OSNet 가중치](https://huggingface.co/kaiyangzhou/osnet),
[모델 비교표](https://kaiyangzhou.github.io/deep-person-reid/MODEL_ZOO),
[소스 및 라이선스](https://github.com/KaiyangZhou/deep-person-reid).

## 배포 릴리스 기록

`export_release_manifest.py`는 Python 표준 라이브러리와 Docker CLI로 실제 배포를 읽어
이미지 ID·registry digest·운영 Python 패키지 버전·지정 모델의 SHA-256을 JSON에 기록한다.
탐지 모델은 설치 도구와 동일하게 `.pt`·`.onnx`·`.engine`을 지원하며 확장자의 대소문자를
구분하지 않는다. 기록은 파일명·바이트 수·해시를 담으며 모델 추론을 실행하지 않는다.
소스 저장소 루트에서 실행하며 출력 폴더는 먼저 준비한다. 기존 기록을 덮어쓰지 않는다.

```powershell
python server/tools/export_release_manifest.py --env-file C:/path/to/compose.env --model C:/path/to/models/default.pt --model C:/path/to/models/osnet_x0_25_msmt17.onnx --output C:/path/to/releases/release-2026-09-09.json
```

실행 중인 Data·External·Preprocessing의 `pip list`를 수집한다. Analysis에서는 명령을
실행하지 않으며 Docker의 이미지 식별 정보만 기록한다. 미기동 서비스는
`missing_services`에 남고, 정지된 컨테이너는 Python 패키지 목록을 수집하지 못한다.
로컬 빌드 이미지에는 registry digest가 없을 수 있어 이미지 ID도 함께 기록한다.
모델은 현재 배포에 실제 마운트한 파일을 지정한다. env 파일 내용·인증키·전체 컨테이너
환경변수를 결과에 넣지 않는다. 이 기록은 의존성 설치용 lock이나 서명·실환경 인수의
대체물이 아니며 [배포 인수 기록](../../docs/operations.md#버전별-인수복원-기록)과 함께 보관한다.
