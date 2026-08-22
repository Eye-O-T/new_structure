# clip_manager.py

import os
import shutil
from datetime import datetime, timedelta
from math import hypot

import cv2


class ClipManager:
    def __init__(
        self,
        base_dir,
        fps=30,
        max_clip_seconds=10,
        disappear_timeout=3.0,
        trajectory_sample_interval=5,
        trajectory_min_distance=10,
        trajectory_smoothing_alpha=0.35,
        trajectory_simplify_epsilon=4.0,
        target_bitrate_kbps=2500,
    ):
        self.base_dir = base_dir
        self.fps = fps if fps and fps > 0 else 30
        self.frame_interval = timedelta(seconds=1.0 / self.fps)
        self.max_clip_seconds = max_clip_seconds
        self.target_bitrate_kbps = target_bitrate_kbps
        self.disappear_timeout = disappear_timeout
        self.trajectory_sample_interval = max(1, int(trajectory_sample_interval))
        self.trajectory_min_distance = max(0, int(trajectory_min_distance))
        self.trajectory_smoothing_alpha = min(
            1.0,
            max(0.0, float(trajectory_smoothing_alpha)),
        )
        self.trajectory_simplify_epsilon = max(0.0, float(trajectory_simplify_epsilon))
        self.clip_root_dir = os.path.join(self.base_dir, "이벤트 CLIP")
        self.person_clips = {}

        os.makedirs(self.clip_root_dir, exist_ok=True)

    def update_person(self, person_id, frame, bbox, crop_path=None):
        if frame is None or bbox is None:
            return

        state = self.person_clips.get(person_id)
        if state is None:
            state = self._create_person_state(person_id, frame)
            self.person_clips[person_id] = state

        state["last_seen"] = datetime.now()
        state["last_frame"] = frame.copy()
        self._add_trajectory_point(state, bbox)

        if crop_path is not None:
            self._copy_crop_once(state, crop_path)

        if state["clip_completed"]:
            return

        frame_size = self._get_frame_size(frame)
        if state["writer"] is None:
            self._start_new_clip(state, frame_size)

        if state["writer"] is None:
            return

        now = datetime.now()
        write_until = self._get_preview_write_until(state, now)
        self._write_frame_by_wall_clock(state, frame, write_until)

        if self._should_complete_preview(state, now):
            self._close_writer(state)
            state["clip_completed"] = True

    def finish_person(self, person_id):
        state = self.person_clips.pop(person_id, None)
        if state is None:
            return

        self._close_writer(state)
        self._save_trajectory_image(state)

    def finish_all(self):
        for person_id in list(self.person_clips.keys()):
            self.finish_person(person_id)

    def _create_person_state(self, person_id, frame):
        now = datetime.now()
        first_seen_text = now.strftime("%Y-%m-%d_%H-%M-%S")
        folder_name = f"{first_seen_text}_person{person_id}_추적영상"
        folder_path = self._get_unique_folder_path(folder_name)

        os.makedirs(folder_path, exist_ok=True)

        return {
            "person_id": person_id,
            "first_seen": now,
            "last_seen": now,
            "folder_path": folder_path,
            "clip_index": 0,
            "clip_started_at": None,
            "next_frame_time": None,
            "frames_written": 0,
            "clip_path": None,
            "clip_completed": False,
            "writer": None,
            "points": [],
            "trajectory_frame_count": 0,
            "last_trajectory_point": None,
            "last_bbox_diagonal": None,
            "jump_skip_count": 0,
            "last_frame": frame.copy(),
            "crop_saved": False,
        }

    def _start_new_clip(self, state, frame_size):
        state["clip_index"] = 1
        state["clip_started_at"] = datetime.now()
        state["next_frame_time"] = state["clip_started_at"]
        state["frames_written"] = 0

        clip_filename = "preview.mp4"
        clip_path = os.path.join(state["folder_path"], clip_filename)

        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(clip_path, fourcc, self.fps, frame_size)
        self._apply_writer_profile(writer)

        if not writer.isOpened():
            print(f"클립 영상 Writer 생성 실패: {clip_path}")
            state["writer"] = None
            state["clip_path"] = None
            return

        state["writer"] = writer
        state["clip_path"] = clip_path

        print(
            f"클립 영상 저장 시작: {clip_path} "
            f"({frame_size[0]}x{frame_size[1]}, {self.fps:.1f}fps, "
            f"목표 {self.target_bitrate_kbps}kbps급)"
        )

    def _apply_writer_profile(self, writer):
        if writer is None:
            return

        if hasattr(cv2, "VIDEOWRITER_PROP_QUALITY"):
            writer.set(cv2.VIDEOWRITER_PROP_QUALITY, 75)

    def _close_writer(self, state):
        writer = state.get("writer")
        if writer is not None:
            ended_at = datetime.now()
            wall_seconds = (
                ended_at - state["clip_started_at"]
            ).total_seconds()
            encoded_seconds = state["frames_written"] / self.fps if self.fps else 0
            effective_fps = (
                state["frames_written"] / wall_seconds
                if wall_seconds > 0
                else 0
            )
            writer.release()
            state["writer"] = None
            print(
                "클립 영상 저장 종료: "
                f"{state.get('clip_path')} "
                f"(실시간 {wall_seconds:.2f}s, 재생예상 {encoded_seconds:.2f}s, "
                f"저장프레임 {state['frames_written']}, 실효FPS {effective_fps:.2f})"
            )
            state["next_frame_time"] = None
            state["frames_written"] = 0
            state["clip_path"] = None

    def _write_frame_by_wall_clock(self, state, frame, now):
        writer = state.get("writer")
        next_frame_time = state.get("next_frame_time")

        if writer is None or next_frame_time is None:
            return

        writes = 0
        max_writes_per_input = max(1, int(self.fps * 2))

        while state["next_frame_time"] <= now and writes < max_writes_per_input:
            writer.write(frame)
            state["next_frame_time"] += self.frame_interval
            state["frames_written"] += 1
            writes += 1

        if writes == max_writes_per_input and state["next_frame_time"] <= now:
            state["next_frame_time"] = now + self.frame_interval
            print("클립 영상 프레임 보정 한도 초과: 긴 지연 구간을 건너뜁니다.")

    def _get_preview_write_until(self, state, now):
        if self.max_clip_seconds is None:
            return now

        clip_deadline = state["clip_started_at"] + timedelta(
            seconds=self.max_clip_seconds
        )
        return min(now, clip_deadline)

    def _should_complete_preview(self, state, now):
        if self.max_clip_seconds is None:
            return False

        if state["clip_started_at"] is None:
            return False

        elapsed_seconds = (now - state["clip_started_at"]).total_seconds()
        return elapsed_seconds >= self.max_clip_seconds

    def _save_trajectory_image(self, state):
        frame = state.get("last_frame")
        points = state.get("points", [])

        if frame is None or len(points) == 0:
            return

        trajectory_frame = frame.copy()
        points = self._simplify_points(points)

        for index, point in enumerate(points):
            cv2.circle(trajectory_frame, point, 4, (0, 255, 255), -1)

            if index > 0:
                cv2.line(
                    trajectory_frame,
                    points[index - 1],
                    point,
                    (0, 255, 255),
                    2,
                )

        save_path = os.path.join(state["folder_path"], "trajectory.jpg")
        cv2.imwrite(save_path, trajectory_frame)

    def _add_trajectory_point(self, state, bbox):
        state["trajectory_frame_count"] += 1

        raw_point = self._get_bbox_ground_anchor(bbox)
        bbox_diagonal = self._get_bbox_diagonal(bbox)
        previous_point = state.get("last_trajectory_point")

        if previous_point is None:
            self._append_trajectory_point(state, raw_point, bbox_diagonal)
            return

        if state["trajectory_frame_count"] % self.trajectory_sample_interval != 0:
            return

        distance = self._distance(previous_point, raw_point)
        if distance < self.trajectory_min_distance:
            return

        max_jump_distance = max(80.0, bbox_diagonal * 1.4)
        if distance > max_jump_distance:
            state["jump_skip_count"] += 1
            if state["jump_skip_count"] < 3:
                return

            state["last_trajectory_point"] = (
                float(raw_point[0]),
                float(raw_point[1]),
            )
            state["last_bbox_diagonal"] = bbox_diagonal
            state["jump_skip_count"] = 0
            return

        state["jump_skip_count"] = 0
        alpha = self.trajectory_smoothing_alpha
        smoothed_point = (
            previous_point[0] * (1.0 - alpha) + raw_point[0] * alpha,
            previous_point[1] * (1.0 - alpha) + raw_point[1] * alpha,
        )
        self._append_trajectory_point(state, smoothed_point, bbox_diagonal)

    def _append_trajectory_point(self, state, point, bbox_diagonal):
        float_point = (float(point[0]), float(point[1]))
        int_point = (int(round(float_point[0])), int(round(float_point[1])))

        points = state["points"]
        if points and points[-1] == int_point:
            state["last_trajectory_point"] = float_point
            state["last_bbox_diagonal"] = bbox_diagonal
            return

        points.append(int_point)
        state["last_trajectory_point"] = float_point
        state["last_bbox_diagonal"] = bbox_diagonal

    def _simplify_points(self, points):
        if len(points) <= 2 or self.trajectory_simplify_epsilon <= 0:
            return points

        simplified = cv2.approxPolyDP(
            curve=self._points_to_curve(points),
            epsilon=self.trajectory_simplify_epsilon,
            closed=False,
        )
        return [tuple(point[0]) for point in simplified]

    def _points_to_curve(self, points):
        import numpy as np

        return np.array(points, dtype="int32").reshape((-1, 1, 2))

    def _copy_crop_once(self, state, crop_path):
        if state["crop_saved"]:
            return

        if not os.path.exists(crop_path):
            return

        save_path = os.path.join(state["folder_path"], "full_crop.jpg")

        try:
            shutil.copy2(crop_path, save_path)
            state["crop_saved"] = True
        except Exception as e:
            print(f"전신 crop 복사 실패: {e}")

    def _get_bbox_ground_anchor(self, bbox):
        x1, y1, x2, y2 = map(int, bbox)
        return ((x1 + x2) // 2, y2)

    def _get_bbox_diagonal(self, bbox):
        x1, y1, x2, y2 = map(int, bbox)
        return hypot(x2 - x1, y2 - y1)

    def _distance(self, point_a, point_b):
        return hypot(point_a[0] - point_b[0], point_a[1] - point_b[1])

    def _get_frame_size(self, frame):
        height, width = frame.shape[:2]
        return width, height

    def _get_unique_folder_path(self, folder_name):
        folder_path = os.path.join(self.clip_root_dir, folder_name)

        if not os.path.exists(folder_path):
            return folder_path

        index = 2
        while True:
            candidate = os.path.join(self.clip_root_dir, f"{folder_name}_{index}")
            if not os.path.exists(candidate):
                return candidate
            index += 1
