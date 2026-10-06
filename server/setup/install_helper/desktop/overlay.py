"""Pure geometry helpers for the live-object display overlay."""


def scale_bbox_to_widget(bbox, frame_width, frame_height, widget_width, widget_height):
    if frame_width <= 0 or frame_height <= 0 or widget_width <= 0 or widget_height <= 0:
        return None
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(value) for value in bbox)
    except (TypeError, ValueError):
        return None
    if not x1 < x2 or not y1 < y2:
        return None
    scale = min(widget_width / frame_width, widget_height / frame_height)
    display_width = frame_width * scale
    display_height = frame_height * scale
    offset_x = (widget_width - display_width) / 2
    offset_y = (widget_height - display_height) / 2
    return (
        max(0.0, min(widget_width, offset_x + x1 * scale)),
        max(0.0, min(widget_height, offset_y + y1 * scale)),
        max(0.0, min(widget_width, offset_x + x2 * scale)),
        max(0.0, min(widget_height, offset_y + y2 * scale)),
    )


def overlay_items(payload, widget_width, widget_height):
    if not isinstance(payload, dict) or payload.get("stale") or not payload.get("objects"):
        return []
    frame_width = payload.get("frame_width")
    frame_height = payload.get("frame_height")
    if not isinstance(frame_width, (int, float)) or not isinstance(frame_height, (int, float)):
        return []
    result = []
    for item in payload["objects"]:
        if not isinstance(item, dict):
            continue
        rect = scale_bbox_to_widget(
            item.get("bbox"), frame_width, frame_height, widget_width, widget_height
        )
        if rect is not None:
            result.append({"rect": rect, "item": item})
    return result
