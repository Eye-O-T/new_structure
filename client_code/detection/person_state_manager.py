# person_state_manager.py

import time


class PersonStateManager:
    def __init__(self, disappear_timeout=3.0):
        self.person_states = {}
        self.disappear_timeout = disappear_timeout

    def create_person_state(self):
        now = time.time()

        return {
            "first_seen": now,
            "last_seen": now,
            "bbox": None,
            "is_full_body": False,
            "crop_saved": False,
            "crop_path": None,
            "is_recording": False,
            "clip_path": None,
            "vlm_done": False,
            "vlm_result": None,
        }

    def update_person(self, person_id, bbox, is_full_body):

        now = time.time()

        if person_id not in self.person_states:
            self.person_states[person_id] = self.create_person_state()

        state = self.person_states[person_id]

        state["last_seen"] = now
        state["bbox"] = bbox
        state["is_full_body"] = is_full_body

        return state

    def mark_crop_saved(self, person_id, crop_path):

        if person_id not in self.person_states:
            self.person_states[person_id] = self.create_person_state()

        self.person_states[person_id]["crop_saved"] = True
        self.person_states[person_id]["crop_path"] = crop_path

    def mark_recording_started(self, person_id, clip_path=None):

        if person_id not in self.person_states:
            self.person_states[person_id] = self.create_person_state()

        self.person_states[person_id]["is_recording"] = True
        self.person_states[person_id]["clip_path"] = clip_path

    def mark_recording_stopped(self, person_id):

        if person_id in self.person_states:
            self.person_states[person_id]["is_recording"] = False

    def mark_vlm_done(self, person_id, vlm_result):
        """
        VLM 분석 완료 상태 기록
        """

        if person_id not in self.person_states:
            self.person_states[person_id] = self.create_person_state()

        self.person_states[person_id]["vlm_done"] = True
        self.person_states[person_id]["vlm_result"] = vlm_result

    def get_state(self, person_id):
        return self.person_states.get(person_id)

    def has_crop_saved(self, person_id):
        state = self.get_state(person_id)
        return state is not None and state["crop_saved"]

    def is_recording(self, person_id):
        state = self.get_state(person_id)
        return state is not None and state["is_recording"]

    def is_vlm_done(self, person_id):
        state = self.get_state(person_id)
        return state is not None and state["vlm_done"]

    def remove_disappeared_persons(self):
        now = time.time()
        removed_ids = []

        for person_id, state in list(self.person_states.items()):
            if now - state["last_seen"] > self.disappear_timeout:
                removed_ids.append(person_id)
                del self.person_states[person_id]

        return removed_ids