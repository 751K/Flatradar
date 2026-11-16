/* Session identity, filtering and persistence are enforced by Flask. This script
   enhances selection and dialogs; normal links/forms remain the fallback. */
(function () {
  'use strict';
  const dialog = document.getElementById('mail-filter');
  document.addEventListener('click',event => {
    if (event.target.closest('[data-open-filter]')) dialog?.showModal();
    if (event.target.closest('[data-close-filter]')) dialog?.close();
  });
  const data = document.getElementById('mail-items');
  if (!data) return;
  let items = new Map(JSON.parse(data.textContent).map(item => [item.id,item]));
  const zh = document.documentElement.lang === 'zh';
  const label = (cn,en) => zh ? cn : en;
  const escape = value => String(value ?? '').replace(/[&<>"']/g,c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function render(item) {
    const detail = document.getElementById('mail-detail');
    const backUrl = new URL(location.href); backUrl.searchParams.delete('selected');
    const sections = item.detail_sections.map(section => `<section class="mail-detail-section"><h3>${escape(section.title)}</h3><dl>${section.fields.map(field => `<div${field.long ? ' class="mail-long-field"' : ''}><dt>${escape(field.label)}</dt><dd>${escape(field.value)}</dd></div>`).join('')}</dl></section>`).join('');
    detail.innerHTML = `<a href="${escape(backUrl.href)}" class="mail-back-list" data-no-transition data-close-detail>${(document.body.classList.contains('calendar-page') || document.body.classList.contains('map-page')) ? label('← 返回房源列表','← Back to listings') : label('← 返回列表','← Back to listings')}</a><div class="mail-detail-heading"><span>${label('房源详情','Listing details')}</span>${item.url ? `<a class="mail-original-link" href="${escape(item.url)}" target="_blank" rel="noopener noreferrer">${label('原网页 ↗','Original listing ↗')}</a>` : ''}</div><div class="mail-detail-body"><h2>${escape(item.display_title || item.name)}</h2><p class="mail-location">${item.display_address ? escape(item.display_address) + ' · ' : ''}${escape(item.city)} · ${escape(item.display_source)}</p><div class="mail-price">${escape(item.price_raw || label('价格未提供','Price unavailable'))}</div><div class="mail-status">${escape(item.display_status)}${item.status_is_inferred ? `<small>${label('系统推测','Inferred')}</small>` : ''}</div>${item.price_note ? `<p class="mail-price-note">${escape(item.price_note)}</p>` : ''}${sections}</div>`;
  }
  const unreadCount = document.getElementById('inbox-unread-count');
  const feedback = document.getElementById('mail-read-feedback');
  async function markRead(link,item) {
    if (!link.form || !item.is_unread) return;
    try {
      // 10 秒超时：超时以 TimeoutError 拒绝，下面的 catch 不区分错误类型，统一提示可重试
      const response = await fetch(link.form.action, {method:'POST',body:new FormData(link.form),headers:{Accept:'application/json'},credentials:'same-origin',signal:AbortSignal.timeout(10000)});
      if (!response.ok) throw new Error('Read state was not saved');
      const result = await response.json();
      item.is_unread = false;
      link.classList.remove('is-unread');
      const unreadLabel = link.querySelector('.mail-unread-label');
      if (unreadLabel) unreadLabel.remove();
      // Responses can arrive out of order during quick selection. Within this
      // page snapshot, confirmed reads can only reduce the unread count.
      if (unreadCount) unreadCount.textContent = Math.min(Number(unreadCount.textContent),result.unread_count);
      feedback.replaceChildren();
      if (new URL(location.href).searchParams.get('view') === 'unread') feedback.textContent = label('房源已标为已读；当前列表保留刚刚查看的房源。','Marked as read. This list keeps the listing you just opened.');
    } catch {
      feedback.replaceChildren(document.createTextNode(label('详情已打开，但已读状态未保存。','Details opened, but read status was not saved.')));
      const retry = document.createElement('button');
      retry.type = 'button'; retry.textContent = label('重试','Retry');
      retry.addEventListener('click',() => markRead(link,item)); feedback.append(retry);
    }
  }
  document.addEventListener('click',event => {
    const link = event.target.closest('[data-listing-id]');
    if (!link) return;
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button) return;
    const item = items.get(link.dataset.listingId);
    if (!item) return;
    event.preventDefault(); render(item);
    document.querySelectorAll('[data-listing-id]').forEach(row => {
      const active = row === link; row.classList.toggle('selected',active);
      if (active) row.setAttribute('aria-current','true'); else row.removeAttribute('aria-current');
    });
    history.replaceState(null,'',link.dataset.selectedUrl || link.href);
    document.dispatchEvent(new CustomEvent('inbox:listing-selected'));
    const detail = document.getElementById('mail-detail');
    detail.classList.add('is-open'); detail.scrollTop = 0;
    // 日历 / 地图视图会在 inbox:listing-selected 里自己移动焦点；焦点还停在被点的行上才由这里处理
    window.syncDetailOverlay?.(detail,{focusIn: document.activeElement === link || !document.activeElement || document.activeElement === document.body});
    markRead(link,item);
  });
  // New calendar fragments carry fresh, server-scoped items. Delegated
  // selection/filter handlers continue to work after the agenda is replaced.
  function markInitialSelection() {
    const initial = document.querySelector('[data-listing-id][aria-current="true"]');
    const item = initial && items.get(initial.dataset.listingId);
    if (item) markRead(initial,item);
  }
  document.addEventListener('inbox:items-updated',event => {
    items = new Map(event.detail.items.map(item => [item.id,item]));
    markInitialSelection();
  });
  markInitialSelection();
  document.addEventListener('click',event => {
    if (!event.target.closest('[data-close-detail]')) return;
    event.preventDefault();
    const detail = document.getElementById('mail-detail');
    detail?.classList.remove('is-open');
    window.syncDetailOverlay?.(detail,{focusRow: true});
    const url = new URL(location.href); url.searchParams.delete('selected');
    history.replaceState(null,'',url);
    document.dispatchEvent(new CustomEvent('inbox:detail-closed'));
  });
})();
