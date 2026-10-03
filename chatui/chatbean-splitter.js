/* Pointer-captured, zoom-aware QQ session-list resizing. No business state changes. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ChatBeanSplitter = api;
})(typeof window === "object" ? window : globalThis, function () {
  "use strict";
  const STORAGE_KEY = "chatbean-sidebar-width-v1";
  function limits(available) {
    available = Math.max(0, Number(available) || 0);
    const max = Math.max(0, Math.min(480, available - Math.min(360, available * .6)));
    return { min: Math.min(available < 560 ? 140 : 180, max), max };
  }
  function clamp(value, bounds) { return Math.max(bounds.min, Math.min(bounds.max, value)); }
  function preference(raw) {
    if (typeof raw !== "string" || !raw.trim()) return null;
    const value = Number(raw);
    return Number.isFinite(value) && value >= 80 && value <= 480 ? value : null;
  }
  function mount(doc, host) {
    const pane = doc.querySelector(".session-pane");
    const rail = doc.querySelector(".nav-rail");
    const app = doc.querySelector(".app-window");
    if (!pane || !rail || !app) return null;
    pane.id ||= "cbSessionPane";
    const handle = doc.createElement("div");
    handle.className = "cb-sidebar-splitter";
    handle.tabIndex = 0;
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-orientation", "vertical");
    handle.setAttribute("aria-label", "会话列表宽度");
    handle.setAttribute("aria-controls", pane.id);
    handle.setAttribute("aria-description", "左右方向键调整，Home 最窄，End 最宽，Delete 或双击恢复默认。拖动时 Escape 取消。");
    handle.title = "拖动调整会话列表宽度 · 双击恢复默认";
    pane.append(handle);
    let wanted = null, drag = null;
    try { wanted = preference(host.localStorage.getItem(STORAGE_KEY)); } catch {}
    function geometry() {
      const scale = app.offsetWidth > 0 ? app.getBoundingClientRect().width / app.offsetWidth : 1;
      return { scale: scale > 0 ? scale : 1, bounds: limits(app.offsetWidth - rail.offsetWidth) };
    }
    function currentWidth() { return pane.getBoundingClientRect().width / geometry().scale; }
    function refresh() {
      const { bounds } = geometry();
      if (wanted === null) pane.style.removeProperty("--cb-user-list-width");
      const width = Math.round(clamp(wanted === null ? currentWidth() : wanted, bounds));
      // Keep default widths responsive. Only override when explicitly chosen or clamped.
      if (wanted !== null || Math.abs(width - currentWidth()) > 1)
        pane.style.setProperty("--cb-user-list-width", width + "px");
      handle.setAttribute("aria-valuemin", String(Math.round(bounds.min)));
      handle.setAttribute("aria-valuemax", String(Math.round(bounds.max)));
      handle.setAttribute("aria-valuenow", String(width));
      handle.setAttribute("aria-valuetext", width + " 像素");
    }
    function persist() {
      try {
        if (wanted === null) host.localStorage.removeItem(STORAGE_KEY);
        else host.localStorage.setItem(STORAGE_KEY, String(Math.round(wanted)));
      } catch { /* Restricted storage must not prevent resizing. */ }
    }
    function end(cancel = false) {
      if (!drag) return;
      const previous = drag; drag = null;
      if (cancel) wanted = previous.wanted;
      doc.body.classList.remove("cb-resizing-sidebar");
      if (handle.hasPointerCapture(previous.id)) handle.releasePointerCapture(previous.id);
      refresh();
      if (!cancel) persist();
    }
    handle.addEventListener("pointerdown", event => {
      if (drag || event.button !== 0 || event.isPrimary === false) return;
      event.preventDefault(); handle.focus();
      drag = { id: event.pointerId, startX: event.clientX, width: currentWidth(), wanted,
        scale: geometry().scale };
      handle.setPointerCapture(event.pointerId);
      doc.body.classList.add("cb-resizing-sidebar");
    });
    handle.addEventListener("pointermove", event => {
      if (!drag || drag.id !== event.pointerId) return;
      wanted = clamp(drag.width + (event.clientX - drag.startX) / drag.scale, geometry().bounds);
      refresh();
    });
    handle.addEventListener("pointerup", event => { if (drag?.id === event.pointerId) end(); });
    handle.addEventListener("pointercancel", event => { if (drag?.id === event.pointerId) end(true); });
    handle.addEventListener("lostpointercapture", () => end(true));
    function reset() { end(true); wanted = null; refresh(); persist(); }
    handle.addEventListener("dblclick", reset);
    handle.addEventListener("keydown", event => {
      if (event.key === "Escape" && drag) { event.preventDefault(); end(true); return; }
      if (drag) return;
      if (event.key === "Delete") { event.preventDefault(); reset(); return; }
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const { bounds } = geometry();
      const step = event.shiftKey ? 32 : 8;
      wanted = event.key === "Home" ? bounds.min : event.key === "End" ? bounds.max :
        clamp(currentWidth() + (event.key === "ArrowRight" ? step : -step), bounds);
      refresh(); persist();
    });
    const blur = () => end(true);
    const resize = () => { end(true); refresh(); };
    host.addEventListener("blur", blur);
    host.addEventListener("resize", resize);
    const observer = new host.ResizeObserver(resize);
    observer.observe(app); observer.observe(rail);
    refresh();
    return { element: handle, refresh, destroy() {
      end(true); observer.disconnect(); host.removeEventListener("blur", blur);
      host.removeEventListener("resize", resize); handle.remove();
    } };
  }
  return Object.freeze({ mount, limits, preference, STORAGE_KEY });
});
