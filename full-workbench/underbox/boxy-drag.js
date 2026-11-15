/* Drag the collapsed boxy launcher without changing the chat window position. */
(function (root) {
  'use strict';
  function mount(handle, dock, view) {
    const KEY = 'underbox.boxy.position.v1';
    const margin = 8, threshold = 6;
    let saved = null, drag = null, suppressClick = false, suppressTimer;
    try {
      const value = JSON.parse(view.localStorage.getItem(KEY) || 'null');
      if (value && Number.isFinite(value.x) && Number.isFinite(value.y)) saved = value;
    } catch {}
    const visible = () => handle.getClientRects().length > 0;
    const clamp = (x, max) => Math.max(margin, Math.min(x, Math.max(margin, max - margin)));
    const place = (x, y) => {
      const rect = dock.getBoundingClientRect();
      const point = {x:clamp(x, view.innerWidth - rect.width), y:clamp(y, view.innerHeight - rect.height)};
      Object.assign(dock.style, {left:point.x + 'px', top:point.y + 'px', right:'auto', bottom:'auto'});
      return point;
    };
    const refresh = () => {
      if (saved && !drag && visible()) saved = place(saved.x, saved.y);
    };
    handle.addEventListener('pointerdown', event => {
      if (!event.isPrimary || event.button !== 0 || drag) return;
      const rect = dock.getBoundingClientRect();
      drag = {id:event.pointerId, startX:event.clientX, startY:event.clientY, x:rect.left, y:rect.top, moved:false};
      handle.setPointerCapture(event.pointerId);
    });
    handle.addEventListener('pointermove', event => {
      if (!drag || event.pointerId !== drag.id) return;
      const dx = event.clientX - drag.startX, dy = event.clientY - drag.startY;
      if (!drag.moved && Math.hypot(dx, dy) < threshold) return;
      drag.moved = true;
      handle.classList.add('is-dragging');
      drag.position = place(drag.x + dx, drag.y + dy);
      event.preventDefault();
    });
    const finish = event => {
      if (!drag || event.pointerId !== drag.id) return;
      const moved = drag.moved, point = drag.position;
      drag = null;
      handle.classList.remove('is-dragging');
      if (!moved) return;
      if (event.type === 'pointerup') {
        saved = point;
        try { view.localStorage.setItem(KEY, JSON.stringify(saved)); } catch {}
        // The synthetic click following a drag must not open the chat.
        suppressClick = true;
        view.clearTimeout(suppressTimer);
        suppressTimer = view.setTimeout(() => { suppressClick = false; }, 500);
      }
    };
    handle.addEventListener('pointerup', finish);
    handle.addEventListener('pointercancel', finish);
    handle.addEventListener('click', event => {
      if (!suppressClick || event.detail === 0) return;
      suppressClick = false;
      view.clearTimeout(suppressTimer);
      event.preventDefault();
      event.stopImmediatePropagation();
    }, true);
    view.addEventListener('resize', refresh);
    return {refresh};
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {mount};
  else root.BoxyDrag = {mount};
})(typeof window !== 'undefined' ? window : globalThis);
