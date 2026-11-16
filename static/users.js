// Admin user list: select a user in place, drag rows to change booking priority,
// send a test notification and confirm deletion.
(() => {
  const grid = document.getElementById('user-grid');
  const detail = document.querySelector('.account-detail');
  if (!grid || !detail) return;
  const zh = document.documentElement.lang === 'zh';
  const label = (cn, en) => zh ? cn : en;
  const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, ch =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[ch]);

  function select(id, open = true) {
    detail.querySelectorAll('[data-user-panel]').forEach(p => { p.hidden = p.dataset.userPanel !== id; });
    grid.querySelectorAll('[data-user-row]').forEach(row => {
      const active = row.dataset.userId === id;
      row.classList.toggle('selected', active);
      if (active) row.setAttribute('aria-current', 'true'); else row.removeAttribute('aria-current');
    });
    detail.classList.toggle('is-open', open);
    detail.scrollTop = 0;
    const url = new URL(location.href);
    if (open) url.searchParams.set('selected', id); else url.searchParams.delete('selected');
    history.replaceState(null, '', url);
  }

  grid.addEventListener('click', event => {
    const row = event.target.closest('[data-user-row]');
    if (!row || event.metaKey || event.ctrlKey || event.shiftKey) return;
    event.preventDefault();
    select(row.dataset.userId);
    window.syncDetailOverlay?.(detail, {focusIn: true});
  });
  detail.addEventListener('click', event => {
    if (!event.target.closest('[data-close-detail]')) return;
    event.preventDefault();
    detail.classList.remove('is-open');
    const url = new URL(location.href);
    url.searchParams.delete('selected');
    history.replaceState(null, '', url);
    window.syncDetailOverlay?.(detail);
    grid.querySelector('[data-user-row][aria-current="true"]')?.focus({preventScroll:true});
  });

  // Search filters the list in place; priority numbers stay those of the full list
  const search = document.querySelector('[data-user-search]');
  const noMatch = document.querySelector('[data-user-no-match]');
  search?.addEventListener('input', () => {
    const query = search.value.trim().toLowerCase();
    let shown = 0;
    grid.querySelectorAll('[data-user-row]').forEach(row => {
      row.hidden = !!query && !row.dataset.search.includes(query);
      if (!row.hidden) shown += 1;
    });
    if (noMatch) noMatch.hidden = shown > 0;
  });

  // Drag rows to reorder; the list order is the booking priority. While dragging,
  // a grey line marks where the row will land; rows only move on drop, and the
  // ones in between slide into place (FLIP: measure, move, animate back).
  let dragged = null;
  let dropBefore = undefined;   // row to insert before; null = end of list; undefined = no change
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const rowsNow = () => [...grid.querySelectorAll('[data-user-row]')];
  const marker = document.createElement('div');
  marker.className = 'user-drop-line';
  marker.setAttribute('aria-hidden', 'true');
  marker.hidden = true;
  grid.appendChild(marker);

  function renumber() { rowsNow().forEach((row, i) => { row.querySelector('.user-rank').textContent = i + 1; }); }
  function slide(move) {
    const rows = rowsNow();
    const before = new Map(rows.map(row => [row, row.getBoundingClientRect().top]));
    move();
    renumber();
    if (reduceMotion) return;
    rows.forEach(row => {
      const dy = before.get(row) - row.getBoundingClientRect().top;
      if (dy) row.animate([{transform: `translateY(${dy}px)`}, {transform: 'translateY(0)'}],
                          {duration: 220, easing: 'cubic-bezier(.2,.8,.2,1)'});
    });
  }
  function hideMarker() { marker.hidden = true; dropBefore = undefined; }
  function showMarker(before) {
    // Dropping right above or below itself changes nothing: no line
    if (before === dragged || before === nextRow(dragged)) { hideMarker(); return; }
    dropBefore = before;
    const rows = rowsNow();
    const anchor = before || rows[rows.length - 1];
    const gap = 1.5;   // rows are 3px apart; put the line in the middle of that gap
    const y = before ? anchor.offsetTop - gap : anchor.offsetTop + anchor.offsetHeight + gap;
    marker.style.top = `${y - 1}px`;
    marker.hidden = false;
  }
  function nextRow(row) {
    let next = row.nextElementSibling;
    while (next && !next.matches('[data-user-row]')) next = next.nextElementSibling;
    return next;
  }

  grid.addEventListener('dragstart', event => {
    dragged = event.target.closest('[data-user-row]');
    if (!dragged) return;
    // Reordering a filtered list would hide where the row lands
    if (search?.value.trim()) { event.preventDefault(); dragged = null; return; }
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', dragged.dataset.userId);
    // Dim the source after the browser has taken its drag image
    requestAnimationFrame(() => dragged?.classList.add('dragging'));
  });
  grid.addEventListener('dragover', event => {
    if (!dragged) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    const over = event.target.closest('[data-user-row]');
    if (!over) {
      // Below the last row: drop at the end
      const rows = rowsNow();
      const last = rows[rows.length - 1];
      if (last && event.clientY > last.getBoundingClientRect().bottom) showMarker(null);
      return;
    }
    const rect = over.getBoundingClientRect();
    showMarker(event.clientY < rect.top + rect.height / 2 ? over : nextRow(over));
  });
  grid.addEventListener('dragleave', event => {
    if (!grid.contains(event.relatedTarget)) hideMarker();
  });
  grid.addEventListener('drop', event => {
    if (!dragged) return;
    event.preventDefault();
    const before = dropBefore;
    hideMarker();
    if (before === undefined) return;
    const row = dragged;
    const previous = rowsNow();
    slide(() => grid.insertBefore(row, before || marker));
    save(previous);
  });
  grid.addEventListener('dragend', () => {
    hideMarker();
    dragged?.classList.remove('dragging');
    dragged = null;
  });

  // 列表在保存前就已按新顺序显示。保存失败（网络错误、服务端拒绝）时要把顺序
  // 换回去并提示，否则管理员看到的抢房优先级和服务端存的不一致。
  // 连续拖动时只处理最后一次保存的结果，避免旧请求的失败把新顺序回滚掉。
  let saveSeq = 0;
  function save(previous) {
    const seq = ++saveSeq;
    fetch('/api/users/reorder', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      body: JSON.stringify({order: rowsNow().map(row => row.dataset.userId)}),
    }).then(r => r.json().catch(() => ({ok: false}))).then(d => {
      if (seq !== saveSeq) return;
      if (d.ok) { resultToast(true, label('优先级已更新', 'Priority updated')); return; }
      throw new Error(d.error || 'reorder rejected');
    }).catch(() => {
      if (seq !== saveSeq) return;
      slide(() => previous.forEach(row => grid.insertBefore(row, marker)));
      resultToast(false, label('优先级没有保存，已恢复原来的顺序', 'Priority was not saved; the previous order is restored'));
    });
  }

  // Test notification: result appears under the button
  detail.addEventListener('click', event => {
    const button = event.target.closest('.js-test-notify');
    if (!button) return;
    const box = button.closest('[data-user-panel]').querySelector('[data-test-result]');
    const original = button.textContent;
    button.disabled = true;
    button.textContent = label('发送中…', 'Sending…');
    box.hidden = true;
    fetch(`/users/${encodeURIComponent(button.dataset.userId)}/test`, {method: 'POST', headers: {'X-CSRF-Token': csrf}})
      .then(r => r.json())
      .then(data => {
        const results = data.results || [];
        box.innerHTML = data.error && !results.length ? escape(data.error)
          : `${escape(label('测试结果', 'Test result'))}<ul>${results.map(r =>
              `<li>${escape(r.channel)}：${r.ok ? escape(label('已发送', 'sent')) : escape(label('失败', 'failed')) + (r.error ? ' — ' + escape(r.error) : '')}</li>`).join('')}</ul>`;
      })
      .catch(error => { box.textContent = label('请求失败：', 'Request failed: ') + error.message; })
      .finally(() => { box.hidden = false; button.disabled = false; button.textContent = original; });
  });

  // Delete confirmation
  const dialog = document.getElementById('deleteModal');
  detail.addEventListener('click', event => {
    const button = event.target.closest('.js-delete-user');
    if (!button || !dialog) return;
    document.getElementById('deleteUserName').textContent = button.dataset.userName || '';
    document.getElementById('deleteForm').action = `/users/${encodeURIComponent(button.dataset.userId || '')}/delete`;
    dialog.showModal();
  });
  dialog?.addEventListener('click', event => {
    // 点遮罩关闭交给 closedby="any"（app.js 有回退）；这里只管「取消」按钮
    if (event.target.closest('[data-close-dialog]')) dialog.close();
  });
})();
