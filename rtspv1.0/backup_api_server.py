import os
import zipfile
import tempfile
import time
from datetime import datetime

import psutil
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title="AI CCTV RPi API Server")

# 백업 디렉터리 경로 설정 (사용자 RPi 환경에 맞춰 홈 폴더의 backups를 가리키도록 설정)
# BACKUP_DIR = os.path.join(os.path.expanduser("~"), "backups")
BACKUP_DIR = os.path.join(os.path.dirname(__file__), "backups")
UPS_I2C_BUS = int(os.getenv("AI_CCTV_UPS_I2C_BUS", "1"))
UPS_I2C_ADDRESS = int(os.getenv("AI_CCTV_UPS_I2C_ADDRESS", "0x17"), 0)
UPS_BATTERY_REMAINING_LOW_REGISTER = 0x13
UPS_BATTERY_REMAINING_HIGH_REGISTER = 0x14

DEFAULT_PROCESS_KEYWORDS = (
    "stream_and_record.sh",
    "ffmpeg",
    "gst-launch",
    "gstreamer",
    "libcamera",
    "rpicam",
    "backup_api_server",
    "resource_monitor",
    "ai_cctv",
)


class UpsBatteryReader:
    def __init__(
        self,
        bus_number=UPS_I2C_BUS,
        device_address=UPS_I2C_ADDRESS,
    ):
        self.bus_number = bus_number
        self.device_address = device_address

    def read_snapshot(self):
        try:
            self._ensure_i2c_device_exists()
            battery_percent = self._read_battery_remaining_percent()
            return {
                "available": True,
                "battery_remaining_percent": battery_percent,
                "i2c_bus": self.bus_number,
                "i2c_address": f"0x{self.device_address:02x}",
            }
        except Exception as error:
            return {
                "available": False,
                "battery_remaining_percent": None,
                "i2c_bus": self.bus_number,
                "i2c_address": f"0x{self.device_address:02x}",
                "error": str(error),
            }

    def _ensure_i2c_device_exists(self):
        if os.name != "posix":
            return

        device_path = f"/dev/i2c-{self.bus_number}"
        if not os.path.exists(device_path):
            raise FileNotFoundError(
                f"{device_path} not found. Enable I2C on the Raspberry Pi."
            )

    def _read_battery_remaining_percent(self):
        try:
            from smbus2 import SMBus
        except ImportError as error:
            raise ImportError("smbus2 is required to read the UPS HAT over I2C.") from error

        with SMBus(self.bus_number) as bus:
            low_byte = bus.read_byte_data(
                self.device_address,
                UPS_BATTERY_REMAINING_LOW_REGISTER,
            )
            high_byte = bus.read_byte_data(
                self.device_address,
                UPS_BATTERY_REMAINING_HIGH_REGISTER,
            )

        value = (high_byte << 8) | low_byte
        return max(0, min(100, int(value)))


class ResourceUsageCollector:
    def __init__(self, sample_interval_seconds=0.1, ups_reader=None):
        self.sample_interval_seconds = sample_interval_seconds
        self.ups_reader = ups_reader or UpsBatteryReader()

    def collect(self):
        processes = self._collect_target_processes()

        for process in processes:
            try:
                process.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        time.sleep(self.sample_interval_seconds)

        cpu_total_percent = psutil.cpu_percent(interval=None)
        cpu_count = psutil.cpu_count() or 1
        process_cpu_percent = self._sum_process_cpu_percent(processes) / cpu_count
        process_cpu_percent = min(100.0, max(0.0, process_cpu_percent))

        memory = psutil.virtual_memory()
        process_memory_bytes = self._sum_process_memory_bytes(processes)
        other_memory_bytes = max(0, memory.used - process_memory_bytes)

        disk_path = os.getenv("AI_CCTV_DISK_PATH", BACKUP_DIR)
        if not os.path.exists(disk_path):
            disk_path = os.getcwd()
        disk = psutil.disk_usage(disk_path)

        return {
            "collected_at": datetime.now().isoformat(timespec="seconds"),
            "process": {
                "count": len(processes),
                "pids": [process.pid for process in processes],
                "keywords": self._get_process_keywords(),
            },
            "cpu": {
                "total_percent": cpu_total_percent,
                "app_percent": process_cpu_percent,
                "other_percent": max(0.0, cpu_total_percent - process_cpu_percent),
                "idle_percent": max(0.0, 100.0 - cpu_total_percent),
            },
            "memory": {
                "used_percent": memory.percent,
                "total_gb": self._bytes_to_gb(memory.total),
                "available_gb": self._bytes_to_gb(memory.available),
                "app_gb": self._bytes_to_gb(process_memory_bytes),
                "other_gb": self._bytes_to_gb(other_memory_bytes),
            },
            "disk": {
                "path": disk_path,
                "used_percent": disk.percent,
                "total_gb": self._bytes_to_gb(disk.total),
                "used_gb": self._bytes_to_gb(disk.used),
                "free_gb": self._bytes_to_gb(disk.free),
            },
            "power": self.ups_reader.read_snapshot(),
        }

    def _collect_target_processes(self):
        target_pids = self._get_env_pids()
        matched_pids = set()

        for pid in target_pids:
            try:
                matched_pids.add(psutil.Process(pid).pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
                continue

        keywords = [keyword.lower() for keyword in self._get_process_keywords()]
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = process.info.get("name") or ""
                cmdline = " ".join(process.info.get("cmdline") or [])
                haystack = f"{name} {cmdline}".lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

            if any(keyword in haystack for keyword in keywords):
                matched_pids.add(process.pid)

        all_pids = set(matched_pids)
        for pid in list(matched_pids):
            try:
                parent = psutil.Process(pid)
                for child in parent.children(recursive=True):
                    all_pids.add(child.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        processes = []
        for pid in sorted(all_pids):
            try:
                processes.append(psutil.Process(pid))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return processes

    def _get_env_pids(self):
        raw_pids = os.getenv("AI_CCTV_MONITOR_PIDS", "")
        pids = []
        for value in raw_pids.split(","):
            value = value.strip()
            if value.isdigit():
                pids.append(int(value))
        return pids

    def _get_process_keywords(self):
        raw_keywords = os.getenv("AI_CCTV_PROCESS_KEYWORDS", "")
        if not raw_keywords.strip():
            return list(DEFAULT_PROCESS_KEYWORDS)
        return [
            keyword.strip()
            for keyword in raw_keywords.split(",")
            if keyword.strip()
        ]

    def _sum_process_cpu_percent(self, processes):
        total = 0.0
        for process in processes:
            try:
                total += process.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total

    def _sum_process_memory_bytes(self, processes):
        total = 0
        for process in processes:
            try:
                total += process.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total

    def _bytes_to_gb(self, value):
        return value / (1024 ** 3)


resource_usage_collector = ResourceUsageCollector()


def remove_temp_file(path: str):
    """파일 전송 완료 후 임시 압축파일을 삭제하는 헬퍼 함수"""
    try:
        if os.path.exists(path):
            os.remove(path)
            print(f"[Backup Server] 임시 파일 삭제 완료: {path}")
    except Exception as e:
        print(f"[Backup Server] 임시 파일 삭제 중 에러 발생: {e}")


@app.get("/recover")
def recover_backups(start: str, end: str, background_tasks: BackgroundTasks):
    """
    지정한 시간대 (start ~ end) 사이의 누락된 .ts 파일들을 ZIP으로 묶어서 반환합니다.
    - start: ISO 8601 형식 (예: 2026-05-30T21:00:15)
    - end: ISO 8601 형식 (예: 2026-05-30T21:00:25)
    """
    # 1. 입력 시각 파싱
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="시작 시각(start) 및 종료 시각(end)은 ISO 8601 형식(예: YYYY-MM-DDTHH:MM:SS)이어야 합니다."
        )

    if start_dt > end_dt:
        raise HTTPException(
            status_code=400,
            detail="시작 시각이 종료 시각보다 늦을 수 없습니다."
        )

    # 2. 백업 디렉터리 확인
    if not os.path.exists(BACKUP_DIR):
        return JSONResponse(
            status_code=404,
            content={"message": f"서버에 백업 디렉터리({BACKUP_DIR})가 존재하지 않습니다."}
        )

    # 3. 백업 파일 탐색 및 필터링
    target_files = []
    
    try:
        files = os.listdir(BACKUP_DIR)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"백업 폴더 읽기 실패: {e}")

    for file in files:
        if not file.endswith(".ts"):
            continue
            
        filepath = os.path.join(BACKUP_DIR, file)
        try:
            # 파일 수정 시간(mtime) 획득 및 datetime 변환
            mtime_timestamp = os.path.getmtime(filepath)
            file_end_time = datetime.fromtimestamp(mtime_timestamp)
            # 녹화 단위는 10초 분량이므로 시작 시각은 (종료 시각 - 10초)로 계산
            file_start_time = datetime.fromtimestamp(mtime_timestamp - 10.0)
            
            # 클라이언트 누락 구간과 파일 비디오의 녹화 시간 구간이 겹치는지 체크
            # 겹침 조건: max(start_dt, file_start_time) < min(end_dt, file_end_time)
            overlap_start = max(start_dt, file_start_time)
            overlap_end = min(end_dt, file_end_time)
            
            if overlap_start < overlap_end:
                target_files.append((filepath, file))
        except Exception as e:
            print(f"[Warning] 파일 정보 확인 중 오류 발생 ({file}): {e}")
            continue

    # 4. 필터링된 파일 개수 검증
    if not target_files:
        return JSONResponse(
            status_code=404,
            content={"message": "해당 시간대에 해당하는 백업 비디오 조각이 존재하지 않습니다."}
        )

    print(f"[Backup Server] 누락 복구 대상 파일 {len(target_files)}개 감지.")

    # 5. 임시 ZIP 파일 생성
    try:
        # 시스템 임시 디렉터리에 ZIP 파일 생성
        temp_dir = tempfile.gettempdir()
        zip_filename = f"recovered_backup_{int(time.time())}.zip"
        temp_zip_path = os.path.join(temp_dir, zip_filename)
        
        with zipfile.ZipFile(temp_zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for filepath, filename in target_files:
                zip_file.write(filepath, arcname=filename)
                
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"서버에서 ZIP 파일 생성 중 오류 발생: {e}"
        )

    # 6. 다운로드 응답 전송 및 전송 완료 후 백그라운드 태스크로 임시 ZIP 파일 삭제 예약
    background_tasks.add_task(remove_temp_file, temp_zip_path)
    
    return FileResponse(
        path=temp_zip_path,
        media_type="application/x-zip-compressed",
        filename="recovered_backups.zip"
    )


@app.get("/monitor/resources")
def read_resource_usage():
    try:
        return resource_usage_collector.collect()
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
