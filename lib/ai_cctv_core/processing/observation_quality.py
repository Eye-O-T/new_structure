"""Pure geometry checks shared by observation producers and consumers."""

BORDER_MARGIN_X_RATIO = 0.02
BORDER_MARGIN_Y_RATIO = 0.03


def evaluate_border_margin(bbox, frame_width, frame_height):
    x1, y1, x2, y2 = bbox
    distances = {
        "left": x1,
        "top": y1,
        "right": frame_width - x2,
        "bottom": frame_height - y2,
    }
    thresholds = {
        "left": frame_width * BORDER_MARGIN_X_RATIO,
        "right": frame_width * BORDER_MARGIN_X_RATIO,
        "top": frame_height * BORDER_MARGIN_Y_RATIO,
        "bottom": frame_height * BORDER_MARGIN_Y_RATIO,
    }
    near_edges = [edge for edge, distance in distances.items() if distance < thresholds[edge]]
    return {
        "method": "frame_margin_v1",
        "border_clear": not near_edges,
        "margin_ratio": {"x": BORDER_MARGIN_X_RATIO, "y": BORDER_MARGIN_Y_RATIO},
        "distances_px": distances,
        "near_edges": near_edges,
    }
