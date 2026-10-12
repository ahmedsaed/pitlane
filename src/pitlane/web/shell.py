"""Shared page shell for the web UI: styles, the live-state script, and form helpers.

Pages are server-rendered. Parts that change (header status, streams, source
health, replay and VOD queues, download progress) are rendered in the browser
from one state snapshot that ``/api/events`` pushes whenever it changes
(Server-Sent Events, with ``/api/state`` polling as a fallback). Forms marked
``data-ajax`` post in the background and show a toast; without JavaScript
they still work as plain forms (redirect back with a message).
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

if TYPE_CHECKING:
    from ..app import App

CSS = """
:root{--bg:#f4f5f7;--fg:#16181d;--muted:#5f6673;--card:#fff;--line:#e3e6eb;--soft:#eef0f3;--ok:#137a3c;--warn:#a35f00;--bad:#b3261e;
--accent:#d0140d;--accent-fg:#fff;--shadow:0 1px 2px rgba(0,0,0,.05),0 2px 8px rgba(0,0,0,.04);--r:10px}
:root[data-theme=dark]{--bg:#111317;--fg:#e8eaee;--muted:#9aa1ad;--card:#1a1d23;--line:#2a2e36;--soft:#22262d;--ok:#4cc27a;--warn:#e0a240;--bad:#f07068;--accent:#ff4d42;--shadow:none}
@media (prefers-color-scheme: dark){:root:not([data-theme=light]){--bg:#111317;--fg:#e8eaee;--muted:#9aa1ad;--card:#1a1d23;--line:#2a2e36;--soft:#22262d;--ok:#4cc27a;--warn:#e0a240;--bad:#f07068;--accent:#ff4d42;--shadow:none}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}[hidden]{display:none!important}
body{margin:0;font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--fg)}
a{color:inherit}a:hover{color:var(--accent)}
header.top{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);box-shadow:var(--shadow)}
.bar-top{display:flex;gap:10px 18px;align-items:center;flex-wrap:wrap;max-width:1240px;margin:auto;padding:10px 16px}
.brand{font-weight:700;font-size:17px;letter-spacing:.2px;text-decoration:none;display:flex;align-items:center;gap:8px}
.brand i{width:10px;height:22px;background:var(--accent);border-radius:2px;transform:skewX(-15deg);display:inline-block}
nav.tabs{display:flex;gap:4px;flex-wrap:wrap}nav.tabs a{text-decoration:none;padding:6px 10px;border-radius:8px;color:var(--muted);font-weight:500}
nav.tabs a.on{background:var(--soft);color:var(--fg)}nav.tabs a:hover{color:var(--fg)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-left:auto}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;background:var(--soft);font-size:12.5px;color:var(--muted);white-space:nowrap;text-decoration:none}
.chip b{color:var(--fg);font-weight:600}.chip.busy b{color:var(--accent)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);display:inline-block;flex:none}
.dot.online,.dot.done,.dot.running,.dot.matched{background:var(--ok)}.dot.degraded,.dot.queued,.dot.paused{background:var(--warn)}
.dot.offline,.dot.failed,.dot.unmatched{background:var(--bad)}.dot.running{animation:pulse 1.4s infinite}
@keyframes pulse{50%{opacity:.35}}
main{max-width:1240px;margin:auto;padding:16px}
section.card,details.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;margin:0 0 16px;box-shadow:var(--shadow)}
section.card>h2{font-size:15px;margin:0 0 10px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
section.card>h2 .sub{font-weight:400;color:var(--muted);font-size:13px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--muted);font-weight:600;font-size:12.5px}tr:last-child td{border-bottom:0}td.num{text-align:right;font-variant-numeric:tabular-nums}
.muted{color:var(--muted)}.small{font-size:12.5px}.err{color:var(--bad)}
.online{color:var(--ok)}.degraded{color:var(--warn)}.offline{color:var(--bad)}.unknown{color:var(--muted)}
button,.btn{font:inherit;font-size:13px;padding:5px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:6px}
button:hover,.btn:hover{border-color:var(--muted)}button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
button.primary:hover{filter:brightness(1.08)}button.danger{color:var(--bad)}button[disabled]{opacity:.5;cursor:default}
button.busy{opacity:.6;pointer-events:none}
input,select{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);max-width:100%}
form.inline{display:inline}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.banner{background:var(--bad);color:#fff;padding:10px 16px}
.empty{color:var(--muted);padding:14px 0;text-align:center}
.progress{height:8px;background:var(--soft);border-radius:4px;overflow:hidden;min-width:90px}
.progress>div{height:100%;background:var(--accent);transition:width .6s ease}
.progress.done>div{background:var(--ok)}.progress.paused>div{background:var(--warn)}
.stat{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.stat>div{background:var(--soft);border-radius:8px;padding:10px 12px}.stat .k{color:var(--muted);font-size:12px}.stat .v{font-size:15px;font-weight:600}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;padding:1px 8px;border-radius:999px;background:var(--soft);color:var(--muted);white-space:nowrap}
.badge.cam{background:transparent;border:1px solid var(--bad);color:var(--bad)}
#toasts{position:fixed;right:16px;bottom:16px;display:flex;flex-direction:column;gap:8px;z-index:20;max-width:min(420px,calc(100vw - 32px))}
.toast{background:var(--fg);color:var(--bg);padding:10px 14px;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.2);font-size:13.5px;animation:in .2s ease}
.toast.bad{background:var(--bad);color:#fff}@keyframes in{from{transform:translateY(8px);opacity:0}}
.live{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px}
.card-t{display:block;color:inherit;text-decoration:none;background:var(--card);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;box-shadow:var(--shadow);transition:transform .12s}
.card-t:hover{transform:translateY(-2px);color:inherit}
.card-t .img{aspect-ratio:2/3;background:var(--soft);display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:12px;text-align:center;padding:8px}
.card-t .img,.detail .poster .img{position:relative}.card-t img,.detail .poster .img img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}.card-t .t{padding:8px 10px;font-size:13px}
.card-t .t .name{font-weight:600;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.detail{display:flex;gap:20px;flex-wrap:wrap}.detail .poster{width:220px;flex:none}.detail .poster img,.detail .poster .img{width:100%;border-radius:8px;aspect-ratio:2/3;object-fit:cover;background:var(--soft);display:flex;align-items:center;justify-content:center}
.detail .info{flex:1;min-width:260px}.detail h2{font-size:22px;margin:0 0 4px}
.facts{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
form.search{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}form.search input[type=search]{flex:1;min-width:200px}
.pager{display:flex;gap:12px;justify-content:center;margin-top:16px}
details>summary{cursor:pointer;color:var(--muted)}
details.season>summary{display:flex;gap:10px;align-items:baseline;font-size:15px;color:var(--fg);list-style:none;padding:2px 0}
details.season>summary::before{content:"▸";color:var(--muted);transition:transform .15s}details.season[open]>summary::before{transform:rotate(90deg)}
details.season>summary::-webkit-details-marker{display:none}details.season .sub{color:var(--muted);font-size:13px}
.card.season{display:block}
.chans{display:flex;flex-direction:column}details.chan{border-bottom:1px solid var(--line)}details.chan:last-child{border-bottom:0}
details.chan>summary{display:grid;grid-template-columns:44px minmax(120px,1.2fr) 2fr 110px;gap:10px;align-items:center;padding:9px 4px;color:var(--fg);list-style:none}
details.chan>summary::-webkit-details-marker{display:none}details.chan>summary:hover{background:var(--soft);border-radius:6px}
details.chan[open]>summary{font-weight:500}details.chan .dots{display:flex;gap:4px;justify-content:flex-end}
details.chan>.scroll{padding:0 4px 10px 54px}
@media (max-width:640px){details.chan>summary{grid-template-columns:36px 1fr auto}details.chan .src{display:none}details.chan>.scroll{padding-left:0}}
@media (max-width:640px){header.top{position:static}.chips{margin-left:0}.detail .poster{width:140px}main{padding:12px}section.card,details.card{padding:12px}}
"""

JS = r"""
(() => {
  const $ = (s, el = document) => el.querySelector(s), $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const size = n => { if (!n) return '–'; for (const [u, d] of [['TB',1e12],['GB',1e9],['MB',1e6],['KB',1e3]]) if (n >= d) return (n/d).toFixed(n/d < 10 ? 2 : 1) + ' ' + u; return n + ' B'; };
  const pct = (a, b) => b ? Math.min(100, Math.floor(100 * a / b)) : 0;
  const eta = (left, rate) => { if (!rate || left <= 0) return ''; let s = Math.round(left / rate); const h = Math.floor(s/3600), m = Math.floor(s%3600/60); return h ? `${h} h ${m} min left` : `${m || 1} min left`; };
  const toast = (msg, bad) => { const t = document.createElement('div'); t.className = 'toast' + (bad ? ' bad' : ''); t.textContent = msg; $('#toasts').append(t); setTimeout(() => t.remove(), bad ? 7000 : 3500); };
  window.pitlane = { toast };
  let S = null;

  // ---------------------------------------------------------------- renderers
  const R = {};
  R.chips = el => {
    const c = [`<span class="chip ${S.tuners.in_use ? 'busy' : ''}" title="Plex tuner slots in use">Tuners <b>${S.tuners.in_use}/${S.tuners.count}</b></span>`];
    for (const [n, x] of Object.entries(S.connections)) c.push(x.held ? `<a class="chip" href="/" title="Freed for you; Pitlane opens no connection">${esc(n)} <b>freed</b></a>` : `<span class="chip ${x.in_use ? 'busy' : ''}" title="${esc(x.users.join(', ') || 'Connections to this provider (streams, checks, downloads)')}">${esc(n)} <b>${x.in_use}/${x.limit}</b></span>`);
    const pl = S.vod && S.vod.playing;
    if (pl && pl.active) c.push(`<span class="chip busy" title="Playing on ${esc(pl.channel_name)}${pl.video ? ' · video ' + esc(pl.video) : ''}">▶ ${esc(pl.channel)} <b>${esc(pl.title)}</b></span>`);
    if (S.vod) c.push(`<a class="chip ${S.vod.current ? 'busy' : ''}" href="/vod/downloads" title="Movies &amp; Shows downloads"><span class="dot ${S.vod.current ? 'running' : ''}"></span>${esc(S.vod.summary)}</a>`);
    el.innerHTML = c.join('');
  };
  R.banner = el => { el.hidden = !S.reload_error; el.innerHTML = S.reload_error ? '<b>Config reload rejected – the previous configuration is still active.</b><br>' + S.reload_error.map(esc).join('<br>') : ''; };
  R.streams = el => {
    if (!S.streams.length) { el.innerHTML = '<div class="empty">Nothing is playing.</div>'; return; }
    el.innerHTML = '<div class="scroll"><table><tr><th>Channel</th><th>Source</th><th>Client</th><th class="num">Sent</th><th class="num">Failovers</th></tr>' +
      S.streams.map(s => `<tr><td><span class="dot running"></span> ${esc(s.channel_name)}</td><td>${esc(s.source || '')}</td><td class="muted">${esc(s.client)}</td><td class="num">${size(s.bytes)}</td><td class="num">${s.failovers}</td></tr>`).join('') + '</table></div>';
  };
  const STATE = {unknown: 'not checked', online: 'online', degraded: 'degraded', offline: 'offline'};
  R.sources = () => {
    for (const el of $$('[data-src-dot]')) { const s = S.sources[el.dataset.srcDot]; if (s) el.className = 'dot ' + s.state; }
    for (const el of $$('[data-src]')) { const s = S.sources[el.dataset.src]; if (!s) continue;
      el.innerHTML = `<span class="dot ${esc(s.state)}"></span> <span class="${esc(s.state)}">${esc(STATE[s.state] || s.state)}</span>` + (s.latency_ms != null ? ` <span class="muted small">${s.latency_ms} ms</span>` : '');
      const e = $(`[data-src-err="${el.dataset.src}"]`); if (e) e.textContent = s.last_error || ''; }
  };
  R.channels = () => { const on = new Set(S.streams.map(s => s.channel));
    for (const el of $$('[data-ch-live]')) el.hidden = !on.has(el.dataset.chLive); };
  const until = t => t === -1 ? 'until you resume it' : 'until ' + new Date(t * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  R.conns = el => {
    el.innerHTML = Object.entries(S.connections).map(([n, x]) => {
      const head = `<div class="row" style="justify-content:space-between"><div><b>${esc(n)}</b> <span class="muted small">${x.in_use}/${x.limit} in use</span>` +
        `<div class="small ${x.held ? 'degraded' : 'muted'}">${x.held ? `Freed for you ${until(x.held)}: Pitlane opens no connection.` : (x.users.length ? 'Used by ' + x.users.map(esc).join(' · ') : 'Not in use')}</div></div>`;
      const ctl = x.held
        ? `<form class="inline" method="post" action="/ui/provider/resume" data-ajax><input type="hidden" name="provider" value="${esc(n)}"><button class="primary">Resume</button></form>`
        : `<form class="row" method="post" action="/ui/provider/free" data-ajax ${x.users.length ? `data-confirm="Stop ${esc(x.users.join(' and '))}?"` : ''}><input type="hidden" name="provider" value="${esc(n)}">` +
          `<select name="minutes"><option value="60">for 1 hour</option><option value="180">for 3 hours</option><option value="0">until I resume</option></select><button class="danger">Free the connection</button></form>`;
      return head + ctl + '</div>'; }).join('<hr style="border:0;border-top:1px solid var(--line);margin:10px 0">');
  };
  R.replays = el => {
    if (!S.replay_jobs.length) { el.innerHTML = '<div class="empty">No replay downloads.</div>'; return; }
    el.innerHTML = '<div class="scroll"><table><tr><th>Event</th><th>Session</th><th>Title</th><th>Status</th><th>Error</th><th></th></tr>' + S.replay_jobs.map(j =>
      `<tr><td>${esc(j.event_id)}</td><td>${esc(j.session_type)}</td><td>${esc(j.title)}</td><td><span class="dot ${esc(j.status)}"></span> ${esc(j.status)}</td><td class="err small">${esc(j.last_error || '')}</td>` +
      `<td>${j.status === 'failed' ? `<form class="inline" method="post" action="/ui/downloads/retry" data-ajax><input type="hidden" name="key" value="${esc(j.key)}"><button>Retry</button></form>` : ''}</td></tr>`).join('') + '</table></div>';
  };
  const ACTIONS = {queued: ['pause','cancel'], running: ['pause','cancel'], paused: ['resume','cancel'], failed: ['retry','cancel'], cancelled: ['retry']};
  const actionBtns = it => (ACTIONS[it.status] || []).map(a =>
    `<form class="inline" method="post" action="/vod/item" data-ajax><input type="hidden" name="key" value="${esc(it.key)}"><button name="action" value="${a}" class="${a === 'cancel' ? 'danger' : ''}">${a[0].toUpperCase() + a.slice(1)}</button></form>`).join(' ');
  const progress = it => { const cur = S.vod.current && S.vod.current.key === it.key ? S.vod.current : null;
    const done = cur ? cur.done : it.done, total = it.total || it.expected; const p = it.status === 'done' ? 100 : pct(done, total);
    const cls = it.status === 'done' ? 'done' : (it.status === 'paused' ? 'paused' : '');
    let note = it.status === 'done' ? size(total) : `${size(done)} of ${size(total)}`;
    if (cur && cur.rate) note += ` · ${size(cur.rate)}/s · ${eta(total - done, cur.rate)}`;
    return `<div class="progress ${cls}"><div style="width:${p}%"></div></div><div class="muted small">${note}</div>`; };
  const statusText = it => it.status === 'running' ? 'downloading' : it.status;
  R.vodStatus = el => { const v = S.vod, c = v.current;
    const cap = (u, g) => size(u) + (g ? ` of ${g} GB` : '');
    el.innerHTML = '<div class="stat">' +
      (v.playing && v.playing.active ? `<div><div class="k">On channel ${esc(v.playing.channel)}</div><div class="v">▶ ${esc(v.playing.title)}</div><div class="muted small">downloads wait while it plays</div></div>` : '') +
      `<div><div class="k">Now</div><div class="v">${c ? esc(c.title) : 'Idle'}</div><div class="muted small">${c ? `${size(c.rate)}/s · ${eta((c.total || 0) - c.done, c.rate)}` : esc(v.blocked || (v.queued ? `${v.queued} queued` : 'Nothing queued'))}</div></div>` +
      `<div><div class="k">Data used</div><div class="v">${cap(v.usage.day, v.caps.day)} today</div><div class="muted small">${cap(v.usage.month, v.caps.month)} this month</div></div>` +
      `<div><div class="k">Free space</div><div class="v">${size(v.free.movie)}</div><div class="muted small">shows ${size(v.free.episode)} · keeps ${size(v.min_free)} free</div></div>` +
      `<div><div class="k">Rules</div><div class="v small" style="font-weight:500">${esc(v.rules)}</div><div class="muted small">${esc(v.resume)}</div></div></div>` +
      (c && v.blocked ? '' : (v.blocked && v.queued ? `<p class="degraded small">Waiting: ${esc(v.blocked)}</p>` : ''));
  };
  R.vodQueue = el => { const items = S.vod.items;
    if (!items.length) { el.innerHTML = '<div class="empty">Nothing requested yet. <a href="/vod">Browse movies and shows</a>.</div>'; return; }
    let tb = $('tbody', el);
    if (!tb) { el.innerHTML = '<div class="scroll"><table><thead><tr><th>Title</th><th>Status</th><th style="width:30%">Progress</th><th>Plex</th><th></th></tr></thead><tbody></tbody></table></div>'; tb = $('tbody', el); }
    const want = items.map(it => it.key), have = new Map($$('tr[data-k]', tb).map(r => [r.dataset.k, r]));
    for (const [k, r] of have) if (!want.includes(k)) r.remove();
    items.forEach((it, i) => {
      const html = `<td>${esc(it.title)}${it.error ? `<div class="small ${it.status === 'failed' ? 'err' : 'muted'}">${esc(it.error)}</div>` : ''}</td>` +
        `<td><span class="dot ${esc(it.status)}"></span> ${esc(statusText(it))}</td><td>${progress(it)}</td>` +
        `<td>${it.match ? `<span class="dot ${esc(it.match)}"></span> ${esc(it.match)}` : ''}</td><td class="row" style="justify-content:flex-end">${actionBtns(it)}</td>`;
      let r = have.get(it.key);
      if (!r) { r = document.createElement('tr'); r.dataset.k = it.key; }
      if (r._h !== html) { r.innerHTML = html; r._h = html; }
      if (tb.children[i] !== r) tb.insertBefore(r, tb.children[i] || null);
    });
  };
  R.vodUnmatched = el => { const u = S.vod.items.filter(it => it.match === 'unmatched' || it.match === 'not found');
    el.closest('section').hidden = !u.length;
    el.innerHTML = u.map(it => `<div class="row" style="justify-content:space-between;padding:6px 0"><span>${esc(it.title)} <span class="muted small">${esc(it.match)}</span></span>` +
      (S.vod.machine_id && it.rating_key ? `<a class="btn" target="_blank" rel="noopener" href="https://app.plex.tv/desktop/#!/server/${esc(S.vod.machine_id)}/details?key=${encodeURIComponent('/library/metadata/' + it.rating_key)}">Fix Match in Plex ↗</a>` : '<span class="muted small">not in Plex yet</span>') + '</div>').join('');
  };
  const playBtn = el => { if (!S.vod.play_channel || !el.dataset.play) return '';
    const [k, v] = el.dataset.play.split('='), pl = S.vod.playing, on = pl && pl.key === el.dataset.vodItem;
    return `<form class="inline" method="post" action="/vod/play" data-ajax><input type="hidden" name="${esc(k)}" value="${esc(v)}"><button class="${on ? '' : 'primary'}" title="Watch now on channel ${esc(S.vod.play_channel)} in Plex Live TV">▶ ${on ? 'On' : 'Play on'} channel ${esc(S.vod.play_channel)}</button></form>`; };
  R.vodItem = el => { const key = el.dataset.vodItem, it = S.vod.items.find(x => x.key === key);
    if (!it || it.status === 'cancelled' || it.status === 'deleted') {
      el.innerHTML = `<div class="row">${playBtn(el)}<form class="inline" method="post" action="${esc(el.dataset.requestUrl)}" data-ajax><input type="hidden" name="id" value="${esc(el.dataset.requestId)}"><button>Download to Plex</button></form></div>`; return; }
    el.innerHTML = `<div class="row"><span class="dot ${esc(it.status)}"></span><b>${esc(statusText(it))}</b>${it.match ? `<span class="muted small">· Plex: ${esc(it.match)}</span>` : ''}</div><div style="max-width:420px;margin:6px 0">${progress(it)}</div><div class="row">${playBtn(el)}${actionBtns(it)}</div>` +
      (it.error ? `<div class="small muted">${esc(it.error)}</div>` : '');
  };
  R.vodBadges = () => { const by = new Map(S.vod.items.map(it => [it.key, it]));
    for (const el of $$('[data-vod-badge]')) { const it = by.get(el.dataset.vodBadge), active = it && !['cancelled','deleted','failed'].includes(it.status);
      el.innerHTML = it && !['deleted', 'cancelled'].includes(it.status) ? `<span class="badge"><span class="dot ${esc(it.status)}"></span>${esc(statusText(it))}${it.status === 'running' ? ' ' + pct((S.vod.current || {}).done || it.done, it.total || it.expected) + '%' : ''}</span>` : '';
      const box = $(`[data-vod-check="${el.dataset.vodBadge}"]`); if (box) { box.disabled = !!active; if (active) box.checked = false; }
      const dl = $(`[data-vod-dl="${el.dataset.vodBadge}"]`); if (dl) dl.hidden = !!active || (it && it.status === 'done'); }
  };

  const render = () => {
    if (!S) return;
    for (const el of $$('[data-r]')) { const f = R[el.dataset.r]; if (!f) continue; if (el.dataset.r.startsWith('vod') && !S.vod) continue; try { f(el); } catch (e) { console.error(e); } }
    R.sources(); R.channels();
    if (S.vod) { R.vodBadges(); for (const el of $$('[data-vod-item]')) R.vodItem(el); }
    const live = $('#live'); if (live) live.innerHTML = '<span class="dot online"></span>live';
  };

  // ---------------------------------------------------------------- state feed
  let es = null, poll = null;
  const set = s => { S = s; render(); };
  const refresh = () => fetch('/api/state', {headers: {Accept: 'application/json'}}).then(r => r.ok ? r.json() : null).then(s => s && set(s)).catch(() => {});
  const connect = () => {
    if (!window.EventSource) { poll = setInterval(refresh, 3000); refresh(); return; }
    es = new EventSource('/api/events');
    es.onmessage = e => set(JSON.parse(e.data));
    es.onerror = () => { const live = $('#live'); if (live) live.innerHTML = '<span class="dot offline"></span>reconnecting'; };
  };
  document.addEventListener('visibilitychange', () => { if (document.hidden) { es && es.close(); es = null; clearInterval(poll); } else if (!es) connect(); });

  // ---------------------------------------------------------------- forms
  document.addEventListener('submit', async ev => {
    const f = ev.target; if (!f.matches('form[data-ajax]')) return;
    ev.preventDefault();
    const btn = ev.submitter, ask = (btn && btn.dataset.confirm) || f.dataset.confirm;
    if (ask && !confirm(ask)) return;
    const body = new URLSearchParams(new FormData(f, btn));
    if (btn) btn.classList.add('busy');
    try {
      const r = await fetch((btn && btn.getAttribute('formaction')) || f.action, {method: 'POST', body, headers: {Accept: 'application/json'}});
      const j = await r.json().catch(() => ({ok: r.ok, msg: r.ok ? 'Done' : `Failed (HTTP ${r.status})`}));
      toast(j.msg, !j.ok);
      if (j.ok && f.dataset.after === 'reload') setTimeout(() => location.reload(), 400);
      if (j.ok && f.dataset.after === 'clear') $$('input[type=checkbox]', f).forEach(b => b.checked = false);
      refresh();
    } catch (e) { toast('Network error', true); }
    finally { if (btn) btn.classList.remove('busy'); }
  });
  document.addEventListener('click', ev => { const b = ev.target.closest('[data-check-all]'); if (!b) return;
    ev.preventDefault(); const boxes = $$(`input[data-season="${b.dataset.checkAll}"]:not(:disabled)`); const on = boxes.some(x => !x.checked); boxes.forEach(x => x.checked = on); });

  const msg = new URLSearchParams(location.search).get('msg');
  if (msg) { toast(msg); history.replaceState(null, '', location.pathname + location.search.replace(/([?&])msg=[^&]*&?/, '$1').replace(/[?&]$/, '')); }
  connect();
})();
"""


def wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def done(request: Request, back: str, msg: str, ok: bool = True) -> Response:
    """Answer a form post: JSON for the page script, a redirect with a message otherwise."""
    if wants_json(request):
        return JSONResponse({"ok": ok, "msg": msg}, status_code=200 if ok else 400)
    return RedirectResponse(f"{back}{'&' if '?' in back else '?'}msg={quote(msg)}", status_code=303)


FAVICON = "data:image/svg+xml," + quote(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='#16181d'/>"
    "<path d='M13 6h7l-6 20H7z' fill='#e10600'/><path d='M21 6h4l-6 20h-4z' fill='#fff'/></svg>"
)

TABS = (("overview", "/", "Overview"), ("vod", "/vod", "Movies & Shows"), ("downloads", "/vod/downloads", "Downloads"))


def page(app: "App", title: str, active: str, body: str, actions: str = "") -> HTMLResponse:
    tabs = [t for t in TABS if t[0] == "overview" or app.vod is not None]
    nav = "".join(f"<a href='{href}' class='{'on' if key == active else ''}'>{escape(label)}</a>" for key, href, label in tabs)
    return HTMLResponse(
        "<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)} · Pitlane</title><link rel=icon href='{FAVICON}'><style>{CSS}</style></head><body>"
        "<header class=top><div class=bar-top><a class=brand href='/'><i></i>Pitlane</a>"
        f"<nav class=tabs>{nav}</nav><div class=chips data-r=chips></div><span id=live class=live></span>{actions}</div></header>"
        "<div class=banner data-r=banner hidden></div>"
        f"<main>{body}</main><div id=toasts></div><script>{JS}</script></body></html>"
    )
