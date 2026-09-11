(function(){
  const DATA = JSON.parse(document.getElementById('board-data').textContent);
  const ROWS = DATA.rows, STATS = DATA.stats;
  const LS_COLS = 'source-board.columns.v1';

  const fmt = (v) => (v === null || v === undefined || v === '') ? '—' : v;
  const num = (v) => (v ? Number(v).toLocaleString('zh-CN') : '0');

  // 每一列自带取值、对齐、排序键与说明。拖动的顺序就是这个数组的顺序。
  const COLUMNS = [
    {key:'name', label:'源', width:230, render:r=>`<div class="name" title="${esc(r.name)}">${esc(r.name)}</div>`},
    {key:'outcome', label:'结论', width:104, render:r=>`<span class="pill p-${r.outcome}">${esc(r.outcomeLabel)}</span>`,
      sort:r=>DATA.outcomeOrder.indexOf(r.outcome), text:r=>r.outcomeLabel},
    {key:'sourceId', label:'source_id', width:170, render:r=>`<span class="sid">${esc(r.sourceId)}</span>`, text:r=>r.sourceId},
    {key:'statusLabel', label:'状态', width:78, render:r=>`<span class="muted">${esc(r.statusLabel)}</span>`, text:r=>r.statusLabel},
    {key:'visibility', label:'可见性', width:80,
      text:r=>r.visibility,
      note:'已采集 / 未采集（active 但一轮没跑过）/ 不参与（非 active）/ 已下线（参数表已无）'},
    {key:'historyItems', label:'历史条目', width:88, align:'num',
      render:r=>`<b>${num(r.historyItems)}</b>`, sort:r=>r.historyItems,
      note:'本机 data/tagged（或 items）落盘条目数，即历史上真正产出的量'},
    {key:'sharePct', label:'贡献占比', width:82, align:'num',
      render:r=>r.sharePct ? r.sharePct.toFixed(2)+'%' : '<span class="muted">0%</span>', sort:r=>r.sharePct,
      note:'该源历史条目 / 全部源历史条目'},
    {key:'briefCount', label:'入选简报', width:78, align:'num', render:r=>num(r.briefCount), sort:r=>r.briefCount,
      note:'近若干期简报里该源被选中的条数：天天入库却从不入选，是另一个问题'},
    {key:'written', label:'入库(观测期)', width:96, align:'num', render:r=>num(r.written), sort:r=>r.written,
      note:'output/health 逐轮漏斗累计的 final 写入量'},
    {key:'raw', label:'抓到原始', width:86, align:'num', render:r=>num(r.raw), sort:r=>r.raw},
    {key:'keepRate', label:'留存率', width:74, align:'num',
      render:r=>r.raw ? r.keepRate.toFixed(1)+'%' : '<span class="muted">—</span>', sort:r=>r.keepRate,
      note:'入库 / 抓到原始。极低＝规则过严，为零且抓到过＝被卡死'},
    {key:'postFilterDrop', label:'末端损耗', width:84, align:'num', render:r=>num(r.postFilterDrop), sort:r=>r.postFilterDrop,
      note:'清洗通过却被末端吃掉的量（跨轮去重 + 富集后质量分）。有值说明问题不在抓取和筛选规则'},
    {key:'entriesPerRun', label:'每轮抓取', width:82, align:'num',
      render:r=>r.entriesPerRun === null ? '<span class="muted">—</span>' : r.entriesPerRun, sort:r=>r.entriesPerRun || 0,
      note:'抓取阶段每轮平均拿到的条目数，来自 fetch 统计：能区分「列表拿到但全文失败」和「列表就是空的」'},
    {key:'runs', label:'采集轮次', width:76, align:'num', render:r=>num(r.runs), sort:r=>r.runs,
      note:'观测期内被点名采集的次数。0＝从未跑过，而不是跑失败'},
    {key:'observedDays', label:'覆盖天数', width:76, align:'num', render:r=>num(r.observedDays), sort:r=>r.observedDays},
    {key:'dryDays', label:'断流天数', width:78, align:'num',
      render:r=>r.dryDays === null ? '<span class="muted">从未入库</span>' : r.dryDays + '天',
      sort:r=>r.dryDays === null ? 99999 : r.dryDays,
      note:'距该源最近一次入库的天数；7 天以内算还在产出'},
    {key:'lastWrittenDt', label:'最近入库', width:96, render:r=>fmt(r.lastWrittenDt), text:r=>r.lastWrittenDt},
    {key:'lastRunDt', label:'最近采集', width:96, render:r=>fmt(r.lastRunDt || r.paramLast), text:r=>r.lastRunDt},
    {key:'blockedAtLabel', label:'卡在哪一步', width:150,
      render:r=>r.blockedAt ? `<span class="trend down">${esc(r.blockedAtLabel)}</span>` : '<span class="muted">—</span>',
      sort:r=>r.blockedAt, text:r=>r.blockedAtLabel},
    {key:'signalScoreDrop', label:'信号分淘汰', width:92, align:'num', render:r=>num(r.signalScoreDrop), sort:r=>r.signalScoreDrop,
      note:'本地信号分 + 富集后质量分两项淘汰之和，这两项通常是最难发现的一整源归零原因'},
    {key:'topFetchErrorLabel', label:'抓取错误', width:170,
      render:r=>r.topFetchErrorLabel ? `<span class="trend down">${esc(r.topFetchErrorLabel)}</span>` : '<span class="muted">—</span>',
      text:r=>r.topFetchErrorLabel},
    {key:'engine', label:'抓取引擎', width:96, render:r=>fmt(r.engine), text:r=>r.engine},
    {key:'fetchMethod', label:'采集方式', width:84, text:r=>r.fetchMethod},
    {key:'tier', label:'层级', width:56, text:r=>r.tier},
    {key:'dimension', label:'分类', width:120, text:r=>r.dimension},
    {key:'format', label:'来源类型', width:80, text:r=>r.format},
    {key:'priority', label:'优先级', width:70, text:r=>r.priority},
    {key:'lookback', label:'时间窗', width:70, text:r=>r.lookback},
    {key:'paramPerDay', label:'上轮条目数', width:88, align:'num', render:r=>num(r.paramPerDay), sort:r=>r.paramPerDay,
      note:'飞书一级参数表回写的最近一轮条目数（覆盖式快照，只代表最后一轮）'},
    {key:'paramWindow', label:'上轮窗过滤', width:92, align:'num', render:r=>num(r.paramWindow), sort:r=>r.paramWindow},
    {key:'paramDedup', label:'上轮查重过滤', width:100, align:'num', render:r=>num(r.paramDedup), sort:r=>r.paramDedup},
    {key:'notes', label:'备注', width:260,
      render:r=>`<span class="muted" title="${esc(r.notes)}">${esc(r.notes || '—')}</span>`, text:r=>r.notes},
  ];
  const DEFAULT_VISIBLE = ['name','outcome','sourceId','statusLabel','visibility','historyItems','sharePct',
    'briefCount','written','raw','keepRate','runs','dryDays','lastWrittenDt','blockedAtLabel','topFetchErrorLabel',
    'fetchMethod','tier','dimension','notes'];
  const FIXED = new Set(['name']);   // 首列钉住，否则拖动时行标识会跑到视野外

  let visible = load(LS_COLS, DEFAULT_VISIBLE).filter(k => COLUMNS.some(c => c.key === k));
  // 默认列全部消失时（比如本地存了一份错配置）退回默认，别渲染一张空表
  if (!visible.length) visible = DEFAULT_VISIBLE.slice();
  let sortKey = 'historyItems', sortDir = -1;
  let outcomeFilter = '', expanded = new Set();

  function load(key, fallback){
    try { const raw = localStorage.getItem(key); return raw ? JSON.parse(raw) : fallback.slice(); }
    catch(e){ return fallback.slice(); }
  }
  function save(){ try{ localStorage.setItem(LS_COLS, JSON.stringify(visible)); }catch(e){} }
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  const colByKey = k => COLUMNS.find(c => c.key === k);

  // 分类行吸顶，列名行贴在它正下方。分类行高度随窗口宽度换行而变，只能实测。
  const tileBar = document.getElementById('tiles');
  const toolBar = document.querySelector('.toolbar');
  function syncPin(){
    // 视口还没量出来时（后台标签、打印预览、0 宽的 iframe）测到的行高是废的，直接跳过
    if (!window.innerHeight || !window.innerWidth) return;
    const tiles = Math.round(tileBar.getBoundingClientRect().height);
    const bar = Math.round(toolBar.getBoundingClientRect().height);
    const root = document.documentElement.style;
    root.setProperty('--pin-tiles', tiles + 'px');
    root.setProperty('--pin-h', (tiles + bar) + 'px');
  }
  syncPin();
  window.addEventListener('resize', syncPin);
  // 系统字体晚到时行高会变，吸顶偏移跟着量一次
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(syncPin);
  // 「列管理」弹窗遮罩会把 --pin-h 继承进 dialog，清掉免得表头在弹窗里也偏移
  document.getElementById('dlg').style.setProperty('--pin-h', '0px');

  function cellText(r, col){
    if (col.text) return col.text(r);
    const v = r[col.key];
    return (v === null || v === undefined) ? '' : String(v);
  }

  function renderHead(){
    const head = document.getElementById('head');
    head.innerHTML = '';
    visible.forEach((key, idx) => {
      const col = colByKey(key); if (!col) return;
      const th = document.createElement('th');
      th.dataset.key = key; th.dataset.idx = idx;
      if (idx === 0) th.classList.add('fixed');
      if (key === sortKey) th.classList.add('sorted');
      th.style.width = (col.width || 100) + 'px';
      th.innerHTML = `<div class="th">${idx === 0 ? '' : '<span class="grip" title="拖动调整列顺序">⠿</span>'}`
        + `<span>${esc(col.label)}</span>`
        + (key === sortKey ? `<span class="sortmark">${sortDir < 0 ? '▼' : '▲'}</span>` : '')
        + `</div>`;
      th.addEventListener('click', () => {
        if (sortKey === key) sortDir = -sortDir; else { sortKey = key; sortDir = col.align === 'num' ? -1 : 1; }
        renderHead(); renderBody();
      });
      if (idx > 0) enableDrag(th);
      head.appendChild(th);
    });
  }

    function enableDrag(th){
    th.draggable = true;
    th.addEventListener('dragstart', e => {
      e.stopPropagation();
      dragKey = th.dataset.key;
      th.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', dragKey); } catch(err){}
    });
    th.addEventListener('dragend', () => {
      th.classList.remove('dragging');
      dragKey = null;
      clearMarks();
    });
    th.addEventListener('dragover', e => {
      if (!dragKey || dragKey === th.dataset.key) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const after = dropSide(th, e.clientX);
      clearMarks();
      th.classList.add(after ? 'drop-after' : 'drop-before');
    });
    th.addEventListener('dragleave', () => th.classList.remove('drop-before','drop-after'));
    th.addEventListener('drop', e => {
      e.preventDefault(); e.stopPropagation();
      const target = th.dataset.key;
      if (!dragKey || dragKey === target) return;
      const after = dropSide(th, e.clientX);
      const key = dragKey; dragKey = null;
      reorder(key, target, after);
    });
  }

  let dragKey = null;
  // 落点按指针在表头中线的左右决定，插到目标列前面还是后面
  function dropSide(th, clientX){
    const box = th.getBoundingClientRect();
    return (clientX - box.left) > box.width / 2;
  }
  function clearMarks(){ document.querySelectorAll('#head th').forEach(x => x.classList.remove('drop-before','drop-after')); }

  function reorder(key, target, after){
    const from = visible.indexOf(key); if (from < 0) return;
    visible.splice(from, 1);
    let to = visible.indexOf(target);
    if (to < 0) to = visible.length - 1;
    visible.splice(after ? to + 1 : to, 0, key);
    if (visible[0] !== 'name') { // 首列钉死：把 name 挪回最前
      visible.splice(visible.indexOf('name'), 1);
      visible.unshift('name');
    }
    save(); renderHead(); renderBody();
  }

  function matches(r){
    const q = document.getElementById('q').value.trim().toLowerCase();
    if (q){
      const hay = [r.name, r.sourceId, r.dimension, r.tier, r.fetchMethod, r.notes, r.endpoint].join(' ').toLowerCase();
      if (hay.indexOf(q) < 0) return false;
    }
    if (outcomeFilter && r.outcome !== outcomeFilter && r.status !== outcomeFilter) return false;
    const f = (id) => document.getElementById(id).value;
    if (f('f-status') && r.status !== f('f-status')) return false;
    if (f('f-tier') && r.tier !== f('f-tier')) return false;
    if (f('f-method') && r.fetchMethod !== f('f-method')) return false;
    if (f('f-dim') && r.dimension !== f('f-dim')) return false;
    if (f('f-health') === 'problem' && !['rule_blocked','post_filter','fetch_broken','dry','never_run','degraded'].includes(r.outcome)) return false;
    if (f('f-health') === 'silent' && (r.historyItems || r.written)) return false;
    if (f('f-health') === 'contrib' && !r.historyItems) return false;
    return true;
  }

  function sortRows(rows){
    const col = colByKey(sortKey) || {key: sortKey};
    const get = col.sort || (r => r[sortKey]);
    return rows.slice().sort((a, b) => {
      const va = get(a), vb = get(b);
      if (typeof va === 'number' || typeof vb === 'number'){
        const na = Number(va) || 0, nb = Number(vb) || 0;
        return (na - nb) * sortDir;
      }
      return String(va).localeCompare(String(vb), 'zh-CN') * sortDir;
    });
  }

  function renderBody(){
    const rows = sortRows(ROWS.filter(matches));
    const body = document.getElementById('body');
    body.innerHTML = '';
    rows.forEach(r => {
      const tr = document.createElement('tr');
      tr.className = 'expandable';
      visible.forEach((key, idx) => {
        const col = colByKey(key); if (!col) return;
        const td = document.createElement('td');
        if (col.align === 'num') td.className = 'num';
        td.innerHTML = col.render ? col.render(r) : esc(cellText(r, col));
        tr.appendChild(td);
      });
      tr.addEventListener('click', () => {
        const key = r.sourceId;
        if (expanded.has(key)) expanded.delete(key); else expanded.add(key);
        renderBody();
      });
      body.appendChild(tr);
      if (expanded.has(r.sourceId)) body.appendChild(detailRow(r));
    });
    document.getElementById('hint').textContent =
      `显示 ${rows.length} / ${ROWS.length} 个源 · 有效 ${STATS.tally.effective} · 被规则卡死 ${STATS.tally.rule_blocked} · 抓取失败 ${STATS.tally.fetch_broken} · 从未采集 ${STATS.tally.never_run} · 历史条目合计 ${num(STATS.historyItems)} 条，前 5 个源占 ${STATS.topShare}%`;
  }

  function detailRow(r){
    const tr = document.createElement('tr');
    tr.className = 'detail';
    const td = document.createElement('td');
    td.colSpan = visible.length;
    const order = ['raw','per_feed_cap','missing_title_url','title_exclude_regex','missing_or_invalid_date',
      'lookback','keyword_regex','keyword_include','keyword_exclude','min_signal_score','min_quality_score',
      'min_chars','min_content_chars','min_duration_sec','typed_filter','dup_round','kept'];
    const steps = order.filter(s => r.funnel[s] !== undefined)
      .map(s => `<div class="fstep ${s === 'kept' ? 'kept' : (s === 'raw' ? '' : 'drop')}">`
        + `${esc(DATA.stageLabels[s] || s)} <b>${num(r.funnel[s])}</b>`
        + (DATA.stageAction[s] ? ` <span class="muted">${esc(DATA.stageAction[s])}</span>` : '')
        + `</div>`).join('') || '<div class="fstep">观测期内没有漏斗记录</div>';
    const errs = Object.keys(r.fetchErrors || {}).length
      ? Object.entries(r.fetchErrors).map(([k, v]) => `${esc(k)} ×${v}`).join('；') : '';
    const kv = [
      ['source_id', r.sourceId], ['飞书 record_id', r.recordId || '—'],
      ['状态', r.statusLabel + '（' + r.status + '）'], ['结论', r.outcomeLabel],
      ['层级 / 分类', [r.tier, r.dimension].filter(Boolean).join(' · ') || '—'],
      ['采集方式 / 引擎', [r.fetchMethod, r.engine].filter(Boolean).join(' · ') || '—'],
      ['时间窗', r.lookback || '—'], ['正文下限', r.minContentChars || '—'],
      ['采集端点', r.endpoint || '—'],
      ['采集轮次 / 覆盖天数', r.runs + ' / ' + r.observedDays],
      ['抓到原始 / 入库', r.raw + ' / ' + r.written],
      ['清洗通过 / 跨轮去重掉', r.cleaned + ' / ' + r.dedupDropped],
      ['末端损耗（清洗过但没入库）', r.postFilterDrop],
      ['每轮抓取条目（fetch.entries）', r.entriesPerRun === null ? '—' : r.entriesPerRun],
      ['卡在哪一步', r.blockedAtLabel || '—'],
      ['抓取错误分布', errs || '—'],
      ['上轮回写（条目/查重/时间窗）', [r.paramPerDay, r.paramDedup, r.paramWindow].join(' / ')],
      ['历史条目 / 贡献占比', r.historyItems + ' 条 · ' + r.sharePct + '%' + (r.historyRenamed ? '（按改名后的名称匹配）' : '')],
      ['入选简报（近若干期）', r.briefCount],
      ['最近入库 / 最近采集', [r.lastWrittenDt || '—', r.lastRunDt || '—'].join(' · ')],
      ['备注', r.notes || '—'],
    ].map(([k, v]) => `<div><span>${esc(k)}：</span>${esc(v)}</div>`).join('');
    td.innerHTML = `<h4>清洗漏斗（观测期累计 · 括号内为归因）</h4><div class="funnel">${steps}</div>`
      + `<h4>字段</h4><div class="kv">${kv}</div>`;
    tr.appendChild(td);
    return tr;
  }

  function renderColumnDialog(){
    const list = document.getElementById('col-list');
    list.innerHTML = '';
    COLUMNS.forEach(col => {
      const label = document.createElement('label');
      label.innerHTML = `<input type="checkbox" ${visible.includes(col.key) ? 'checked' : ''} ${FIXED.has(col.key) ? 'disabled' : ''}>`
        + `<span>${esc(col.label)}${col.note ? `<br><span class="muted" style="font-size:11px">${esc(col.note)}</span>` : ''}</span>`;
      label.querySelector('input').addEventListener('change', e => {
        if (e.target.checked){ if (!visible.includes(col.key)) visible.push(col.key); }
        else visible = visible.filter(k => k !== col.key);
        save(); renderHead(); renderBody();
      });
      list.appendChild(label);
    });
  }

  function fillSelect(id, values){
    const sel = document.getElementById(id);
    values.filter(Boolean).filter((v, i, a) => a.indexOf(v) === i).sort()
      .forEach(v => { const o = document.createElement('option'); o.value = v; o.textContent = v; sel.appendChild(o); });
  }

  function exportCSV(){
    const rows = sortRows(ROWS.filter(matches));
    const cols = visible.map(colByKey).filter(Boolean);
    const q = v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
    const lines = [cols.map(c => q(c.label)).join(',')];
    rows.forEach(r => lines.push(cols.map(c => q(c.text ? c.text(r) : r[c.key])).join(',')));
    const blob = new Blob(['﻿' + lines.join('\n')], {type: 'text/csv;charset=utf-8'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'source-board-view.csv';
    a.click();
    URL.revokeObjectURL(a.href);
  }

  // 顶部磁贴：点一下把结论/状态筛出来，再点取消
  document.getElementById('tiles').addEventListener('click', e => {
    const tile = e.target.closest('.tile'); if (!tile) return;
    const key = tile.dataset.outcome || '';
    outcomeFilter = (outcomeFilter === key) ? '' : key;
    document.querySelectorAll('.tile').forEach(t =>
      t.setAttribute('aria-pressed', String((t.dataset.outcome || '') === outcomeFilter)));
    renderBody();
  });

  ['q','f-status','f-tier','f-method','f-dim','f-health'].forEach(id =>
    document.getElementById(id).addEventListener('input', renderBody));
  document.getElementById('btn-csv').addEventListener('click', exportCSV);
  document.getElementById('btn-cols').addEventListener('click', () => { renderColumnDialog(); document.getElementById('dlg').showModal(); });
  document.getElementById('dlg-close').addEventListener('click', () => document.getElementById('dlg').close());
  document.getElementById('btn-reset').addEventListener('click', () => {
    visible = DEFAULT_VISIBLE.slice();
    sortKey = 'historyItems'; sortDir = -1;
    save(); renderHead(); renderBody();
  });

  fillSelect('f-status', ROWS.map(r => r.status));
  fillSelect('f-tier', ROWS.map(r => r.tier));
  fillSelect('f-method', ROWS.map(r => r.fetchMethod));
  fillSelect('f-dim', ROWS.map(r => r.dimension));
  document.getElementById('foot-note').textContent = ' 配置来源：' + STATS.configSource + '。';
  // 归不到现行源的条目：源改名或中途下线时必然出现，报出来才不会被当成数据缺失
  document.getElementById('residual').textContent = STATS.unattributedItems
    ? `本地条目合计 ${num(STATS.itemTotal)} 条，其中 ${num(STATS.unattributedItems)} 条归不到现行源上：源改名或已从参数表移除（条目表只存显示名，不存 source_id）。`
    : `本地条目合计 ${num(STATS.itemTotal)} 条，全部归到了现行源上。`;
  renderHead();
  renderBody();
  syncPin(); syncStuck();
})();