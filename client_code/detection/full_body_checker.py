# full_body_checker.py


class FullBodyChecker:
    def __init__(
        self,
        min_body_height_ratio=0.45,
        margin=10
    ):
        """
        min_body_height_ratio:
        - 사람 bbox 높이가 전체 프레임 높이의 몇 % 이상이어야 전신 후보로 볼지
        - 0.45면 화면 높이의 45% 이상

        margin:
        - bbox가 화면 가장자리에 너무 붙어 있으면 잘린 것으로 판단
        """
        self.min_body_height_ratio = min_body_height_ratio
        self.margin = margin

    def is_full_body_visible(self, bbox, frame_shape):
        """
        bbox:
        - (x1, y1, x2, y2)

        frame_shape:
        - frame.shape

        return:
        - True: 전신이 화면 안에 들어온 것으로 판단
        - False: 전신이 잘렸거나 너무 작다고 판단
        """

        x1, y1, x2, y2 = bbox
        frame_height, frame_width = frame_shape[:2]

        bbox_width = x2 - x1
        bbox_height = y2 - y1

        if bbox_width <= 0 or bbox_height <= 0:
            return False

        # 1. 사람 크기가 너무 작으면 전신 판단 불가
        is_large_enough = bbox_height >= frame_height * self.min_body_height_ratio

        # 2. 머리 쪽이 화면 위에 잘리지 않았는지
        is_not_cut_top = y1 > self.margin

        # 3. 다리 쪽이 화면 아래에 잘리지 않았는지
        is_not_cut_bottom = y2 < frame_height - self.margin

        # 4. 좌우가 화면 밖으로 잘리지 않았는지
        is_not_cut_left = x1 > self.margin
        is_not_cut_right = x2 < frame_width - self.margin

        return (
            is_large_enough
            and is_not_cut_top
            and is_not_cut_bottom
            and is_not_cut_left
            and is_not_cut_right
        )

    def get_status_text(self, bbox, frame_shape):
        """
        화면 표시용 상태 텍스트
        """

        if self.is_full_body_visible(bbox, frame_shape):
            return "FULL"

        return "PARTIAL"