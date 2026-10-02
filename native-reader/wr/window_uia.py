"""Bounded, read-only UI Automation probe for the WeChat main window.

Purpose: determine whether the WeChat client exposes real control rectangles (title / input /
message list) through UI Automation, so positioning can avoid OCR entirely.

Safety and bounds:
* Strictly read-only: walks the automation tree and reads ControlType/BoundingRectangle/Name
  presence. It never invokes patterns, never sets focus/values, never synthesizes input, never
  enables accessibility, and never injects into another process.
* Bounded by node count, depth and a wall-clock deadline; any UIA failure stops cleanly.
* Names are never returned in bulk: only a title-length count and the presence of an editable
  input / message list, so no chat text or contact name leaves this module.

If the client does not expose usable rectangles, callers must not fabricate coordinates; the probe
reports exactly what is (not) available.
"""

from __future__ import annotations

import time

MAX_NODES = 1000
MAX_DEPTH = 12
TIME_BUDGET_S = 8.0

# Control types that would matter for native positioning.
_TITLE_TYPES = {"WindowControl"}
_INPUT_TYPES = {"EditControl", "DocumentControl"}
_MESSAGE_TYPES = {"ListControl", "ListItemControl", "PaneControl", "GroupControl", "CustomControl"}


def _control_type(el) -> str:
    try:
        return str(el.ControlTypeName)
    except Exception:  # noqa: BLE001 - a vanished element must not abort the probe
        return "Unknown"


def _rect(el):
    try:
        rect = el.BoundingRectangle
    except Exception:  # noqa: BLE001
        return None
    try:
        width = int(rect.right) - int(rect.left)
        height = int(rect.bottom) - int(rect.top)
    except Exception:  # noqa: BLE001
        return None
    if width <= 0 or height <= 0:
        return None
    return {"x": int(rect.left), "y": int(rect.top), "width": width, "height": height}


def probe_window(
    hwnd: int,
    max_nodes: int = MAX_NODES,
    max_depth: int = MAX_DEPTH,
    time_budget_s: float = TIME_BUDGET_S,
) -> dict:
    """Breadth-first, bounded UIA survey of one window handle. Never raises."""
    started = time.monotonic()
    try:
        import uiautomation as auto  # imported lazily so the reader runs without UIA
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "uia-import", "errorType": type(exc).__name__}

    try:
        root = auto.ControlFromHandle(int(hwnd))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "control-from-handle", "errorType": type(exc).__name__}
    if root is None:
        return {"ok": False, "reason": "no-element"}

    deadline = started + float(time_budget_s)
    control_types: dict[str, int] = {}
    nodes = 0
    max_depth_reached = 0
    truncated = False

    title_rects: list[dict] = []
    input_rects: list[dict] = []
    message_rects: list[dict] = []
    named_title_chars = 0

    queue = [(root, 0)]
    while queue:
        if nodes >= max_nodes or time.monotonic() > deadline:
            truncated = True
            break
        element, depth = queue.pop(0)
        nodes += 1
        if depth > max_depth_reached:
            max_depth_reached = depth
        ctype = _control_type(element)
        control_types[ctype] = control_types.get(ctype, 0) + 1
        rect = _rect(element)
        if rect is not None:
            if ctype in _TITLE_TYPES:
                title_rects.append(rect)
                try:
                    named_title_chars = max(named_title_chars, len(element.Name or ""))
                except Exception:  # noqa: BLE001
                    pass
            elif ctype in _INPUT_TYPES:
                input_rects.append(rect)
            elif ctype in _MESSAGE_TYPES:
                message_rects.append(rect)
        if depth < max_depth:
            try:
                children = element.GetChildren() or []
            except Exception:  # noqa: BLE001
                children = []
            for child in children:
                queue.append((child, depth + 1))

    return {
        "ok": True,
        "nodes": nodes,
        "maxDepth": max_depth_reached,
        "truncated": truncated,
        "controlTypes": dict(sorted(control_types.items(), key=lambda item: -item[1])),
        "hasTitle": bool(title_rects),
        "titleCount": len(title_rects),
        "titleNameChars": named_title_chars,
        "hasInput": bool(input_rects),
        "inputCount": len(input_rects),
        "hasMessageRegion": bool(message_rects),
        "messageRegionCount": len(message_rects),
        "elapsedMs": int((time.monotonic() - started) * 1000),
    }


def probe_rects(hwnd: int, max_nodes: int = MAX_NODES, max_depth: int = MAX_DEPTH,
                time_budget_s: float = TIME_BUDGET_S) -> dict:
    """Like :func:`probe_window` but also returns the concrete candidate rectangles it found.

    Used only by an explicit, bounded feasibility run; the rectangles are the window's own
    physical client coordinates and are not persisted anywhere.
    """
    summary = probe_window(hwnd, max_nodes=max_nodes, max_depth=max_depth, time_budget_s=time_budget_s)
    if not summary.get("ok"):
        return summary
    try:
        import uiautomation as auto
        root = auto.ControlFromHandle(int(hwnd))
    except Exception:  # noqa: BLE001
        return summary
    if root is None:
        return summary
    deadline = time.monotonic() + float(time_budget_s)
    inputs: list[dict] = []
    lists: list[dict] = []
    edges: list[dict] = []
    queue = [(root, 0)]
    nodes = 0
    while queue and nodes < max_nodes and time.monotonic() < deadline:
        element, depth = queue.pop(0)
        nodes += 1
        ctype = _control_type(element)
        rect = _rect(element)
        if rect is not None:
            if ctype in _INPUT_TYPES:
                inputs.append({"type": ctype, **rect})
            elif ctype in ("ListControl", "ListItemControl"):
                lists.append({"type": ctype, **rect})
            elif ctype in ("PaneControl", "GroupControl"):
                edges.append({"type": ctype, **rect})
        if depth < max_depth:
            try:
                for child in element.GetChildren() or []:
                    queue.append((child, depth + 1))
            except Exception:  # noqa: BLE001
                pass
    summary["inputs"] = inputs[:5]
    summary["lists"] = lists[:5]
    summary["panes"] = edges[:8]
    return summary
