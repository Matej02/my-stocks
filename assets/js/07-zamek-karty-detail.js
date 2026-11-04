// ====================== ZÁMEK (ochrana heslem) ======================
let mainStarted = false;
function startMain() {
    renderStarter();
    applySimple();
    loadWatchlist();
    mainStarted = true;
    // Default tab je Přehled — nahodíme widgety hned
    if (!investorSimple) loadOverview();
    // Onboarding tour pro prvouživatele — krátká pauza, ať se UI zobrazí
    setTimeout(openOnbIfFirst, 700);
    // Notifikační polling každé 3 minuty (in-app dot + browser Notification API)
    if (Notification && Notification.permission === 'granted') _notifBrowserGranted = true;
    setTimeout(pollNotifUnread, 2000);
    setInterval(pollNotifUnread, 3 * 60 * 1000);
}

// Rozhodne, zda ukázat přístupovou zeď, nebo appku. Vrací true = má přístup.
function refreshGate() {
    const ok = _refreshGateInner();
    document.body.classList.toggle('has-dash-tabs', !!ok);   // místo pro spodní lištu jen když je vidět appka
    return ok;
}
function _refreshGateInner() {
    const wall = document.getElementById('gateWall');
    if (!cloudEnabled) { wall.style.display = 'none'; return true; }   // dev bez účtů
    if (!authToken) {
        document.getElementById('gate-auth').style.display = 'block';
        document.getElementById('gate-noaccess').style.display = 'none';
        wall.style.display = 'flex';
        return false;
    }
    if (!hasAccess()) {
        document.getElementById('gate-auth').style.display = 'none';
        document.getElementById('gate-noaccess').style.display = 'block';
        wall.style.display = 'flex';
        return false;
    }
    wall.style.display = 'none';
    return true;
}

async function initGate() {
    await initAccount();
    if (refreshGate()) startMain();
    handlePaidReturn();
    handleTickerParam();
    handleRefParam();
}

// Otevři konkrétní akcii, když je v URL ?ticker=NVDA (z SEO stránky /stock/NVDA)
function handleTickerParam() {
    try {
        const params = new URLSearchParams(location.search);
        const t = (params.get('ticker') || '').toUpperCase();
        if (!t || !/^[A-Z0-9.^=-]{1,10}$/.test(t)) return;
        history.replaceState(null, '', location.pathname);
        setTimeout(() => { if (typeof loadStockDetail === 'function') loadStockDetail(t); }, 400);
    } catch(e) {}
}

// Referral link ?ref=CODE — otevři registraci a předvyplň kód
function handleRefParam() {
    try {
        const params = new URLSearchParams(location.search);
        const code = (params.get('ref') || '').toUpperCase().slice(0, 24);
        if (!code || !/^[A-Z0-9-]{4,24}$/.test(code)) return;
        // Ulož si to i pro pozdější použití (kdyby uživatel zavřel modal)
        try { sessionStorage.setItem('mystocks_pending_ref', code); } catch(e) {}
        // Otevři registraci předvyplněnou kódem — jen když uživatel není přihlášen
        if (!authToken) {
            openProfile();
            setAuthTab('register');
            const el = document.getElementById('auth-ref');
            if (el) el.value = code;
            showToast(`Referral kód ${code} načten. Zaregistruj se a oba dostanete +30 dní.`, 'up', 'ti-gift');
        }
        history.replaceState(null, '', location.pathname);
    } catch(e) {}
}

// Návrat z platby (Stripe success_url ?paid=1) – obnovit plán a poděkovat
async function handlePaidReturn() {
    const params = new URLSearchParams(location.search);
    if (!params.has('paid')) return;
    const paid = params.get('paid') === '1';
    history.replaceState(null, '', location.pathname);
    if (!paid) { showToast('Platba zrušena', 'down', 'ti-x'); return; }
    showToast('Děkujeme! Aktivuji předplatné…', 'up', 'ti-check');
    // Webhook může dorazit s malým zpožděním – pár pokusů o refresh
    for (let i = 0; i < 5; i++) {
        try {
            const me = await (await fetch(`${BACKEND_URL}/api/me`, { headers: authHeaders() })).json();
            if (me.ok) { mySub = me.subscription; if (mySub && mySub.has_access) break; }
        } catch (e) {}
        await new Promise(r => setTimeout(r, 1500));
    }
    updateProfileBtn();
    if (refreshGate() && !mainStarted) startMain();
    showToast('Předplatné aktivní 🎉', 'up', 'ti-crown');
}

// ====================== SCANNER PŘÍLEŽITOSTÍ ======================
let oppData = [];
let oppLoading = false;


let signalsLoaded = false;
async function loadSignals(force) {
    const grid = document.getElementById('signals-grid');
    if (!hasFeature('pro')) {
        grid.innerHTML = lockedCardHTML('Co koupit teď — ověřený signál', 'pro',
            'Akcie, které právě teď splňují náš backtestem prověřený nákupní signál.');
        return;
    }
    if (signalsLoaded && !force) return;
    grid.innerHTML = '<div class="opp-loading"><i class="ti ti-loader spin"></i> Skenuji trh podle ověřeného modelu… (poprvé to chvíli trvá)</div>';
    try {
        const data = await (await fetch(`${BACKEND_URL}/api/signals`)).json();
        if (!data.ok) throw new Error();
        if (data.scanning) { grid.innerHTML = '<div class="opp-loading" style="color:var(--text2)">První sken S&amp;P 500 ještě neproběhl — spustí se sám ráno v 7:00 našeho času, nebo ho admin pustí hned v Profilu → Admin → Spustit sken.</div>'; return; }
        signalsLoaded = true;
        document.getElementById('signals-updated').textContent = `${data.count} ${data.count === 1 ? 'signál' : (data.count < 5 ? 'signály' : 'signálů')} · ${data.updated}`;
        if (btProof && btProof.buy && btProof.buy.win_rate != null) {
            document.getElementById('signals-proof').textContent =
                `Ověřeno na 2023–2026: ${btProof.buy.win_rate} % trefnost, ${btProof.buy.avg_return >= 0 ? '+' : ''}${btProof.buy.avg_return} % na obchod.`;
        }
        renderSignals(data.results || []);
    } catch (e) {
        grid.innerHTML = `<div class="opp-loading" style="color:var(--down)">Skener se nepodařilo načíst.</div>`;
    }
}

function renderSignals(list) {
    const grid = document.getElementById('signals-grid');
    const beforeN = list.length;
    list = (list || []).filter(o => riskFilterPass(o));
    const filteredOut = beforeN - list.length;
    if (!list.length) {
        const extra = filteredOut > 0 ? ` (${filteredOut} ${filteredOut === 1 ? 'titul vyfiltrován' : 'titulů vyfiltrováno'} dle tvého rizikového profilu)` : '';
        grid.innerHTML = `<div class="opp-loading" style="color:var(--text2)">Dnes žádný titul nesplňuje nákupní signál${extra}. To je v pořádku — model nakupuje jen ověřené situace, ne pořád. Mrkni sem zítra.</div>`;
        return;
    }
    grid.innerHTML = list.map((o, i) => {
        const chg = o.change_pct;
        const chgTxt = (chg || chg === 0) ? `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%` : '';
        const chgCol = (chg >= 0) ? 'var(--up)' : 'var(--down)';
        const isTop = o.conviction === 'top';
        const overCap = (o.sector_rank || 0) > 2;
        const vmLine = o.vm != null
            ? `<div class="opp-why" style="margin-top:8px"><i class="ti ti-diamond"></i> Hodnota+momentum <b>${o.vm}/100</b>${o.vm_rank ? ` (#${o.vm_rank} v S&amp;P 500)` : ''}${o.pe ? ` · P/E ${o.pe}` : ''}${o.mom_pct != null ? ` · momentum ${o.mom_pct >= 0 ? '+' : ''}${o.mom_pct} %` : ''}</div>` : '';
        const capLine = overCap
            ? `<div class="opp-why" style="color:var(--amber)"><i class="ti ti-alert-triangle"></i> ${o.sector_rank}. signál z odvětví ${escapeHtml(o.sector || '')} — model drží max 2 na sektor.</div>` : '';
        return `
        <div class="opp-card${isTop && !overCap ? ' moon' : ''}" onclick="loadStockDetail('${o.ticker}')" style="${overCap ? 'opacity:.6' : ''}">
            <span class="opp-rank">${isTop && !overCap ? '⭐ TOP' : '#' + (i + 1)}</span>
            <div class="opp-c-top">
                <div class="opp-score p-high">${o.score}<small>SIGNÁL</small></div>
                <div class="opp-c-id">
                    <div class="opp-c-tkr">${o.ticker}</div>
                    <div class="opp-c-name">${escapeHtml(niceName(o.ticker, o.name))}</div>
                </div>
            </div>
            <div class="opp-upside">
                <span class="opp-up-val" style="color:var(--up)">cíl +${o.reward_pct ?? '?'}%</span>
                <span class="opp-up-lbl">stop −${o.risk_pct ?? '?'} % · zisk:riziko ${o.rr ?? '—'} : 1</span>
            </div>
            <div class="opp-c-px">
                <span class="opp-c-price">${(o.price ?? 0).toFixed(2)} <span style="font-size:11px;color:var(--text2)">${o.currency || ''}</span></span>
                <span style="font-size:12px;color:${chgCol}">${chgTxt} · RSI ${o.rsi ?? '—'}</span>
            </div>
            ${vmLine}${capLine}
        </div>`;
    }).join('');
}

async function loadOpportunities() {
    const grid = document.getElementById('opp-grid');
    if(!hasFeature('pro')) {
        grid.innerHTML = lockedCardHTML('Podhodnocené s momentem', 'pro',
            'Nejlevnější akcie S&amp;P 500, které zároveň mají roční momentum — ověřená kombinace.');
        return;
    }
    if(oppLoading) return;
    oppLoading = true;
    grid.innerHTML = '<div class="opp-loading"><i class="ti ti-loader spin"></i> Načítám dnešní sken S&amp;P 500…</div>';
    try {
        const res = await fetch(`${BACKEND_URL}/api/opportunities`);
        const data = await res.json();
        if(!data.ok) throw new Error();
        if (data.scanning) { grid.innerHTML = '<div class="opp-loading" style="color:var(--text2)">První sken S&amp;P 500 ještě neproběhl — spustí se sám ráno v 7:00 našeho času, nebo ho admin pustí hned v Profilu → Admin → Spustit sken.</div>'; return; }
        oppData = data.results || [];
        document.getElementById('opp-updated').textContent = data.count
            ? `${data.count} kandidátů · ${data.updated}` : '';
        renderOpportunities();
    } catch(e) {
        grid.innerHTML = `<div class="opp-loading" style="color:var(--down)">Scanner se nepodařilo načíst (backend neběží?).</div>`;
    } finally {
        oppLoading = false;
    }
}

function tierClass(tier) {
    return ({ 'TOP 2 %': 'p-high', 'TOP 5 %': 'p-strong', 'TOP 10 %': 'p-mid' })[tier] || 'p-low';
}

function renderOpportunities() {
    const grid = document.getElementById('opp-grid');
    const all = oppData.slice();
    const list = all.filter(o => riskFilterPass(o));
    const filteredOut = all.length - list.length;
    if(!list.length) {
        const extra = filteredOut > 0 ? ` Tvůj rizikový profil ${filteredOut} titulů vyfiltroval — zkus „Vyvážená/Agresivní" v profilu.` : '';
        grid.innerHTML = `<div class="opp-loading" style="color:var(--text2)">Žádné příležitosti k zobrazení.${extra} Zkus jiný filtr nebo Skenovat.</div>`;
        return;
    }

    grid.innerHTML = list.map((o, i) => {
        const badges = (o.badges || []).slice(0, 3).map(b =>
            `<span class="opp-badge ${b.k}">${b.t}</span>`).join('');
        const yr = o.year_pct;
        const yrTxt = (yr || yr === 0) ? `${yr >= 0 ? '+' : ''}${yr.toFixed(0)}%` : '—';
        const why = o.why ? `<div class="opp-why"><i class="ti ti-bulb"></i> ${escapeHtml(o.why)}</div>` : '';
        return `
        <div class="opp-card" onclick="loadStockDetail('${o.ticker}')" title="${o.exchange || ''}">
            <span class="opp-rank">#${i+1}</span>
            <div class="opp-c-top">
                <div class="opp-score ${tierClass(o.tier)}">${o.score}<small>HODN.+MOM.</small></div>
                <div class="opp-c-id">
                    <div class="opp-c-tkr">${o.ticker}</div>
                    <div class="opp-c-name">${o.name || ''}</div>
                </div>
            </div>
            <div class="opp-upside">
                <span class="opp-up-val">#${o.vm_rank ?? '—'}</span>
                <span class="opp-up-lbl">pořadí v S&amp;P 500 · ${escapeHtml(o.sector || '')}</span>
            </div>
            <div class="opp-c-px">
                <span class="opp-c-price">${(o.price ?? 0).toFixed(o.price < 10 ? 2 : 2)} <span style="font-size:11px;color:var(--text2)">${o.currency||''}</span></span>
                <span class="opp-tier-lbl ${tierClass(o.tier)}">${o.tier}</span>
            </div>
            ${why}
            <div class="opp-badges">${badges}</div>
            <div class="opp-c-stats">
                <div class="opp-stat"><span class="l">P/E</span><span class="v">${o.pe ?? '—'}</span></div>
                <div class="opp-stat"><span class="l">Cash flow / cena</span><span class="v">${o.fcf_yield != null ? o.fcf_yield + ' %' : '—'}</span></div>
                <div class="opp-stat"><span class="l">Za rok</span><span class="v" style="color:${(yr||0)>=0?'var(--up)':'var(--down)'}">${yrTxt}</span></div>
            </div>
        </div>`;
    }).join('') +
    `<div class="opp-disclaimer" style="grid-column:1/-1;"><i class="ti ti-info-circle"></i> Skóre hodnota+momentum = průměr pořadí v S&amp;P 500 podle zisku, účetní hodnoty a cash flow vůči ceně a ročního momenta (fundamenty z oficiálních dat SEC). Nejlepších 20 titulů v testu 2015–2026 porazilo průměrnou akcii indexu v obou obdobích. Kupuj je v nákupní zóně, ne naslepo. Není to investiční doporučení.</div>`;
}

// --- VYKRESLENÍ KARET: rozdělení na Moje pozice vs Sledované ---
// Hezké názvy pro komodity/indexy/krypto (Yahoo je má ošklivé, např. „Gold Jun 26")
const NAME_MAP = {
    'GC=F': 'Zlato', 'SI=F': 'Stříbro', 'HG=F': 'Měď', 'PL=F': 'Platina', 'PA=F': 'Palladium',
    'CL=F': 'Ropa (WTI)', 'BZ=F': 'Ropa (Brent)', 'NG=F': 'Zemní plyn',
    'ZC=F': 'Kukuřice', 'ZW=F': 'Pšenice', 'ZS=F': 'Sója',
    'BTC-USD': 'Bitcoin', 'ETH-USD': 'Ethereum',
    '^GSPC': 'S&P 500', '^IXIC': 'Nasdaq', '^DJI': 'Dow Jones', '^GDAXI': 'DAX',
    // České akcie na BCPP (Yahoo suffix .PR)
    'CEZ.PR': 'ČEZ', 'KOMB.PR': 'Komerční banka', 'MONET.PR': 'Moneta Money Bank',
    'PHILIP.PR': 'Philip Morris ČR', 'ERSTE.PR': 'Erste Group Bank',
    'KOFOL.PR': 'Kofola ČeskoSlovensko', 'COLT.PR': 'Colt CZ Group',
    'CETV.PR': 'Central European Media (CETV)', 'FORT.PR': 'Fortuna Entertainment',
    'PRIM.PR': 'Primoco UAV', 'PILULKA.PR': 'Pilulka.cz',
    'GEVO.PR': 'Gevorkyan',
    // Německé (Xetra, Yahoo suffix .DE)
    'SAP.DE': 'SAP', 'SIE.DE': 'Siemens', 'ALV.DE': 'Allianz', 'DTE.DE': 'Deutsche Telekom',
    'BMW.DE': 'BMW', 'MBG.DE': 'Mercedes-Benz', 'VOW3.DE': 'Volkswagen',
    // Londýn (.L)
    'ULVR.L': 'Unilever', 'HSBA.L': 'HSBC', 'BP.L': 'BP', 'SHEL.L': 'Shell',
    // Amsterdam
    'ASML.AS': 'ASML', 'HEIA.AS': 'Heineken',
    // Měnové páry
    'EURCZK=X': 'EUR/CZK', 'USDCZK=X': 'USD/CZK', 'EURUSD=X': 'EUR/USD', 'GBPUSD=X': 'GBP/USD',
};
function niceName(ticker, name) {
    if (NAME_MAP[ticker]) return NAME_MAP[ticker];
    let n = (name || ticker).trim();
    // ořež expirace u futures názvů („Gold Jun 26" -> „Gold")
    n = n.replace(/\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*['\d].*$/i, '').trim();
    return n || ticker;
}

function stockCardHTML(s) {
    const isUp = s.change_pct >= 0;
    const hasAlert = myAlerts.some(a => a.ticker === s.ticker);
    const bell = hasAlert ? '<i class="ti ti-bell-ringing" style="color:var(--accent); font-size:14px;"></i>' : '';
    const tag = (myTags && myTags[s.ticker]) || '';
    const tagChip = `<span class="sc-tag${tag ? '' : ' empty'}" onclick="event.stopPropagation();editTag('${s.ticker}')">${tag ? '#' + escapeHtml(tag) : '+ skupina'}</span>`;
    return `<div class="stock-card" onclick="loadStockDetail('${s.ticker}')">
        <div class="sc-head">
            <div style="min-width:0">
                <div class="sc-name">${escapeHtml(niceName(s.ticker, s.name))}</div>
                <div class="sc-sub">${s.ticker}</div>
            </div>
            <div style="display:flex; align-items:center; gap:8px;">${tagChip}${bell}</div>
        </div>
        <div class="sc-foot">
            <div class="sc-price">${s.price.toFixed(s.price < 10 ? 3 : 2)} <span>${s.currency}</span></div>
            <div class="sc-change ${isUp ? 'up':'down'}"><i class="ti ${isUp ? 'ti-trending-up':'ti-trending-down'}"></i> ${isUp?'+':''}${s.change_pct.toFixed(2)}%</div>
        </div>
    </div>`;
}

let myTags = JSON.parse(localStorage.getItem('mystocks_tags') || '{}');
let activeTag = '';
function editTag(ticker) {
    const cur = myTags[ticker] || '';
    const v = prompt('Skupina pro ' + ticker + ' (např. „AI", „Dividendy", „Růst"). Nech prázdné a smaž skupinu.', cur);
    if (v === null) return;
    const clean = v.trim().replace(/[#,;\n]/g, '').slice(0, 20);
    if (clean) myTags[ticker] = clean; else delete myTags[ticker];
    localStorage.setItem('mystocks_tags', JSON.stringify(myTags));
    renderWatchCards(lastWatchlistData);
}
function setActiveTag(t) {
    activeTag = t || '';
    renderWatchCards(lastWatchlistData);
}

function renderWatchTagsBar() {
    const bar = document.getElementById('wl-tags');
    if (!bar) return;
    const tags = [...new Set(Object.values(myTags || {}).filter(Boolean))].sort();
    if (!tags.length) { bar.innerHTML = ''; return; }
    const chip = (k, label) => `<button class="wl-chip${activeTag === k ? ' active' : ''}" onclick="setActiveTag('${k.replace(/'/g,"\\'")}')">${escapeHtml(label)}</button>`;
    bar.innerHTML = chip('', '🌐 Vše') + tags.map(t => chip(t, '#' + t)).join('');
}

function renderWatchCards(results) {
    results = Array.isArray(results) ? results.slice() : [];
    if (activeTag) {
        results = results.filter(r => (myTags[r.ticker] || '') === activeTag);
    }
    const sort = (document.getElementById('watch-sort') || {}).value || 'change_desc';
    const cmp = {
        change_desc: (a, b) => (b.change_pct ?? 0) - (a.change_pct ?? 0),
        change_asc: (a, b) => (a.change_pct ?? 0) - (b.change_pct ?? 0),
        name: (a, b) => niceName(a.ticker, a.name).localeCompare(niceName(b.ticker, b.name)),
        price_desc: (a, b) => (b.price ?? 0) - (a.price ?? 0),
    }[sort];
    if (cmp) results.sort(cmp);
    const watchGrid = document.getElementById('watch-grid');
    watchGrid.innerHTML = results.map(stockCardHTML).join('');
    document.getElementById('watch-empty').style.display = results.length ? 'none' : 'block';
    renderPortfolioSummary(results);
    renderWatchTagsBar();
}

function renderPortfolioSummary(results) {
    const el = document.getElementById('portfolio-summary');
    if (!el) return;
    if (!results || !results.length) { el.style.display = 'none'; return; }
    const up = results.filter(r => (r.change_pct ?? 0) > 0).length;
    const down = results.filter(r => (r.change_pct ?? 0) < 0).length;
    const avg = results.reduce((s, r) => s + (r.change_pct ?? 0), 0) / results.length;
    const best = results.reduce((a, b) => ((b.change_pct ?? 0) > (a.change_pct ?? 0) ? b : a));
    const worst = results.reduce((a, b) => ((b.change_pct ?? 0) < (a.change_pct ?? 0) ? b : a));
    const col = v => v >= 0 ? 'var(--up)' : 'var(--down)';
    const chg = v => `${v >= 0 ? '+' : ''}${(v ?? 0).toFixed(2)}%`;
    el.style.display = 'grid';
    el.innerHTML = `
        <div class="pf-stat"><div class="l">Titulů</div><div class="v">${results.length}</div></div>
        <div class="pf-stat"><div class="l">Dnes</div><div class="v"><span style="color:var(--up)">${up}↑</span> <span style="color:var(--down)">${down}↓</span></div></div>
        <div class="pf-stat"><div class="l">Průměr dnes</div><div class="v" style="color:${col(avg)}">${chg(avg)}</div></div>
        <div class="pf-stat"><div class="l">Nejlepší</div><div class="v" style="font-size:15px">${best.ticker} <span style="color:${col(best.change_pct)};font-size:13px">${chg(best.change_pct)}</span></div></div>
        <div class="pf-stat"><div class="l">Nejhorší</div><div class="v" style="font-size:15px">${worst.ticker} <span style="color:${col(worst.change_pct)};font-size:13px">${chg(worst.change_pct)}</span></div></div>`;
}

// --- DETAIL AKCIE S OVLÁDÁNÍM GRAFU ---
function setChartType(type) {
    chartType = type;
    document.querySelectorAll('#chart-types .c-btn').forEach(b => { if(b.id !== 'ma-toggle') b.classList.remove('active'); });
    event.target.classList.add('active');
    if(currentChartData) renderChart(currentChartData);
}

function setChartPeriod(period) {
    chartPeriod = period;
    document.querySelectorAll('#chart-periods .c-btn').forEach(b => b.classList.remove('active'));
    event.target.classList.add('active');
    if(currentStockData) loadStockDetail(currentStockData.ticker, false); // false = negeneruj znovu AI
}

function detailTab(name) {
    ['overview', 'analysis', 'news', 'ai'].forEach(t => {
        const p = document.getElementById('dpane-' + t);
        const b = document.getElementById('dt-' + t);
        if (p) p.style.display = (t === name) ? 'block' : 'none';
        if (b) b.classList.toggle('active', t === name);
    });
}

// isNew = true → čerstvé otevření akcie (resetuj analýzu, chat, načti zprávy/analytiky).
// isNew = false → jen lehké překreslení (změna období grafu, přepnutí sledování) –
//                 analýza, chat i otevřená záložka zůstanou zachované.
let _dashScrollY = 0;
async function loadStockDetail(ticker, isNew = true) {
    // Uložit pozici scrollu v dashboardu jen při reálném otevření (ne při rerender po přidání do sledování)
    if (isNew) _dashScrollY = window.scrollY || window.pageYOffset || 0;
    document.getElementById('dashboard').style.display = 'none';
    const detail = document.getElementById('detail-view');
    detail.style.display = 'block';
    // Skok nahoru bez animace — jako by uživatel navigoval na novou stránku
    if (isNew) window.scrollTo(0, 0);
    if (isNew) detailTab('overview');

    document.getElementById('d-ticker').innerHTML = `<i class="ti ti-loader spin"></i>`;

    try {
        const res = await fetch(`${BACKEND_URL}/api/stock/${ticker}?period=${chartPeriod}${isNew ? '&full=1' : ''}`);
        const data = await res.json();
        
        if(!data.ok) { alert(data.error); closeDetail(); return; }
        
        currentStockData = data;
        currentChartData = data.chart;
        
        document.getElementById('d-ticker').innerText = data.ticker;
        document.getElementById('d-name').innerText = data.name;
        document.getElementById('d-sector').innerText = `${data.sector} | ${data.industry}`;
        document.getElementById('d-price').innerText = data.price.toFixed(3); // Desetinná místa u hlavní ceny
        
        const changeEl = document.getElementById('d-change');
        const isUp = data.change_pct >= 0;
        changeEl.className = `dh-change ${isUp ? 'up' : 'down'}`;
        changeEl.innerHTML = `<i class="ti ${isUp ? 'ti-arrow-up-right' : 'ti-arrow-down-right'}"></i> ${isUp?'+':''}${data.change_pct.toFixed(2)}%`;
        
        document.getElementById('d-pe').innerText = (data.pe_ratio !== "N/A" && data.pe_ratio != null) ? parseFloat(data.pe_ratio).toFixed(2) : "N/A";
        document.getElementById('d-cap').innerText = formatNum(data.market_cap);
        document.getElementById('d-high').innerText = parseFloat(data.high52).toFixed(3);
        document.getElementById('d-low').innerText = parseFloat(data.low52).toFixed(3);

        const ind = data.indicators || {};
        const rsiEl = document.getElementById('d-rsi');
        if(ind.rsi != null) {
            rsiEl.innerText = ind.rsi.toFixed(1);
            rsiEl.style.color = ind.rsi > 70 ? 'var(--down)' : ind.rsi < 30 ? 'var(--up)' : 'var(--text)';
        } else { rsiEl.innerText = 'N/A'; rsiEl.style.color = 'var(--text)'; }
        document.getElementById('d-vol').innerText = ind.volatility != null ? ind.volatility.toFixed(1)+'%' : 'N/A';
        document.getElementById('d-beta').innerText = data.beta != null ? data.beta.toFixed(2) : 'N/A';
        document.getElementById('d-div').innerText = data.dividend_yield != null ? data.dividend_yield.toFixed(2)+'%' : '—';
        const shortEl = document.getElementById('d-short');
        if (shortEl) {
            shortEl.innerText = data.short_percent_float != null ? data.short_percent_float.toFixed(2)+'%' : '—';
            shortEl.style.color = data.short_percent_float > 20 ? 'var(--down)' : (data.short_percent_float > 10 ? 'var(--amber)' : 'var(--text)');
        }
        const shortREl = document.getElementById('d-shortr');
        if (shortREl) shortREl.innerText = data.short_ratio != null ? data.short_ratio.toFixed(1)+' d' : '—';

        renderSignal(data);
        if (isNew) renderVerdict(data.model);
        // Tyto panely nezávisí na zvoleném období grafu – při pouhé změně období
        // (isNew=false) je nepřekreslujeme, aby nezmizela už vygenerovaná analýza.
        if (isNew) {
            resetDeep();
            resetChat(data.ticker);
            loadNews(data.ticker);
            loadAnalyst(data.ticker, data.price);
            loadEarningsHistory(data.ticker);
            loadTopHolders(data.ticker);
        }

        const btnWatch = document.getElementById('btn-toggle-watch');
        if(myStocks.includes(data.ticker)) {
            btnWatch.innerHTML = '<i class="ti ti-star-off"></i> Odebrat';
            btnWatch.style.borderColor = 'var(--accent)'; btnWatch.style.color = 'var(--accent)';
        } else {
            btnWatch.innerHTML = '<i class="ti ti-star"></i> Sledovat';
            btnWatch.style.borderColor = 'var(--text2)'; btnWatch.style.color = 'var(--text)';
        }

        renderChart(currentChartData);
        renderPeriodPerf(currentChartData);
    } catch(e) {
        alert("Chyba při stahování dat ze serveru.");
        closeDetail();
    }
}

function renderChart(historyData) {
    if(chartInstance) chartInstance.destroy();

    const isUp = historyData[0].c <= historyData[historyData.length-1].c;
    const mainColor = isUp ? '#00E676' : '#FF3D00';
    const cats = historyData.map(d => d.x);

    // Hlavní série
    const isCandle = chartType === 'candlestick';
    const mainData = isCandle
        ? historyData.map(d => ({ x: d.x, y: [d.o, d.h, d.l, d.c] }))
        : historyData.map(d => ({ x: d.x, y: d.c }));

    const series = [{ name: 'Cena', type: isCandle ? 'candlestick' : 'area', data: mainData }];
    const colors = [mainColor];

    // MA overlay (jen pokud zapnuto a máme data)
    const maOn = showMA && currentStockData && Array.isArray(currentStockData.sma20);
    document.getElementById('ma-legend').style.display = maOn ? 'flex' : 'none';
    if(maOn) {
        const maDefs = [['SMA20','sma20','#FFC400'],['SMA50','sma50','#29B6F6'],['SMA200','sma200','#AB47BC']];
        maDefs.forEach(([name,key,col]) => {
            const arr = currentStockData[key];
            if(arr && arr.some(v => v != null)) {
                series.push({ name, type: 'line', data: historyData.map((d,i) => ({ x: d.x, y: arr[i] })) });
                colors.push(col);
            }
        });
    }

    // Combo (více sérií) vyžaduje základní typ 'line'
    const baseType = (series.length > 1) ? 'line' : (isCandle ? 'candlestick' : 'area');

    const options = {
        series: series,
        chart: { type: baseType, height: 360, toolbar: {show: false}, zoom: {enabled: false, allowMouseWheelZoom: false}, animations: {enabled: false}, id: 'mainChart', group: 'sync' },
        colors: colors,
        stroke: { curve: 'smooth', width: series.map((s,i) => i===0 ? (isCandle?1:2) : 1.5) },
        plotOptions: { candlestick: { colors: { upward: '#00E676', downward: '#FF3D00' }, wick: { useFillColor: true } } },
        fill: {
            type: series.map((s,i) => (i===0 && !isCandle) ? 'gradient' : 'solid'),
            gradient: { shadeIntensity: 1, opacityFrom: 0.4, opacityTo: 0.0, stops: [0, 100] }
        },
        dataLabels: { enabled: false },
        legend: { show: false },
        xaxis: { type: 'category', categories: cats, labels: {show: false}, axisBorder: {show:false}, axisTicks: {show:false}, tooltip: {enabled: false} },
        yaxis: { labels: { style: { colors: '#9ba1b0', fontFamily: 'Space Grotesk' }, formatter: (value) => value != null ? value.toFixed(3) : '' } },
        grid: { borderColor: 'rgba(255,255,255,0.05)', strokeDashArray: 4 },
        theme: { mode: 'dark' },
        tooltip: { theme: 'dark', shared: true }
    };
    chartInstance = new ApexCharts(document.querySelector("#chart"), options);
    chartInstance.render();

    renderVolumeChart(historyData);
}

function renderVolumeChart(historyData) {
    if(volChartInstance) volChartInstance.destroy();
    const hasVol = historyData.some(d => d.v && d.v > 0);
    const volWrap = document.getElementById('volChart');
    if(!hasVol) { volWrap.style.display = 'none'; return; }
    volWrap.style.display = 'block';

    const volData = historyData.map(d => ({
        x: d.x,
        y: d.v || 0,
        fillColor: (d.c >= d.o) ? 'rgba(0,230,118,0.45)' : 'rgba(255,61,0,0.45)'
    }));

    const opts = {
        series: [{ name: 'Objem', data: volData }],
        chart: { type: 'bar', height: 90, toolbar: {show:false}, zoom: {enabled: false, allowMouseWheelZoom: false}, animations:{enabled:false}, brush:{enabled:false} },
        plotOptions: { bar: { columnWidth: '80%' } },
        dataLabels: { enabled: false },
        xaxis: { type:'category', categories: historyData.map(d=>d.x), labels:{show:false}, axisBorder:{show:false}, axisTicks:{show:false}, tooltip:{enabled:false} },
        yaxis: { labels: { show:false }, },
        grid: { show:false, padding:{top:0,bottom:0} },
        tooltip: { theme:'dark', y: { formatter: v => formatNum(v) } },
        theme: { mode: 'dark' }
    };
    volChartInstance = new ApexCharts(volWrap, opts);
    volChartInstance.render();
}

function toggleMA() {
    showMA = !showMA;
    document.getElementById('ma-toggle').classList.toggle('active', showMA);
    if(currentChartData) renderChart(currentChartData);
}

// --- TECHNICKÝ SIGNAL PANEL ---
// Varování před blížícími se výsledky (earnings) – riziko velkého skoku ceny.
function earnWarn(days) {
    if (days == null) return '';
    const txt = days <= 0 ? 'dnes' : (days === 1 ? 'zítra' : 'za ' + days + ' ' + (days < 5 ? 'dny' : 'dní'));
    return `<div style="margin:8px 0;padding:9px 12px;border-radius:10px;background:rgba(255,179,71,0.10);border:1px solid rgba(255,179,71,0.32);color:#ffb347;font-size:12.5px;line-height:1.55">`
        + `<i class="ti ti-calendar-event"></i> <b>Výsledky ${txt}.</b> Před výsledky cena často prudce skočí (nahoru i dolů) — proto jsme snížili jistotu. Zvaž menší pozici nebo počkej na reakci po zveřejnění.</div>`;
}

function renderVerdict(model) {
    const box = document.getElementById('verdict-box');
    if (!model || !model.verdict) { box.style.display = 'none'; return; }
    box.style.display = 'block';
    const vt = model.verdict.toLowerCase();
    const vcls = vt.includes('koup') ? 'buy' : (vt.includes('prod') ? 'sell' : 'hold');
    const vEl = document.getElementById('vb-verdict');
    vEl.textContent = model.verdict;
    vEl.className = 'vb-verdict ' + vcls;
    document.getElementById('vb-score').textContent = (model.score != null ? model.score : '--') + '/100';
    document.getElementById('vb-cov').textContent =
        'jistota ' + (model.confidence != null ? model.confidence + '%' : '—') +
        ' · pokrytí ' + (model.coverage_pct != null ? model.coverage_pct + '%' : '—');
    const pillars = (model.pillars || []).map(p => `${escapeHtml(p.label)} ${p.score}`).join(' · ');
    document.getElementById('vb-note').innerHTML =
        earnWarn(model.earnings_in) +
        `Spočítáno z pilířů: ${pillars || '—'}. Kompletní rozbor (teze, scénáře, exit plán) najdeš v <b>Hloubkové analýze</b>. ` +
        `<a class="profile-cloud-link" onclick="detailTab('analysis')">Otevřít analýzu</a> · ` +
        `<a class="profile-cloud-link" onclick="openMethod()">Jak hodnotíme?</a>`;
}

function renderSignal(data) {
    const box = document.getElementById('signal-box');
    if(data.score == null) { box.style.display = 'none'; return; }
    box.style.display = 'block';

    document.getElementById('signal-num').innerText = data.score;
    document.getElementById('signal-needle').style.left = data.score + '%';

    const verdict = document.getElementById('signal-verdict');
    verdict.innerText = data.score_label || '';
    const s = data.score;
    let vc;
    if(s >= 58) vc = ['var(--up)','rgba(0,230,118,0.15)'];
    else if(s >= 43) vc = ['#FFC400','rgba(255,196,0,0.12)'];
    else vc = ['var(--down)','rgba(255,61,0,0.15)'];
    verdict.style.color = vc[0]; verdict.style.background = vc[1];
    document.getElementById('signal-num').style.color = vc[0];

    const comps = document.getElementById('signal-comps');
    comps.innerHTML = '';
    (data.score_components || []).forEach(c => {
        const icon = c.kind === 'bull' ? 'ti-circle-arrow-up-right' : c.kind === 'bear' ? 'ti-circle-arrow-down-right' : 'ti-minus';
        comps.innerHTML += `<div class="signal-comp"><i class="ti ${icon} sc-${c.kind}"></i> ${c.label}</div>`;
    });
}

function renderPeriodPerf(historyData) {
    const badge = document.getElementById('period-perf-badge');
    const valEl = document.getElementById('period-perf-val');
    const labelEl = document.getElementById('period-perf-label');
    
    if(!historyData || historyData.length < 2) { badge.style.display = 'none'; return; }
    
    const first = historyData[0].c;
    const last = historyData[historyData.length - 1].c;
    const pct = ((last - first) / first) * 100;
    const isUp = pct >= 0;
    
    const labelMap = {
        '1d': 'dnes', '5d': 'za týden', '1mo': 'za měsíc',
        '3mo': 'za 3 měsíce', '6mo': 'za 6 měsíců', '1y': 'za rok', 'max': 'celkem'
    };
    
    badge.style.display = 'inline-flex';
    badge.className = `period-perf ${isUp ? 'up' : 'down'}`;
    badge.querySelector('i').className = `ti ${isUp ? 'ti-trending-up' : 'ti-trending-down'}`;
    valEl.innerText = `${isUp ? '+' : ''}${pct.toFixed(2)}%`;
    labelEl.innerText = labelMap[chartPeriod] || '';
}

function closeDetail() {
    document.getElementById('detail-view').style.display = 'none';
    document.getElementById('dashboard').style.display = 'block';
    document.getElementById('searchInput').value = '';
    document.getElementById('period-perf-badge').style.display = 'none';
    // Vrátit uživatele na původní pozici v dashboardu (žádné poskakování)
    requestAnimationFrame(() => window.scrollTo(0, _dashScrollY || 0));
    
    // Reset grafu — 6M je index 4 (1D, 1T, 1M, 3M, 6M, 1Y, MAX)
    chartType = 'area'; chartPeriod = '6mo'; showMA = false;
    if(volChartInstance) { volChartInstance.destroy(); volChartInstance = null; }
    document.getElementById('ma-toggle').classList.remove('active');
    document.getElementById('ma-legend').style.display = 'none';
    document.querySelectorAll('#chart-types .c-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('#chart-types .c-btn')[0].classList.add('active');
    document.querySelectorAll('#chart-periods .c-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('#chart-periods .c-btn')[4].classList.add('active'); // index 4 = 6M

    loadWatchlist();
}

function toggleWatchlist() {
    if(!currentStockData) return;
    const t = currentStockData.ticker;
    if(myStocks.includes(t)) {
        myStocks = myStocks.filter(s => s !== t);
    } else {
        myStocks.push(t);
    }
    localStorage.setItem('mystocks_portfolio', JSON.stringify(myStocks));
    cloudSync();
    loadStockDetail(t, false);
}

// --- AUTO-AI ANALÝZA V DETAILU ---
// --- PORTFOLIO HODNOCENÍ: CO PRODAT / PŘIKOUPIT (server-side AI) ---
// ---------- Doporučení: stejné hodnocení jako Moje portfolio + změny, výkon, tipy, AI shrnutí ----------
let recComment = { key: '', html: '' };
function renderRecommendations() {
    const root = document.getElementById('rec-root');
    if (!root) return;
    const d = planData;
    if (!planLots.length) {
        root.innerHTML = `<div class="mp-card"><h4><i class="ti ti-bulb"></i> Doporučení pro tvoje akcie</h4>
            <div class="mp-sub">Doporučení vychází z akcií, které vlastníš. Nahraj je v <b>Moje portfolio</b> (screenshot, CSV z Trading 212 nebo seznam) a uvidíš tady hodnocení každé, co se změnilo od včera a jak se jim daří.</div>
            <button class="tool-btn primary" onclick="switchTab('portfolio')"><i class="ti ti-briefcase"></i> Otevřít Moje portfolio</button></div>` + (d && d.picks ? recPicksHTML(d) : '');
        if (!d && !planLoading) planRefresh();
        return;
    }
    if (!d) {
        root.innerHTML = `<div class="mp-card"><div class="mp-progress"><i class="ti ti-loader spin"></i> Hodnotím tvoje akcie…</div></div>`;
        if (!planLoading) planRefresh();
        return;
    }
    root.innerHTML = `<div class="rec-head"><h2><i class="ti ti-bulb"></i> Doporučení pro tvoje akcie</h2>
          <div class="mp-sub">Stejné hodnocení jako v Moje portfolio – jedno místo pravdy. Říká, kam dávat <b>nové peníze</b>; prodávat podle něj nedoporučuji.</div></div>
        <div id="rec-ai">${recComment.html}</div>
        ${planChangesHTML(d)}
        ${planEvalHTML(d)}
        <div style="margin:-8px 0 18px"><button class="tool-btn" onclick="switchTab('portfolio')"><i class="ti ti-list-details"></i> Detail každé akcie v Moje portfolio</button></div>
        ${recPerfHTML(d)}
        ${recPicksHTML(d)}`;
    loadRecComment(d);
}

function recPerfHTML(d) {
    const pf = d.perf, bp = d.basket_perf;
    if (!(pf && pf.n) && !bp) return '';
    const row = (lbl, x, note) => `<div class="rec-perf-row"><div class="l">${lbl}</div>
        <div class="v" style="color:var(--${x.ret >= x.spy ? 'up' : 'amber'})">${mpSigned(x.ret)}</div>
        <div class="b">S&amp;P 500 ${mpSigned(x.spy)}</div><div class="n">${note}</div></div>`;
    return `<div class="mp-card"><h4><i class="ti ti-activity"></i> Výkon – živý záznam</h4>
        ${pf && pf.n ? row(`Tvoje akcie od ${mpDate(pf.since)}`, pf, pf.weighted ? 'vážené podle velikostí, které jsi zadal' : 'rovnoměrně – každá akcie stejně') : ''}
        ${bp ? row(`Koš tipů od ${mpDate(bp.since)}`, bp, `koš ${bp.n} akcií, měsíčně obměněný`) : ''}
        <div class="mp-sub" style="margin:8px 0 0">Počítáno z cen při přidání do appky (v měně akcie), bez poplatků a dividend. Krátké období je hlavně náhoda – vypovídací hodnotu to má po měsících.</div></div>`;
}

function recPicksHTML(d) {
    const pk = d.picks;
    if (!pk || !(pk.new || []).length) return '';
    const items = pk.new.slice(0, 5).map(p => `<div class="rec-pick" onclick="loadStockDetail('${mpEsc(p.ticker)}')">
        <b>${mpEsc(mpBase(p.ticker))}</b> <span class="mp-pick-src ${p.source}">${p.source === 'vm' ? 'Levná + roste' : 'Silné momentum'}</span>
        <div class="mp-sub" style="margin:2px 0 0">${mpEsc(p.name)} – ${mpEsc(p.why)}</div></div>`).join('');
    return `<div class="mp-card"><h4><i class="ti ti-target-arrow"></i> Co koupit teď (z koše tipů, co ještě nemáš)</h4>${items}
        <button class="tool-btn" style="margin-top:10px" onclick="switchTab('portfolio'); setTimeout(()=>{const e=document.getElementById('mp-picks'); if(e) e.scrollIntoView({behavior:'smooth',block:'start'});},200)"><i class="ti ti-coins"></i> Celý koš a kam s penězi</button></div>`;
}

async function loadRecComment(d) {
    const el = document.getElementById('rec-ai');
    if (!el || !authToken) return;
    const key = new Date().toISOString().slice(0, 10) + '|' + JSON.stringify(((d.evaluation || {}).items || []).map(e => [e.ticker, e.level])) + '|' + ((d.changes || {}).items || []).length;
    if (recComment.key === key) return;
    recComment = { key, html: '' };
    el.innerHTML = `<div class="rec-ai loading"><i class="ti ti-loader spin"></i> AI shrnuje tvoje akcie…</div>`;
    try {
        const r = await fetch(`${BACKEND_URL}/api/ai/portfolio-comment`, { method: 'POST', headers: authHeaders() });
        const j = await r.json().catch(() => ({}));
        recComment.html = j.ok && j.comment
            ? `<div class="rec-ai"><div class="t"><i class="ti ti-sparkles"></i> Shrnutí od AI</div><div>${mpEsc(j.comment)}</div><div class="n">AI jen převypráví data níže – nic si nevymýšlí a prodej nedoporučuje.</div></div>`
            : (j.error ? `<div class="rec-ai muted"><i class="ti ti-info-circle"></i> ${mpEsc(j.error)}</div>` : '');
    } catch (e) { recComment.html = ''; }
    const el2 = document.getElementById('rec-ai');
    if (el2) el2.innerHTML = recComment.html;
}

async function loadTrackRecord() {
    const box = document.getElementById('track-box');
    const statsEl = document.getElementById('track-stats');
    const listEl = document.getElementById('track-list');
    box.style.display = 'block';
    statsEl.innerHTML = '';
    listEl.innerHTML = '<div class="track-empty"><i class="ti ti-loader spin"></i> Načítám track record…</div>';
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/track-record`)).json();
        const wcol = w => w >= 50 ? 'var(--up)' : 'var(--down)';
        const s = (d && d.stats) || {};
        const MIN = 3; // málo dat = nezobrazovat zavádějící %
        const enough = s.buy_count && s.buy_count >= MIN;

        // Hlavní karty z backtestu (důkaz, ne 0 %): TOP tier + expectancy
        let cards = '';
        const bp = btBest();
        if (bp && bp.win_rate != null) {
            cards += `<div class="track-stat" style="border-color:rgba(0,230,118,0.4)"><div class="l">Trefnost signálu (ověřeno 2023–2026)</div><div class="v" style="color:${wcol(bp.win_rate)}">${bp.win_rate}%</div><div style="font-size:10px;color:var(--text2);margin-top:2px">${btProof.period || '10 let'}, ${btProof.sample_count} obchodů</div></div>`;
            cards += `<div class="track-stat"><div class="l">Očekávaný výnos / obchod</div><div class="v" style="color:${bp.avg_return >= 0 ? 'var(--up)' : 'var(--down)'}">${bp.avg_return >= 0 ? '+' : ''}${bp.avg_return}%</div><div style="font-size:10px;color:var(--text2);margin-top:2px">do 6 týdnů</div></div>`;
        }
        if (enough) {
            cards += `
                <div class="track-stat"><div class="l">Živých verdiktů „Koupit"</div><div class="v">${s.buy_count}</div></div>
                <div class="track-stat"><div class="l">Úspěšnost (živě)</div><div class="v" style="color:${wcol(s.buy_win_rate)}">${s.buy_win_rate}%</div></div>
                <div class="track-stat"><div class="l">Prům. výnos</div><div class="v" style="color:${s.buy_avg_return >= 0 ? 'var(--up)' : 'var(--down)'}">${s.buy_avg_return >= 0 ? '+' : ''}${s.buy_avg_return}%</div></div>`;
        }
        statsEl.innerHTML = cards;

        if (!d.ok || !d.count) {
            listEl.innerHTML = '<div class="track-empty">Živé verdikty se teprve sbírají – první přibudou s dnešní automatickou aktualizací (nebo po ručně spuštěné hloubkové analýze); postupně tu uvidíš reálnou úspěšnost v čase. Mezitím platí ověřená spolehlivost metody z backtestu (nahoře).</div>';
            return;
        }
        if (!enough) {
            listEl.innerHTML = `<div class="track-empty">Zatím málo živých verdiktů (${d.count}) na statistiku — sbírá se. Níže je jejich aktuální stav; souhrnná úspěšnost se ukáže po pár verdiktech.</div>` + trackListHTML(d.items);
            return;
        }
        listEl.innerHTML = trackListHTML(d.items);
    } catch (e) {
        listEl.innerHTML = '<div class="track-empty">Track record se nepodařilo načíst.</div>';
    }
}

function trackListHTML(items) {
        return (items || []).map(it => {
            const vt = (it.verdict || '').toLowerCase();
            const vcls = vt.includes('koup') ? 'buy' : (vt.includes('prod') ? 'sell' : 'hold');
            const ret = it.return_pct;
            const retTxt = ret != null
                ? `<span class="track-ret" style="color:${ret >= 0 ? 'var(--up)' : 'var(--down)'}">${ret >= 0 ? '+' : ''}${ret}%</span>`
                : '<span class="track-ret" style="color:var(--text2)">—</span>';
            const scoreTxt = (it.score != null) ? `<span class="track-score" title="MA Skóre">${it.score}</span>` : '';
            const exitCols = { target: 'var(--up)', stop: 'var(--down)', expired: '#FFC400', open: 'var(--text2)' };
            const srcTxt = it.src ? `<span class="track-src" style="margin:0 6px 0 0">${it.src === 'signal' ? 'signál dne' : 'koš tipů'}</span>` : '';
            const exitTxt = (it.exit_label || srcTxt)
                ? `<div class="tr-exit" style="color:${exitCols[it.exit_status] || 'var(--text2)'}">${srcTxt}${it.exit_label ? escapeHtml(it.exit_label) : ''}</div>` : '';
            return `<div class="track-row2">
                <div class="tr-top">
                    <span class="tk">${escapeHtml(it.ticker)}</span>
                    <span class="track-vdwrap"><span class="track-vd ${vcls}">${escapeHtml(it.verdict || '—')}</span>${scoreTxt}</span>
                    <span style="color:var(--text2);font-size:12px;text-align:right">${fmtDate(it.ts)}</span>
                    ${retTxt}
                </div>
                ${exitTxt}</div>`;
        }).join('');
}
