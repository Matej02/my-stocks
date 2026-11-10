// ====================== PŘEHLED (uvítací dashboard) ======================
let overviewLoaded = false;
let overviewLoading = false;
/* Živý stav otevřených pozic. Plán (cíl/stop/max dní) se nastaví při analýze,
   ale kontroluje se KAŽDÝ DEN proti aktuální ceně – to je podle backtestu ta
   funkční ochrana. Reagovat navíc na zeslábnutí skóre naopak škodí, proto to
   tady schválně není. */
/* Pruh rozložení portfolia – JEDINÝ renderer pro kartu pozic i ranní přehled.
   Segment hotovosti se ukáže jen u konzervativního profilu v rozkolísaném trhu. */
function allocHTML(a, withNote) {
    if (!a) return '';
    const dot = c => `<span class="alloc-dot" style="background:${c}"></span>`;
    const cash = a.cash_pct > 0;
    return `<div class="alloc" style="margin-top:6px">
        <div class="alloc-bar">
          <i class="a" style="width:${a.active_pct}%"></i>
          <i class="c" style="width:${a.core_pct}%"></i>
          ${cash ? `<i class="h" style="width:${a.cash_pct}%"></i>` : ''}
        </div>
        <div class="alloc-leg">
          <span>${dot('var(--accent)')}Signály <b>${a.active_pct} %</b> (${a.slots_used}/${a.slots_max} pozic po ${a.per_position_pct} %)</span>
          <span>${dot('rgba(255,255,255,0.18)')}Indexové jádro <b>${a.core_pct} %</b></span>
          ${cash ? `<span>${dot('var(--amber)')}Hotovost <b>${a.cash_pct} %</b></span>` : ''}
        </div>
        ${withNote && a.note ? `<div class="alloc-note">${escapeHtml(a.note)}</div>` : ''}
      </div>`;
}

async function fetchOverviewPositions() {
    const wrap = document.getElementById('ov-positions-wrap');
    if (!wrap) return;
    try {
        const r = await fetch(`${BACKEND_URL}/api/positions?risk=${encodeURIComponent(investorRisk)}`, { headers: authHeaders() });
        const d = await r.json();
        if (!d.ok) { wrap.style.display = 'none'; return; }
        const tag = {target:'Cíl', stop:'Stop', expired:'Horizont', open:'Drží se'};
        document.getElementById('ov-positions').innerHTML = d.items.map(i => {
            const col = (i.return_pct || 0) >= 0 ? 'var(--up)' : 'var(--down)';
            return `<div class="pos-row">
                <span class="pos-t">${escapeHtml(i.ticker)}</span>
                <span class="pos-tag ${i.status}">${tag[i.status] || i.status}</span>
                <span class="pos-lbl">${escapeHtml(i.label || '')} · ${i.days_held} dní</span>
                <span class="pos-ret" style="color:${col}">${(i.return_pct||0) >= 0 ? '+' : ''}${i.return_pct}%</span>
            </div>`;
        }).join('');
        document.getElementById('ov-pos-badge').textContent =
            d.action_count ? `${d.action_count} vyžaduje akci` : '';
        const a = d.allocation;
        document.getElementById('ov-alloc').innerHTML = allocHTML(a, true);
        wrap.style.display = '';
    } catch (e) {
        wrap.style.display = 'none';
    }
}

async function loadOverview(force) {
    if (overviewLoading) return;
    if (overviewLoaded && !force) { renderOverviewPortfolio(); return; }
    overviewLoading = true;
    renderOverviewGreeting();
    // Spuštění paralelně
    const tasks = [
        fetchOverviewMacro(),
        fetchOverviewSignal(),
        fetchOverviewNews(),
        fetchOverviewOpps(),
        fetchOverviewBasket(),
        fetchOverviewSectors(),
        fetchOverviewAlerts(),
        fetchOverviewPerf(),
        fetchOverviewPositions(),
    ];
    renderOverviewPortfolio();
    renderOverviewEvents();
    await Promise.allSettled(tasks);
    overviewLoading = false;
    overviewLoaded = true;
}

function renderOverviewGreeting() {
    try {
        const h = new Date().getHours();
        let g = 'Dobrý den';
        if (h < 11) g = 'Dobré ráno';
        else if (h < 17) g = 'Dobrý den';
        else if (h < 22) g = 'Dobrý večer';
        else g = 'Dobrou noc';
        const name = myNameVoc || myName || '';
        const el = document.getElementById('ov-greet');
        if (el) el.innerHTML = `${g}${name ? ', <em>' + escapeHtml(name) + '</em>' : ''}.`;
        const sub = document.getElementById('ov-greet-sub');
        if (sub) {
            const today = new Date().toLocaleDateString('cs-CZ', {weekday:'long', day:'numeric', month:'long'});
            sub.textContent = `Dnes je ${today}. Tady je tvůj přehled.`;
        }
    } catch(e) {}
}

async function fetchOverviewMacro() {
    const box = document.getElementById('ov-macro');
    if (!box) return;
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/macro`)).json();
        if (!d.ok || !d.items || !d.items.length) {
            box.innerHTML = '<div class="ov-empty">Makro data se teď nepodařilo načíst.</div>';
            return;
        }
        box.innerHTML = d.items.map(m => {
            const chgCls = m.chg_pct >= 0 ? 'up' : 'down';
            const chgTxt = (m.chg_pct >= 0 ? '+' : '') + m.chg_pct.toFixed(2) + '%';
            let valFmt;
            if (m.label === 'Bitcoin') valFmt = '$' + Math.round(m.price).toLocaleString('en-US');
            else if (m.label === 'US10Y') valFmt = m.price.toFixed(2) + '%';
            else if (m.label === 'Ropa') valFmt = '$' + m.price.toFixed(2);
            else valFmt = m.price.toFixed(2);
            return `<div class="macro-pill ${m.regime}">
                <span class="lbl">${escapeHtml(m.label)}</span>
                <span class="val">${valFmt}</span>
                <span class="chg ${chgCls}">${chgTxt}</span>
            </div>`;
        }).join('');
    } catch(e) {
        box.innerHTML = '<div class="ov-empty">Makro snímek nedostupný.</div>';
    }
}

// Přehled: první tip z koše (který ještě nemáš) + další tři. Stejná data jako Příležitosti a Moje portfolio.
function picksNotHeld(d) {
    const held = new Set(picksHeldRows().map(r => String(r.ticker).replace('.', '-')));
    const all = ((d.picks && d.picks.basket) || []).concat(d.more || []);
    return all.filter(p => !held.has(p.ticker));
}
async function fetchOverviewSignal() {
    const box = document.getElementById('ov-top-signal');
    if (!box) return;
    if (!hasFeature('pro')) {
        box.innerHTML = `<div class="ss-tag"><i class="ti ti-lock"></i> Top signál dne</div>
            <div style="font-size:15px;line-height:1.5;color:var(--text);margin-bottom:14px;">Konkrétní top nákupní signál dnes — odemkneme ho v plánu <b>Pro</b>.</div>
            <button class="ss-cta" onclick="openProfile('plans')"><i class="ti ti-rocket"></i> Vybrat plán Pro</button>`;
        return;
    }
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/signals`)).json();
        const list = (d && d.results) || [];
        const top = list.find(o => o.conviction === 'top') || list[0];
        if (!top) {
            box.innerHTML = `<div class="ss-tag"><i class="ti ti-zzz"></i> Top signál dne</div>
                <div style="font-size:15px;line-height:1.5;color:var(--text);margin-bottom:6px;">Dnes <b>žádný titul nesplňuje</b> náš přísný nákupní signál.</div>
                <div style="color:var(--text2);font-size:13px;margin-bottom:14px;">To je v pořádku — model nakupuje jen ověřené situace, ne pořád. Mrkni sem zítra.</div>
                <button class="ss-cta" onclick="switchTab('opportunities')"><i class="ti ti-eye"></i> Mrkni na příležitosti</button>`;
            return;
        }
        const isTop = top.conviction === 'top';
        const chgTxt = (top.change_pct >= 0 ? '+' : '') + (top.change_pct || 0).toFixed(2) + '%';
        const chgCol = top.change_pct >= 0 ? 'var(--up)' : 'var(--down)';
        box.innerHTML = `
            <div class="ss-tag">${isTop ? '⭐ TOP signál dne' : 'Nejlepší signál dne'}</div>
            <div class="ss-ticker">${escapeHtml(top.ticker)}</div>
            <div class="ss-name">${escapeHtml(niceName(top.ticker, top.name))} · <span style="color:${chgCol}">${chgTxt} dnes</span></div>
            <div class="ss-row">
                <div class="it"><span class="l">Cena</span><span class="v">${(top.price || 0).toFixed(2)} ${escapeHtml(top.currency || '')}</span></div>
                <div class="it"><span class="l">MA skóre</span><span class="v g">${top.score}</span></div>
                <div class="it"><span class="l">Cíl</span><span class="v g">+${top.reward_pct ?? '?'}%</span></div>
                <div class="it"><span class="l">Stop-loss</span><span class="v">−${top.risk_pct ?? '?'}%</span></div>
                <div class="it"><span class="l">Zisk:riziko</span><span class="v">${top.rr ?? '—'} : 1</span></div>
            </div>
            <button class="ss-cta" onclick="loadStockDetail('${top.ticker}')"><i class="ti ti-zoom-scan"></i> Otevřít hloubkovou analýzu</button>`;
    } catch(e) {
        box.innerHTML = '<div class="ss-tag">Top signál dne</div><div class="ov-empty">Skener teď není dostupný.</div>';
    }
}

function renderOverviewPortfolio() {
    const list = lastWatchlistData || [];
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    set('ov-port-count', list.length || '0');
    const ups = list.filter(s => (s.change_pct || 0) > 0).length;
    const downs = list.filter(s => (s.change_pct || 0) < 0).length;
    set('ov-port-up', ups);
    set('ov-port-down', downs);
}

// Tickery pro Přehled: nejdřív to, co držím (Moje portfolio), pak co jen sleduji. Bez futures a indexů (=F, ^) –
// u nich žádné výsledky ani zprávy o firmě nejsou.
function ovTickers(max) {
    const held = (typeof planLots !== 'undefined' ? planLots : []).map(l => l.ticker);
    const all = held.concat(myStocks || []).map(t => String(t || '').toUpperCase()).filter(t => t && !/[=^]/.test(t));
    return [...new Set(all)].slice(0, max || 30);
}

async function renderOverviewEvents() {
    const box = document.getElementById('ov-events');
    if (!box) return;
    try {
        const tk = ovTickers(30).join(',');
        const d = await (await fetch(`${BACKEND_URL}/api/calendar?days=10&tickers=${encodeURIComponent(tk)}`)).json();
        const items = (d && d.items) || [];
        if (!items.length) {
            box.innerHTML = '<div class="ov-empty">Tento týden žádné klíčové události.</div>';
            return;
        }
        const dayNames = ['NE','PO','ÚT','ST','ČT','PÁ','SO'];
        box.innerHTML = items.slice(0, 6).map(ev => {
            const dt = new Date(ev.date + 'T12:00:00Z');
            const wd = dayNames[dt.getUTCDay()];
            const dnum = dt.getUTCDate();
            const isEarn = ev.kind === 'earnings';
            const tag = isEarn ? '<span class="tag earn">Earnings</span>' : '<span class="tag macro">Makro</span>';
            const click = isEarn ? `onclick="loadStockDetail('${ev.ticker}')"` : '';
            const label = isEarn
                ? `<b>${escapeHtml(ev.ticker)}</b> · výsledky hospodaření`
                : `<b>${escapeHtml(ev.name)}</b>`;
            return `<div class="ov-event" ${click} style="cursor:${isEarn?'pointer':'default'};">
                <div class="day">${wd}<span class="d">${dnum}</span></div>
                <div class="name">${label}</div>
                ${tag}
            </div>`;
        }).join('');
    } catch(e) {
        box.innerHTML = '<div class="ov-empty">Kalendář teď nelze načíst.</div>';
    }
}

async function fetchOverviewOpps() {
    const box = document.getElementById('ov-opps');
    if (!box) return;
    if (!hasFeature('pro')) {
        box.innerHTML = '<div class="ov-empty">Skener příležitostí je k dispozici v plánu <b>Pro</b>.</div>';
        return;
    }
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/opportunities`)).json();
        const list = (d && d.results) || [];
        if (!list.length) {
            box.innerHTML = '<div class="ov-empty">Skener zrovna nenašel žádné další příležitosti.</div>';
            return;
        }
        box.innerHTML = `<div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:8px;">${list.slice(0, 4).map(o => `
            <div class="ov-opp-row" onclick="loadStockDetail('${o.ticker}')">
                <div class="l">
                    <span class="t">${escapeHtml(o.ticker)}</span>
                    <span class="n">${escapeHtml(niceName(o.ticker, o.name))}</span>
                </div>
                <div class="r">
                    <span class="s">${o.score ?? '?'}</span>
                    <span class="h">#${o.vm_rank ?? '?'}${o.pe ? ' · P/E ' + o.pe : ''}</span>
                </div>
            </div>`).join('')}</div>`;
    } catch(e) {
        box.innerHTML = '<div class="ov-empty">Skener teď není dostupný.</div>';
    }
}

// Přehled: koš tipů (měsíční, dlouhodobě) – první tipy, které ještě nemáš. Stejná data jako Příležitosti a Moje portfolio.
async function fetchOverviewBasket() {
    const box = document.getElementById('ov-basket');
    if (!box) return;
    if (!hasFeature('pro')) { box.innerHTML = '<div class="ov-empty">Koš tipů je k dispozici v plánu <b>Pro</b>.</div>'; return; }
    try {
        const d = await fetchPicksData(planPicksN);
        if (!d || !d.ok || !d.picks) { box.innerHTML = `<div class="ov-empty">${d && d.scanning ? 'Ranní sken ještě běží.' : 'Koš tipů teď není dostupný.'}</div>`; return; }
        const list = picksNotHeld(d).slice(0, 5);
        if (!list.length) { box.innerHTML = '<div class="ov-empty">Všechny tipy z koše už máš. Koš se mění jednou měsíčně.</div>'; return; }
        const month = d.picks.month ? MP_MONTH_CZ[parseInt(d.picks.month.slice(5, 7), 10) - 1] : '';
        box.innerHTML = `<div style="color:var(--text2);font-size:13px;line-height:1.5;margin:0 0 10px">Nejsilnější akce S&amp;P 500 na ${month} (ty, které ještě nemáš). <b>Kupuj celý koš, ne jednu akcii.</b></div>
            <div style="display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:8px;">${list.map(o => `
            <div class="ov-opp-row" onclick="loadStockDetail('${mpEsc(o.ticker)}')">
                <div class="l"><span class="t">${escapeHtml(mpBase(o.ticker))}</span><span class="n">${escapeHtml(o.name || '')}</span></div>
                <div class="r"><span class="h">${o.source === 'vm' ? 'Levná + roste' : 'Silné momentum'}</span></div>
            </div>`).join('')}</div>`;
    } catch (e) { box.innerHTML = '<div class="ov-empty">Koš tipů teď není dostupný.</div>'; }
}

let _pfpDays = 30;
let _pfpChart = null;
function setPfpRange(days) {
    _pfpDays = days;
    document.querySelectorAll('.pfp-r').forEach(b => b.classList.toggle('active', parseInt(b.dataset.days) === days));
    fetchOverviewPerf(true);
}

async function fetchOverviewPerf(force) {
    const stats = document.getElementById('pfp-stats');
    const chartEl = document.getElementById('pfp-chart');
    if (!chartEl) return;
    // Má-li uživatel portfolio, počítá se výkon JEHO akcií; jinak jen sledovaných.
    const held = (typeof planLots !== 'undefined' ? planLots : []).map(l => String(l.ticker || '').toUpperCase()).filter(t => t && !/[=^]/.test(t));
    const isPf = held.length > 0, who = isPf ? 'portfolia' : 'watchlistu';
    const tk = (isPf ? [...new Set(held)] : (myStocks || [])).slice(0, 40).join(',');
    const setTxt = (id, t) => { const el = document.getElementById(id); if (el) el.innerHTML = t; };
    setTxt('pfp-title', `Výkon tvého ${who} vs S&amp;P 500`);
    setTxt('pfp-fine', isPf
        ? 'Rovnoměrný index z akcií, které máš v Moje portfolio (velikosti pozic nepočítám). SPY = S&amp;P 500 ETF jako srovnání. Alfa = <b>o kolik % tvůj výběr překonal trh</b>.'
        : 'Rovnoměrný index z akcií, které sleduješ. Nahraj si portfolio a uvidíš výkon svých akcií. SPY = S&amp;P 500 ETF jako srovnání. Alfa = <b>o kolik % tvůj výběr překonal trh</b>.');
    if (!tk) {
        stats.innerHTML = '';
        chartEl.innerHTML = '<div class="ov-empty">Nahraj si portfolio (Moje portfolio) nebo přidej pár akcií do sledování – pak tady uvidíš, jak si vedou proti S&P 500.</div>';
        return;
    }
    if (!force) chartEl.innerHTML = `<div class="ov-load"><i class="ti ti-loader spin"></i> Počítám výkon ${who} vs S&P 500…</div>`;
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/portfolio-history?tickers=${encodeURIComponent(tk)}&days=${_pfpDays}`)).json();
        if (!d.ok || !d.series || !d.series.length) {
            stats.innerHTML = '';
            chartEl.innerHTML = '<div class="ov-empty">Nedostatek historických dat pro tyto akcie.</div>';
            return;
        }
        const yours = d.final_change_pct ?? 0;
        const spy = d.baseline_change_pct ?? 0;
        const alpha = d.alpha_pct ?? 0;
        const cls = v => v > 0 ? 'up' : (v < 0 ? 'down' : '');
        const sign = v => (v > 0 ? '+' : '') + v.toFixed(2) + '%';
        const heroCls = alpha >= 0 ? 'hero' : '';
        stats.innerHTML = `
            <div class="pfp-stat ${heroCls}">
                <div class="l">Alfa vs. S&P 500</div>
                <div class="v ${alpha >= 0 ? 'up' : 'down'}">${sign(alpha)}</div>
            </div>
            <div class="pfp-stat">
                <div class="l">Tvé ${isPf ? 'portfolio' : 'sledované'}</div>
                <div class="v ${cls(yours)}">${sign(yours)}</div>
            </div>
            <div class="pfp-stat">
                <div class="l">S&P 500 (SPY)</div>
                <div class="v ${cls(spy)}">${sign(spy)}</div>
            </div>
            <div class="pfp-stat">
                <div class="l">Období</div>
                <div class="v" style="font-size:15px;">${_pfpDays} dní · ${d.tickers_used} tickerů</div>
            </div>`;

        // Zničit předchozí graf
        if (_pfpChart) { try { _pfpChart.destroy(); } catch(e){} _pfpChart = null; }
        chartEl.innerHTML = '';

        const opts = {
            chart: { type: 'area', height: 280, background: 'transparent', toolbar: { show: false },
                     zoom: { enabled: false, allowMouseWheelZoom: false },
                     animations: { enabled: false }, foreColor: '#8a8f99' },
            series: [
                { name: isPf ? 'Tvé portfolio' : 'Tvé sledované', data: d.series },
                { name: 'S&P 500', data: d.baseline },
            ],
            xaxis: { categories: d.labels, tickAmount: 6, labels: { style: { fontFamily: 'Outfit', fontSize: '11px' } } },
            yaxis: { labels: { formatter: v => v.toFixed(0), style: { fontFamily: 'Outfit', fontSize: '11px' } } },
            stroke: { curve: 'smooth', width: [3, 2] },
            colors: [alpha >= 0 ? '#00E676' : '#FF3D00', '#4fa3ff'],
            fill: { type: 'gradient', gradient: { shadeIntensity: 1, opacityFrom: 0.35, opacityTo: 0.05, stops: [0, 100] } },
            grid: { borderColor: '#1e2028', strokeDashArray: 3 },
            legend: { fontFamily: 'Outfit', fontSize: '12px', markers: { radius: 3 } },
            tooltip: {
                theme: 'dark',
                shared: true,
                y: { formatter: v => v.toFixed(2) + ' (base 100)' },
            },
            dataLabels: { enabled: false },
        };
        _pfpChart = new ApexCharts(chartEl, opts);
        _pfpChart.render();
    } catch(e) {
        stats.innerHTML = '';
        chartEl.innerHTML = '<div class="ov-empty">Historii výkonu se nepodařilo načíst.</div>';
    }
}

async function fetchOverviewAlerts() {
    const box = document.getElementById('ov-alerts');
    if (!box) return;
    const tk = ovTickers(40).join(',');
    if (!tk) { box.innerHTML = '<div class="ov-empty">Nahraj si portfolio nebo přidej akcii do sledování a začneme hlídat její události.</div>'; return; }
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/alerts/events?tickers=${encodeURIComponent(tk)}&days=10`)).json();
        const items = (d && d.events) || [];
        if (!items.length) { box.innerHTML = '<div class="ov-empty">Za poslední 10 dnů žádné významné události. Klid je taky info.</div>'; return; }
        box.innerHTML = items.slice(0, 8).map(ev => {
            const dt = ev.date ? new Date(ev.date + 'T12:00:00Z') : null;
            const dtxt = dt ? dt.toLocaleDateString('cs-CZ', {day:'2-digit', month:'2-digit'}) : '';
            const click = ev.url ? `window.open('${ev.url.replace(/'/g,"\\'")}', '_blank', 'noopener')` : `loadStockDetail('${ev.ticker}')`;
            return `<div class="ov-alert ${ev.severity || 'info'}" onclick="${click}">
                <i class="ti ${ev.icon} big"></i>
                <div class="tk">${escapeHtml(ev.ticker)}</div>
                <div class="tt"><span class="n">${escapeHtml(ev.title)}</span><span class="h">${escapeHtml(ev.hint || '')}</span></div>
                <div class="dt">${dtxt}</div>
            </div>`;
        }).join('');
    } catch(e) {
        box.innerHTML = '<div class="ov-empty">Události teď nelze načíst.</div>';
    }
}

async function fetchOverviewSectors() {
    const box = document.getElementById('ov-sectors');
    if (!box) return;
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/sectors`)).json();
        const items = (d && d.items) || [];
        if (!items.length) { box.innerHTML = '<div class="ov-empty">Sektorová data teď nejsou dostupná.</div>'; return; }
        const heatColor = (pct) => {
            const clamp = Math.max(-3, Math.min(3, pct));
            const intensity = Math.min(1, Math.abs(clamp) / 2.5);
            if (clamp >= 0) {
                // zelená 0-3%
                const a = 0.18 + intensity * 0.55;
                return `rgba(0, 230, 118, ${a.toFixed(2)})`;
            }
            const a = 0.18 + intensity * 0.55;
            return `rgba(255, 61, 0, ${a.toFixed(2)})`;
        };
        box.innerHTML = items.map(s => {
            const pct = s.chg_pct || 0;
            const bg = heatColor(pct);
            const sign = pct >= 0 ? '+' : '';
            return `<div class="heat-cell" style="background:${bg}" onclick="loadStockDetail('${s.ticker}')">
                <span class="hc-tk">${s.ticker}</span>
                <div>
                    <div class="hc-top"><i class="ti ${s.icon}"></i></div>
                    <div class="hc-name">${escapeHtml(s.label)}</div>
                </div>
                <div class="hc-pct">${sign}${pct.toFixed(2)}%</div>
            </div>`;
        }).join('');
    } catch(e) {
        box.innerHTML = '<div class="ov-empty">Sektorová heatmapa teď nedostupná.</div>';
    }
}

// ====================== VÝSLEDKY (TRACK RECORD) ======================
let resultsLoaded = false;
async function loadResults(force) {
    if (resultsLoaded && !force) return;
    resultsLoaded = true;
    await Promise.allSettled([loadResultsBacktest(), loadResultsLive()]);
}

async function loadResultsBacktest() {
    const kpis = document.getElementById('tr-stats');
    const grid = document.getElementById('tr-year-grid');
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/backtest`)).json();
        const v = d && d.result && d.result.validation;
        if (!v) { kpis.innerHTML = '<div class="ov-empty">Ověřená data nejsou k dispozici.</div>'; grid.innerHTML = ''; return; }
        const h = v.signals.holdout, pf = v.profiles, spy = v.benchmark.spy;
        kpis.innerHTML = `
            <div class="tr-kpi hero"><div class="lbl">Trefnost signálu</div><div class="val">${h.model.win_rate}%</div>
                <div class="sub">2023–2026, na nich se nic neladilo · všechny poklesy ${h.all_dips.win_rate}%</div></div>
            <div class="tr-kpi good"><div class="lbl">Průměr na obchod</div><div class="val">+${h.model.avg_return}%</div>
                <div class="sub">cíl +${v.rule.target_pct} % / stop −${v.rule.stop_pct} % / max ${v.rule.max_days} dní</div></div>
            <div class="tr-kpi"><div class="lbl">Vyvážený profil ročně</div><div class="val">+${pf.balanced.full.cagr}%</div>
                <div class="sub">2015–2026 · index +${spy.full.cagr}%</div></div>
            <div class="tr-kpi"><div class="lbl">Nejhorší propad (konzerv.)</div><div class="val" style="color:var(--up)">${pf.conservative.full.max_dd}%</div>
                <div class="sub">index ${spy.full.max_dd}%</div></div>`;
        const row = (lbl, x) => `<tr><td>${lbl}</td>${['train','holdout','full'].map(k =>
            `<td><b>${x[k].cagr >= 0 ? '+' : ''}${x[k].cagr}%</b><br><span>${x[k].max_dd}%<span class="sh"> · Sharpe ${x[k].sharpe}</span></span></td>`).join('')}</tr>`;
        grid.innerHTML = `<div class="tr-prof"><table>
            <tr><th>ročně · nejhorší propad</th><th>2015–2022<br><span>ladění</span></th><th>2023–2026<br><span>čisté ověření</span></th><th>celé období</th></tr>
            ${row('Index S&amp;P 500', spy)}${row('Konzervativní', pf.conservative)}${row('Vyvážený', pf.balanced)}${row('Agresivní', pf.aggressive)}
          </table>
          <p>Pokles u drahé akcie se slábnoucím momentem (skóre H+M pod 30) dal na čistém ověření ${h.rejected_low_vm.avg_return}% na obchod — proto ho model nekupuje.
          Data: ${escapeHtml(v.data)}. Stav k ${escapeHtml(v.as_of)}.</p></div>`;
    } catch (e) {
        kpis.innerHTML = '<div class="ov-empty" style="color:var(--down)">Ověřená data se nepodařilo načíst.</div>';
        grid.innerHTML = '';
    }
}

async function loadResultsLive() {
    const list = document.getElementById('tr-live-list');
    const meta = document.getElementById('tr-live-meta');
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/track-record`)).json();
        // Hlavní je ŽIVÝ ZÁZNAM MODELU (každý denní signál automaticky), ne to,
        // co si kdo rozklikl – ten nejde vybrat zpětně, takže je to poctivý důkaz.
        renderResultsBasket(d && d.basket);
        const md = (d && d.model) || {};
        const items = (md.items && md.items.length) ? md.items.map(x => ({...x, verdict: 'Koupit', ts: null}))
                                                     : ((d && d.items) || []);
        const c = md.closed, al = md.all;
        if (md.count) {
            meta.textContent = `Model živě od ${md.since}: ${md.count} obchodů` +
                (c ? ` · uzavřené ${c.count}× · trefnost ${c.win_rate}% · průměr ${c.avg_return >= 0 ? '+' : ''}${c.avg_return}%` : ' · zatím žádný uzavřený');
        }
        if (!items.length) {
            list.innerHTML = '<div class="ov-empty">Živý záznam modelu se teprve plní — každý denní signál se sem automaticky zapíše a vyhodnotí podle plánu.</div>';
            return;
        }
        list.innerHTML = `<div class="tr-live-list">${items.map(v => {
            const ret = v.return_pct;
            const open = !ret && ret !== 0;
            const cls = open ? 'open' : (ret > 0 ? 'win' : 'loss');
            const retCls = ret > 0 ? 'up' : (ret < 0 ? 'down' : '');
            const retTxt = ret == null ? '—' : `${ret > 0 ? '+' : ''}${ret.toFixed(1)}%`;
            const vCls = (v.verdict || '').toLowerCase() === 'koupit' ? 'buy'
                : (v.verdict || '').toLowerCase() === 'prodat' ? 'sell' : 'hold';
            const when = v.ts ? new Date(v.ts * 1000).toLocaleDateString('cs-CZ', {day:'2-digit', month:'2-digit'})
                               : (v.date ? v.date.slice(8, 10) + '.' + v.date.slice(5, 7) + '.' : '');
            return `<div class="tr-live-row ${cls}" onclick="loadStockDetail('${v.ticker || ''}')">
                <div>
                    <div class="tkr">${escapeHtml(v.ticker || '?')}</div>
                    <div class="when">${when}</div>
                </div>
                <div style="min-width:0; overflow:hidden;"><span class="verdict ${vCls}">${escapeHtml(v.verdict || '')}</span></div>
                <div class="px">${(v.price ?? '—')}${v.current ? ' → ' + v.current : ''}</div>
                <div style="font-size:11px; color:var(--text2); white-space:nowrap;">${v.exit_label ? escapeHtml(v.exit_label.replace(/[✅🛑⏳]/g,'').trim()) : ''}</div>
                <div class="ret ${retCls}">${retTxt}</div>
            </div>`;
        }).join('')}</div>`;
    } catch(e) {
        list.innerHTML = '<div class="ov-empty" style="color:var(--down)">Track record se nepodařilo načíst.</div>';
        renderResultsBasket(null);
    }
}

// Rady aplikace u tvých akcií: od dne, kdy je appka poprvé řekla, do dneška proti S&P 500 za stejné období
const ADVICE_LBL = { buy: ['🟢 Podhodnocená – dokup', 'up'], risk: ['🔴 Vážný problém', 'down'], watch: ['🟡 Slabá rozvaha', 'amber'] };
async function loadAdviceRecord() {
    const card = document.getElementById('tr-advice'), body = document.getElementById('tr-advice-body'), meta = document.getElementById('tr-advice-meta');
    if (!card || !authToken) { if (card) card.style.display = 'none'; return; }
    card.style.display = '';
    body.innerHTML = '<div class="ov-load"><i class="ti ti-loader spin"></i> Načítám záznam rad…</div>';
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/advice-record`, { headers: authHeaders() })).json();
        if (!d.ok || !d.count) {
            body.innerHTML = '<div class="ov-empty">Záznam se plní. Pokaždé, když appka u tvé akcie poprvé řekne „podhodnocená – dokup“, „vážný problém“ nebo „slabá rozvaha“, zapíše se datum a cena. Po týdnech tady uvidíš, jak ta rada dopadla proti S&amp;P 500.</div>';
            return;
        }
        meta.textContent = `od ${mpDate(d.since)} ${d.since.slice(0, 4)} · ${d.count} rad`;
        const kpi = Object.entries(d.groups || {}).map(([a, g]) => {
            const [lbl, col] = ADVICE_LBL[a] || [a, 'up'];
            const better = a === 'buy' ? g.avg_ret >= g.avg_spy : g.avg_ret <= g.avg_spy;       // u varování je „dobře“, když akcie zaostala
            return `<div class="tr-kpi"><div class="lbl">${lbl} · ${g.n} ${g.n === 1 ? 'rada' : (g.n < 5 ? 'rady' : 'rad')}</div>
                <div class="val" style="color:var(--${better ? 'up' : 'amber'})">${mpSigned(g.avg_ret)}</div>
                <div style="font-size:11.5px;color:var(--text2);margin-top:4px">S&amp;P 500 ${mpSigned(g.avg_spy)} · průměrně za ${g.avg_days} dní${a === 'buy' ? ` · index porazilo ${g.beat_pct} %` : ''}</div></div>`;
        }).join('');
        const rows = (d.items || []).slice(0, 15).map(x => {
            const [lbl] = ADVICE_LBL[x.action] || [x.action];
            return `<div class="tr-bk-row" onclick="loadStockDetail('${mpEsc(x.ticker)}')" style="cursor:pointer"><div><div class="m">${mpEsc(mpBase(x.ticker))} <span style="font-weight:500;color:var(--text2)">${lbl}</span></div>
                <div class="d">${mpDate(x.date)} · před ${x.days} ${x.days === 1 ? 'dnem' : 'dny'} · ${mpQty(x.price0)} → ${mpQty(x.price1)} ${mpEsc(x.currency || '')}</div></div>
                <div class="v" style="color:var(--${x.ret >= 0 ? 'up' : 'down'})">${mpSigned(x.ret)}${x.spy != null ? ` <span style="font-weight:500;color:var(--text2)">· S&amp;P ${mpSigned(x.spy)}</span>` : ''}</div></div>`;
        }).join('');
        body.innerHTML = (kpi ? `<div class="tr-stats" style="margin-bottom:12px">${kpi}</div>` : '<div class="ov-empty" style="margin-bottom:10px">Rady jsou čerstvé (do týdne) – souhrn se ukáže, až trochu zestárnou.</div>') +
            `<div class="tr-live-list">${rows}</div>
            <div class="tr-fine">Cena v den rady (v měně akcie, bez dividend) → dnes, proti S&amp;P 500 za stejné období. U „podhodnocená“ je dobře, když akcie porazí index; u varování, když zaostane. Krátké období je hlavně náhoda – vypovídá až po měsících.</div>`;
    } catch (e) { body.innerHTML = '<div class="ov-empty" style="color:var(--down)">Záznam rad se nepodařilo načíst.</div>'; }
}

// Živý záznam koše: měsíční koše (prvních n v pořadí), rovnoměrně, řetězeno přes měsíce, proti S&P 500
async function renderResultsBasket(basket) {
    const body = document.getElementById('tr-basket-body'), meta = document.getElementById('tr-basket-meta');
    if (!body) return;
    const b = basket && basket['10'], b6 = basket && basket['6'];
    if (!b) {
        body.innerHTML = '<div class="ov-empty">Záznam koše se teprve plní – první měsíc se vyhodnotí po sestavení dalšího koše.</div>';
        return;
    }
    if (meta) meta.textContent = `od ${mpDate(b.since)}${b.since ? ' ' + b.since.slice(0, 4) : ''} · ${b.periods.length} ${b.periods.length === 1 ? 'měsíc' : (b.periods.length < 5 ? 'měsíce' : 'měsíců')}`;
    const diff = b.ret - b.spy, col = v => v >= 0 ? 'up' : 'amber';
    const rows = b.periods.slice().reverse().map(x => {
        const mon = MP_MONTH_CZ[parseInt(x.month.slice(5, 7), 10) - 1] + ' ' + x.month.slice(0, 4);
        return `<div class="tr-bk-row"><div><div class="m">${mon}</div><div class="d">od ${mpDate(x.since)}${x.until ? ' do ' + mpDate(x.until) : ' – běží'} · ${x.n} akcií</div></div>
            <div class="v" style="color:var(--${col(x.ret)})">${mpSigned(x.ret)} <span style="font-weight:500;color:var(--text2)">· S&amp;P ${mpSigned(x.spy)}</span></div></div>`;
    }).join('');
    let proof = '';
    try {
        const pk = await fetchPicksData(10);
        const st = pk && pk.picks && pk.picks.stats, tr = st && st.train, ho = st && st.holdout;
        if (tr && ho) proof = `<div class="tr-fine"><b>Ověření na historii S&amp;P 500:</b> koš 10 tipů porazil index za 12 měsíců v <b>${tr.beat_pct} %</b> případů v letech 2015–2022 a v <b>${ho.beat_pct} %</b> v letech 2023–2026, na kterých se nic neladilo. Jednotlivý tip porazí index jen asi v ${ho.tip_pct} % – kupuj celý koš.</div>`;
    } catch (e) {}
    body.innerHTML = `<div class="tr-stats" style="margin-bottom:12px">
          <div class="tr-kpi hero"><div class="lbl">Koš (10 tipů) celkem</div><div class="val" style="color:var(--${col(b.ret)})">${mpSigned(b.ret)}</div></div>
          <div class="tr-kpi"><div class="lbl">S&amp;P 500 stejné období</div><div class="val">${mpSigned(b.spy)}</div></div>
          <div class="tr-kpi ${diff >= 0 ? 'good' : ''}"><div class="lbl">Rozdíl proti indexu</div><div class="val">${mpSigned(diff)}</div></div>
          ${b6 ? `<div class="tr-kpi"><div class="lbl">Užší koš (6 tipů)</div><div class="val">${mpSigned(b6.ret)}</div></div>` : ''}
        </div>
        <div class="tr-live-list">${rows}</div>
        <div class="tr-fine">Počítáme poctivě: každý měsíc se zmrazí koš tak, jak jsme ho tehdy doporučili (ceny ze dne sestavení), drží se rovnoměrně do dalšího koše a výsledky se řetězí. Krátké období je hlavně náhoda – smysl to dává po měsících až letech, nezaručuje to žádný model. Aktuální měsíc zatím běží.</div>
        ${proof}`;
}
