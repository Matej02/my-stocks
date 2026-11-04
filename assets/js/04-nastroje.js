// ====================== ONBOARDING TOUR ======================
let onbStep = 0;
const ONB_PAGES = 3;
function openOnbIfFirst() {
    try {
        if (localStorage.getItem('mystocks_onboarded')) return;
        if (!authToken || !hasAccess()) return;
        openOnb();
    } catch(e) {}
}
function openOnb() {
    onbStep = 0;
    showOnbStep();
    document.getElementById('onbModal').classList.add('open');
}
function showOnbStep() {
    document.querySelectorAll('#onbModal .onb-page').forEach(p => {
        p.style.display = (parseInt(p.dataset.page) === onbStep) ? 'block' : 'none';
    });
    const prog = document.getElementById('onb-prog');
    prog.innerHTML = Array.from({length: ONB_PAGES}, (_, i) =>
        `<span class="seg ${i <= onbStep ? 'active' : ''}"></span>`
    ).join('');
    const nextBtn = document.getElementById('onb-next');
    if (onbStep >= ONB_PAGES - 1) {
        nextBtn.innerHTML = 'Začít používat <i class="ti ti-check"></i>';
    } else {
        nextBtn.innerHTML = 'Dál <i class="ti ti-arrow-right"></i>';
    }
}
function nextOnb() {
    onbStep += 1;
    if (onbStep >= ONB_PAGES) { closeOnb(); return; }
    showOnbStep();
}
function closeOnb() {
    try { localStorage.setItem('mystocks_onboarded', '1'); } catch(e) {}
    document.getElementById('onbModal').classList.remove('open');
}

// ====================== POSITION SIZING KALKULAČKA ======================
function openSizer(prefill) {
    if (prefill) {
        if (prefill.price != null) document.getElementById('sz-price').value = prefill.price;
        if (prefill.stop_pct != null) document.getElementById('sz-stop').value = prefill.stop_pct;
    }
    document.getElementById('sz-out').className = 'sz-out';
    document.getElementById('sz-out').innerHTML = '';
    document.getElementById('sizerModal').classList.add('open');
}
function closeSizer() {
    document.getElementById('sizerModal').classList.remove('open');
}
function runSizer() {
    const cap = Math.max(0, parseFloat(document.getElementById('sz-cap').value) || 0);
    const riskPct = Math.max(0.01, parseFloat(document.getElementById('sz-risk').value) || 0);
    const px = Math.max(0.01, parseFloat(document.getElementById('sz-price').value) || 0);
    const stopPct = Math.max(0.1, parseFloat(document.getElementById('sz-stop').value) || 0);
    const fx = Math.max(0.01, parseFloat(document.getElementById('sz-fx').value) || 1);
    const out = document.getElementById('sz-out');
    if (!cap || !px) {
        out.className = 'sz-out';
        out.innerHTML = '<div class="ov-empty" style="color:var(--down)">Vyplň kapitál a cenu akcie.</div>';
        return;
    }
    const maxRisk = cap * (riskPct / 100);            // v té samé měně jako kapitál
    const stopAbs = px * (stopPct / 100);             // v USD na 1 akcii
    const riskPerShareInCap = stopAbs * fx;           // riziko 1 akcie v měně kapitálu
    const sharesRaw = maxRisk / riskPerShareInCap;
    const shares = Math.floor(sharesRaw);
    const posCost = shares * px * fx;
    const posCostUsd = shares * px;
    const exposureRatio = (posCost / cap) * 100;
    const stopLossDollar = shares * stopAbs;
    const stopLossInCap = stopLossDollar * fx;
    const stopPrice = px * (1 - stopPct / 100);
    const warn = exposureRatio > 100 ? '<div class="warn">⚠ Pozice je větší než tvůj kapitál — sniž riziko nebo zvyš stop-loss.</div>' : '';
    const fmt = (v) => v.toLocaleString('cs-CZ', {maximumFractionDigits: 0});
    out.className = 'sz-out has';
    out.innerHTML = `
        <div class="res-hero">${shares} ks akcií</div>
        <div class="res-line"><span>Vstupní cena</span><b>${px.toFixed(2)} USD</b></div>
        <div class="res-line"><span>Hodnota pozice</span><b>${fmt(posCost)} ${fx === 1 ? 'USD' : 'Kč'} <span style="color:var(--text2);font-weight:400">(${fmt(posCostUsd)} USD)</span></b></div>
        <div class="res-line"><span>Stop-loss cena</span><b>${stopPrice.toFixed(2)} USD</b></div>
        <div class="res-line"><span>Max ztráta při stop-lossu</span><b style="color:var(--down)">−${fmt(stopLossInCap)} ${fx === 1 ? 'USD' : 'Kč'}</b></div>
        <div class="res-line"><span>Riziko vs. kapitál</span><b>${(stopLossInCap / cap * 100).toFixed(2)} %</b></div>
        <div class="res-line"><span>Pozice / kapitál</span><b>${exposureRatio.toFixed(1)} %</b></div>
        ${warn}
        <div style="margin-top:10px; font-size:12px; color:var(--text2); line-height:1.5;">Klíč: ztrátu řídí stop-loss, ne počet akcií. Profi obchodník risuje na 1 obchodě obvykle <b>1–2 %</b> kapitálu — víc nedělej, dokud nemáš ověřený systém.</div>`;
}

// ====================== SROVNÁVAČ AKCIÍ ======================
let _cmpSlots = [null, null, null];  // { ticker, name }
let _cmpPickerSlotIdx = 0;
let _cmpPickerTimer = null;
let _cmpNameCache = {};  // ticker -> pretty name

function openCompare(prefill) {
    _cmpSlots = [null, null, null];
    if (Array.isArray(prefill)) {
        prefill.slice(0, 3).forEach((t, i) => {
            if (t) _cmpSlots[i] = { ticker: String(t).toUpperCase(), name: '' };
        });
    } else {
        // Předvyplň prvními 2 z watchlistu (ne 3 – aby uživatel viděl, že si může přidat)
        const wl = (myStocks || []).slice(0, 2);
        wl.forEach((t, i) => { _cmpSlots[i] = { ticker: t.toUpperCase(), name: '' }; });
    }
    renderCompareSlots();
    document.getElementById('cmp-result').innerHTML = '';
    document.getElementById('compareModal').classList.add('open');
}
function closeCompare() {
    closeCmpPicker();
    document.getElementById('compareModal').classList.remove('open');
}
function clearCompareSlots() {
    _cmpSlots = [null, null, null];
    renderCompareSlots();
    document.getElementById('cmp-result').innerHTML = '';
}

function renderCompareSlots() {
    const box = document.getElementById('cmp-slots');
    if (!box) return;
    box.innerHTML = _cmpSlots.map((s, i) => {
        if (!s) {
            return `<div class="cmp-slot" onclick="openCmpPicker(${i})">
                <div class="slot-lbl">Akcie ${i + 1}</div>
                <div class="slot-empty">
                    <i class="ti ti-plus"></i>
                    <span>Klikni a vyber</span>
                </div>
            </div>`;
        }
        const nm = s.name || _cmpNameCache[s.ticker] || niceName(s.ticker, s.ticker);
        return `<div class="cmp-slot filled" onclick="openCmpPicker(${i})">
            <button class="slot-x" onclick="event.stopPropagation();clearCmpSlot(${i})"><i class="ti ti-x"></i></button>
            <div class="slot-lbl">Akcie ${i + 1}</div>
            <div class="slot-tk">${escapeHtml(s.ticker)}</div>
            <div class="slot-nm">${escapeHtml(nm)}</div>
        </div>`;
    }).join('');
    const filledCount = _cmpSlots.filter(Boolean).length;
    const btn = document.getElementById('cmp-run-btn');
    if (btn) btn.disabled = filledCount < 2;
}

function clearCmpSlot(i) {
    _cmpSlots[i] = null;
    renderCompareSlots();
}

function openCmpPicker(slotIdx) {
    _cmpPickerSlotIdx = slotIdx;
    document.getElementById('cmp-picker-slot').textContent = slotIdx + 1;
    document.getElementById('cmp-picker-search').value = '';
    renderCmpPickerDefault();
    document.getElementById('cmp-picker').style.display = 'flex';
    setTimeout(() => document.getElementById('cmp-picker-search').focus(), 60);
}
function closeCmpPicker() {
    document.getElementById('cmp-picker').style.display = 'none';
}

function renderCmpPickerDefault() {
    const body = document.getElementById('cmp-picker-body');
    const wlData = lastWatchlistData || [];
    const already = new Set(_cmpSlots.filter(Boolean).map(s => s.ticker));
    const wl = wlData.filter(x => !already.has(x.ticker));
    let html = '';
    if (wl.length) {
        html += '<div class="cmp-picker-grp">Ze tvého portfolia</div>';
        html += wl.map(x => cmpPickerItemHTML({
            ticker: x.ticker, name: niceName(x.ticker, x.name), type: 'EQUITY'
        })).join('');
    }
    html += '<div class="cmp-picker-grp">Populární</div>';
    const popular = [
        { ticker:'AAPL', name:'Apple', type:'EQUITY' },
        { ticker:'MSFT', name:'Microsoft', type:'EQUITY' },
        { ticker:'NVDA', name:'NVIDIA', type:'EQUITY' },
        { ticker:'GOOGL', name:'Alphabet', type:'EQUITY' },
        { ticker:'AMZN', name:'Amazon', type:'EQUITY' },
        { ticker:'TSLA', name:'Tesla', type:'EQUITY' },
        { ticker:'META', name:'Meta', type:'EQUITY' },
        { ticker:'SPY', name:'S&P 500 ETF', type:'ETF' },
    ].filter(x => !already.has(x.ticker));
    html += popular.map(cmpPickerItemHTML).join('');
    body.innerHTML = html;
}

function cmpPickerItemHTML(item) {
    const t = item.type || 'EQUITY';
    const col = SEARCH_TYPE_COL[t] || 'var(--text2)';
    const badge = `<span class="type-badge" style="background:${col}22;color:${col};">${SEARCH_TYPE_CZ[t] || 'Akcie'}</span>`;
    return `<div class="cmp-picker-item" onclick="pickCmpTicker('${item.ticker}', '${(item.name||'').replace(/'/g, "\\'")}')">
        <div class="l">
            <span class="t">${escapeHtml(item.ticker)}</span>
            <span class="n">${escapeHtml(item.name || item.ticker)}</span>
        </div>
        ${badge}
    </div>`;
}

function pickCmpTicker(ticker, name) {
    _cmpSlots[_cmpPickerSlotIdx] = { ticker: ticker.toUpperCase(), name: name || '' };
    _cmpNameCache[ticker.toUpperCase()] = name || '';
    closeCmpPicker();
    renderCompareSlots();
}

function onCmpPickerInput() {
    const q = document.getElementById('cmp-picker-search').value.trim();
    clearTimeout(_cmpPickerTimer);
    if (!q) { renderCmpPickerDefault(); return; }
    _cmpPickerTimer = setTimeout(() => cmpPickerSearch(q), 200);
}

async function cmpPickerSearch(q) {
    const body = document.getElementById('cmp-picker-body');
    body.innerHTML = '<div class="cmp-picker-empty"><i class="ti ti-loader spin"></i> Hledám…</div>';
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/search?q=${encodeURIComponent(q)}`)).json();
        const items = (d && d.results) || [];
        if (!items.length) {
            body.innerHTML = '<div class="cmp-picker-empty">Nic nenalezeno.</div>';
            return;
        }
        const already = new Set(_cmpSlots.filter(Boolean).map(s => s.ticker));
        const filtered = items.filter(x => !already.has(x.ticker));
        if (!filtered.length) {
            body.innerHTML = '<div class="cmp-picker-empty">Všechny výsledky už máš vybrané.</div>';
            return;
        }
        body.innerHTML = filtered.map(x => cmpPickerItemHTML({
            ticker: x.ticker, name: niceName(x.ticker, x.name), type: x.type || 'EQUITY'
        })).join('');
    } catch (e) {
        body.innerHTML = '<div class="cmp-picker-empty" style="color:var(--down)">Chyba vyhledávání.</div>';
    }
}

const _CMP_METRICS = [
    { key: 'price',                lbl: 'Cena',                fmt: (v, r) => v == null ? '—' : v.toFixed(2) + ' ' + (r.currency || ''),               better: 'n' },
    { key: 'change_pct',           lbl: 'Dnes',                fmt: (v) => v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%',                  better: 'high' },
    { key: 'ma_score',             lbl: 'MA skóre',            fmt: (v) => v == null ? '—' : v + ' / 100',                                              better: 'high' },
    { key: 'pe',                   lbl: 'P/E (TTM)',           fmt: (v) => v == null ? '—' : v.toFixed(1),                                              better: 'lowpos' },
    { key: 'market_cap_musd',      lbl: 'Tržní kap.',          fmt: (v) => v == null ? '—' : (v >= 1000 ? (v/1000).toFixed(1) + ' B$' : Math.round(v) + ' M$'), better: 'n' },
    { key: 'revenue_growth_pct',   lbl: 'Růst tržeb YoY',      fmt: (v) => v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(1) + '%',                  better: 'high' },
    { key: 'profit_margin_pct',    lbl: 'Čistá marže',         fmt: (v) => v == null ? '—' : v.toFixed(1) + '%',                                        better: 'high' },
    { key: 'roe_pct',              lbl: 'ROE',                 fmt: (v) => v == null ? '—' : v.toFixed(1) + '%',                                        better: 'high' },
    { key: 'debt_to_equity',       lbl: 'Dluh / vlastní kap.', fmt: (v) => v == null ? '—' : v.toFixed(2),                                              better: 'low' },
    { key: 'target_upside_pct',    lbl: 'Cíl analytiků',       fmt: (v) => v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(1) + '%',                  better: 'high' },
    { key: 'rsi',                  lbl: 'RSI(14)',             fmt: (v) => v == null ? '—' : v.toFixed(0),                                              better: 'n' },
    { key: 'volatility_pct',       lbl: 'Volatilita',          fmt: (v) => v == null ? '—' : v.toFixed(1) + '%',                                        better: 'low' },
];

function _cmpRank(values, dir) {
    const vals = values.map((v, i) => ({ i, v })).filter(x => typeof x.v === 'number' && !isNaN(x.v));
    if (vals.length < 2) return {};
    let best, worst;
    if (dir === 'high') {
        best = vals.reduce((a, b) => b.v > a.v ? b : a);
        worst = vals.reduce((a, b) => b.v < a.v ? b : a);
    } else if (dir === 'low') {
        best = vals.reduce((a, b) => b.v < a.v ? b : a);
        worst = vals.reduce((a, b) => b.v > a.v ? b : a);
    } else if (dir === 'lowpos') {
        const pos = vals.filter(x => x.v > 0);
        if (pos.length < 2) return {};
        best = pos.reduce((a, b) => b.v < a.v ? b : a);
        worst = pos.reduce((a, b) => b.v > a.v ? b : a);
    } else return {};
    return best.v === worst.v ? {} : { best: best.i, worst: worst.i };
}

async function runCompare() {
    const tickers = _cmpSlots.filter(Boolean).map(s => s.ticker);
    const out = document.getElementById('cmp-result');
    if (tickers.length < 2) {
        out.innerHTML = '<div class="ov-empty" style="color:var(--down)">Vyber alespoň 2 akcie.</div>';
        return;
    }
    out.innerHTML = '<div class="ov-load"><i class="ti ti-loader spin"></i> Načítám data pro porovnání…</div>';
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/compare?tickers=${tickers.join(',')}`)).json();
        const rows = (d && d.results) || [];
        const missing = tickers.filter(t => !rows.some(r => r.ticker === t));
        if (rows.length < 2) {
            out.innerHTML = `<div class="ov-empty" style="color:var(--down)">Podařilo se načíst jen ${rows.length} z ${tickers.length} akcií. ${missing.length ? 'Chybí: ' + missing.join(', ') : ''}</div>`;
            return;
        }
        // Ulož pěkné jméno do cache pro budoucí zobrazení
        rows.forEach(r => { if (r.ticker && r.name) _cmpNameCache[r.ticker] = niceName(r.ticker, r.name); });
        const n = rows.length;
        const cols = `120px repeat(${n}, 1fr)`;
        const missingBanner = missing.length ? `<div style="background:var(--amber-soft); color:var(--amber); padding:10px 14px; border-radius:10px; margin-bottom:12px; font-size:13px;"><i class="ti ti-info-circle"></i> Nepodařilo se načíst: <b>${missing.join(', ')}</b>. Zkontroluj správnost tickeru.</div>` : '';
        out.innerHTML = missingBanner;
        // Hlavička
        let html = `<div class="cmp-table">`;
        html += `<div class="cmp-row head" style="grid-template-columns:${cols}"><div class="lbl">Ukazatel</div>` + rows.map(r => `
            <div class="cmp-ticker">
                <span class="t">${escapeHtml(r.ticker)}</span>
                <span class="n">${escapeHtml(r.name || '')}</span>
            </div>`).join('') + `</div>`;
        // Tělo
        _CMP_METRICS.forEach(m => {
            const values = rows.map(r => r[m.key]);
            const rank = _cmpRank(values, m.better);
            html += `<div class="cmp-row" style="grid-template-columns:${cols}"><div class="lbl">${m.lbl}</div>`;
            values.forEach((v, i) => {
                let cls = 'cmp-cell';
                if (rank.best === i) cls += ' best';
                else if (rank.worst === i) cls += ' worst';
                else if (v == null) cls += ' muted';
                html += `<div class="${cls}">${m.fmt(v, rows[i])}</div>`;
            });
            html += `</div>`;
        });
        html += `</div>`;
        out.innerHTML += html;  // připojit k missing banneru
    } catch (e) {
        out.innerHTML = '<div class="ov-empty" style="color:var(--down)">Srovnání se nepodařilo načíst.</div>';
    }
}

// „Jak hodnotíme?" odkazy teď otevírají jednotný panel O nás (bez prozrazení receptu)
function openMethod() { openAbout(); }
function openSim() { document.getElementById('simModal').classList.add('open'); runSim(); }
function closeSim() { document.getElementById('simModal').classList.remove('open'); }
function runSim() {
    // Ověřená PORTFOLIOVÁ výkonnost profilu (celé období 2015–2026, zbytek kapitálu
    // v indexu, max 5 pozic, max 2 na sektor). Dřív se tu skládal průměrný výnos
    // jednoho obchodu přes N obchodů s celým kapitálem – to dávalo fiktivní stovky %.
    const out = document.getElementById('sim-out');
    const cap = Math.max(0, parseFloat(document.getElementById('sim-cap').value) || 0);
    const yrs = Math.max(1, Math.min(parseInt(document.getElementById('sim-trades').value) || 1, 30));
    const v = btProof && btProof.validation;
    if (!v) { out.innerHTML = '<div style="color:var(--text2);font-size:13px;">Ověřená data se načítají…</div>'; return; }
    const prof = v.profiles[investorRisk] || v.profiles.balanced;
    const spy = v.benchmark.spy.full, me = prof.full;
    const grow = r => cap * Math.pow(1 + r / 100, yrs);
    const money = x => Math.round(x).toLocaleString('cs-CZ') + ' €';
    const label = ({conservative:'konzervativní', balanced:'vyvážený', aggressive:'agresivní'})[investorRisk] || 'vyvážený';
    const a = grow(spy.cagr), b = grow(me.cagr);
    out.innerHTML = `
        <div class="sim-compare">
            <div class="sim-side"><div class="l">Jen index (S&amp;P 500)</div><div class="big">${money(a)}</div>
                <div class="d">${spy.cagr} % ročně · nejhorší propad ${spy.max_dd} %</div></div>
            <div class="sim-vs">vs</div>
            <div class="sim-side win"><div class="l">Model, profil ${label}</div><div class="big">${money(b)}</div>
                <div class="d">${me.cagr} % ročně · nejhorší propad ${me.max_dd} %</div></div>
        </div>
        <div class="sim-diff">V nejhorší chvíli by portfolio bylo dočasně na <b>${money(cap * (1 + me.max_dd / 100))}</b> (index ${money(cap * (1 + spy.max_dd / 100))}).</div>
        <div style="font-size:12px;color:var(--text2);margin-top:10px;line-height:1.5;">
            Průměrné roční tempo z let 2015–2026 přenesené na ${yrs} ${yrs === 1 ? 'rok' : (yrs < 5 ? 'roky' : 'let')}. Na období 2023–2026, kdy trh táhlo pár gigantů, dal profil ${prof.holdout.cagr} % ročně vs index ${v.benchmark.spy.holdout.cagr} %. Skutečnost se bude lišit.
        </div>`;
}

function renderProfileView() {
    const authView = document.getElementById('profile-auth');
    const pfView = document.getElementById('profile-portfolio');
    const acct = document.getElementById('profile-acct');
    const localNote = document.getElementById('profile-local-note');
    const subTabs = document.getElementById('pf-subtabs');

    // Cloud zapnutý, ale nepřihlášen → ukaž jen přihlášení/registraci
    if (cloudEnabled && !authToken) {
        authView.style.display = 'block';
        pfView.style.display = 'none';
        setAuthTab(authTab);
        return;
    }

    authView.style.display = 'none';
    pfView.style.display = 'block';
    renderLevelButtons();
    renderRiskButtons();
    const st = document.getElementById('simple-toggle'); if (st) st.checked = investorSimple;

    // Nastavení (úroveň investora, zjednodušený režim, ranní souhrn) ukaž jen
    // uživateli s aktivním předplatným. Nový účet bez předplatného vidí jen plány.
    const hasAccess = !cloudEnabled || !!(mySub && (mySub.has_access || mySub.is_admin));
    const invRow = document.getElementById('level-row-investor');
    const simpleRow = document.getElementById('level-row-simple');
    const lvlHint = document.getElementById('level-hint');
    if (invRow) invRow.style.display = hasAccess ? 'flex' : 'none';
    if (simpleRow) simpleRow.style.display = hasAccess ? 'flex' : 'none';
    if (lvlHint) lvlHint.style.display = hasAccess ? 'block' : 'none';
    const nrow = document.getElementById('notif-row');
    if (nrow) {
        nrow.style.display = (authToken && hasAccess) ? 'flex' : 'none';
        document.getElementById('notif-morning').checked = !!(myNotif && myNotif.morning);
    }
    const narow = document.getElementById('notif-alert-row');
    if (narow) {
        narow.style.display = (authToken && hasAccess) ? 'flex' : 'none';
        const aEl = document.getElementById('notif-alerts');
        if (aEl) aEl.checked = !!(myNotif && myNotif.alerts);
    }
    const nwrow = document.getElementById('notif-week-row');
    if (nwrow) {
        nwrow.style.display = (authToken && hasAccess) ? 'flex' : 'none';
        const wEl = document.getElementById('notif-weekly');
        if (wEl) wEl.checked = !!(myNotif && myNotif.weekly);
    }
    // Referral panel — jen pro přihlášené
    const refBox = document.getElementById('ref-box');
    if (refBox) {
        refBox.style.display = authToken ? 'block' : 'none';
        if (authToken) loadReferral();
    }
    // Push notif toggle
    const npr = document.getElementById('notif-push-row');
    if (npr) {
        npr.style.display = (authToken && hasAccess) ? 'flex' : 'none';
        updateNotifPushBtn();
    }
    if (authToken) {
        acct.style.display = 'flex';
        localNote.style.display = 'none';
        document.getElementById('profile-acct-name').textContent = authUser;
        const nmEl = document.getElementById('profile-name-input');
        if (nmEl && document.activeElement !== nmEl) nmEl.value = myName;
        subTabs.style.display = 'flex';
        document.getElementById('pftab-admin').style.display = (mySub && mySub.is_admin) ? 'block' : 'none';
        renderSubStatus();
    } else {
        // Cloud vypnutý → lokální režim
        acct.style.display = 'none';
        localNote.style.display = 'flex';
        subTabs.style.display = 'none';
        document.getElementById('sub-status').style.display = 'none';
        document.getElementById('profile-cloud-cta').textContent =
            ' Synchronizace a předplatné se zapnou po připojení úložiště na Vercelu.';
    }
    if (authToken) setPfView(pendingPfView === 'admin' ? 'admin' : 'plans');
}

function setPfView(view) {
    if (view !== 'admin') view = 'plans';
    pendingPfView = view;
    ['plans', 'admin'].forEach(v => {
        const el = document.getElementById('pfview-' + v);
        const tab = document.getElementById('pftab-' + v);
        if (el) el.style.display = (v === view) ? 'block' : 'none';
        if (tab) tab.classList.toggle('active', v === view);
    });
    if (view === 'plans') renderPlans();
    if (view === 'admin') loadAdmin();
}

function renderSubStatus() {
    const el = document.getElementById('sub-status');
    if (!mySub) { el.style.display = 'none'; return; }
    el.style.display = 'flex';
    const eff = mySub.effective || 'start';
    let extra = '';
    if (mySub.plan === 'trial' && mySub.trial_active) {
        extra = `<span class="trial-pill">Zkušební verze · zbývá ${mySub.days_left} ${mySub.days_left === 1 ? 'den' : (mySub.days_left < 5 ? 'dny' : 'dní')}</span>`;
    } else if (mySub.plan === 'comped') {
        extra = `<span class="trial-pill">Přístup zdarma (zvací kód)</span>`;
    } else if (mySub.plan === 'trial' && !mySub.trial_active) {
        extra = `<span style="color:var(--text2);font-size:12px;">Zkušební verze skončila</span>`;
    }
    el.innerHTML = `<span style="font-size:13px;color:var(--text2)">Tvůj plán:</span>
        <span class="sub-badge ${eff}"><i class="ti ti-crown"></i> ${PLAN_NAMES[eff]}</span>
        ${extra}
        ${eff !== 'elite' ? '<button class="btn-primary" style="margin-left:auto;display:inline-flex;align-items:center;gap:6px;padding:8px 14px;flex:none;" onclick="setPfView(\'plans\')"><i class="ti ti-arrow-up"></i> Vylepšit</button>' : ''}`;
}

function setAuthTab(tab) {
    authTab = tab;
    document.getElementById('tab-login').classList.toggle('active', tab === 'login');
    document.getElementById('tab-register').classList.toggle('active', tab === 'register');
    document.getElementById('auth-submit-label').textContent = tab === 'login' ? 'Přihlásit se' : 'Vytvořit účet';
    document.getElementById('auth-invite-field').style.display = tab === 'register' ? 'flex' : 'none';
    const refField = document.getElementById('auth-ref-field');
    if (refField) refField.style.display = tab === 'register' ? 'flex' : 'none';
    document.getElementById('auth-forgot').style.display = tab === 'login' ? 'block' : 'none';
    document.getElementById('auth-hint').textContent = tab === 'login'
        ? 'Ještě nemáš účet? Klikni na „Registrovat“.'
        : 'S promo kódem máš 7 dní Pro zdarma + slevu. Bez kódu si po registraci vybereš plán.';
    document.getElementById('auth-error').textContent = '';
}

async function submitAuth() {
    const u = document.getElementById('auth-email').value.trim();
    const p = document.getElementById('auth-password').value;
    const errEl = document.getElementById('auth-error');
    const btn = document.getElementById('auth-submit');
    errEl.textContent = '';
    if (!u || !p) { errEl.textContent = 'Vyplň e-mail i heslo.'; return; }

    const endpoint = authTab === 'login' ? 'login' : 'register';
    const invite = document.getElementById('auth-invite').value.trim();
    let refCode = ((document.getElementById('auth-ref') || {}).value || '').trim().toUpperCase();
    if (!refCode) {
        try { refCode = (sessionStorage.getItem('mystocks_pending_ref') || '').toUpperCase(); } catch(e) {}
    }
    btn.disabled = true;
    try {
        const payload = { email: u, password: p };
        if (endpoint === 'register' && invite) payload.invite = invite;
        if (endpoint === 'register' && refCode) payload.ref_code = refCode;
        const r = await fetch(`${BACKEND_URL}/api/${endpoint}`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const d = await r.json();
        if (!d.ok) { errEl.textContent = d.error || 'Nepodařilo se.'; return; }

        authToken = d.token; authUser = d.user;
        mySub = d.subscription || null;
        myName = d.display_name || ''; myNameVoc = d.display_name_voc || '';
        localStorage.setItem('mystocks_token', authToken);
        localStorage.setItem('mystocks_user', authUser);

        if (endpoint === 'login') {
            applyPortfolio(d.portfolio);
            syncLotsFromCloud();
            if (mySub && mySub.is_admin) setTimeout(adminDailyKick, 4000);
        } else {
            // Nový účet začíná PRÁZDNÝ – žádné přednastavené akcie
            myStocks = [];
            localStorage.setItem('mystocks_portfolio', '[]');
        }
        document.getElementById('auth-password').value = '';
        document.getElementById('auth-invite').value = '';
        updateProfileBtn(); renderOverviewGreeting();
        showToast(endpoint === 'login' ? `Přihlášen jako ${authUser}` : `Účet ${authUser} vytvořen`, 'up', 'ti-user-check');

        // Vyhodnotit přístup: má plán → spustit appku, nemá → ukázat „vyber plán"
        if (refreshGate()) {
            closeProfile();
            if (!mainStarted) startMain(); else { await loadWatchlist(); cloudSync(); }
        } else {
            closeProfile();   // zeď „bez přístupu" se ukáže sama
        }
    } catch (e) {
        errEl.textContent = 'Chyba spojení se serverem.';
    } finally {
        btn.disabled = false;
    }
}

function doLogout() {
    authToken = null; authUser = null; mySub = null; myName = ''; myNameVoc = '';
    localStorage.removeItem('mystocks_token');
    localStorage.removeItem('mystocks_user');
    // Vyčistit citlivá data z tohoto zařízení (pozice + upozornění), watchlist na výchozí
    myAlerts = []; myStocks = DEFAULT_WATCHLIST.slice();
    planClearLocal();
    localStorage.setItem('mystocks_alerts', JSON.stringify(myAlerts));
    localStorage.setItem('mystocks_portfolio', JSON.stringify(myStocks));
    updateProfileBtn();
    closeProfile();
    showToast('Odhlášeno', 'down', 'ti-logout');
    refreshGate();   // ukáže přihlašovací zeď
}

async function forgotPassword() {
    const email = document.getElementById('auth-email').value.trim();
    const errEl = document.getElementById('auth-error');
    if (!email) { errEl.textContent = 'Nejdřív zadej svůj e-mail výše.'; return; }
    errEl.textContent = '';
    try {
        await fetch(`${BACKEND_URL}/api/forgot`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email })
        });
    } catch (e) {}
    showToast('Pokud účet existuje, poslali jsme odkaz na e-mail.', 'up', 'ti-mail');
}

// ====================== OBNOVENÍ HESLA (z odkazu) ======================
let resetToken = null;
function initResetFromUrl() {
    const params = new URLSearchParams(location.search);
    const t = params.get('reset');
    if (!t) return false;
    resetToken = t;
    document.getElementById('resetScreen').style.display = 'flex';
    setTimeout(() => document.getElementById('resetInput').focus(), 100);
    return true;
}

async function submitReset() {
    const pw = document.getElementById('resetInput').value;
    const pw2 = document.getElementById('resetInput2').value;
    const err = document.getElementById('resetError');
    err.textContent = '';
    if (!pw || pw.length < 6) { err.textContent = 'Heslo musí mít aspoň 6 znaků.'; return; }
    if (pw !== pw2) { err.textContent = 'Hesla se neshodují.'; return; }
    try {
        const r = await fetch(`${BACKEND_URL}/api/reset`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token: resetToken, password: pw })
        });
        const d = await r.json();
        if (!d.ok) { err.textContent = d.error || 'Nepodařilo se.'; return; }
        // Úspěch → odebrat ?reset z URL a otevřít přihlášení
        history.replaceState(null, '', location.pathname);
        document.getElementById('resetScreen').style.display = 'none';
        showToast('Heslo změněno. Přihlas se novým heslem.', 'up', 'ti-check');
        openProfile();
    } catch (e) {
        err.textContent = 'Chyba spojení se serverem.';
    }
}
