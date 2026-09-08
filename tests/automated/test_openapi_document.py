"""모바일 교체 명세가 실행 API와 어긋나거나 내부 API를 노출하지 않는지 확인한다."""

from copy import deepcopy
from pathlib import Path

from fastapi.openapi.models import OpenAPI

from server.scripts.export_openapi import build_document, render_document
from server.services.external.app.config import Settings
from server.services.external.app.main import create_app


ROOT = Path(__file__).resolve().parents[2]


def test_openapi_is_current_without_deployment_settings(monkeypatch):
    def no_environment(*args, **kwargs):
        raise AssertionError("문서 생성은 운영 설정을 읽으면 안 된다")

    monkeypatch.setattr(Settings, "from_env", no_environment)
    assert (ROOT / "docs/openapi.yaml").read_text(encoding="utf-8") == render_document()


def test_openapi_keeps_public_requests_and_declared_responses():
    original = create_app().openapi()
    document = build_document()
    expected_paths = {path for path in original["paths"] if path.startswith("/api/v1/")}
    assert set(document["paths"]) == expected_paths
    # Any/dict 응답 세 가지를 보완한 부분 외에는 서버 선언을 그대로 유지해야 한다.
    supplemented = {
        ("/api/v1/cameras/{camera_id}/objects", "get"),
        ("/api/v1/notifications/status", "get"),
        ("/api/v1/notifications/devices", "put"),
    }
    for path in expected_paths:
        assert document["paths"][path].keys() == original["paths"][path].keys()
        for method, declared in original["paths"][path].items():
            documented = document["paths"][path][method]
            assert documented.get("requestBody") == declared.get("requestBody")
            if (path, method) not in supplemented:
                for status, response in declared["responses"].items():
                    assert documented["responses"][status].get(
                        "content"
                    ) == response.get("content")
    for name, declared in original["components"]["schemas"].items():
        documented = deepcopy(document["components"]["schemas"][name])
        # 설명을 덧붙여도 필드의 형식·필수값·길이 제한은 바뀌면 안 된다.
        for prop in documented.get("properties", {}).values():
            prop.pop("description", None)
        expected = deepcopy(declared)
        for prop in expected.get("properties", {}).values():
            prop.pop("description", None)
        assert documented == expected


def test_openapi_references_and_alternative_authentication_are_valid():
    document = build_document()
    OpenAPI.model_validate(document)

    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value:
                reference = value["$ref"]
                assert reference.startswith("#/"), reference
                target = document
                for part in reference[2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)
    for path, methods in document["paths"].items():
        for operation in methods.values():
            if path == "/api/v1/auth/logout":
                expected = [{"HTTPBearer": []}, {"AccessCookie": []}, {}]
            elif path.startswith("/api/v1/auth/"):
                expected = []
            else:
                expected = [{"HTTPBearer": []}, {"AccessCookie": []}]
            assert operation["security"] == expected
