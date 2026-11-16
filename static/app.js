/* ===================================================================
   Holland2Stay — Frontend JS
   =================================================================== */

// Theme: applyTheme() / toggleTheme() live in templates/_theme_head.html,
// which runs in <head> before first paint. Don't redefine them here.

// ── Mobile Sidebar ────────────────────────────────────────────────
function toggleSidebar() {
  var sb = document.querySelector('.sidebar');
  var ov = document.querySelector('.sidebar-overlay');
  if(!sb) return;
  if(sb.classList.contains('open')) { closeSidebar(); return; }
  sb.classList.add('open');
  if(ov) ov.classList.add('show');
  // 窄屏下侧栏是盖住页面的模态抽屉（下面 main 已设 inert）：告诉读屏这是对话框
  if(sidebarIsMobile()) {
    sb.setAttribute('role', 'dialog');
    sb.setAttribute('aria-modal', 'true');
    sb.setAttribute('aria-label', getLang() === 'zh' ? '菜单' : 'Menu');
  }
  var toggle = document.querySelector('.nav-toggle');
  if(toggle) toggle.setAttribute('aria-expanded', 'true');
  var main = document.getElementById('main-content');
  if(main && sidebarIsMobile()) main.inert = true;
  var first = sb.querySelector('a[href], button:not([disabled])');
  if(first) first.focus();
}

// app.js 在 body 末尾加载，按钮此时已在 DOM 里（CSP 不允许内联 onclick）
document.querySelector('.nav-toggle')?.addEventListener('click', toggleSidebar);

// 普通表单的通用行为，替代内联 onsubmit：
//   data-confirm="…"        提交前 confirm()，取消则不提交
//   data-disable-on-submit  提交后禁用按钮，防重复点击
document.addEventListener('submit', function(e) {
  var form = e.target;
  if(form.dataset.confirm && !confirm(form.dataset.confirm)) { e.preventDefault(); return; }
  if(form.hasAttribute('data-disable-on-submit')) {
    var btn = form.querySelector('button');
    if(btn) btn.disabled = true;
  }
});

function sidebarIsMobile() {
  return window.matchMedia(document.body.classList.contains('mail-page') ?
    '(max-width:900px)' : '(max-width:768px)').matches;
}

function closeSidebar(restoreFocus) {
  var sb = document.querySelector('.sidebar');
  var ov = document.querySelector('.sidebar-overlay');
  var wasOpen = sb && sb.classList.contains('open');
  if(sb) {
    sb.classList.remove('open');
    sb.removeAttribute('role'); sb.removeAttribute('aria-modal'); sb.removeAttribute('aria-label');
  }
  if(ov) ov.classList.remove('show');
  var main = document.getElementById('main-content');
  if(main) main.inert = false;
  var toggle = document.querySelector('.nav-toggle');
  if(toggle) {
    toggle.setAttribute('aria-expanded', 'false');
    if(wasOpen && restoreFocus !== false && sidebarIsMobile()) toggle.focus();
  }
}

// ── Page Transitions ──────────────────────────────────────────────
// 页面间的淡入淡出由 design.css 的 @view-transition 负责（浏览器原生，不支持的
// 直接跳转）。以前这里拦截所有链接点击、把主内容设成透明再延迟 150ms 跳转：
// 从 bfcache 返回时透明状态被原样恢复，整页空白；每次导航还白等 150ms。

// ── Utility Functions ─────────────────────────────────────────────
function getLang() {
  var meta = document.querySelector('meta[name="lang"]');
  return (meta && meta.getAttribute('content')) || 'zh';
}

function escapeHtml(s) {
  return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// 外部链接只放行 http/https，其余返回 ''。URL 解析器会按浏览器规则处理
// 前导空白、tab/换行等，与服务端 app.safety.safe_external_url 对应。
function externalUrl(u) {
  if(!u) return '';
  try {
    var parsed = new URL(u, location.href);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : '';
  } catch (e) { return ''; }
}

// ── List / detail panes ───────────────────────────────────────────
// 窄屏下 .mail-detail.is-open 是盖住列表的浮层（inbox.css 里 position:absolute）。
// 浮层打开时：列表设 inert（Tab 不再走到看不见的行），焦点移到详情标题；
// 关闭时撤掉 inert，焦点回到选中的行。宽屏是并排显示，什么都不用做。
// 按计算后的 position 判断是否浮层，不在 JS 里重复写断点。
function detailIsOverlay(detail) {
  return detail.classList.contains('is-open') && /absolute|fixed/.test(getComputedStyle(detail).position);
}
function syncDetailOverlay(detail, opts) {
  if(!detail) return;
  opts = opts || {};
  var list = detail.parentElement && detail.parentElement.querySelector('.mail-list');
  var overlay = detailIsOverlay(detail);
  if(list) list.inert = overlay;
  if(overlay && opts.focusIn) {
    var heading = Array.prototype.find.call(detail.querySelectorAll('h2'), function(h){ return h.offsetParent !== null; });
    if(heading) { heading.setAttribute('tabindex', '-1'); heading.focus({preventScroll: true}); }
  }
  if(!detail.classList.contains('is-open') && opts.focusRow && list) {
    var row = list.querySelector('[aria-current="true"]');
    if(row) row.focus({preventScroll: true});
  }
}
// 旋转屏幕 / 拉宽窗口时浮层可能变成并排，inert 要跟着撤掉
window.addEventListener('resize', function(){
  document.querySelectorAll('.mail-detail').forEach(function(d){ syncDetailOverlay(d); });
});

// 可横向滚动的表格容器（tabindex=0 让键盘能滚动）：有所在区块的标题就用它命名
document.querySelectorAll('.admin-scroll[role="region"]').forEach(function(box, i) {
  var section = box.closest('section');
  var heading = section && section.querySelector('h2');
  if(!heading) return;
  if(!heading.id) heading.id = 'table-heading-' + i;
  box.removeAttribute('aria-label');
  box.setAttribute('aria-labelledby', heading.id);
});

// ── <dialog closedby="any"> 回退 ────────────────────────────────
// 支持 closedby 的浏览器：点遮罩和 Esc 都由浏览器关闭。不支持的：这里补上点遮罩。
// 用点击坐标是否落在对话框矩形外判断——以前用 event.target === dialog，点到
// 对话框自己的内边距（包括在 <pre> 里拖选文字松手）也会误关。
if(window.HTMLDialogElement && !('closedBy' in HTMLDialogElement.prototype)) {
  document.addEventListener('click', function(e) {
    var d = e.target;
    if(!(d instanceof HTMLDialogElement) || !d.open || d.getAttribute('closedby') !== 'any') return;
    var r = d.getBoundingClientRect();
    if(e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) d.close();
  });
}

// ── Toasts ────────────────────────────────────────────────────────
// 所有 toast 都走 makeToast：
// - #toast-container 是有名字的 region；朗读交给页面加载时就在的 #toast-live
//   （role="status"）。live region 必须先存在、再改内容，读屏才会播报。
// - 每条都有真正的关闭按钮；鼠标悬停或键盘焦点在里面时暂停自动消失。
// - 带链接的（房源通知）用真正的 <a>，键盘可达。
function announce(text) {
  var live = document.getElementById('toast-live');
  if(!live || !text) return;
  live.textContent = '';
  // 清空后下一帧再写，连续两条相同文字也会被重新播报
  requestAnimationFrame(function(){ live.textContent = text; });
}

function makeToast(opts) {
  var wrap = document.getElementById('toast-container');
  if(!wrap) return null;
  var zh = getLang() === 'zh';
  var toast = document.createElement('div');
  toast.className = 'toast' + (opts.ok === true ? ' toast-success' : opts.ok === false ? ' toast-error' : '');
  var title = document.createElement('div');
  title.className = 'toast-title';
  var url = externalUrl(opts.url);
  if(url) {
    var link = document.createElement('a');
    link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    link.textContent = opts.title;
    title.appendChild(link);
  } else {
    title.textContent = opts.title;
  }
  toast.appendChild(title);
  if(opts.body) {
    var body = document.createElement('div');
    body.className = 'toast-body';
    body.textContent = opts.body;
    toast.appendChild(body);
  }
  var close = document.createElement('button');
  close.type = 'button';
  close.className = 'toast-close';
  close.setAttribute('aria-label', zh ? '关闭通知' : 'Dismiss notification');
  close.innerHTML = '<span aria-hidden="true">&times;</span>';
  toast.appendChild(close);
  wrap.appendChild(toast);
  announce(opts.title + (opts.body ? '. ' + opts.body : ''));

  var timer = null, held = false;
  function remove(fromUser) {
    clearTimeout(timer);
    // 用户关掉的是当前有焦点的那条：焦点交给下一条的关闭按钮，没有就回到主内容
    if(fromUser && toast.contains(document.activeElement)) {
      var next = toast.nextElementSibling || toast.previousElementSibling;
      var target = next ? next.querySelector('.toast-close') : document.getElementById('main-content');
      if(target) { if(!next) target.setAttribute('tabindex', '-1'); target.focus({preventScroll: true}); }
    }
    toast.remove();
  }
  function schedule() {
    clearTimeout(timer);
    if(held) return;
    timer = setTimeout(function(){
      // 到点时再确认一次：焦点或鼠标还在里面就再等一轮（不只依赖 focusin/mouseenter 事件）
      if(toast.contains(document.activeElement) || toast.matches(':hover')) { schedule(); return; }
      toast.classList.add('toast-leaving');
      setTimeout(function(){
        if(held || toast.contains(document.activeElement) || toast.matches(':hover')) { toast.classList.remove('toast-leaving'); schedule(); }
        else remove(false);
      }, 250);
    }, opts.duration || 5000);
  }
  function hold(on) { held = on; if(on) { clearTimeout(timer); toast.classList.remove('toast-leaving'); } else schedule(); }
  toast.addEventListener('mouseenter', function(){ hold(true); });
  toast.addEventListener('mouseleave', function(){ hold(toast.contains(document.activeElement)); });
  toast.addEventListener('focusin', function(){ hold(true); });
  toast.addEventListener('focusout', function(e){ if(!toast.contains(e.relatedTarget)) hold(toast.matches(':hover')); });
  close.addEventListener('click', function(){ remove(true); });
  schedule();
  return toast;
}

function resultToast(ok, msg) {
  var zh = getLang() === 'zh';
  makeToast({ok: ok, title: ok ? (zh ? '成功' : 'Success') : (zh ? '失败' : 'Failed'), body: msg || '', duration: ok ? 4000 : 6000});
}

// ── Flash Dismiss ─────────────────────────────────────────────────
document.addEventListener('click', function(e) {
  var btn = e.target.closest('.alert-dismiss');
  if(btn) {
    var alert = btn.closest('.alert');
    if(!alert) return;
    // 按钮正有焦点，元素一删焦点就掉回 <body>：先交给下一条提示，没有就给主内容
    var next = alert.nextElementSibling && alert.nextElementSibling.matches('.alert')
      ? alert.nextElementSibling.querySelector('.alert-dismiss') : null;
    var main = document.getElementById('main-content');
    if(next) next.focus();
    else if(main) { main.setAttribute('tabindex', '-1'); main.focus({preventScroll: true}); }
    alert.style.opacity = '0'; alert.style.transition = 'opacity .15s';
    setTimeout(function(){ alert.remove(); }, 150);
  }
});

// ── Monitor Status ────────────────────────────────────────────────
// 把定时器引用挂模块作用域，pagehide 时能 clearInterval ——和 SSE 同样的
// 道理：活跃定时器在某些浏览器（Safari 尤甚）会阻止 bfcache。
var _monitorTimer = null;

function updateMaintenanceBanner(d) {
  var banner = document.getElementById('upstream-maintenance-banner');
  if(!banner) return;
  var maintenance = d && d.upstream_maintenance ? d.upstream_maintenance : {};
  var active = !!maintenance.active;
  banner.classList.toggle('hidden', !active);

  var since = document.getElementById('upstream-maintenance-since');
  if(!since) return;
  if(active && maintenance.since) {
    var zh = getLang() === 'zh';
    since.textContent = '· ' + (zh ? '自 ' : 'Since ') + String(maintenance.since);
    since.classList.remove('hidden');
  } else {
    since.textContent = '';
    since.classList.add('hidden');
  }
}

function updateSystemMonitorControls(d) {
  var startBtn = document.getElementById('monitor-start-btn');
  var stopBtn = document.getElementById('monitor-stop-btn');
  var restartBtn = document.getElementById('monitor-restart-btn');
  if(startBtn) startBtn.classList.toggle('hidden', !!d.running);
  if(stopBtn) stopBtn.classList.toggle('hidden', !d.running);
  if(restartBtn) restartBtn.disabled = false;
  var cell = document.getElementById('system-monitor-status-cell');
  if(cell) {
    var zh = getLang() === 'zh';
    if(d.running) {
      cell.innerHTML = '<span class="badge badge-success">' +
        (zh ? '监控运行中' : 'Monitor running') +
        (d.pid ? ' (PID ' + escapeHtml(String(d.pid)) + ')' : '') +
        '</span>';
    } else {
      cell.innerHTML = '<span class="badge badge-danger">' +
        (zh ? '系统暂停' : 'System paused') +
        '</span>';
    }
  }
}

// 每 15 秒拉一次 /api/status：平台维护横幅和系统信息页的启停按钮
// 都靠它更新。controlMonitor 点完后也会立刻调一次。
function updateMonitorBadge() {
  fetch('/api/status').then(function(r){ return r.json(); }).then(function(d){
    updateMaintenanceBanner(d);
    updateSystemMonitorControls(d);
  }).catch(function(e){ console.error('FlatRadar fetch error:', e); });
}

function controlMonitor(btn, action) {
  var lang = getLang();
  var labels = {
    start: lang === 'zh' ? '启动中...' : 'Starting...',
    stop: lang === 'zh' ? '停止中...' : 'Stopping...',
    restart: lang === 'zh' ? '重启中...' : 'Restarting...'
  };
  if(action === 'stop') {
    var stopMsg = lang === 'zh'
      ? '确定要暂停监控吗？\n\n暂停后所有用户都不会收到新房源/状态变更，自动预订也会停止。'
      : 'Pause monitoring?\n\nAll users will stop receiving new listing/status updates and auto-booking will stop.';
    if(!confirm(stopMsg)) return;
  } else if(action === 'restart') {
    var restartMsg = lang === 'zh'
      ? '确定要重启监控进程吗？\n\n这会中断本轮抓取并重新加载代码。'
      : 'Restart the monitor process?\n\nThis interrupts the current scrape and reloads code.';
    if(!confirm(restartMsg)) return;
  }
  var csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
  var orig = btn ? btn.innerHTML : '';
  if(btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner mr-1"></span>' + (labels[action] || labels.restart);
  }
  fetch('/api/monitor/' + action, {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrf },
  }).then(function(r){ return r.json().then(function(d){ d._status = r.status; return d; }); })
    .then(function(d) {
      var ok = !!d.ok;
      var msg = ok ? d.message : (d.error || (lang === 'zh' ? '未知错误' : 'Unknown error'));
      resultToast(ok, msg);
      updateMonitorBadge();
      setTimeout(updateMonitorBadge, 1500);
    })
    .catch(function() {
      resultToast(false, lang === 'zh' ? '请求失败' : 'Request failed');
    })
    .finally(function() {
      if(btn) {
        btn.disabled = false;
        btn.innerHTML = orig;
      }
    });
}

function startMonitorPoll() {
  if(_monitorTimer) return;
  updateMonitorBadge();
  _monitorTimer = setInterval(updateMonitorBadge, 15000);
}

function stopMonitorPoll() {
  if(_monitorTimer) {
    clearInterval(_monitorTimer);
    _monitorTimer = null;
  }
}

startMonitorPoll();

// ── Notification System ───────────────────────────────────────────
var _notifLastId = 0;

function showToast(n) {
  // n.url 是抓取到的第三方房源 URL：makeToast 只对 http/https 生成链接（externalUrl）
  makeToast({title: n.title, body: n.body, url: n.url, duration: 8000});
}

// SSE 从最新一条之后开始推，先取一次最新 id，免得把旧通知当新的弹出来
function loadNotifications() {
  return fetch('/api/notifications?limit=1')
    .then(function(r){ return r.json(); })
    .then(function(d) {
      if(d.ok && d.notifications.length) _notifLastId = d.notifications[0].id;
    }).catch(function(e){ console.error('FlatRadar fetch error:', e); });
}

// ── SSE ───────────────────────────────────────────────────────────
//
// bfcache 友好的 SSE 生命周期管理：
// - _sseConn       当前 EventSource 引用，方便 pagehide 时 close
// - _sseRetryTimer 失败重连的 setTimeout 引用，必须能取消——否则
//                  pagehide 后这个 timer 在隐藏页面上 fire，又新开一条
//                  EventSource，bfcache 还是被破坏。
//
// 不 close + cancel timer 的话浏览器 bfcache 拒绝缓存当前页（活跃连接 /
// pending timer 都是 bfcache 杀手），用户点"返回"就得整页重拉，单线程
// dev server 还被 SSE 占着 worker，偶尔出现空白卡死。
var _sseConn = null;
var _sseRetryTimer = null;

function connectSSE() {
  if(!window.EventSource) return;
  if(_sseConn) { try { _sseConn.close(); } catch(_){} }
  if(_sseRetryTimer) { clearTimeout(_sseRetryTimer); _sseRetryTimer = null; }
  var src = new EventSource('/api/events?last_id=' + _notifLastId);
  _sseConn = src;
  src.onmessage = function(e) {
    var items;
    try { items = JSON.parse(e.data); } catch(_) { return; }
    if(!items || !items.length) return;
    _notifLastId = items[items.length - 1].id;
    // 只对未读通知弹 toast（新标签页不会重复弹已读的）
    items.filter(function(n){ return !n.read; }).slice(0, 3).forEach(showToast);
  };
  src.onerror = function() {
    try { src.close(); } catch(_){}
    if(_sseConn === src) _sseConn = null;
    // 关键：retry timer 必须可取消。closeSSE() 会清掉它，所以 pagehide
    // 后 10s 内即使本来要重连，也不会真的 fire。
    _sseRetryTimer = setTimeout(function(){
      _sseRetryTimer = null;
      connectSSE();
    }, 10000);
  };
}

function closeSSE() {
  if(_sseConn) {
    try { _sseConn.close(); } catch(_){}
    _sseConn = null;
  }
  if(_sseRetryTimer) {
    clearTimeout(_sseRetryTimer);
    _sseRetryTimer = null;
  }
}

// ── Init ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
  // 仅 admin 加载通知 + SSE（访客不显示通知，避免无意义的 403 请求）
  if(window._isAdmin === true) {
    // Load notifications first, then connect SSE (avoids race where SSE
    // uses _notifLastId=0 before loadNotifications updates it)
    loadNotifications().then(function(){ connectSSE(); });
  }

  // bfcache 友好：导航离开页面前**关掉所有活跃连接和定时器**，浏览器才
  // 会把当前页放进 back-forward cache。Safari 对这块尤其严格——任何一
  // 个活跃 EventSource / setInterval 都让它拒绝 bfcache。
  //
  // - pagehide:  导航离开（含点返回、点链接、关 tab）
  // - pageshow:  导航回到（含从 bfcache 复原）。event.persisted=true 表
  //              示是从 bfcache 复活的，把连接和定时器恢复回来。
  function _onPageHide() {
    closeSSE();
    stopMonitorPoll();
  }
  function _onPageShow(e) {
    if(e.persisted) {
      if(window._isAdmin === true && !_sseConn) connectSSE();
      startMonitorPoll();
    }
  }
  window.addEventListener('pagehide', _onPageHide);
  window.addEventListener('pageshow', _onPageShow);

  // Close sidebar when clicking overlay
  var overlay = document.querySelector('.sidebar-overlay');
  if(overlay) overlay.addEventListener('click', closeSidebar);

  // Close sidebar on Escape
  document.addEventListener('keydown', function(e) {
    var sidebar = document.querySelector('.sidebar');
    if(!sidebar || !sidebar.classList.contains('open') || !sidebarIsMobile()) return;
    if(e.key === 'Escape') { e.preventDefault(); closeSidebar(); return; }
    if(e.key !== 'Tab') return;
    var toggle = document.querySelector('.nav-toggle');
    var controls = [toggle].concat(Array.from(sidebar.querySelectorAll(
      'a[href], button:not([disabled]), input:not([type="hidden"]):not([disabled]), select:not([disabled]), [tabindex="0"]'
    ))).filter(function(el) { return el && el.getClientRects().length; });
    var first = controls[0], last = controls[controls.length - 1];
    if(e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if(!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  window.addEventListener('resize', function() {
    if(!sidebarIsMobile()) closeSidebar(false);
    else {
      var sidebar = document.querySelector('.sidebar');
      if(sidebar && !sidebar.classList.contains('open') && sidebar.contains(document.activeElement)) {
        document.querySelector('.nav-toggle')?.focus();
      }
    }
  });

});

/* ── 平台显示名 ────────────────────────────────────────────────────
 * 原本 map / calendar / stats 各写了一份 sourceLabel，**三份都漏了
 * ourcampus**，于是它在界面上以裸的 source key 露出来（monitoring 页更
 * 直接，连转换都没有，四个平台全是小写 key）。名称由服务端平台登记表注入。
 *
 */
window.SOURCE_LABELS = window.SOURCE_LABELS || {};

/** 平台全名。认不出的 key 首字母大写后返回，而不是硬套一个默认平台名——
 *  把未知 source 显示成 "Holland2Stay" 会让人以为数据是那边来的。 */
window.sourceLabel = function (source) {
  var key = String(source || '').toLowerCase();
  if (window.SOURCE_LABELS[key]) return window.SOURCE_LABELS[key];
  return key ? key.charAt(0).toUpperCase() + key.slice(1) : '';
};

document.addEventListener('click', function (e) {
  var btn = e.target.closest('[data-onb-test]');
  if (!btn) return;

  var lang = getLang();
  var box = btn.parentElement.querySelector('.onb-test-result');
  var csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
  var orig = btn.innerHTML;

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner mr-1"></span>' +
                  (lang === 'zh' ? '发送中' : 'Sending');
  if (box) { box.hidden = true; box.innerHTML = ''; }

  fetch(btn.getAttribute('data-onb-test'), {
    method: 'POST',
    headers: { 'X-CSRF-Token': csrf },
  })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (!box) return;
      var rows = (d && d.results) || [];
      if (!rows.length) {
        box.className = 'onb-test-result onb-test-fail';
        box.textContent = (d && d.error) ||
          (lang === 'zh' ? '没有可用的接收方式' : 'No delivery route available');
      } else {
        box.className = 'onb-test-result';
        box.innerHTML = rows.map(function (x) {
          var cls = x.ok ? 'onb-test-ok' : 'onb-test-fail';
          var mark = x.ok ? '✓' : '✗';
          var tail = x.ok ? '' : ' — ' + escapeHtml(x.error || '');
          return '<div class="' + cls + '">' + mark + ' ' +
                 escapeHtml(x.channel || '') + tail + '</div>';
        }).join('');
      }
      box.hidden = false;
    })
    .catch(function () {
      if (!box) return;
      box.className = 'onb-test-result onb-test-fail';
      box.textContent = lang === 'zh' ? '请求失败' : 'Request failed';
      box.hidden = false;
    })
    .finally(function () {
      btn.disabled = false;
      btn.innerHTML = orig;
    });
});
