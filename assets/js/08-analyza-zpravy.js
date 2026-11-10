// ============ CENOVÁ UPOZORNĚNÍ ============
function openAlertModal() {
    if(!currentStockData) return;
    document.getElementById('alertModal-ticker').innerText = currentStockData.ticker;
    document.getElementById('alertModal-price').value = currentStockData.price.toFixed(2);
    setAlertDir('above');
    renderAlertList();
    document.getElementById('alertModal').classList.add('open');
}
function closeAlertModal() { document.getElementById('alertModal').classList.remove('open'); }
function setAlertDir(dir) {
    alertDir = dir;
    document.getElementById('alert-above').classList.toggle('active', dir==='above');
    document.getElementById('alert-below').classList.toggle('active', dir==='below');
}
function saveAlert() {
    if(!currentStockData) return;
    const price = parseFloat(document.getElementById('alertModal-price').value);
    if(!(price > 0)) { alert('Zadej platnou cenu.'); return; }
    myAlerts.push({ ticker: currentStockData.ticker, dir: alertDir, price, currency: currentStockData.currency });
    localStorage.setItem('mystocks_alerts', JSON.stringify(myAlerts));
    cloudSync();
    renderAlertList();
    requestNotif();
    showToast(`Upozornění nastaveno: ${currentStockData.ticker} ${alertDir==='above'?'≥':'≤'} ${price}`, 'up', 'ti-bell-plus');
}
function renderAlertList() {
    const list = document.getElementById('alertModal-list');
    const t = currentStockData ? currentStockData.ticker : null;
    const mine = myAlerts.filter(a => a.ticker === t);
    if(mine.length === 0) { list.innerHTML = '<div style="color:var(--text2); font-size:13px;">Zatím žádná upozornění pro tento titul.</div>'; return; }
    list.innerHTML = '';
    mine.forEach(a => {
        const idx = myAlerts.indexOf(a);
        list.innerHTML += `<div class="alert-row">
            <span><i class="ti ti-arrow-${a.dir==='above'?'up':'down'}"></i> ${a.dir==='above'?'Nad':'Pod'} <b>${a.price} ${a.currency||''}</b></span>
            <button onclick="removeAlert(${idx})"><i class="ti ti-trash"></i></button></div>`;
    });
}
function removeAlert(idx) {
    myAlerts.splice(idx, 1);
    localStorage.setItem('mystocks_alerts', JSON.stringify(myAlerts));
    cloudSync();
    renderAlertList();
}
function checkAlerts(results) {
    if(myAlerts.length === 0) return;
    const triggered = [];
    myAlerts.forEach(a => {
        const s = results.find(r => r.ticker === a.ticker);
        if(!s) return;
        if((a.dir === 'above' && s.price >= a.price) || (a.dir === 'below' && s.price <= a.price)) {
            triggered.push(a);
            const up = a.dir === 'above';
            showToast(`${a.ticker} ${up?'překročil':'klesl pod'} ${a.price} ${a.currency||''} — nyní ${s.price.toFixed(2)}`, up?'up':'down', 'ti-bell-ringing');
            notify(`${a.ticker} cílová cena dosažena`, `${up?'≥':'≤'} ${a.price} • aktuálně ${s.price.toFixed(2)} ${s.currency}`);
        }
    });
    if(triggered.length) {
        myAlerts = myAlerts.filter(a => !triggered.includes(a));
        localStorage.setItem('mystocks_alerts', JSON.stringify(myAlerts));
        cloudSync();
    }
}

// ============ NOTIFIKACE & TOASTY ============
function requestNotif() {
    if(!('Notification' in window)) return;
    if(Notification.permission === 'default') Notification.requestPermission();
}
function notify(title, body) {
    if(('Notification' in window) && Notification.permission === 'granted') {
        try { new Notification(title, { body }); } catch(e) {}
    }
}
function showToast(msg, kind='up', icon='ti-bell') {
    const wrap = document.getElementById('toastWrap');
    const el = document.createElement('div');
    el.className = 'toast ' + kind;
    el.innerHTML = `<i class="ti ${icon}"></i><div style="font-size:14px;">${msg}</div>`;
    wrap.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateX(40px)'; el.style.transition='all .4s'; }, 5000);
    setTimeout(() => el.remove(), 5500);
}

// ============ NEWS FEED ============
// ====================== SOUHRNNÉ TECHNICKÉ HODNOCENÍ (vlastní budík) ======================
// ====================== HLOUBKOVÁ ANALÝZA (server-side AI) ======================
function escapeHtml(s) {
    return (s == null ? '' : String(s)).replace(/[&<>"]/g, c =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function resetDeep() {
    const out = document.getElementById('deep-output');
    const btn = document.getElementById('deep-btn');
    if (btn) btn.disabled = false;
    if (out) out.innerHTML = '<div class="deep-teaser">Klikni na <b>Vygenerovat</b> pro kompletní rozbor: verdikt, cílová cena, scénáře, rizika a katalyzátory.</div>';
}

async function runDeepAnalysis() {
    if (!currentStockData) return;
    const out = document.getElementById('deep-output');
    const btn = document.getElementById('deep-btn');

    if (cloudEnabled && !authToken) {
        out.innerHTML = '<div class="deep-teaser">Pro hloubkovou analýzu se přihlas v profilu.</div>';
        openProfile(); return;
    }
    if (!hasFeature('pro')) {
        out.innerHTML = lockedCardHTML('Hloubková analýza', 'pro',
            'Kompletní rozbor: verdikt, cílová cena, scénáře, rizika a katalyzátory. '
            + 'V plánu Pro 5 analýz denně, v Elite 30 denně.');
        return;
    }
    btn.disabled = true;
    out.innerHTML = `<div class="deep-teaser"><i class="ti ti-loader spin"></i> Analyzuji ${escapeHtml(currentStockData.ticker)}… může to pár sekund trvat.</div>`;
    try {
        const r = await fetch(`${BACKEND_URL}/api/analysis/${encodeURIComponent(currentStockData.ticker)}`,
            { method: 'POST', headers: authHeaders() });
        const d = await r.json();
        if (!d.ok) {
            out.innerHTML = `<div class="deep-teaser" style="color:var(--down)">${escapeHtml(d.error || 'Nepodařilo se.')}</div>`;
            if (d.upgrade) setTimeout(() => openProfile('plans'), 900);
            return;
        }
        renderDeepReport(d);
    } catch (e) {
        out.innerHTML = '<div class="deep-teaser" style="color:var(--down)">Chyba spojení se serverem.</div>';
    } finally {
        btn.disabled = false;
    }
}

function renderDeepReport(d) {
    const rp = d.report || {};
    const cur = d.currency || '';
    const vt = (rp.verdict || '').toLowerCase();
    const vcls = vt.includes('koup') ? 'buy' : (vt.includes('prod') ? 'sell' : 'hold');
    const fmt = x => (x != null && !isNaN(x)) ? Number(x).toFixed(2) + ' ' + cur : '—';
    const scen = rp.scenarios || {};
    const sec = (icon, title, txt) => txt ? `<div class="deep-sec"><h4><i class="ti ${icon}"></i> ${title}</h4><p>${escapeHtml(txt)}</p></div>` : '';
    const risks = (rp.risks || []).map(x => `<li>${escapeHtml(x)}</li>`).join('');
    const cats = (rp.catalysts || []).map(x => `<li>${escapeHtml(x)}</li>`).join('');

    // Transparentní rozpad pravidlového modelu (z čeho verdikt vychází)
    const colFor = s => s >= 70 ? 'var(--up)' : (s >= 45 ? '#FFC400' : 'var(--down)');
    const m = d.model;
    const lv = d.levels;
    const modelHtml = (m && m.pillars && m.pillars.length) ? `
        <div class="deep-model">
            <div class="dm-head">
                <span class="t"><i class="ti ti-adjustments-bolt"></i> Z čeho verdikt vychází</span>
                <span class="dm-cov"><b>MA Skóre ${m.score}/100</b> · pokrytí daty ${m.coverage_pct}%</span>
            </div>
            ${m.pillars.map(p => `
                <div class="dm-row">
                    <span class="dm-lbl">${escapeHtml(p.label)}</span>
                    <span class="dm-bar"><span style="width:${Math.max(0, Math.min(100, p.score))}%;background:${colFor(p.score)}"></span></span>
                    <span class="dm-val" style="color:${colFor(p.score)}">${p.score}</span>
                    ${p.note ? `<span class="dm-note">${escapeHtml(p.note)}</span>` : ''}
                </div>`).join('')}
            <div class="dm-foot">Verdikt i jistota jsou spočítané z těchto pilířů (váhy podle dostupných dat). Slovní rozbor je jen doplněk, verdikt nemění. <a class="profile-cloud-link" onclick="openMethod()">Jak hodnotíme?</a></div>
        </div>` : '';

    // Důkaz spolehlivosti z backtestu – vedeno přes TOP tier + expectancy (pokud je cache)
    const bp = btBest();
    const bpLabel = (btProof && btProof.top && btProof.top.count) ? 'TOP signál' : 'signál Koupit';
    const expTxt = (bp && bp.avg_return != null) ? ` · očekávaný výnos <b>${bp.avg_return >= 0 ? '+' : ''}${bp.avg_return}%</b>/obchod` : '';
    const alphaTxt = (bp && bp.alpha != null) ? ` · nad indexem ${bp.alpha >= 0 ? '+' : ''}${bp.alpha} b.` : '';
    const proofHtml = (bp && bp.count) ? `
        <div class="deep-proof"><i class="ti ti-shield-check"></i>
            <span><b>Spolehlivost naší metody</b> (platí obecně, ne predikce tohoto titulu): ${bpLabel} měl <b>${bp.win_rate}%</b> úspěšnost na ${btProof.horizon_days} dní${expTxt}${alphaTxt}.
            <span class="dp-sub">Ověřeno na historii — ${btProof.period ? btProof.period + ', ' : ''}${btProof.sample_count} obchodů, bez nahlížení do budoucna. ${(100 - bp.win_rate).toFixed(0)} % obchodů přesto skončí v mínusu — proto vždy se stop-lossem. <a class="profile-cloud-link" onclick="openMethod()">Jak hodnotíme?</a></span></span>
        </div>` : '';

    // Živé signály / katalyzátory (doplňkové, nevstupují do verdiktu)
    const live = (d.facts && d.facts.live_signals) || [];
    const liveHtml = live.length ? `
        <div class="deep-sec"><h4><i class="ti ti-bolt"></i> Živé signály (doplňkové)</h4>
        <ul class="deep-list">${live.map(x => `<li>${x.k === 'bull' ? '🟢' : (x.k === 'bear' ? '🔴' : '⚪')} ${escapeHtml(x.t)}</li>`).join('')}</ul>
        <div style="font-size:11px;color:var(--text2);margin-top:4px">Aktuální události (výsledky, revize analytiků, insideři) — kontext, ne součást backtestovaného verdiktu.</div></div>` : '';

    document.getElementById('deep-output').innerHTML = `
        <div class="deep-verdict">
            <span class="dv-badge ${vcls}">${escapeHtml(rp.verdict || '—')}</span>
            <div class="dv-meta">
                <div><div class="l">Sebejistota</div><div class="v">${rp.confidence != null ? rp.confidence + '%' : '—'}</div></div>
                <div><div class="l">Horizont</div><div class="v">${escapeHtml(rp.horizon || '—')}</div></div>
            </div>
        </div>
        ${rp.headline ? `<div class="deep-headline">${escapeHtml(rp.headline)}</div>` : ''}
        ${earnWarn(rp.earnings_in)}
        ${proofHtml}
        ${modelHtml}
        <div class="deep-levels">
            <div class="dl-cell"><div class="l">Vstup</div><div class="v">${fmt(rp.entry)}</div></div>
            <div class="dl-cell"><div class="l">Cíl</div><div class="v" style="color:var(--up)">${fmt(rp.target_price)}</div></div>
            <div class="dl-cell"><div class="l">Stop-loss</div><div class="v" style="color:var(--down)">${fmt(rp.stop_loss)}</div></div>
        </div>
        ${lv ? `<div class="deep-rr">Poměr zisk:riziko <b>${lv.rr ? lv.rr + ' : 1' : '—'}</b> · cíl <b style="color:var(--up)">+${lv.reward_pct}%</b> / stop <b style="color:var(--down)">−${lv.risk_pct}%</b> · horizont ~${lv.horizon_days} dní <span style="color:var(--text2)">(z volatility, ne odhad)</span></div>` : ''}
        ${lv ? `<div class="deep-exit"><b>🚪 Exit plán:</b> prodej při dosažení cíle <b>${fmt(rp.target_price)}</b>, při poklesu na stop <b>${fmt(rp.stop_loss)}</b>, nebo po ~2 týdnech, pokud nenastane ani jedno. <a class="profile-cloud-link" onclick="openMethod()">Jak hodnotíme?</a></div>` : ''}
        ${(rp.entry && lv && lv.risk_pct) ? `<div style="margin:8px 0 14px;"><button class="tool-btn" onclick="openSizer({price:${rp.entry}, stop_pct:${lv.risk_pct}})"><i class="ti ti-calculator"></i> Kolik koupit? (kalkulačka pozice)</button></div>` : ''}
        ${liveHtml}
        ${sec('ti-bulb', 'Investiční teze', rp.thesis)}
        ${sec('ti-building-bank', 'Fundament', rp.fundamentals)}
        ${sec('ti-chart-line', 'Technika', rp.technicals)}
        ${sec('ti-mood-smile', 'Sentiment', rp.sentiment)}
        ${(scen.bull || scen.base || scen.bear) ? `<div class="deep-sec"><h4><i class="ti ti-arrows-split"></i> Scénáře</h4><div class="deep-scen">
            ${scen.bull ? `<div class="scen-row bull"><b>📈 Býčí</b><br>${escapeHtml(scen.bull)}</div>` : ''}
            ${scen.base ? `<div class="scen-row base"><b>➡️ Základní</b><br>${escapeHtml(scen.base)}</div>` : ''}
            ${scen.bear ? `<div class="scen-row bear"><b>📉 Medvědí</b><br>${escapeHtml(scen.bear)}</div>` : ''}
        </div></div>` : ''}
        ${risks ? `<div class="deep-sec"><h4><i class="ti ti-alert-triangle"></i> Rizika</h4><ul class="deep-list">${risks}</ul></div>` : ''}
        ${cats ? `<div class="deep-sec"><h4><i class="ti ti-rocket"></i> Katalyzátory</h4><ul class="deep-list">${cats}</ul></div>` : ''}
        <div class="deep-usage">Dnes využito ${d.used_today}/${d.daily_limit} analýz</div>
        <div class="deep-disclaimer"><i class="ti ti-info-circle"></i> Sestaveno z tržních dat. Není to investiční doporučení — slouží jako podklad k vlastnímu rozhodnutí.</div>`;
}

// ====================== DOPORUČENÍ ANALYTIKŮ + FUNDAMENTY ======================
async function loadAnalyst(ticker, price) {
    const box = document.getElementById('analyst-box');
    box.style.display = 'none';
    if (!hasFeature('pro')) {
        box.style.display = 'block';
        document.getElementById('analyst-target').innerHTML = '';
        document.getElementById('analyst-recs').innerHTML =
            lockedCardHTML('Doporučení analytiků', 'pro', 'Hodnocení bank, cílové ceny a fundamenty.');
        return;
    }
    try {
        const res = await fetch(`${BACKEND_URL}/api/analyst/${encodeURIComponent(ticker)}`);
        const d = await res.json();
        if (!d.ok || !d.configured) return;

        // Doplnit P/E a kapitalizaci, pokud je Yahoo nedodal
        const peEl = document.getElementById('d-pe');
        if ((peEl.innerText === 'N/A' || peEl.innerText === '--') && d.pe_ratio != null)
            peEl.innerText = parseFloat(d.pe_ratio).toFixed(2);
        const capEl = document.getElementById('d-cap');
        if ((capEl.innerText === 'N/A' || capEl.innerText === '--' || !capEl.innerText) && d.market_cap)
            capEl.innerText = formatNum(d.market_cap);

        const hasTarget = d.target && d.target.mean;
        const hasRecs = d.recs && (d.recs.strongBuy + d.recs.buy + d.recs.hold + d.recs.sell + d.recs.strongSell) > 0;
        if (!hasTarget && !hasRecs) return;
        box.style.display = 'block';

        // Cílové ceny
        const tEl = document.getElementById('analyst-target');
        if (hasTarget) {
            const cur = price || 0;
            const upside = cur ? ((d.target.mean - cur) / cur * 100) : null;
            const upTxt = upside != null
                ? `<div class="at-upside ${upside>=0?'at-up':'at-down'}">${upside>=0?'+':''}${upside.toFixed(1)}% potenciál</div>` : '';
            tEl.innerHTML = `
                <div class="at-cell"><div class="l">Min. cíl</div><div class="v">${d.target.low ?? '—'}</div></div>
                <div class="at-cell mean"><div class="l">Průměrný cíl</div><div class="v">${d.target.mean}</div>${upTxt}</div>
                <div class="at-cell"><div class="l">Max. cíl</div><div class="v">${d.target.high ?? '—'}</div></div>`;
        } else { tEl.innerHTML = ''; }

        // Doporučení (bar)
        const rEl = document.getElementById('analyst-recs');
        if (hasRecs) {
            const r = d.recs;
            const total = r.strongBuy + r.buy + r.hold + r.sell + r.strongSell;
            const rows = [
                ['strongBuy', 'Silně kupovat', r.strongBuy],
                ['buy', 'Kupovat', r.buy],
                ['hold', 'Držet', r.hold],
                ['sell', 'Prodávat', r.sell],
                ['strongSell', 'Silně prodávat', r.strongSell],
            ];
            rEl.innerHTML = rows.map(([cls, lbl, n]) => `
                <div class="rec-row">
                    <span class="rec-lbl">${lbl}</span>
                    <span class="rec-bar-wrap"><span class="rec-bar ${cls}" style="width:${total?(n/total*100):0}%"></span></span>
                    <span class="rec-num">${n}</span>
                </div>`).join('') +
                (r.period ? `<div class="rec-period">Data k ${r.period} • ${total} analytiků</div>` : '');
        } else { rEl.innerHTML = ''; }
    } catch (e) { /* tiché selhání – panel zůstane skrytý */ }
}

async function loadEarningsHistory(ticker) {
    const box = document.getElementById('es-box');
    const grid = document.getElementById('es-grid');
    const sum = document.getElementById('es-summary');
    if (!box) return;
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/earnings/${encodeURIComponent(ticker)}`)).json();
        const items = (d && d.items) || [];
        const s = (d && d.summary) || {};
        if (!items.length) { box.style.display = 'none'; return; }
        box.style.display = 'block';
        // Souhrn: beat rate
        const br = s.beat_rate;
        if (br != null) {
            const cls = br >= 75 ? 'good' : (br < 50 ? 'bad' : '');
            sum.className = 'es-summary ' + cls;
            sum.textContent = `${s.beat_count}/${s.sample} beat · ${br}%`;
        } else {
            sum.textContent = '';
        }
        grid.innerHTML = items.slice().reverse().map(x => {
            const q = x.year && x.quarter ? `Q${x.quarter}/${String(x.year).slice(2)}` : (x.period || '');
            const sign = x.surprise_pct >= 0 ? '+' : '';
            return `<div class="es-cell ${x.result}" title="Actual: ${x.actual} | Est: ${x.estimate}">
                <div class="q">${escapeHtml(q)}</div>
                <div class="r">${sign}${x.surprise_pct}%</div>
                <div class="a">${x.result === 'beat' ? 'beat' : x.result === 'miss' ? 'miss' : 'na odhad'}</div>
            </div>`;
        }).join('');
    } catch(e) {
        box.style.display = 'none';
    }
}

async function loadTopHolders(ticker) {
    const box = document.getElementById('hd-box');
    const list = document.getElementById('hd-list');
    if (!box) return;
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/holders/${encodeURIComponent(ticker)}`)).json();
        const items = (d && d.items) || [];
        if (!items.length) { box.style.display = 'none'; return; }
        box.style.display = 'block';
        list.innerHTML = items.slice(0, 10).map(h => {
            const share = h.share ? h.share.toLocaleString('en-US') : '—';
            let chg = '', chgCls = 'neu', chgTxt = '';
            if (typeof h.change === 'number' && h.change !== 0) {
                chgCls = h.change > 0 ? 'up' : 'down';
                chgTxt = (h.change > 0 ? '+' : '') + h.change.toLocaleString('en-US');
            } else if (h.change === 0) {
                chgTxt = 'beze změny';
            }
            return `<div class="hd-row">
                <div class="n">${escapeHtml(h.name || '—')}</div>
                <div class="p">${share} akcií</div>
                <div class="c ${chgCls}">${chgTxt || 'nová'}</div>
            </div>`;
        }).join('');
    } catch(e) {
        box.style.display = 'none';
    }
}

let _currentNewsTicker = null;
let _currentNewsSrc = 'all';
async function loadNews(ticker) {
    _currentNewsTicker = ticker;
    _currentNewsSrc = 'all';
    document.querySelectorAll('.news-src').forEach(b => b.classList.toggle('active', b.dataset.src === 'all'));
    return loadNewsFromSource('all');
}
function setNewsSrc(src) {
    _currentNewsSrc = src;
    document.querySelectorAll('.news-src').forEach(b => b.classList.toggle('active', b.dataset.src === src));
    loadNewsFromSource(src);
}

// České překlady titulků: seznam se ukáže hned (anglicky), překlad se doplní, jakmile je hotový (a příště je uložený).
async function translateNewsTitles(ticker, scope) {
    const els = [...scope.querySelectorAll('.news-title[data-tr="1"]')];
    if (!els.length) return;
    try {
        const r = await fetch(`${BACKEND_URL}/api/news-translate`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ticker, titles: els.map(e => e.dataset.orig) }) });
        const d = await r.json();
        if (!d.ok || _currentNewsTicker !== ticker) return;
        els.forEach(e => {
            const cs = d.t && d.t[e.dataset.orig];
            if (!cs || !e.isConnected) return;
            e.textContent = cs;
            e.title = 'Originál: ' + e.dataset.orig;
            const meta = e.parentElement && e.parentElement.querySelector('.news-meta');
            if (meta) meta.insertAdjacentHTML('beforeend', '<span class="news-src-tag">přeloženo AI</span>');
        });
    } catch (e) {}
}

async function loadNewsFromSource(src) {
    const list = document.getElementById('news-list');
    if (!list) return;
    const ticker = _currentNewsTicker;
    if (!ticker) return;
    list.innerHTML = '<div style="color:var(--text2); font-size:13px;"><i class="ti ti-loader spin"></i> Načítám zprávy...</div>';
    try {
        if (src === 'all') {
            const d = await (await fetch(`${BACKEND_URL}/api/news-feed?ticker=${encodeURIComponent(ticker)}&limit=30`)).json();
            const items = (d && d.items) || [];
            if (!items.length) { list.innerHTML = '<div style="color:var(--text2); font-size:13px;">Nenašli jsme žádné aktuální zprávy k této akcii.</div>'; return; }
            list.innerHTML = items.map(n => {
                const when = n.ts ? formatNewsTime(n.ts) : '';
                const safe = n.link ? `href="${n.link}" target="_blank" rel="noopener noreferrer nofollow"` : '';
                const dom = n.source_domain ? `<span class="news-src-tag">z: ${escapeHtml(n.source_domain)}</span>` : '';
                const cz = /[ěščřžýáíéúůďťňó]/i.test(n.title || '') || /(^|\.)(kurzy|e15|roklen24|idnes)\.cz$/.test(n.source_domain || '');
                return `<a class="news-item" ${safe}>
                    <div class="news-title"${cz ? '' : ` data-tr="1" data-orig="${escapeHtml(n.title)}"`}>${escapeHtml(n.title)}</div>
                    <div class="news-meta">${when ? '<i class="ti ti-clock"></i> ' + when : ''}${dom}</div>
                </a>`;
            }).join('');
            translateNewsTitles(ticker, list);
        } else if (src === 'yahoo') {
            const d = await (await fetch(`${BACKEND_URL}/api/news/${encodeURIComponent(ticker)}`)).json();
            const arr = d && d.results;
            if (!arr || !arr.length) { list.innerHTML = '<div style="color:var(--text2); font-size:13px;">Žádné aktuální zprávy z Yahoo.</div>'; return; }
            list.innerHTML = arr.map(n => {
                const when = formatNewsTime(n.time);
                const safe = n.link ? `href="${n.link}" target="_blank" rel="noopener noreferrer"` : '';
                return `<a class="news-item" ${safe}>
                    <div class="news-title">${escapeHtml(n.title)}</div>
                    <div class="news-meta"><i class="ti ti-building-broadcast-tower"></i> ${escapeHtml(n.publisher || 'Zdroj')}${when ? ' • ' + when : ''}</div>
                </a>`;
            }).join('');
        } else if (['kurzy','kurzy_svet','roklen','e15','idnes','fool','cnbc','seekingalpha','marketwatch'].includes(src)) {
            const srcLabel = ({kurzy:'Kurzy.cz — akcie ČR', kurzy_svet:'Kurzy.cz — akcie svět', roklen:'Roklen24', e15:'E15', idnes:'iDnes Ekonomika', fool:'Motley Fool', cnbc:'CNBC', seekingalpha:'Seeking Alpha', marketwatch:'MarketWatch'})[src];
            const d = await (await fetch(`${BACKEND_URL}/api/rss?src=${src}&ticker=${encodeURIComponent(ticker)}`)).json();
            let items = (d && d.items) || [];
            let filtered = items.length;
            if (!items.length) {
                const d2 = await (await fetch(`${BACKEND_URL}/api/rss?src=${src}`)).json();
                items = (d2 && d2.items) || [];
                filtered = 0;
            }
            if (!items.length) { list.innerHTML = `<div style="color:var(--text2); font-size:13px;">Zdroj ${escapeHtml(srcLabel)} teď nevrací nic.</div>`; return; }
            const preface = filtered === 0 ? `<div style="color:var(--text2); font-size:12px; margin-bottom:10px;">Přímo pro <b>${escapeHtml(ticker)}</b> nic — zobrazuji nejnovější tržní přehled.</div>` : '';
            list.innerHTML = preface + items.slice(0, 15).map(n => {
                const when = formatRssTime(n.pub_date);
                const safe = n.link ? `href="${n.link}" target="_blank" rel="noopener noreferrer"` : '';
                const source = n.source || srcLabel;
                return `<a class="news-item" ${safe}>
                    <div class="news-title">${escapeHtml(n.title)}</div>
                    <div class="news-meta"><i class="ti ti-building-broadcast-tower"></i> ${escapeHtml(source)}${when ? ' • ' + when : ''}</div>
                </a>`;
            }).join('');
        } else if (src === 'sec') {
            const d = await (await fetch(`${BACKEND_URL}/api/sec/${encodeURIComponent(ticker)}`)).json();
            const items = (d && d.items) || [];
            const s = (d && d.summary) || {};
            if (!items.length) { list.innerHTML = '<div style="color:var(--text2); font-size:13px;">SEC nemá záznamy — pravděpodobně jde o neamerickou akcii nebo neveřejnou firmu.</div>'; return; }
            const sum = `<div class="sec-sum">
                <div class="sec-sum-cell"><div class="l">Insider (90 dní)</div><div class="v">${s.insider_90d ?? 0}</div></div>
                <div class="sec-sum-cell"><div class="l">Materiální události (90 d)</div><div class="v">${s.material_90d ?? 0}</div></div>
                <div class="sec-sum-cell"><div class="l">Poslední 10-Q</div><div class="v" style="font-size:14px;">${s.latest_10q_date || '—'}</div></div>
                <div class="sec-sum-cell"><div class="l">Poslední 10-K</div><div class="v" style="font-size:14px;">${s.latest_10k_date || '—'}</div></div>
            </div>`;
            const rows = `<div class="sec-list">${items.map(it => `
                <a class="sec-row" href="${it.url || '#'}" target="_blank" rel="noopener noreferrer">
                    <span class="form ${it.kind}">${escapeHtml(it.form)}</span>
                    <div class="meta">
                        <span class="nm">${escapeHtml(it.form_cz)}</span>
                        <span class="hint">${escapeHtml(it.hint)}</span>
                    </div>
                    <span class="date">${escapeHtml(it.date)}</span>
                </a>`).join('')}</div>`;
            list.innerHTML = sum + rows;
        }
    } catch(e) {
        list.innerHTML = '<div style="color:var(--down); font-size:13px;">Zdroj se nepodařilo načíst.</div>';
    }
}

function formatRssTime(pub) {
    if (!pub) return '';
    try {
        const dt = new Date(pub);
        if (isNaN(dt.getTime())) return pub;
        const now = new Date();
        const mins = Math.floor((now - dt) / 60000);
        if (mins < 60) return `před ${mins} min`;
        const hrs = Math.floor(mins / 60);
        if (hrs < 24) return `před ${hrs} h`;
        return dt.toLocaleDateString('cs-CZ', {day:'2-digit', month:'2-digit', year:'2-digit'});
    } catch (e) { return pub; }
}
function formatNewsTime(ts) {
    if(!ts) return '';
    let d;
    if(typeof ts === 'number') d = new Date(ts * 1000);
    else d = new Date(ts);
    if(isNaN(d.getTime())) return '';
    const diff = (Date.now() - d.getTime()) / 1000;
    if(diff < 3600) return Math.max(1,Math.round(diff/60)) + ' min';
    if(diff < 86400) return Math.round(diff/3600) + ' h';
    return d.toLocaleDateString('cs-CZ');
}

// ============ AI CHAT O AKCII ============
function resetChat(ticker) {
    chatHistory = [];
    const log = document.getElementById('chat-log');
    log.innerHTML = `<div class="chat-msg chat-ai">Ahoj! Zeptej se mě na cokoliv ohledně <b>${ticker}</b> — fundamenty, rizika, výhled, srovnání.</div>`;
    const sug = document.getElementById('chat-suggest');
    sug.innerHTML = '';
    ['Je teď dobrý čas nakoupit?','Jaká jsou hlavní rizika?','Shrň poslední vývoj','Pro koho je vhodná?'].forEach(q => {
        sug.innerHTML += `<span class="chat-chip" onclick="askChat('${q.replace(/'/g,"\\'")}')">${q}</span>`;
    });
}
function askChat(q) { document.getElementById('chat-input').value = q; sendChat(); }
async function sendChat() {
    const input = document.getElementById('chat-input');
    const q = input.value.trim();
    if(!q || !currentStockData) return;
    const log = document.getElementById('chat-log');

    log.innerHTML += `<div class="chat-msg chat-user">${escapeHtml(q)}</div>`;
    input.value = '';
    if(!hasFeature('elite')) {
        log.innerHTML += `<div class="chat-msg chat-ai" style="color:var(--text2)">Asistent je součástí plánu Elite. <a class="profile-cloud-link" onclick="openProfile('plans')">Zobrazit předplatné</a></div>`;
        log.scrollTop = log.scrollHeight; return;
    }
    const thinking = document.createElement('div');
    thinking.className = 'chat-msg chat-ai';
    thinking.innerHTML = '<i class="ti ti-loader spin"></i> Přemýšlím...';
    log.appendChild(thinking);
    log.scrollTop = log.scrollHeight;

    const d = currentStockData;
    const ind = d.indicators || {};
    const context = `Akcie ${d.ticker} (${d.name}). Cena ${d.price} ${d.currency}, změna ${d.change_pct}%. Sektor ${d.sector}/${d.industry}. P/E ${d.pe_ratio}, RSI ${ind.rsi}, 52T ${d.low52}-${d.high52}, Technický signál ${d.score}/100 (${d.score_label}). Beta ${d.beta}, dividenda ${d.dividend_yield}%.`;
    chatHistory.push({ role: 'user', content: q });

    const result = await serverAI('chat', { q, context, history: chatHistory.slice(-6) });
    thinking.remove();
    if(!result.ok) {
        log.innerHTML += `<div class="chat-msg chat-ai" style="color:var(--down)">${result.error || 'Nepodařilo se, zkus to znovu.'}</div>`;
        log.scrollTop = log.scrollHeight; return;
    }
    const answer = (result.answer || '').replace(/\*\*/g,'').trim();
    chatHistory.push({ role: 'assistant', content: answer });
    log.innerHTML += `<div class="chat-msg chat-ai">${escapeHtml(answer).replace(/\n/g,'<br>')}</div>`;
    log.scrollTop = log.scrollHeight;
}

// ============ EXPORT CSV ============
function exportCSV() {
    if(!lastWatchlistData.length) { alert('Žádná data k exportu.'); return; }
    const rows = [['Ticker','Cena','Měna','Změna %']];
    lastWatchlistData.forEach(s => {
        rows.push([s.ticker, s.price, s.currency, s.change_pct]);
    });
    const csv = rows.map(r => r.join(';')).join('\n');
    const blob = new Blob(["﻿"+csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `portfolio_${new Date().toISOString().slice(0,10)}.csv`;
    a.click(); URL.revokeObjectURL(url);
    showToast('Portfolio exportováno do CSV', 'up', 'ti-download');
}
