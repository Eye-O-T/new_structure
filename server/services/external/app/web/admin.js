// 관리자 UI: 탭 메모리의 세션으로 공개 API를 호출하고 일회성 송출 자격 증명 파일을 전달한다.
"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const pageSize = 25;
  let session = null;
  let busy = false;
  let offset = 0;
  let hasNext = false;
  let selectedCamera = null;
  let profileAvailable = false;
  let pendingCredential = null;
  let refreshTask = null;

  // 서버 데이터는 HTML로 해석하지 않고 상태 영역의 텍스트로 표시한다.
  function message(text, error = false) {
    byId("message").textContent = text;
    byId("message").classList.toggle("error", error);
  }

  // 공통 실행 잠금 외에 페이지·지원 프로필·미전달 파일 상태로 개별 버튼 사용 여부를 계산한다.
  function controls() {
    document.querySelectorAll("button, input, select").forEach((node) => {
      node.disabled = busy;
    });
    byId("previous-page").disabled = busy || offset === 0;
    byId("next-page").disabled = busy || !hasNext;
    byId("apply-profile").disabled = busy || !profileAvailable;
    byId("profile").disabled = busy || !profileAvailable;
    byId("register-camera").disabled = busy || pendingCredential !== null;
    byId("rotate-credential").disabled = busy || pendingCredential !== null;
    byId("download-credential").disabled = busy || pendingCredential === null;
    byId("finish-handoff").disabled = busy || !pendingCredential?.downloadAttempted;
  }

  // 화면 작업을 하나씩 실행하여 중복 제출을 막고 예외가 나도 입력 잠금을 해제한다.
  async function run(action) {
    if (busy) return;
    busy = true;
    controls();
    message("");
    try {
      await action();
    } catch (error) {
      message(error.message || "처리하지 못했습니다. 서버 연결을 확인하세요.", true);
    } finally {
      busy = false;
      controls();
    }
  }

  // 오류 원문 대신 고정 안내와 제한된 형식의 코드만 표시하여 내부 주소나 비밀값 노출을 피한다.
  function responseError(status, body) {
    const descriptions = {
      400: "입력한 주소와 설정값을 확인하세요.",
      401: "인증에 실패했거나 로그인이 만료되었습니다. 계정을 확인하고 다시 로그인하세요.",
      403: "이 작업에는 관리자 권한이 필요합니다.",
      404: "카메라 또는 요청한 항목을 찾지 못했습니다. 목록을 새로고침하세요.",
      409: "현재 장치 상태에서는 적용할 수 없습니다. 장치 상태와 중복 등록 여부를 확인하세요.",
      422: "입력값의 형식이나 길이를 확인하세요.",
      429: "로그인 시도가 많습니다. 잠시 후 다시 시도하세요.",
      502: "Edge 응답을 확인하지 못했습니다. 장치 상태를 확인하세요.",
      503: "서버 또는 연결된 서비스가 준비되지 않았습니다.",
      504: "장치 응답 대기 시간이 초과되었습니다. 상태를 조회한 뒤 다시 시도하세요.",
    };
    const code = body?.error?.code;
    const suffix = typeof code === "string" && /^[A-Z_]{1,64}$/.test(code) ? ` (${code})` : "";
    return new Error((descriptions[status] || `요청을 처리하지 못했습니다. HTTP ${status}`) + suffix);
  }

  // 쿠키를 사용하지 않고 토큰은 현재 탭의 메모리에만 보관한다.
  async function request(path, method = "GET", body, token = null) {
    let response;
    try {
      response = await fetch(path, {
        method,
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        signal: AbortSignal.timeout(90000),
        headers: {
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      throw new Error("서버 연결을 확인하세요. 변경 요청의 성공 여부가 불명확하므로 상태를 조회한 뒤 재시도하세요.");
    }
    const result = response.status === 204 ? null : await response.json().catch(() => null);
    return { status: response.status, ok: response.ok, body: result };
  }

  // 두 토큰을 함께 교체한 뒤 관리자 역할 여부를 호출자에게 알려 추가 처리를 맡긴다.
  function acceptSession(body) {
    if (!body || typeof body.access_token !== "string" || typeof body.refresh_token !== "string") {
      throw new Error("서버의 로그인 응답을 확인하지 못했습니다.");
    }
    session = { access: body.access_token, refresh: body.refresh_token };
    return body.user?.role === "admin";
  }

  // 현재 갱신 토큰으로 새 쌍을 받고 역할이 변경되었는지도 다시 확인한다.
  async function refreshSession() {
    const result = await request("/api/v1/auth/refresh", "POST", { refresh_token: session.refresh });
    if (!result.ok) throw responseError(result.status, result.body);
    if (!acceptSession(result.body)) throw new Error("관리자 권한이 변경되었습니다. 로그아웃하세요.");
  }

  // 401에 한해서 세션 갱신 후 원래 요청을 한 번만 재시도한다. 일반 변경 실패는 자동 반복하지 않는다.
  async function api(path, method = "GET", body) {
    if (!session) throw new Error("관리자 계정으로 로그인하세요.");
    let result = await request(path, method, body, session.access);
    if (result.status === 401) {
      // 여러 상태 조회가 함께 만료되어도 갱신 토큰은 한 번만 교체한다.
      if (!refreshTask) refreshTask = refreshSession().finally(() => { refreshTask = null; });
      await refreshTask;
      result = await request(path, method, body, session.access);
    }
    if (!result.ok) throw responseError(result.status, result.body);
    return result.body;
  }

  // 브라우저에 만든 파일 URL을 해제하여 전달을 마친 자격 증명이 메모리에 남지 않게 한다.
  function clearCredential() {
    if (pendingCredential) URL.revokeObjectURL(pendingCredential.url);
    pendingCredential = null;
    byId("handoff").hidden = true;
    byId("handoff-camera").textContent = "";
  }

  // 토큰·선택 카메라·일회성 파일·입력값을 함께 지우고 로그인 전 화면으로 돌린다.
  function clearSession() {
    session = null;
    selectedCamera = null;
    profileAvailable = false;
    offset = 0;
    hasNext = false;
    clearCredential();
    document.querySelectorAll("form").forEach((form) => form.reset());
    byId("workspace").hidden = true;
    byId("login-panel").hidden = false;
    byId("logout").hidden = true;
    byId("camera-detail").hidden = true;
    byId("camera-rows").replaceChildren();
    byId("camera-status").replaceChildren();
    controls();
  }

  // 관리자 전용 점검 API로 서버와 Data 준비 상태를 확인하고 조회 시각을 표시한다.
  async function systemStatus() {
    const result = await api("/api/v1/admin/system/status");
    byId("system-status").textContent = `External: ${display(result.external?.status)} · Data: ${display(result.data?.status)} · 조회 ${new Date().toLocaleTimeString("ko-KR")}`;
  }

  // 공통 상태는 한국어로 표시하고 미관측 값은 false나 0과 구분한다.
  function display(value) {
    const words = { true: "온라인", false: "오프라인", online: "연결됨", offline: "연결 안 됨", lost: "입력 끊김", unknown: "확인되지 않음", external: "외부 전원", battery: "배터리", running: "실행 중", ready: "준비됨", hd: "HD", fhd: "FHD" };
    return value === null || value === undefined ? "정보 없음" : (words[String(value)] || String(value));
  }

  // 서버가 반환한 페이지를 텍스트 노드로 만들며 총 개수 없이 페이지 크기로 다음 조회 가능성을 판단한다.
  async function loadCameras() {
    const result = await api(`/api/v1/cameras?limit=${pageSize}&offset=${offset}`);
    const rows = byId("camera-rows");
    rows.replaceChildren();
    for (const camera of result.items) {
      const row = document.createElement("tr");
      for (const value of [camera.camera_id, camera.name || "—", camera.enabled === false ? "중지" : "사용"]) {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      }
      const cell = document.createElement("td");
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "관리";
      button.className = "secondary";
      button.addEventListener("click", () => run(() => selectCamera(camera)));
      cell.append(button);
      row.append(cell);
      rows.append(row);
    }
    hasNext = result.items.length === pageSize;
    byId("page-label").textContent = result.items.length ? `${offset + 1}–${offset + result.items.length}번째 카메라` : "등록된 카메라가 없습니다.";
  }

  // 이전 카메라의 수정 입력을 초기화한 뒤 새 대상의 실제 상태와 지원 화질을 조회한다.
  async function selectCamera(camera) {
    selectedCamera = camera.camera_id;
    byId("camera-title").textContent = `${camera.name || camera.camera_id} · ${camera.camera_id}`;
    byId("camera-detail").hidden = false;
    byId("update-form").reset();
    byId("camera-detail").scrollIntoView({ behavior: "smooth", block: "start" });
    await cameraStatus();
  }

  // 상태와 프로필을 독립적으로 조회해 하나의 실패가 다른 결과 표시를 막지 않게 한다.
  async function cameraStatus() {
    const path = `/api/v1/cameras/${encodeURIComponent(selectedCamera)}`;
    const [statusResult, profileResult] = await Promise.allSettled([api(`${path}/status`), api(`${path}/video-profile`)]);
    const list = byId("camera-status");
    list.replaceChildren();
    profileAvailable = false;
    byId("profile").replaceChildren();
    if (statusResult.status === "fulfilled") {
      const status = statusResult.value;
      const fields = { online: "Edge 연결", camera_input: "카메라 입력", central_connection_status: "중앙 영상 연결", current_video_profile: "현재 화질", power_source: "전원", battery_percent: "배터리 (%)", storage_percent: "저장 공간 사용 (%)", cpu_percent: "CPU 사용 (%)", memory_percent: "메모리 사용 (%)", last_seen_at: "마지막 상태 수신", last_error_code: "최근 오류 코드" };
      for (const [key, label] of Object.entries(fields)) {
        const term = document.createElement("dt");
        const value = document.createElement("dd");
        term.textContent = label;
        value.textContent = display(status[key]);
        list.append(term, value);
      }
    } else {
      const term = document.createElement("dt");
      const value = document.createElement("dd");
      term.textContent = "장치 상태";
      value.textContent = statusResult.reason.message;
      list.append(term, value);
    }
    if (profileResult.status === "fulfilled") {
      const profile = profileResult.value;
      for (const name of profile.supported_profiles.filter((item) => ["hd", "fhd"].includes(item))) {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name.toUpperCase();
        byId("profile").append(option);
      }
      byId("profile").value = profile.current_profile;
      profileAvailable = profile.edge_online === true && byId("profile").options.length > 0;
      byId("profile-status").textContent = `현재 ${display(profile.current_profile)} / 요청 ${display(profile.desired_profile)}. ${profileAvailable ? "Edge가 확인한 화질만 선택할 수 있습니다." : "Edge가 연결되고 지원 화질이 확인되어야 변경할 수 있습니다."}`;
    } else {
      byId("profile-status").textContent = profileResult.reason.message;
    }
  }

  // 빈 입력은 PATCH에서 생략하여 기존 값을 보존하고 명시적으로 입력한 값만 보낸다.
  function formValues(form) {
    return Object.fromEntries([...new FormData(form).entries()].map(([key, value]) => [key, value.trim()]).filter(([, value]) => value !== ""));
  }

  // 응답의 대상·자격 증명을 검증하고 사용자가 저장을 확인할 때까지 다운로드 파일 하나를 보관한다.
  function holdCredential(result, cameraId) {
    const credential = result?.publish_credentials;
    if (result?.camera_id !== cameraId || typeof credential?.username !== "string" || !credential.username || typeof credential?.password !== "string" || !credential.password) {
      throw new Error("요청은 처리되었지만 게시 계정 응답을 확인하지 못했습니다. 카메라 상태를 조회하고 게시 계정을 재발급하세요.");
    }
    // 다운로드를 재시도할 때 서버에서 계정을 다시 발급하지 않는다.
    const file = new Blob([JSON.stringify({ camera_id: cameraId, username: credential.username, password: credential.password }, null, 2) + "\n"], { type: "application/json" });
    pendingCredential = { url: URL.createObjectURL(file), cameraId, downloadAttempted: false };
    byId("handoff-camera").textContent = `대상 카메라: ${cameraId}`;
    byId("handoff").hidden = false;
    byId("handoff").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // 로그인 응답의 역할뿐 아니라 관리자 전용 상태 API까지 확인한 뒤 운영 화면을 연다.
  byId("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const body = Object.fromEntries(new FormData(event.currentTarget));
    byId("login-form").elements.password.value = "";
    run(async () => {
      const result = await request("/api/v1/auth/login", "POST", body);
      if (!result.ok) throw responseError(result.status, result.body);
      if (!acceptSession(result.body)) {
        try {
          await request("/api/v1/auth/logout", "POST", { refresh_token: session.refresh }, session.access);
        } finally {
          clearSession();
        }
        throw new Error("관리자 계정으로 로그인하세요. 일반 조회 계정은 사용할 수 없습니다.");
      }
      try {
        await systemStatus();
      } catch (error) {
        const previousSession = session;
        clearSession();
        await request("/api/v1/auth/logout", "POST", { refresh_token: previousSession.refresh }, previousSession.access).catch(() => {});
        throw error;
      }
      byId("workspace").hidden = false;
      byId("login-panel").hidden = true;
      byId("logout").hidden = false;
      await loadCameras();
    });
  });

  // 서버 폐기 확인과 무관하게 로컬 세션은 지우되 파일 전달이 끝나지 않았으면 먼저 알린다.
  byId("logout").addEventListener("click", () => {
    if (pendingCredential && !confirm("게시 계정 파일을 저장했습니까? 로그아웃하면 현재 파일을 다시 다운로드할 수 없습니다.")) return;
    run(async () => {
      let revoked = false;
      try {
        // 로그아웃에서는 토큰을 갱신하지 않고 현재 로그인 계열을 폐기한다.
        const result = await request("/api/v1/auth/logout", "POST", { refresh_token: session.refresh }, session.access);
        revoked = result.ok;
      } catch {
        revoked = false;
      } finally {
        clearSession();
      }
      message(revoked ? "로그아웃했습니다." : "이 탭의 로그인을 지웠습니다. 연결 오류로 서버의 토큰 폐기는 확인하지 못했습니다.", !revoked);
    });
  });
  byId("refresh-system").addEventListener("click", () => run(systemStatus));
  byId("refresh-cameras").addEventListener("click", () => run(loadCameras));
  byId("previous-page").addEventListener("click", () => run(async () => { offset = Math.max(0, offset - pageSize); await loadCameras(); }));
  byId("next-page").addEventListener("click", () => run(async () => { offset += pageSize; await loadCameras(); }));
  byId("refresh-camera").addEventListener("click", () => run(cameraStatus));
  // 설정 변경 후 서버 상태를 다시 읽어 실제로 적용된 프로필을 표시한다.
  byId("profile-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const profile = byId("profile").value;
    run(async () => {
      await api(`/api/v1/cameras/${encodeURIComponent(selectedCamera)}/video-profile`, "PATCH", { profile });
      await cameraStatus();
      message("영상 화질을 적용했습니다.");
    });
  });
  byId("update-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const body = formValues(event.currentTarget);
    if ("enabled" in body) body.enabled = body.enabled === "true";
    if (Object.keys(body).length === 0) { message("변경할 항목을 입력하세요.", true); return; }
    run(async () => {
      await api(`/api/v1/cameras/${encodeURIComponent(selectedCamera)}`, "PATCH", body);
      byId("update-form").reset();
      await loadCameras();
      await cameraStatus();
      message("등록 정보를 저장했습니다.");
    });
  });
  // 기존 일회성 파일의 전달을 마치기 전에는 다음 카메라의 자격 증명을 발급하지 않는다.
  byId("register-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (pendingCredential) return;
    const body = formValues(event.currentTarget);
    run(async () => {
      const result = await api("/api/v1/cameras", "POST", body);
      holdCredential(result, body.camera_id);
      byId("register-form").reset();
      offset = 0;
      await loadCameras();
      message("등록했습니다. 게시 계정 파일을 저장하고 해당 Edge에 전달하세요.");
    });
  });
  byId("rotate-credential").addEventListener("click", () => {
    if (pendingCredential || !selectedCamera) return;
    if (!confirm(`${selectedCamera}의 기존 게시 비밀번호가 폐기되고 영상 송출이 끊깁니다. 재발급하시겠습니까?`)) return;
    run(async () => {
      const result = await api(`/api/v1/cameras/${encodeURIComponent(selectedCamera)}/publish-credentials/rotate`, "POST");
      holdCredential(result, selectedCamera);
      message("게시 계정을 재발급했습니다. 새 파일을 Edge에 적용하세요.");
    });
  });
  // 다운로드 요청과 실제 저장 완료는 다르므로 여기서는 시도 여부만 기록한다.
  byId("download-credential").addEventListener("click", () => {
    if (!pendingCredential) return;
    const link = document.createElement("a");
    link.href = pendingCredential.url;
    link.download = `${pendingCredential.cameraId}-publish.json`;
    document.body.append(link);
    link.click();
    link.remove();
    pendingCredential.downloadAttempted = true;
    controls();
    message("다운로드를 요청했습니다. 파일이 저장되었는지 직접 확인한 뒤 닫으세요.");
  });
  byId("finish-handoff").addEventListener("click", () => {
    if (!pendingCredential?.downloadAttempted) return;
    if (!confirm("게시 계정 JSON 파일이 저장되었는지 확인했습니까? 닫은 뒤에는 다시 다운로드할 수 없습니다.")) return;
    clearCredential();
    controls();
    message("파일을 해당 Edge에 전달하고 연결 상태를 확인하세요.");
  });
  // 진행 작업이나 아직 전달하지 않은 파일이 있으면 페이지 이탈로 인한 유실을 경고한다.
  window.addEventListener("beforeunload", (event) => {
    if (pendingCredential || busy) { event.preventDefault(); event.returnValue = ""; }
  });
  window.addEventListener("pagehide", clearSession);
  controls();
})();
