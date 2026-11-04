// ====================== MŮJ INVESTIČNÍ PLÁN (dlouhodobě) ======================
// Pozice se ukládají jako jednotlivé nákupy (loty) – 3letý časový test platí pro
// každý nákup zvlášť. Lokálně v prohlížeči, po přihlášení i v cloudu (vlastní
// klíč, ukládání watchlistu je nepřepíše). Výpočty dělá bezstavově /api/plan.
// Počty kusů zatím nezadáváme (rozhodnutí uživatele 2026-10-23, viz paměť my-stocks-qty-later):
// portfolio = seznam držených akcií a hodnocení každé z nich. Kód pro kusy/váhy/daňový test zůstává, jen je vypnutý.
const FEATURE_QTY = false;
let planLots = (() => { try { return JSON.parse(localStorage.getItem('ma_holdings') || '[]') || []; } catch (e) { return []; } })();
let planData = null, planLoading = false, planErr = '', planTs = 0;
let planAmount = (() => { try { return localStorage.getItem('ma_plan_amount') || ''; } catch (e) { return ''; } })();
let planShowDeposit = false;
// „Kam s penězi“ (režim jen názvů akcií): app = doporučení aplikace, mine = jen dokup mých silných, new = jen nové tipy, custom = vyberu si sám
let planDepMode = (() => { try { const v = localStorage.getItem('ma_dep_mode'); return ['app', 'mine', 'new', 'custom'].includes(v) ? v : 'app'; } catch (e) { return 'app'; } })();
let planDepN = 5, planDepPick = [], planDepAmts = {}, planDepDirty = false;
// Profil plánu = profil rizika appky, navíc „maximum" (jen pro dlouhodobý plán)
let planRisk = FEATURE_QTY ? (() => { try { return localStorage.getItem('ma_plan_risk') || 'maximum'; } catch (e) { return 'maximum'; } })() : 'maximum';
// 2026-10: výchozí je „Jen akcie" (hodnocení + koš tipů, bez ETF) – jednorázový přechod i pro dřívější volbu
(function () { try { if (!localStorage.getItem('ma_plan_mig2')) { planRisk = 'maximum'; localStorage.setItem('ma_plan_risk', 'maximum'); localStorage.setItem('ma_plan_mig2', '1'); } } catch (e) {} })();
let planPicksN = (() => { try { return parseInt(localStorage.getItem('ma_picks_n'), 10) === 6 ? 6 : 10; } catch (e) { return 10; } })();
let planStress = null, planStressKey = '', planStressLoading = false;
let planGoal = null, planGoalLoading = false;
let tipNews = null, tipNewsKey = '', tipNewsLoading = false;

const MP_SECTOR_CZ = {
    'Information Technology': 'Technologie', 'Health Care': 'Zdravotnictví', 'Financials': 'Finance',
    'Consumer Discretionary': 'Zbytná spotřeba', 'Consumer Staples': 'Základní spotřeba',
    'Communication Services': 'Komunikace a média', 'Industrials': 'Průmysl', 'Energy': 'Energetika',
    'Materials': 'Suroviny', 'Utilities': 'Energetické sítě', 'Real Estate': 'Nemovitosti', 'Ostatní': 'Ostatní (sektor neznám)',
};
const MP_RISK = {
    conservative: { label: 'Opatrný', desc: 'Jádro 80 % + 10 akcií po 2 %. Nejklidnější varianta, pořád s akciovým výběrem navíc.' },
    balanced: { label: 'Vyvážený', desc: 'Jádro 60 % + 20 akcií po 2 %. Doporučený střed: výběr akcií má váhu, index drží stabilitu.' },
    aggressive: { label: 'Odvážný', desc: 'Jádro 30 % + 25 akcií po 2,8 %. Nejvíc sázíš na výběr akcií – víc se odlišuješ od indexu, nahoru i dolů.' },
    maximum: { label: 'Jen akcie', desc: 'Hodnocení tvých akcií + koš nejsilnějších tipů z S&P 500. Vklad jde jen do akcií – žádné ETF. Koš se mění jednou měsíčně; nic se neprodává podle skóre.' },
};
const mpKc = v => (v == null || isNaN(v)) ? '–' : Math.round(v).toLocaleString('cs-CZ') + ' Kč';
const mpKcShort = v => {
    if (v == null || isNaN(v)) return '–';
    const a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toLocaleString('cs-CZ', { maximumFractionDigits: a >= 1e7 ? 1 : 2 }) + ' mil. Kč';
    return mpKc(v);
};
const mpPct = (v, d = 1) => (v == null || isNaN(v)) ? '–' : Number(v).toLocaleString('cs-CZ', { minimumFractionDigits: d, maximumFractionDigits: d }) + ' %';
const mpMonth = ym => ym ? ym.slice(5, 7).replace(/^0/, '') + '/' + ym.slice(0, 4) : '';
const mpBase = t => String(t || '').replace(/\.(DE|L|AS|MI|PA|SW|MU|SG|HM|F)$/i, '');
const mpQty = q => (q == null) ? '–' : Number(q).toLocaleString('cs-CZ', { maximumFractionDigits: q < 1 ? 4 : (q < 100 ? 3 : 1) });
const mpEsc = s => (typeof escapeHtml === 'function') ? escapeHtml(String(s == null ? '' : s)) : String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const mpDate = iso => iso ? `${parseInt(iso.slice(8, 10), 10)}. ${parseInt(iso.slice(5, 7), 10)}.` : '';
const MP_MONTH_CZ = ['leden', 'únor', 'březen', 'duben', 'květen', 'červen', 'červenec', 'srpen', 'září', 'říjen', 'listopad', 'prosinec'];
const mpDays = n => n === 0 ? 'dnes' : (n === 1 ? 'zítra' : `za ${n} ${n < 5 ? 'dny' : 'dní'}`);
function mpEarnHTML(er) {
    if (!er) return '';
    const when = er.when === 'pre' ? ', před otevřením burzy' : (er.when === 'post' ? ', po zavření burzy' : '');
    return `<div class="mp-earn${er.days <= 7 ? ' soon' : ''}"><i class="ti ti-calendar-event"></i> Výsledky <b>${mpDays(er.days)}</b> (${mpDate(er.date)}${when})${er.eps != null ? ` · analytici čekají zisk ${mpEsc(er.eps.toFixed(2).replace('.', ','))} $/akcii${er.n ? ` (${er.n} odhadů)` : ''}` : ''}${er.days <= 7 ? '<div class="n">Kolem výsledků akcie kolísá víc než obvykle – na hodnocení to nic nemění, jen s tím počítej.</div>' : ''}</div>`;
}
function mpShortHTML(sh) {
    if (!sh) return '';
    return `<div class="mp-short"><i class="ti ti-alert-triangle"></i> <b>Vysoký short interest</b> – uzavřít všechny sázky na pokles by trvalo ${mpEsc(String(sh.dtc).replace('.', ','))} dní obchodování (horní pětina S&amp;P 500, údaj k ${mpDate(sh.settle)}). Takové akcie za rok zaostávaly zhruba o 3 body za průměrem. Informace, ne signál k prodeji.</div>`;
}
const mpSigned = (v, d = 1) => (v == null || isNaN(v)) ? '–' : (v >= 0 ? '+' : '') + mpPct(v, d);
const mpThisMonth = () => { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0'); };

function saveLots(sync = true) {
    try { localStorage.setItem('ma_holdings', JSON.stringify(planLots)); } catch (e) {}
    planStress = null; planStressKey = ''; planGoal = null;
    if (sync && authToken) {
        fetch(`${BACKEND_URL}/api/holdings`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ holdings: planLots, risk: planRisk }) })
            .catch(() => {});
    }
}

// Po přihlášení: cloud má přednost; prázdný cloud dostane to, co je v zařízení.
async function syncLotsFromCloud() {
    if (!authToken) return;
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/holdings`, { headers: authHeaders() })).json();
        if (!d.ok) return;
        if (d.risk && MP_RISK[d.risk] && d.risk !== planRisk) { planRisk = d.risk; try { localStorage.setItem('ma_plan_risk', planRisk); } catch (e) {} }
        if ((d.holdings || []).length) {
            planLots = d.holdings; saveLots(false); planData = null;
            if (activeTab === 'portfolio') planOpen();
            renderOverviewPlan();
            if (typeof fetchOverviewPerf === 'function') { fetchOverviewPerf(); fetchOverviewAlerts(); renderOverviewEvents(); }
        } else if (planLots.length) {
            saveLots(true);
        }
    } catch (e) {}
}

function planClearLocal() {
    planLots = []; planData = null; planStress = null; planGoal = null;
    try { localStorage.removeItem('ma_holdings'); } catch (e) {}
}

function planOpen() {
    renderPlan();
    if (!planLoading && (!planData || Date.now() - planTs > 5 * 60 * 1000)) planRefresh();
}

function planOnRisk() {
    planSetRisk(investorRisk, true);
}

function planSetRisk(r, fromApp) {
    if (!MP_RISK[r]) return;
    if (!FEATURE_QTY && r !== 'maximum') return;          // indexové profily potřebují počty kusů
    if (r !== 'maximum' && !fromApp && r !== investorRisk) { setRisk(r); return; }   // setRisk zavolá planOnRisk
    planRisk = r;
    try { localStorage.setItem('ma_plan_risk', r); } catch (e) {}
    if (authToken && planLots.length) saveLots(true);
    if (r === 'maximum' && !fromApp) showToast('Profil plánu: Maximum (100 % akcie)', 'up', 'ti-flame');
    if (planData || planLots.length || planShowDeposit) planRefresh();
    else renderPlan();
}

async function planRefresh() {
    planLoading = true; planErr = '';
    renderPlan();
    try {
        const body = { holdings: FEATURE_QTY ? planLots : planNamesOnly(), risk: planRisk, picks_n: planPicksN };
        const amt = parseFloat(String(planAmount).replace(/\s/g, '').replace(',', '.'));
        if (planShowDeposit && amt > 0) {
            body.amount_czk = amt;
            if (!FEATURE_QTY) {
                body.deposit_mode = planDepMode; body.deposit_n = planDepN;
                if (planDepMode === 'custom') { body.deposit_pick = planDepPick; body.deposit_amounts = planDepAmts; }
            }
        }
        const r = await fetch(`${BACKEND_URL}/api/plan`, { method: 'POST', headers: authHeaders(), body: JSON.stringify(body) });
        let d;
        try { d = await r.json(); } catch (e) { throw new Error(`Server teď neodpověděl (HTTP ${r.status}) – tvoje akcie jsou uložené, zkus to za chvíli.`); }
        if (!d.ok) throw new Error(d.error || 'Plán se nepodařilo spočítat.');
        planData = d; planTs = Date.now(); planDepDirty = false;
    } catch (e) {
        planErr = e.message || 'Plán se nepodařilo spočítat.';
    }
    planLoading = false;
    if (planData && planData.picks && planData.picks.loading) planData.picks.loading = false;     // chyba serveru: zůstane oříznutý koš
    renderPlan();
    renderOverviewPlan();
    if (typeof activeTab !== 'undefined' && activeTab === 'ai' && hasFeature('elite')) renderRecommendations();
    if (FEATURE_QTY && planData && planData.rows && planData.rows.length) planLoadStress();
    planLoadTipNews();
}

// Zprávy k tipům z „Kam s penězi" a k tvým pozicím (co je nového a proč)
async function planLoadTipNews() {
    if (!planData) return;
    const names = {};
    const dep = (planData.deposit && planData.deposit.lines) || [];
    dep.filter(l => l.action !== 'core').slice(0, 14).forEach(l => { names[l.ticker] = l.name || l.ticker; });
    (planData.rows || []).filter(r => r.status !== 'core').sort((a, b) => b.weight - a.weight).slice(0, 8)
        .forEach(r => { names[r.ticker] = r.name; });
    ((planData.picks && planData.picks.new) || []).slice(0, 10).forEach(p => { names[p.ticker] = p.name; });
    const tickers = Object.keys(names).slice(0, 20);
    if (!tickers.length) return;
    const key = tickers.slice().sort().join(',');
    if (tipNewsLoading || (tipNews && tipNewsKey === key)) return;
    tipNewsLoading = true;
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/news/mine`, { method: 'POST', headers: authHeaders(),
            body: JSON.stringify({ tickers, names }) })).json();
        if (d.ok) { tipNews = d; tipNewsKey = key; }
    } catch (e) {}
    tipNewsLoading = false;
    if (activeTab === 'portfolio') renderPlan();
}

function planTipNewsHTML(ticker, ctx, status) {
    const it = tipNews && tipNews.ai && (tipNews.items || []).find(x => x.ticker === ticker);
    if (!it) return '';
    const cls = { 'pozitivní': 'up', 'negativní': 'down' }[it.impact] || 'mix';
    let note = '';
    if (it.severe) note = 'Vážná zpráva – nové nákupy tohoto titulu jsou na 30 dní pozastavené.';
    else if (ctx === 'buy') note = it.impact === 'negativní'
        ? 'Plán ji přesto kupuje: hodnota a momentum se počítají z dat a historicky reakce na zprávy dál nepokračovala spolehlivě.'
        : (it.impact === 'pozitivní' ? 'Kupuje se podle plánu – ne víc jen kvůli dobrému titulku.' : '');
    else note = it.impact === 'negativní'
        ? (status === 'plan' ? 'Držíme – jeden titulek plán nemění.' : 'Neprodávej v panice; o prodeji rozhoduje jen pravidlo po 3 letech.')
        : '';
    const src = (it.sources || [])[0];
    return `<div class="mp-news ${cls}">📰 <b>${mpEsc(it.what)}</b> ${mpEsc(it.why || '')}${src ? ` <a href="${mpEsc(src.link)}" target="_blank" rel="noopener noreferrer">(${mpEsc(src.src)})</a>` : ''}${note ? `<span class="pn">${note}</span>` : ''}</div>`;
}

function planPausedHTML(p) {
    const ps = (p && p.paused) || [];
    if (!ps.length) return '';
    return ps.map(x => {
        const r = (x.reasons || []).find(y => y.pause) || (x.reasons || [])[0] || {};
        return `<div class="mp-pause"><i class="ti ti-player-pause"></i><div><b>${mpEsc(mpBase(x.ticker))} – nákup pozastaven do ${mpEsc(x.until ? new Date(x.until).toLocaleDateString('cs-CZ', { day: 'numeric', month: 'numeric', year: 'numeric' }) : '30 dní')}.</b>
          ${mpEsc(r.reason || '')}${r.url ? ` <a href="${mpEsc(r.url)}" target="_blank" rel="noopener noreferrer" style="color:var(--text2)">zdroj</a>` : ''}.
          ${x.held ? 'Pozici držíš dál, jen se teď nedokupuje.' : 'Peníze šly dalšímu titulu v pořadí.'}
          <span style="color:var(--text3)">Pojistka proti vzácné katastrofě – historicky tyhle zprávy výnos nesnižovaly ani nezvyšovaly, trh je zpracuje hned.</span></div></div>`;
    }).join('');
}

function planDeposit(v) {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    if (v != null) planAmount = String(v);
    else { const el = document.getElementById('mp-amount'); if (el) planAmount = el.value; }
    const amt = parseFloat(String(planAmount).replace(/\s/g, '').replace(',', '.'));
    if (!(amt > 0)) { showToast('Zadej částku v Kč', 'down', 'ti-alert-circle'); return; }
    try { localStorage.setItem('ma_plan_amount', String(amt)); } catch (e) {}
    planShowDeposit = true;
    planRefresh().then(() => {
        const el = document.getElementById('mp-deposit');
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
}

// ---------- vykreslení ----------
// Když uživatel píše do políčka v sekci plánu, na pozadí doběhlé načítání (stres test,
// zprávy…) sekci NEPŘEKRESLÍ – dřív to zrušilo fokus, na telefonu zmizela klávesnice a
// psaní se přerušilo. Překreslí se hned po opuštění políčka.
let planPending = false;
function planTyping() {
    const ae = document.activeElement, root = document.getElementById('plan-root');
    return !!(ae && root && root.contains(ae) && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName));
}
function renderPlan() {
    const root = document.getElementById('plan-root');
    if (!root) return;
    if (planTyping()) { planPending = true; return; }
    planPending = false;
    if (!FEATURE_QTY) { renderPlanNames(root); return; }
    const d = planData;
    const has = planLots.length > 0;
    const pill = r => `<button class="risk-pill ${r === planRisk ? 'active' : ''}" onclick="planSetRisk('${r}')">${MP_RISK[r].label}</button>`;
    const riskPills = pill('maximum') + `<span class="mp-pill-sep">s indexem:</span>` + ['conservative', 'balanced', 'aggressive'].map(pill).join('');
    let html = `<div class="mp-box">
      <div class="mp-head">
        <div>
          <div class="mp-title"><i class="ti ti-compass"></i> Moje portfolio</div>
          <div class="mp-sub"><b>Hodnocení tvých akcií</b> (co držet, co přikoupit, co nedokupovat), <b>nové tipy z S&amp;P 500</b> a <b>kam dát peníze</b>. Jednou měsíčně – zprávy ani grafy hlídat nemusíš.</div>
        </div>
        <div class="mp-actions">
          <button class="tool-btn primary" onclick="openImport()"><i class="ti ti-camera"></i> ${has ? 'Aktualizovat ze screenshotu' : 'Nahrát screenshot'}</button>
          <button class="tool-btn" onclick="openLot()"><i class="ti ti-plus"></i> Přidat ručně</button>
          ${has ? `<button class="tool-btn" onclick="planRefresh()" title="Přepočítat"><i class="ti ti-refresh ${planLoading ? 'spin' : ''}"></i></button>` : ''}
        </div>
      </div>
      <div class="mp-riskrow"><span class="lbl">Režim</span>${riskPills}</div>
      <div class="mp-riskdesc">${MP_RISK[planRisk] ? MP_RISK[planRisk].desc : ''}</div>`;

    if (!has) {
        html += `<div class="mp-empty">
            <div class="mp-step"><span class="n">1</span><b>Screenshot z Trading 212</b>Otevři Portfolio a vyfoť seznam pozic – klidně víc obrázků, překryv nevadí.</div>
            <div class="mp-step"><span class="n">2</span><b>Rychlá kontrola</b>Appka přečte tituly a počty kusů a ověří je proti hodnotě v Kč. Ty jen potvrdíš.</div>
            <div class="mp-step"><span class="n">3</span><b>Plán a „Kam s penězi"</b>Uvidíš, co držet, co dokoupit a kam poslat každý další vklad.</div>
          </div>
          <div class="mp-note"><i class="ti ti-info-circle"></i><span>Začínáš od nuly? Nic nahrávat nemusíš – rovnou zadej částku níže a appka ti sestaví první nákup.</span></div>`;
    } else if (planLoading && !d) {
        html += `<div class="mp-progress"><i class="ti ti-loader spin"></i> Oceňuji ${planLots.length} nákupů v Kč a porovnávám s plánem…</div>`;
    } else if (planErr && !d) {
        html += `<div class="mp-note warn"><i class="ti ti-alert-triangle"></i><span>${mpEsc(planErr)} <a style="color:var(--accent);cursor:pointer" onclick="planRefresh()">Zkusit znovu</a></span></div>
          <div class="mp-sub" style="margin-top:10px">Uložené akcie: ${[...new Set(planLots.map(l => mpBase(l.ticker)))].map(t => '<b>' + mpEsc(t) + '</b>').join(', ')}</div>`;
    } else if (d) {
        html += planKpisHTML(d);
    }
    html += `</div>`;

    if (d && d.rows && d.rows.length) {
        html += planEvalHTML(d);
        html += planRowsHTML(d);
    }
    if (d && d.picks) html += planPicksHTML(d);
    html += planDepositHTML(d);
    if (d && d.rows && d.rows.length) {
        html += planTasksHTML(d);
        html += planXrayHTML(d);
        html += planStressHTML();
    }
    html += planGoalHTML(d);
    if (d) html += planWhyHTML(d);
    root.innerHTML = html;
    if (planGoal) planDrawFan();
}

// ---------- portfolio bez počtu kusů: seznam akcií + hodnocení každé z nich ----------
function planNamesOnly() {
    const seen = new Map();
    planLots.forEach(l => {
        if (!l || !l.ticker) return;
        const o = seen.get(l.ticker) || { ticker: l.ticker, qty: null, buy_month: null, name: l.name || '', size: null };
        if (!o.size && l.size) o.size = l.size;
        seen.set(l.ticker, o);
    });
    return [...seen.values()];
}
let planSizeT = null;
function planSetSize(ticker, k) {
    const cur = (planLots.find(l => l.ticker === ticker && l.size) || {}).size || null;
    const nv = cur === k ? null : k;                      // klik na aktivní = zrušit
    planLots = planLots.map(l => l.ticker === ticker ? Object.assign({}, l, { size: nv }) : l);
    saveLots();
    if (planData && planData.rows) planData.rows.forEach(r => { if (r.ticker === ticker) r.size = nv; });
    renderPlan();
    clearTimeout(planSizeT);
    planSizeT = setTimeout(() => planRefresh(), 700);     // přepočítá odhad vah, koncentraci a výkon
}

function planChangesHTML(d) {
    const ch = d && d.changes;
    if (!ch) return '';
    const items = ch.items || [];
    if (!ch.since) return `<div class="mp-card mp-chg"><h4><i class="ti ti-history"></i> Co se změnilo od včera</h4>
        <div class="mp-sub" style="margin:0">Dnes jsem si uložil výchozí stav – změny hodnocení tvých akcií začnu hlídat od zítřka. Když se některá zhorší, uvidíš to tady${myNotif && myNotif.alerts ? ' i v e-mailu' : ' (a v e-mailu, když si upozornění zapneš v profilu)'}.</div></div>`;
    if (!items.length) return `<div class="mp-card mp-chg"><h4><i class="ti ti-history"></i> Co se změnilo od včera</h4>
        <div class="mp-chg-none"><i class="ti ti-circle-check"></i> Od ${mpDate(ch.since)} se hodnocení tvých akcií nezměnilo.</div></div>`;
    return `<div class="mp-card mp-chg"><h4><i class="ti ti-history"></i> Co se změnilo od včera <span class="mp-chg-since">(od ${mpDate(ch.since)})</span></h4>
      ${items.map(c => `<div class="mp-chg-row is-${c.dir}"><span class="ar">${c.dir === 'down' ? '▼' : '▲'}</span><div>${mpEsc(c.text)}</div></div>`).join('')}
      <div class="mp-sub" style="margin:8px 0 0">Malé posuny kolem hranice nehlásím – jen změny, které něco znamenají.</div></div>`;
}

function planSectorsHTML(d) {
    const sm = d && d.sectors;
    if (!sm || !(sm.items || []).length) return '';
    const cols = ['var(--accent)', 'var(--up)', '#8FB0FF', 'var(--amber)', '#C792EA', '#5CD6C0', 'var(--text3)'];
    const top = sm.items.slice(0, 6);
    const nm = x => mpEsc(MP_SECTOR_CZ[x] || x);
    return `<div class="mp-sec">
        <div class="mp-sec-bar">${top.map((x, i) => `<i style="width:${x.pct}%;background:${cols[i % cols.length]}" title="${nm(x.sector)} ${mpPct(x.pct, 0)}"></i>`).join('')}</div>
        <div class="mp-legend">${top.map((x, i) => `<span><i class="mp-dot" style="background:${cols[i % cols.length]}"></i>${nm(x.sector)} <b>${mpPct(x.pct, 0)}</b></span>`).join('')}</div>
        <div class="mp-sub" style="margin:4px 0 0">${sm.sized ? 'Podle velikostí pozic, které jsi zadal (odhad).' : 'Podle počtu akcií – přesnější bude, když u akcií zadáš velikost (malá / střední / velká).'}</div>
        ${sm.warn ? `<div class="mp-flag" style="margin-top:6px">⚠ ${mpEsc(sm.warn)}</div>` : ''}
      </div>`;
}
function planRemove(ticker) {
    if (!confirm(`Odebrat ${mpBase(ticker)} z portfolia?`)) return;
    planLots = planLots.filter(l => l.ticker !== ticker);
    saveLots();
    if (planData && planData.rows) planData.rows = planData.rows.filter(r => r.ticker !== ticker);
    planData = null;
    if (planLots.length) planRefresh(); else renderPlan();
    renderOverviewPlan();
}

function renderPlanNames(root) {
    const d = planData, has = planLots.length > 0, n = new Set(planLots.map(l => l.ticker)).size;
    let html = `<div class="mp-box">
      <div class="mp-head">
        <div>
          <div class="mp-title"><i class="ti ti-compass"></i> Moje portfolio</div>
          <div class="mp-sub"><b>Hodnocení každé tvé akcie</b> (přikupuj / drž / nedokupuj), <b>nové tipy z S&amp;P 500</b> a <b>kam dát peníze</b>. Stačí seznam akcií – počty kusů nepotřebuju.</div>
        </div>
        <div class="mp-actions">
          <button class="tool-btn primary" onclick="openImport()"><i class="ti ti-camera"></i> ${has ? 'Aktualizovat ze screenshotu' : 'Nahrát screenshot'}</button>
          <button class="tool-btn" onclick="openLot()"><i class="ti ti-plus"></i> Přidat akcii</button>
          ${has ? `<button class="tool-btn" onclick="planRefresh()" title="Přepočítat"><i class="ti ti-refresh ${planLoading ? 'spin' : ''}"></i></button>` : ''}
        </div>
      </div>`;
    if (!has) {
        html += `<div class="mp-empty">
            <div class="mp-step"><span class="n">1</span><b>Seznam akcií</b>Vyfoť seznam z Trading 212 (stačí názvy nebo tickery) – nebo ho napiš.</div>
            <div class="mp-step"><span class="n">2</span><b>Rychlá kontrola</b>Appka najde tickery a ověří je na burze. Ty jen potvrdíš.</div>
            <div class="mp-step"><span class="n">3</span><b>Hodnocení každé akcie</b>U každé uvidíš, jestli přikupovat, držet, nebo nedokupovat – a k tomu nové tipy na nákup.</div>
          </div>
          <div class="mp-note"><i class="ti ti-info-circle"></i><span>Žádné portfolio zatím nemáš? Tipy na nákup najdeš níže i bez něj.</span></div>`;
    } else if (planLoading && !d) {
        html += `<div class="mp-progress"><i class="ti ti-loader spin"></i> Hodnotím tvoje akcie (${n})…</div>`;
    } else if (planErr && !d) {
        html += `<div class="mp-note warn"><i class="ti ti-alert-triangle"></i><span>${mpEsc(planErr)} <a style="color:var(--accent);cursor:pointer" onclick="planRefresh()">Zkusit znovu</a></span></div>
          <div class="mp-sub" style="margin-top:10px">Uložené akcie: ${[...new Set(planLots.map(l => l.ticker))].map(t => '<b>' + mpEsc(mpBase(t)) + '</b>').join(', ')}</div>`;
    } else if (d) {
        html += planNamesKpisHTML(d);
    }
    html += `</div>`;
    if (d && d.rows && d.rows.length) { html += planChangesHTML(d); html += planEvalHTML(d); html += planNamesHTML(d); }
    if (d && d.picks) html += planPicksHTML(d);
    html += planDepositHTML(d);
    if (d) html += planWhyHTML(d);
    root.innerHTML = html;
}

function planNamesKpisHTML(d) {
    const c = (d.evaluation && d.evaluation.counts) || {}, n = (d.rows || []).length;
    const rated = (c.strong || 0) + (c.hold || 0) + (c.weak || 0);
    return `<div class="mp-kpis">
        <div class="mp-kpi"><div class="l">Moje akcie</div><div class="v">${n}</div><div class="s">${rated} hodnoceno${n - rated > 0 ? `, ${n - rated} bez hodnocení` : ''}</div></div>
        <div class="mp-kpi"><div class="l">Silné</div><div class="v" style="color:var(--up)">${c.strong || 0}</div><div class="s">přikupuj / drž</div></div>
        <div class="mp-kpi"><div class="l">Slabší</div><div class="v" style="color:${c.weak ? 'var(--amber)' : 'var(--text)'}">${c.weak || 0}</div><div class="s">${c.weak ? 'nedokupuj' : 'žádná'}${c.hold ? ` · drž ${c.hold}` : ''}</div></div>
        ${d.perf && d.perf.n ? `<div class="mp-kpi"><div class="l">Od přidání (${mpDate(d.perf.since)})</div><div class="v" style="color:var(--${d.perf.ret >= d.perf.spy ? 'up' : 'amber'})">${mpSigned(d.perf.ret)}</div><div class="s">S&amp;P 500 ${mpSigned(d.perf.spy)} · ${d.perf.weighted ? 'podle velikostí' : 'rovnoměrně'}</div></div>`
          : `<div class="mp-kpi"><div class="l">Drž</div><div class="v">${c.hold || 0}</div><div class="s">nic neměň</div></div>`}
      </div>`;
}

function planNamesHTML(d) {
    const evBy = {}; ((d.evaluation && d.evaluation.items) || []).forEach(e => { evBy[e.ticker] = e; });
    const order = { strong: 0, hold: 1, weak: 2, na: 3 };
    const rows = d.rows.slice().sort((a, b) => {
        const ea = evBy[a.ticker] || {}, eb = evBy[b.ticker] || {};
        return (order[ea.level] ?? 4) - (order[eb.level] ?? 4) || (eb.pct_better ?? -1) - (ea.pct_better ?? -1) || String(a.ticker).localeCompare(String(b.ticker));
    });
    const cards = rows.map(r => {
        const ev = evBy[r.ticker] || { level: 'na', verdict: 'Zatím bez hodnocení', why: '' };
        const sec = r.status === 'core' ? 'ETF' : (MP_SECTOR_CZ[r.sector] || r.sector || '');
        const bits = [sec];
        if (ev.pe) bits.push(ev.pe >= 200 ? 'P/E velmi vysoké' : 'P/E ' + Math.round(ev.pe));
        if (ev.mom_pct != null) bits.push(`za rok ${ev.mom_pct >= 0 ? '+' : ''}${mpPct(ev.mom_pct, 0)}`);
        const price = r.price && !r.no_price ? `${mpQty(r.price)} ${mpEsc(r.currency || '')}` : '';
        const bar = ev.pct_better != null
            ? `<div class="mp-bar ${ev.level}"><i style="width:${ev.pct_better}%"></i></div><div class="mp-bar-l">Lepší než <b>${ev.pct_better} %</b> ${ev.source === 'eu' ? 'velkých evropských akcií (podle momenta)' : 'akcií v S&amp;P 500'} · #${ev.rank} z ${ev.of}</div>`
            : (ev.score != null ? `<div class="mp-bar ${ev.level}"><i style="width:${Math.max(0, Math.min(100, ev.score))}%"></i></div><div class="mp-bar-l">Orientační skóre <b>${ev.score}</b>/100 (technika, valuace, růst, analytici)</div>` : '');
        const flag = r.flag ? `<div class="mp-flag">${r.flag.pause ? '⏸ Nákup pozastaven' : '⚠'} – ${mpEsc(((r.flag.reasons || [])[0] || {}).reason || '')}</div>` : '';
        const news = r.status !== 'core' ? planTipNewsHTML(r.ticker, 'hold', 'plan') : '';
        if (ev.approx_weight != null) bits.push(`~${mpPct(ev.approx_weight, 0)} portfolia (odhad)`);
        const pi = d.perf && d.perf.items && d.perf.items[r.ticker];
        const perfL = pi ? `<div class="mp-stock-perf">Od ${mpDate(pi.since)}: <b style="color:var(--${pi.ret >= (pi.spy || 0) ? 'up' : 'amber'})">${mpSigned(pi.ret)}</b> · S&amp;P 500 ${mpSigned(pi.spy)}</div>` : '';
        const earnL = r.status !== 'core' ? mpEarnHTML(d.earnings && d.earnings[r.ticker]) + mpShortHTML(d.short && d.short[r.ticker]) : '';
        const sz = r.size || '';
        const sizeL = r.status !== 'core' ? `<div class="mp-size"><span>Velikost:</span>${['S', 'M', 'L'].map(k => `<button class="${sz === k ? 'on' : ''}" onclick="planSetSize('${mpEsc(r.ticker)}','${k}')">${{ S: 'malá', M: 'střední', L: 'velká' }[k]}</button>`).join('')}</div>` : '';
        return `<div class="mp-stock ${ev.level}">
          <div class="mp-stock-h">
            <div class="who"><span class="tk">${mpEsc(mpBase(r.ticker))}</span>${r.chg != null ? `<span class="chg" style="color:var(--${r.chg >= 0 ? 'up' : 'down'})">${r.chg >= 0 ? '+' : ''}${mpPct(r.chg, 1)}</span>` : ''}${price ? `<span class="px">${price}</span>` : ''}
              <div class="nm">${mpEsc(r.name)}</div></div>
            <span class="mp-verdict ${ev.level}">${mpEsc(ev.verdict)}</span>
            <button class="mp-icon-btn" onclick="planRemove('${mpEsc(r.ticker)}')" title="Odebrat z portfolia"><i class="ti ti-x"></i></button>
          </div>
          <div class="mp-stock-meta">${bits.filter(Boolean).map(mpEsc).join(' · ')}</div>
          ${bar}
          <div class="mp-ev-why">${mpEsc(ev.why || '')}</div>
          ${perfL}
          ${earnL}
          ${(ev.warn || []).map(w => `<div class="mp-flag">⚠ ${mpEsc(w)}</div>`).join('')}${flag}
          ${news}
          ${sizeL}
        </div>`;
    }).join('');
    return `<div class="mp-card"><h4><i class="ti ti-briefcase"></i> Moje akcie – hodnocení každé</h4>
      <div class="mp-sub">Od nejsilnější. Hodnocení říká, co dělat s <b>novými penězi</b> – prodávat podle něj nedoporučuji, na historii slabší akcie spolehlivě nezaostávaly.</div>
      <div class="mp-stocks">${cards}</div>
    </div>`;
}

function planKpisHTML(d) {
    const p = d.plan, rows = d.rows || [];
    const tot = d.total_czk || 0;
    const day = rows.reduce((s, r) => s + (r.chg != null ? r.value_czk * r.chg / (100 + r.chg) : 0), 0);
    const dayPct = tot ? day / (tot - day) * 100 : 0;
    const w = k => rows.filter(k).reduce((s, r) => s + r.weight, 0);
    const corePct = w(r => r.status === 'core'), planPct = w(r => r.status === 'plan'), legacyPct = Math.max(0, 100 - corePct - planPct);
    const acts = (p.actions || []).length;
    if (p.risk === 'maximum' && d.evaluation) {
        const c = d.evaluation.counts, ranked = c.strong + c.hold + c.weak;
        return `<div class="mp-kpis">
        <div class="mp-kpi"><div class="l">Hodnota</div><div class="v">${mpKcShort(tot)}</div>
          <div class="s ${day >= 0 ? 'up' : 'down'}">${day >= 0 ? '+' : '−'}${mpKc(Math.abs(day))} dnes (${day >= 0 ? '+' : '−'}${mpPct(Math.abs(dayPct), 2)})</div></div>
        <div class="mp-kpi"><div class="l">Silné akcie</div><div class="v" style="color:var(--up)">${c.strong}${ranked ? ' / ' + ranked : ''}</div><div class="s">mezi nejlepšími v S&amp;P 500</div></div>
        <div class="mp-kpi"><div class="l">Slabší</div><div class="v" style="color:${c.weak ? 'var(--amber)' : 'var(--text)'}">${c.weak}</div><div class="s">${c.weak ? 'nedokupovat' : 'žádná'}</div></div>
        <div class="mp-kpi"><div class="l">Úkoly</div><div class="v">${acts ? acts : '✓'}</div><div class="s">${acts ? 'doporučení níže' : 'nic dělat nemusíš'}</div></div>
      </div>`;
    }
    return `<div class="mp-kpis">
        <div class="mp-kpi"><div class="l">Hodnota</div><div class="v">${mpKcShort(tot)}</div>
          <div class="s ${day >= 0 ? 'up' : 'down'}">${day >= 0 ? '+' : '−'}${mpKc(Math.abs(day))} dnes (${day >= 0 ? '+' : '−'}${mpPct(Math.abs(dayPct), 2)})</div></div>
        <div class="mp-kpi"><div class="l">Indexové jádro</div><div class="v">${mpPct(corePct, 0)}</div><div class="s">cíl ${p.core_pct} %</div></div>
        <div class="mp-kpi"><div class="l">Akcie v plánu</div><div class="v">${p.slots_used} / ${p.slots}</div><div class="s">${p.slots - p.slots_used > 0 ? (p.slots - p.slots_used) + ' volných míst pro nové tituly' : 'všechna místa obsazená'}</div></div>
        <div class="mp-kpi"><div class="l">Úkoly</div><div class="v">${acts ? acts : '✓'}</div><div class="s">${acts ? 'doporučení níže' : 'nic dělat nemusíš'}</div></div>
      </div>
      <div class="mp-alloc">
        <div class="alloc-bar"><i class="a" style="width:${corePct}%"></i><i class="p" style="width:${planPct}%"></i><i class="h" style="width:${legacyPct}%"></i></div>
        <div class="mp-legend">
          <span><i class="mp-dot" style="background:var(--accent)"></i>Jádro <b>${mpPct(corePct, 0)}</b></span>
          <span><i class="mp-dot" style="background:var(--up)"></i>Akcie v plánu <b>${mpPct(planPct, 0)}</b></span>
          ${legacyPct > 0.5 ? `<span><i class="mp-dot" style="background:var(--amber)"></i>Mimo plán (držíš, nedokupuješ) <b>${mpPct(legacyPct, 0)}</b></span>` : ''}
          <span>Cíl: jádro ${p.core_pct} % · akcie ${p.stocks_pct} % (${p.slots} titulů po ${mpPct(p.slot_pct, 1)})</span>
        </div>
        ${rows.filter(r => r.no_qty).length ? `<div class="mp-note warn"><i class="ti ti-info-circle"></i><span>U <b>${rows.filter(r => r.no_qty).length}</b> ${rows.filter(r => r.no_qty).length === 1 ? 'akcie' : 'akcií'} neznám počet kusů. Appka ví, že je vlastníš (hlídá k nim zprávy a nenabídne je jako nový nákup), ale <b>nemůže je ocenit</b>, spočítat váhy ani navrhnout úkoly. Počet doplníš tužkou u pozice.</span></div>` : ''}
        ${d.missing && d.missing.length ? `<div class="mp-note warn"><i class="ti ti-alert-triangle"></i><span>Nenašel jsem cenu pro: <b>${d.missing.map(mpEsc).join(', ')}</b> – nejsou v hodnotě. Oprav ticker v pozicích.</span></div>` : ''}
      </div>`;
}

function planDepositHTML(d) {
    const dep = d && d.deposit;
    const quick = [2000, 5000, 10000, 25000].map(v => `<button onclick="planDeposit(${v})">${v.toLocaleString('cs-CZ')} Kč</button>`).join('');
    let html = `<div class="mp-card" id="mp-deposit">
      <h4><i class="ti ti-coins"></i> Kam s penězi</h4>
      <div class="mp-sub">${!FEATURE_QTY
        ? 'Napiš, kolik můžeš teď investovat, a vyber, jak to rozdělit – nechej poradit aplikaci, nebo si vyber sám. Žádné ETF, nic se neprodává.'
        : planRisk === 'maximum'
        ? 'Napiš, kolik můžeš teď investovat. Peníze půjdou rovnoměrně do koše tipů – akcie z koše, které už držíš, dokoupí, ostatní otevře. Žádné ETF, nic se neprodává.'
        : 'Napiš, kolik můžeš teď investovat. Peníze půjdou tam, kde nejvíc chybí do cíle – doplní jádro, dokoupí akcie z plánu, nebo otevřou nový titul. Nic se neprodává.'}</div>
      <div class="mp-dep-form">
        <div class="mp-input"><input id="mp-amount" type="text" inputmode="decimal" placeholder="10 000" value="${mpEsc(planAmount)}" oninput="planAmount=this.value" onkeydown="if(event.key==='Enter')planDeposit()"><span>Kč</span></div>
        <button class="btn-primary" style="flex:none; display:inline-flex; align-items:center; gap:6px;" onclick="planDeposit()"><i class="ti ti-arrows-split-2"></i> Rozdělit</button>
        <div class="mp-quick">${quick}</div>
      </div>`;
    if (!FEATURE_QTY) html += planDepModeHTML();
    if (planShowDeposit && planLoading && !dep) html += `<div class="mp-progress"><i class="ti ti-loader spin"></i> Počítám rozdělení…</div>`;
    if (dep) {
        const p = d.plan;
        html += `<div class="mp-dep-split">
            <span>Celkem <b>${mpKc(dep.amount_czk)}</b></span>
            ${dep.split.core ? `<span><i class="mp-dot" style="background:var(--accent)"></i> do jádra <b>${mpKc(dep.split.core)}</b></span>` : ''}
            ${dep.split.topup ? `<span><i class="mp-dot" style="background:var(--up)"></i> dokoupit <b>${mpKc(dep.split.topup)}</b></span>` : ''}
            ${dep.split.new ? `<span><i class="mp-dot" style="background:#8FB0FF"></i> nové tituly <b>${mpKc(dep.split.new)}</b></span>` : ''}
          </div>`;
        if (!FEATURE_QTY) html += planDepAdviceHTML(dep);
        if (dep.stocks_full && d.total_czk) {
            html += `<div class="mp-note warn"><i class="ti ti-scale"></i><span><b>Akciová část je plná.</b> V jednotlivých akciích mimo plán máš ${mpPct(dep.legacy_pct, 0)} portfolia, profil ${MP_RISK[p.risk].label.toLowerCase()} chce akcie celkem ${p.stocks_pct} %. Proto jde vklad do jádra, dokud se poměr nesrovná.${p.risk === 'maximum' ? '' : (p.risk === 'aggressive' ? ' Chceš víc akcií? Profil Maximum je 100 % akcie.' : ' Chceš víc akcií? Přepni profil na Odvážný nebo Maximum.')}</span></div>`;
        }
        html += `<div class="mp-lines" style="margin-top:10px">` + ((!FEATURE_QTY && planDepMode === 'custom' && planDepDirty) ? [] : dep.lines).map(l => {
            const b = { core: ['core', 'Jádro'], topup: ['topup', 'Dokoupit'], new: ['new', dep.mode === 'basket' ? 'Nový tip' : 'Nový titul'] }[l.action] || ['hold', l.action];
            const sh = l.shares != null ? `≈ ${mpQty(l.shares)} ks · ${mpQty(l.price)} ${mpEsc(l.currency || '')}` : '';
            return `<div class="mp-line click" title="Otevřít detail akcie" onclick="if(!event.target.closest('a'))loadStockDetail('${mpEsc(l.ticker)}')">
              <span class="mp-badge ${b[0]}">${b[1]}</span>
              <div class="nm"><div class="t">${mpEsc(mpBase(l.ticker))}<small>${mpEsc(l.name || '')}</small></div><div class="w">${mpEsc(l.why)}</div>${l.action !== 'core' ? planTipNewsHTML(l.ticker, 'buy') : ''}</div>
              <div class="amt"><div class="a">${mpKc(l.amount_czk)}</div><div class="q">${sh}</div></div>
            </div>`;
        }).join('') + `</div>`;
        html += planPausedHTML(p);
        if (dep.deferred && dep.deferred.length) {
            html += `<div class="mp-note"><i class="ti ti-clock"></i><span>Na řadu příště: <b>${dep.deferred.slice(0, 12).map(x => mpEsc(mpBase(x.ticker))).join(', ')}</b>${dep.deferred.length > 12 ? ' a další' : ''}. Nákup pod ${dep.min_order} Kč nedává smysl – každý měsíc dostanou peníze tituly s největší mezerou, takže se vystřídají.</span></div>`;
        }
        const showLines = (dep.lines || []).length && !(!FEATURE_QTY && planDepMode === 'custom' && planDepDirty);
        if (showLines) {
        html += `<div class="mp-btnrow">
            <button class="tool-btn" onclick="planCopy()"><i class="ti ti-copy"></i> Kopírovat seznam</button>
            <button class="tool-btn" onclick="openBought()"><i class="ti ti-circle-check"></i> ${FEATURE_QTY ? 'Nakoupeno – připsat do portfolia' : 'Koupil jsem – přidat do portfolia'}</button>
          </div>
          <div class="mp-note"><i class="ti ti-device-mobile"></i><span>V Trading 212: vyhledej ticker → <b>Koupit</b> → přepni na <b>hodnotu</b> a zadej částku v Kč. Zlomky akcií jsou v pořádku. ${FEATURE_QTY ? 'Po nákupu klikni „Nakoupeno" – appka si zapamatuje měsíc nákupu kvůli 3letému testu.' : 'Po nákupu klikni „Koupil jsem" – appka akcii přidá mezi tvoje a bude ji hodnotit.'}</span></div>`;
        }
    }
    return html + `</div>`;
}

const MP_DEP_DESC = {
    app: 'Aplikace sama vybere, kolik půjde do dokupu tvých nejsilnějších akcií a kolik do nových tipů z koše – podle toho, jak velké portfolio máš.',
    mine: 'Peníze půjdou jen do akcií, které už máš a které aplikace hodnotí jako silné (nejlepší první).',
    new: 'Peníze půjdou rovnoměrně do nejlepších nových tipů z koše, které ještě nemáš.',
    custom: 'Zaškrtni akcie, které chceš koupit. Částka se rozdělí rovnoměrně, u každé ji můžeš změnit.',
};
function planDepModeHTML() {
    const chip = (k, label, ic) => `<button class="risk-pill ${planDepMode === k ? 'active' : ''}" onclick="planDepSetMode('${k}')"><i class="ti ${ic}"></i> ${label}</button>`;
    const nChip = k => `<button class="risk-pill ${planDepN === k ? 'active' : ''}" onclick="planDepSetN(${k})">${k}</button>`;
    return `<div class="mp-dep-modes">${chip('app', 'Doporučení aplikace', 'ti-sparkles')}${chip('mine', 'Jen do mých akcií', 'ti-wallet')}${chip('new', 'Jen nové tipy', 'ti-basket')}${chip('custom', 'Vyberu si sám', 'ti-hand-click')}
        ${(planDepMode === 'mine' || planDepMode === 'new') ? `<span class="mp-pill-sep">do kolika akcií:</span>${[2, 3, 5, 6].map(nChip).join('')}` : ''}
      </div><div class="mp-sub" style="margin:8px 0 0">${MP_DEP_DESC[planDepMode]}</div>`;
}
function planDepAdviceHTML(dep) {
    let h = (dep.advice || []).map(a => `<div class="mp-note"><i class="ti ti-bulb"></i><span>${mpEsc(a)}</span></div>`).join('');
    if (planDepMode === 'custom' && (dep.candidates || []).length) h += planDepCustomHTML(dep);
    return h;
}
function planDepCustomHTML(dep) {
    const amt = parseFloat(String(planAmount).replace(/\s/g, '').replace(',', '.')) || 0;
    const sel = new Set(planDepPick), eq = sel.size && amt ? Math.round(amt / sel.size) : 0;
    const badge = c => c.group === 'new' ? '<span class="mp-pick-src vm">Nový tip</span>'
        : ({ strong: '<span class="mp-verdict strong">Silná</span>', hold: '<span class="mp-verdict hold">Drž</span>', weak: '<span class="mp-verdict weak">Slabší</span>' }[c.level] || '');
    const row = c => `<div class="mp-cand ${sel.has(c.ticker) ? 'on' : ''}">
        <label><input type="checkbox" ${sel.has(c.ticker) ? 'checked' : ''} onchange="planDepToggle('${mpEsc(c.ticker)}')"> <b>${mpEsc(mpBase(c.ticker))}</b> <small>${mpEsc(c.name)}</small> ${badge(c)}</label>
        ${sel.has(c.ticker) ? `<span class="mp-cand-a"><input type="text" inputmode="decimal" placeholder="${eq || ''}" value="${mpEsc(planDepAmts[c.ticker] || '')}" oninput="planDepAmt('${mpEsc(c.ticker)}', this.value)"> Kč</span>` : ''}
        <a class="mp-cand-d" onclick="loadStockDetail('${mpEsc(c.ticker)}')" title="Otevřít detail akcie"><i class="ti ti-chevron-right"></i></a></div>`;
    const ord = { strong: 0, hold: 1, weak: 2 };
    const mine = dep.candidates.filter(c => c.group === 'mine').sort((a, b) => (ord[a.level] ?? 3) - (ord[b.level] ?? 3) || (a.rank || 9999) - (b.rank || 9999));
    const nw = dep.candidates.filter(c => c.group === 'new');
    return `<div class="mp-cands">
        <div class="mp-cand-h">Moje akcie <small>(silné nahoře)</small></div>${mine.map(row).join('') || '<div class="mp-sub">Zatím žádné.</div>'}
        <div class="mp-cand-h">Nové tipy z koše</div>${nw.map(row).join('') || '<div class="mp-sub">Všechny tipy z koše už máš.</div>'}
        <button class="btn-primary" style="margin-top:10px" onclick="planDeposit()"><i class="ti ti-arrows-split-2"></i> Rozdělit mezi vybrané (${sel.size})</button>
      </div>`;
}
function planDepRecalc() {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    const el = document.getElementById('mp-amount'); if (el) planAmount = el.value;
    const amt = parseFloat(String(planAmount).replace(/\s/g, '').replace(',', '.'));
    if (!(amt > 0)) { renderPlan(); return; }          // bez částky jen přepni volbu
    planShowDeposit = true;
    planRefresh();
}
function planDepSetMode(k) { planDepMode = k; try { localStorage.setItem('ma_dep_mode', k); } catch (e) {} planDepRecalc(); }
function planDepSetN(k) { planDepN = k; planDepRecalc(); }
function planDepToggle(t) {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    const i = planDepPick.indexOf(t);
    if (i >= 0) { planDepPick.splice(i, 1); delete planDepAmts[t]; } else planDepPick.push(t);
    planDepDirty = true; renderPlan();
}
function planDepAmt(t, v) { if (String(v).trim()) planDepAmts[t] = v; else delete planDepAmts[t]; planDepDirty = true; }

function planTasksHTML(d) {
    const acts = d.plan.actions || [];
    const warns = (d.xray && d.xray.warnings) || [];
    if (!acts.length && !warns.length) return '';
    let html = `<div class="mp-card"><h4><i class="ti ti-checklist"></i> Co teď udělat</h4>
      <div class="mp-sub">Prodeje navrhujeme jen výjimečně a jen u kusů, které jsou v ČR bez daně (držené přes 3 roky).</div>`;
    acts.forEach(a => {
        html += `<div class="mp-task"><i class="ti ${a.type === 'trim' ? 'ti-scissors' : 'ti-trash'}"></i><div>
            <b>${a.type === 'trim' ? 'Oříznout' : 'Prodat'} ${mpEsc(mpBase(a.ticker))} za ≈ ${mpKc(a.amount_czk)}</b> – ${mpEsc(a.why)}</div></div>`;
    });
    warns.forEach(w => { html += `<div class="mp-task info"><i class="ti ti-eye"></i><div>${mpEsc(w)}</div></div>`; });
    return html + `</div>`;
}

function planTaxHTML(r) {
    if (r.tax_free) return `<div class="tax ok"><i class="ti ti-circle-check"></i> bez daně</div>`;
    if (r.unknown_qty > 0) return `<div class="tax unk"><a style="cursor:pointer" onclick="openLot('${mpEsc(r.ticker)}')"><i class="ti ti-calendar-question"></i> doplň měsíc nákupu</a></div>`;
    if (r.tax_free_qty > 0) return `<div class="tax wait">${mpQty(r.tax_free_qty)} ks bez daně, další od ${mpMonth(r.tax_next)}</div>`;
    return `<div class="tax wait">bez daně od ${mpMonth(r.tax_next || r.tax_free_from)}</div>`;
}

function planRowsHTML(d) {
    const badge = r => ({
        core: `<span class="mp-badge core">Jádro</span>`,
        plan: `<span class="mp-badge topup">V plánu #${r.vm_rank}</span>`,
        hold: `<span class="mp-badge hold">Mimo plán</span>`,
        other: `<span class="mp-badge other">Mimo S&amp;P 500</span>`,
    })[r.status] || '';
    const evBy = {}; ((d.evaluation && d.evaluation.items) || []).forEach(e => { evBy[e.ticker] = e; });
    const rows = d.rows.map(r => {
        const sec = r.status === 'core' ? 'ETF' : (MP_SECTOR_CZ[r.sector] || r.sector || '');
        const ev = evBy[r.ticker];
        const act = r.action ? `<div class="act">${{ trim: '✂ Oříznout', trim_wait: '⏳ Nad stropem', trim_check: '⚠ Nad stropem', sell: '✂ Prodat', sell_wait: '⏳ Úklid později' }[r.action.type] || ''}${r.action.amount_czk && r.action.type !== 'sell_wait' ? ' ≈ ' + mpKc(r.action.amount_czk) : ''}</div>` : '';
        const lots = (r.lots || []).length > 1 ? ` · ${r.lots.length} nákupy` : '';
        return `<div class="mp-row">
          <div style="min-width:0"><div class="tk">${mpEsc(mpBase(r.ticker))} ${r.chg != null ? `<span style="font-size:11.5px;font-weight:500;color:var(--${r.chg >= 0 ? 'up' : 'down'})">${r.chg >= 0 ? '+' : ''}${mpPct(r.chg, 1)}</span>` : ''}</div><div class="sm">${mpEsc(r.name)}</div></div>
          ${r.no_qty ? `<div><div class="num" style="color:var(--text2)">–</div><div class="sm">${r.no_price ? '<span style="color:var(--text3)">cenu teď nemám</span>' : `<a style="color:var(--accent);cursor:pointer" onclick="openLot('${mpEsc(r.ticker)}')">doplň počet kusů</a>`}</div></div>
          <div class="num" style="color:var(--text3)">–</div>`
          : `<div><div class="num">${mpKc(r.value_czk)}</div><div class="sm">${mpQty(r.qty)} ks${lots}</div></div>
          <div class="num">${mpPct(r.weight, 1)}</div>`}
          <div style="min-width:0">${ev ? `<span class="mp-verdict ${ev.level}">${mpEsc(ev.verdict)}</span>` : badge(r)}<div class="sm" style="margin-top:3px">${mpEsc(sec)}${ev && ev.rank ? ` · celkově #${ev.rank} z ${ev.of}` : ''}${ev && ev.mom_pct != null ? ` · za rok ${ev.mom_pct >= 0 ? '+' : ''}${mpPct(ev.mom_pct, 0)}` : ''}</div>${ev ? `<div class="mp-ev-why">${mpEsc(ev.why)}</div>` + (ev.warn || []).map(w => `<div class="mp-flag">⚠ ${mpEsc(w)}</div>`).join('') : ''}${act}${r.flag ? `<div class="mp-flag">${r.flag.pause ? '⏸ Nákup pozastaven' : '⚠'} – ${mpEsc(((r.flag.reasons || [])[0] || {}).reason || '')}</div>` : ''}</div>
          <div style="min-width:0">${r.no_qty ? '<div class="tax wait">–</div>' : planTaxHTML(r)}</div>
          <div><button class="mp-icon-btn" onclick="openLot('${mpEsc(r.ticker)}')" title="Upravit nákupy"><i class="ti ti-pencil"></i></button></div>
          ${r.status !== 'core' && planTipNewsHTML(r.ticker, 'hold', r.status) ? `<div style="grid-column:1 / -1">${planTipNewsHTML(r.ticker, 'hold', r.status)}</div>` : ''}
        </div>`;
    }).join('');
    return `<div class="mp-card"><h4><i class="ti ti-briefcase"></i> Moje akcie – co s nimi</h4>
      <div class="mp-sub">Vítěze necháváme běžet – titul, který vypadne z plánu, se jen přestane dokupovat. Prodat dává smysl až po 3 letech (bez daně) a jen když je pozice přerostlá nebo zakrnělá.</div>
      <div class="mp-table">
        <div class="mp-row hd"><div>Titul</div><div>Hodnota</div><div>Váha</div><div>Hodnocení</div><div>3letý test</div><div></div></div>
        ${rows}
      </div>
      <div class="mp-note"><i class="ti ti-receipt-tax"></i><span>Daně v ČR: zisk z akcií a ETF držených přes 3 roky je osvobozený (každý nákup se počítá zvlášť). Pokud za rok prodáš celkem do 100 000 Kč, je to bez daně i dřív. Ověř si v daňovém přehledu brokera, které kusy prodej bere.</span></div>
    </div>`;
}

function planXrayHTML(d) {
    const x = d.xray;
    if (!x) return '';
    const direct = {};
    (d.rows || []).forEach(r => { if (r.status !== 'core') direct[r.ticker.replace('-', '.')] = (direct[r.ticker.replace('-', '.')] || 0) + r.weight; });
    const maxPct = Math.max(...x.top.map(t => t.pct), 1);
    const top = x.top.map(t => {
        const dp = Math.min(t.pct, direct[t.ticker] || 0), ep = Math.max(0, t.pct - dp);
        return `<div class="mp-xrow"><div style="min-width:0"><div class="nm">${mpEsc(t.name)}<small>${mpEsc(mpBase(t.ticker))}</small></div>
          <div class="mp-xbar"><i style="width:${dp / maxPct * 100}%;background:var(--accent)"></i><i style="width:${ep / maxPct * 100}%;background:rgba(255,255,255,0.28)"></i></div></div>
          <div class="pc">${mpPct(t.pct, 1)}</div></div>`;
    }).join('');
    const maxS = Math.max(...x.sectors.map(s => s.pct), 1);
    const sec = x.sectors.slice(0, 11).map(s => `<div class="mp-xrow"><div style="min-width:0"><div class="nm">${mpEsc(s.sector)}</div>
        <div class="mp-xbar"><i style="width:${s.pct / maxS * 100}%;background:var(--up)"></i></div></div><div class="pc">${mpPct(s.pct, 0)}</div></div>`).join('');
    return `<div class="mp-card"><h4><i class="ti ti-scan"></i> Rentgen portfolia</h4>
      <div class="mp-sub">Co doopravdy vlastníš, když se rozbalí i obsah ETF. Ukáže skrytou koncentraci – třeba Apple, který držíš napřímo i uvnitř indexu.</div>
      <div class="mp-xgrid">
        <div><div class="mp-xh">Největší skutečné expozice</div>${top}
          <div class="mp-legend"><span><i class="mp-dot" style="background:var(--accent)"></i>napřímo</span><span><i class="mp-dot" style="background:rgba(255,255,255,0.28)"></i>přes ETF</span></div></div>
        <div><div class="mp-xh">Sektory · USA ${mpPct(x.us_pct, 0)}</div>${sec}</div>
      </div>
      ${x.approx ? `<div class="mp-note"><i class="ti ti-info-circle"></i><span>Celosvětový ETF je rozbalený přibližně (≈ 63 % USA ve vahách S&amp;P 500, zbytek „mimo USA").</span></div>` : ''}
    </div>`;
}

async function planLoadStress() {
    const key = JSON.stringify(planLots);
    if (planStressLoading || (planStress && planStressKey === key)) return;
    planStressLoading = true; planStressKey = key;
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/plan/stress`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ holdings: planLots }) })).json();
        planStress = d.ok ? d : { error: d.error || 'Stres test se nepodařil.' };
    } catch (e) { planStress = { error: 'Stres test se nepodařil.' }; }
    planStressLoading = false;
    if (activeTab === 'portfolio') renderPlan();
}

function planStressHTML() {
    let body;
    if (!planStress) body = `<div class="mp-progress"><i class="ti ti-loader spin"></i> Přehrávám tvoje portfolio minulými krachy…</div>`;
    else if (planStress.error) body = `<div class="mp-note warn"><i class="ti ti-alert-triangle"></i><span>${mpEsc(planStress.error)}</span></div>`;
    else {
        body = `<div class="mp-stress">` + planStress.scenarios.map(s => {
            const fx = s.usd_czk_pct;
            return `<div class="mp-sc"><div class="l">${mpEsc(s.label)}</div>
              <div class="v">${s.loss_czk <= 0 ? '−' : '+'}${mpKcShort(Math.abs(s.loss_czk))}</div>
              <div class="p">${s.loss_pct > 0 ? '+' : ''}${mpPct(s.loss_pct, 0)} v Kč</div>
              <div class="d">S&amp;P 500 v USD: <b>${mpPct(s.spx_pct, 0)}</b><br>
                Dolar vůči koruně: <b>${fx > 0 ? '+' : ''}${mpPct(fx, 0)}</b> ${fx > 1 ? '– ztrátu tlumil' : (fx < -1 ? '– ztrátu prohloubil' : '')}<br>
                ${s.recovery_months != null ? `Index zpět na vrcholu za <b>${s.recovery_months} měs.</b>` : ''}
                ${s.proxied_pct > 0 ? `<br><span style="color:var(--text3)">${s.proxied_pct} % portfolia odhadnuto přes S&amp;P 500 (tehdy neexistovalo)</span>` : ''}</div></div>`;
        }).join('') + `</div>
        <div class="mp-note"><i class="ti ti-shield-check"></i><span>Takhle by tvoje <b>dnešní</b> portfolio prošlo minulými krizemi (ceny i kurz koruny). Plán v krachu nic neprodává – vklady dál nakupují levněji. Největší chybou dlouhodobého investora je prodat na dně.</span></div>`;
    }
    return `<div class="mp-card"><h4><i class="ti ti-activity-heartbeat"></i> Stres test v korunách</h4>
      <div class="mp-sub">Kolik by to bolelo, kdyby přišel krach jako v minulosti. Lepší to vědět teď než v panice.</div>${body}</div>`;
}

function planGoalHTML(d) {
    const g = planGoal;
    const start = d ? (d.total_czk || 0) : 0;
    const val = (id, def) => { const el = document.getElementById(id); return el ? el.value : def; };
    const iStart = (() => { const el = document.getElementById('mp-g-start'); return (el && el.value !== '') ? el.value : (start ? Math.round(start) : ''); })();
    const iMonthly = val('mp-g-monthly', planAmount || 5000);
    const iYears = val('mp-g-years', 15);
    const iGoal = val('mp-g-goal', '');
    let out = '';
    if (planGoalLoading) out = `<div class="mp-progress"><i class="ti ti-loader spin"></i> Simuluji 3 000 možných budoucností…</div>`;
    else if (g && g.error) out = `<div class="mp-note warn"><i class="ti ti-alert-triangle"></i><span>${mpEsc(g.error)}</span></div>`;
    else if (g) {
        out = `<div class="mp-goal-out">
            <div><svg class="mp-fan" id="mp-fan" viewBox="0 0 640 260" preserveAspectRatio="xMidYMid meet"></svg></div>
            <div class="mp-goal-kpis">
              ${g.prob != null ? `<div><div class="mp-xh">Šance dosáhnout cíle</div><div class="mp-prob" style="color:var(--${g.prob >= 70 ? 'up' : (g.prob >= 40 ? 'amber' : 'down')})">${g.prob} %</div>
                <div class="mp-sub" style="margin:6px 0 0">Pro 80% šanci vkládej <b style="color:var(--text)">${mpKc(g.monthly_for_80)}</b> měsíčně${g.monthly_for_50 ? `, pro 50% ${mpKc(g.monthly_for_50)}` : ''}.</div></div>` : ''}
              <div><div class="mp-xh">Za ${g.years} let – střední odhad</div><div class="mp-big">${mpKcShort(g.p50)}</div>
                <div class="mp-sub" style="margin:4px 0 0">v dnešních penězích ≈ ${mpKcShort(g.p50_real)} (inflace 2,5 %)</div></div>
              <div class="mp-sub" style="margin:0">Horší scénář (1 z 10): <b style="color:var(--text)">${mpKcShort(g.p10)}</b><br>Lepší scénář (1 z 10): <b style="color:var(--text)">${mpKcShort(g.p90)}</b><br>Vloženo celkem: <b style="color:var(--text)">${mpKcShort(g.contributed)}</b></div>
            </div></div>
          <div class="mp-note"><i class="ti ti-info-circle"></i><span>Simulace skládá náhodné roční úseky skutečné historie S&amp;P 500 vč. dividend (${mpEsc(g.history)}) – i s krachy. Záměrně <b>nepočítá</b> s tím, že výběr akcií trh porazí, a nezahrnuje kurz koruny. Léta 1988–2026 byla pro akcie spíš nadprůměrná, střední odhad ber jako optimističtější střed.</span></div>`;
    }
    return `<div class="mp-card"><h4><i class="ti ti-flag-3"></i> Cesta k cíli</h4>
      <div class="mp-sub">Kam se můžeš dostat a jakou máš šanci. Ne jedno číslo, ale rozpětí – tak to na trhu doopravdy je.</div>
      <div class="mp-goal-form">
        <div class="mp-field"><label>Teď mám (Kč)</label><input id="mp-g-start" type="number" min="0" step="1000" value="${mpEsc(iStart)}"></div>
        <div class="mp-field"><label>Měsíčně vložím (Kč)</label><input id="mp-g-monthly" type="number" min="0" step="500" value="${mpEsc(iMonthly)}"></div>
        <div class="mp-field"><label>Let</label><input id="mp-g-years" type="number" min="1" max="40" value="${mpEsc(iYears)}"></div>
        <div class="mp-field"><label>Cíl (Kč, nepovinné)</label><input id="mp-g-goal" type="number" min="0" step="100000" placeholder="např. 5000000" value="${mpEsc(iGoal)}"></div>
        <button class="btn-primary" style="flex:none; height:44px;" onclick="planRunGoal()">Spočítat</button>
      </div>${out}</div>`;
}

async function planRunGoal() {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    const v = id => (document.getElementById(id) || {}).value;
    const body = { start_czk: v('mp-g-start'), monthly_czk: v('mp-g-monthly'), years: v('mp-g-years'), goal_czk: v('mp-g-goal') };
    planGoalLoading = true; renderPlan();
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/plan/goal`, { method: 'POST', headers: authHeaders(), body: JSON.stringify(body) })).json();
        planGoal = d.ok ? Object.assign(d, { goal: parseFloat(body.goal_czk) || null }) : { error: d.error || 'Nepodařilo se spočítat.' };
    } catch (e) { planGoal = { error: 'Nepodařilo se spočítat.' }; }
    planGoalLoading = false; renderPlan();
}

function planDrawFan() {
    const svg = document.getElementById('mp-fan');
    const g = planGoal;
    if (!svg || !g || !g.fan) return;
    const W = 640, H = 260, L = 64, R = 14, T = 14, B = 30;
    const maxV = Math.max(g.fan[g.fan.length - 1].p90, g.goal || 0) * 1.05;
    const n = g.fan.length - 1;
    const X = i => L + (W - L - R) * (i / Math.max(1, n));
    const Y = v => T + (H - T - B) * (1 - v / maxV);
    const line = k => g.fan.map((f, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(f[k]).toFixed(1)}`).join(' ');
    const band = g.fan.map((f, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(f.p90).toFixed(1)}`).join(' ') + ' ' +
        g.fan.slice().reverse().map((f, j) => `L${X(n - j).toFixed(1)},${Y(f.p10).toFixed(1)}`).join(' ') + ' Z';
    const start = g.fan[0].p50, monthly = (g.contributed - start) / Math.max(1, n * 12);
    const contrib = g.fan.map((f, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(start + monthly * 12 * i).toFixed(1)}`).join(' ');
    let grid = '';
    for (let k = 0; k <= 4; k++) {
        const v = maxV * k / 4, y = Y(v);
        grid += `<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}" stroke="rgba(255,255,255,0.06)"/>
          <text x="${L - 8}" y="${y + 4}" text-anchor="end" font-size="11" fill="#5A6273">${v >= 1e6 ? (v / 1e6).toLocaleString('cs-CZ', { maximumFractionDigits: 1 }) + ' M' : Math.round(v / 1000) + ' tis.'}</text>`;
    }
    const step = n > 20 ? 5 : (n > 10 ? 2 : 1);
    let xl = '';
    for (let i = 0; i <= n; i += step) xl += `<text x="${X(i)}" y="${H - 10}" text-anchor="middle" font-size="11" fill="#5A6273">${i ? i + ' r.' : 'teď'}</text>`;
    const goal = g.goal ? `<line x1="${L}" x2="${W - R}" y1="${Y(g.goal)}" y2="${Y(g.goal)}" stroke="#FFB347" stroke-dasharray="5 4"/>
        <text x="${W - R}" y="${Y(g.goal) - 6}" text-anchor="end" font-size="11" fill="#FFB347">cíl</text>` : '';
    svg.innerHTML = `${grid}${xl}
      <path d="${band}" fill="rgba(255,122,0,0.16)"/>
      <path d="${line('p10')}" fill="none" stroke="rgba(255,122,0,0.45)" stroke-width="1"/>
      <path d="${line('p90')}" fill="none" stroke="rgba(255,122,0,0.45)" stroke-width="1"/>
      <path d="${contrib}" fill="none" stroke="rgba(255,255,255,0.35)" stroke-width="1.2" stroke-dasharray="3 4"/>
      <path d="${line('p50')}" fill="none" stroke="#FF7A00" stroke-width="2.5"/>${goal}
      <text x="${L + 6}" y="${T + 12}" font-size="11" fill="#9098AA">— střed · pás = 8 z 10 scénářů · čárkovaně = jen vklady</text>`;
}

function planWhyHTML(d) {
    if (d.plan.risk === 'maximum' && d.picks) {
        const S = d.picks.stats, r = (lbl, x) => `<tr><td>${lbl}</td><td>${x.beat_pct} %</td><td>+${mpPct(x.excess, 1)}</td><td>${x.tip_pct} %</td></tr>`;
        return `<div class="mp-card"><details class="mp-why"><summary><i class="ti ti-microscope" style="color:var(--accent)"></i> Jak vznikají tipy a jak přesné jsou – ověření na historii</summary>
          <div class="body">Každý měsíc seřadím ~500 firem S&amp;P 500 dvakrát: podle <b>hodnoty a momenta</b> (levná podle zisků, cash flow a vlastního kapitálu a zároveň rostoucí) a podle <b>čistého momenta</b> (nejsilnější růst za rok bez posledního měsíce). Do koše beru střídavě nejlepší z obou pořadí. Test na historickém složení indexu včetně firem, které z něj vypadly, s fundamenty až po jejich zveřejnění:
            <table><tr><th>Koš ${d.picks.n} tipů</th><th>porazil index</th><th>průměr navíc</th><th>jeden tip porazil index</th></tr>
              ${r('2015–2022', S.train)}${r('2023–2026 (nic neladěno)', S.holdout)}</table>
            <ul>
              <li><b>Proč mix:</b> samotná hodnota + momentum byla skvělá 2015–2022, ale 2023–2026 jen remíza s indexem. Samotné momentum naopak. Mix byl dobrý v obou obdobích.</li>
              <li><b>Proč koš a ne jedna akcie:</b> jednotlivý tip je skoro hod mincí; výsledek dělá pár velkých vítězů v koši.</li>
              <li><b>Co nezkoušet:</b> přidání „kvality" firmy (ziskovost) výsledky zhoršilo; prodávat držené akcie podle skóre se nevyplácelo.</li>
              <li><b>Poctivě:</b> data mají jen ~80 % firem, které kdy v indexu byly (zaniklé chybí) – výsledky mohou být mírně optimistické. Žádný výnos není zaručený.</li>
            </ul></div></details></div>`;
    }
    const v = d.plan.validation, b = d.plan.benchmark, rule = d.plan.rule || {};
    const row = (lbl, k) => `<tr><td>${lbl}</td><td>${mpPct(v[k].cagr, 1)}</td><td>${mpPct(b[k].cagr, 1)}</td><td>${mpPct(v[k].max_dd, 0)}</td><td>${mpPct(b[k].max_dd, 0)}</td></tr>`;
    return `<div class="mp-card"><details class="mp-why"><summary><i class="ti ti-microscope" style="color:var(--accent)"></i> Proč zrovna tahle pravidla – ověření na historii (profil ${MP_RISK[d.plan.risk].label.toLowerCase()})</summary>
      <div class="body">Simulace investora, který každý měsíc jen přidává peníze, na historickém složení S&amp;P 500 (vč. firem, které z indexu vypadly) a s poplatky. Pravidla jsme ladili na letech 2015–2022 a pak je beze změn pustili na 2023–2026.
        <table><tr><th>Období</th><th>Plán ročně</th><th>Index ročně</th><th>Plán max. propad</th><th>Index max. propad</th></tr>
          ${row('Ladění 2015–2022', 'train')}${row('Nezávislý test 2023–2026', 'holdout')}${row('Celé 2015–2026', 'full')}</table>
        <ul>
          <li><b>Vítěze nechat běžet.</b> Prodávat titul jen proto, že vypadl z plánu, stálo ~3 body ročně (odřízlo by NVIDII, Broadcom, KLA…) – platí i bez NVIDIE.</li>
          <li><b>Strop 3× cílové váhy</b> po 3 letech: bez něj by jedna akcie narostla na 45 % portfolia. Ořez drží riziko jedné firmy pod kontrolou a nic nestojí.</li>
          <li><b>Nové tituly jen do volného místa</b> a max ${rule.sector_cap_pct || 30} % míst z jednoho sektoru. Měsíční přeskupování celého portfolia bylo o ~5 bodů ročně horší.</li>
          <li><b>Žádné dluhopisy.</b> Ubraly 2,5–3,7 bodu ročně a propad 2022 skoro nezmírnily – padaly s akciemi.</li>
          <li><b>Poctivě:</b> v nezávislém testu 2023–2026 byl plán zhruba na úrovni indexu, náskok vznikl hlavně v letech 2015–2022. Propady jsou podobné jako u indexu – plán je o výnosu, ne o menším riziku.</li>
        </ul></div></details></div>`;
}

function planCopy() {
    const dep = planData && planData.deposit;
    if (!dep) return;
    const txt = dep.lines.map(l => `${mpBase(l.ticker)}\t${Math.round(l.amount_czk)} Kč\t${l.name || ''}`).join('\n');
    (navigator.clipboard ? navigator.clipboard.writeText(txt) : Promise.reject())
        .then(() => showToast('Seznam zkopírován', 'up', 'ti-copy'))
        .catch(() => showToast('Kopírování se nepodařilo', 'down', 'ti-alert-circle'));
}

// ---------- přehled (mini karta) ----------
function renderOverviewPlan() {
    const el = document.getElementById('ov-plan-mini');
    if (!el) return;
    if (!planLots.length) {
        el.innerHTML = `<div class="mp-note" style="margin:0 0 10px"><i class="ti ti-compass"></i><span>${FEATURE_QTY ? 'Nahraj screenshot portfolia a appka ti řekne, kam posílat každý vklad.' : 'Nahraj seznam svých akcií a appka ti u každé řekne, jestli přikupovat, držet, nebo nedokupovat.'}</span></div>`;
        return;
    }
    const d = planData;
    if (!d) {
        el.innerHTML = `<div class="mp-note" style="margin:0 0 10px"><i class="ti ti-loader spin"></i><span>${FEATURE_QTY ? 'Oceňuji tvé portfolio…' : 'Hodnotím tvé akcie…'}</span></div>`;
        if (!planLoading) planRefresh();
        return;
    }
    const acts = (d.plan.actions || []).length;
    const evc = (d.evaluation && d.evaluation.counts) || {};
    el.innerHTML = (FEATURE_QTY ? `<div class="ov-mini-stats" style="margin-bottom:10px">
        <div class="ov-mini-stat"><div class="l">${d.total_czk ? 'Hodnota' : 'Akcií'}</div><div class="v">${d.total_czk ? mpKcShort(d.total_czk) : d.rows.length}</div></div>
        <div class="ov-mini-stat"><div class="l">Úkoly</div><div class="v ${acts ? 'r' : 'g'}">${acts || '✓'}</div></div>
      </div>` : `<div class="ov-mini-stats" style="margin-bottom:10px">
        <div class="ov-mini-stat"><div class="l">Silné</div><div class="v g">${evc.strong || 0}</div></div>
        <div class="ov-mini-stat"><div class="l">Drž</div><div class="v">${evc.hold || 0}</div></div>
        <div class="ov-mini-stat"><div class="l">Slabší</div><div class="v ${evc.weak ? 'r' : ''}">${evc.weak || 0}</div></div>
      </div>`) + `
      <button class="tool-btn" onclick="switchTab('portfolio'); setTimeout(()=>{const e=document.getElementById('mp-amount'); if(e){e.scrollIntoView({behavior:'smooth',block:'center'}); e.focus();}},150)" style="width:100%; justify-content:center; margin-bottom:8px;"><i class="ti ti-coins"></i> Kam s penězi</button>`;
}

// ---------- import ze screenshotu ----------
let impState = { step: 'pick', rows: [], mode: 'replace', done: 0, total: 0, errors: [] };

function openImport() {
    impState = { step: 'pick', rows: [], mode: planLots.length ? 'replace' : 'replace', done: 0, total: 0, errors: [], csvNote: '' };
    renderImport();
    document.getElementById('importModal').classList.add('open');
}
function closeImport() { document.getElementById('importModal').classList.remove('open'); }

function renderImport() {
    const body = document.getElementById('imp-body'), btns = document.getElementById('imp-btns');
    if (impState.step === 'pick') {
        body.innerHTML = `<div class="cmp-sub" style="margin-bottom:12px">${FEATURE_QTY
            ? 'Appka z obrázku přečte <b>názvy akcií</b> (a počty kusů, když jsou vidět) a každou ověří proti burze. Počty kusů nejsou povinné – stačí vědět, co vlastníš. Než se cokoli uloží, všechno zkontroluješ.'
            : 'Appka z obrázku přečte <b>názvy akcií</b> a každou ověří proti burze. Počty kusů ani ceny nepotřebuju – stačí vědět, co vlastníš. Než se cokoli uloží, všechno zkontroluješ.'}</div>
          <label class="mp-drop" id="imp-drop"><i class="ti ti-photo-up"></i><b>Vyber screenshoty</b> nebo je sem přetáhni<br><span style="font-size:12px">JPG / PNG / HEIC (iPhone), klidně víc najednou</span>
            <input type="file" id="imp-file" accept="${impAccept()}" multiple style="display:none" onchange="impFiles(this.files)"></label>
          <ul class="mp-tips">
            <li>Trading 212 → <b>Portfolio</b> (Investice) → seznam pozic. Vyfoť celý seznam, při scrollování klidně s překryvem.</li>
            ${FEATURE_QTY ? '<li>Nejlepší je, když je u pozic vidět <b>počet kusů</b> i <b>hodnota v Kč</b> – podle hodnoty appka pozná správnou burzu (např. CSPX v USD vs. SXR8 v EUR).</li>' : '<li>Stačí, aby byly vidět <b>názvy nebo tickery</b> akcií. Víc nepotřebuju.</li>'}
            <li>Obrázek se nikam neukládá, jen ho jednorázově přečte AI model.</li>
          </ul>
          <div class="mp-or"><span>nebo</span></div>
          <div class="cmp-sub" style="margin-bottom:8px"><b>CSV z Trading 212</b> – nejpřesnější, žádné hádání názvů: v Trading 212 otevři <b>Historii</b> → <b>Export</b> (CSV) a vyber období od prvního nákupu. Appka z nákupů a prodejů spočítá, co držíš. Když export pustí jen kratší období, nahraj klidně víc souborů najednou.</div>
          <label class="tool-btn" style="display:inline-flex; cursor:pointer"><i class="ti ti-file-spreadsheet"></i> Vybrat CSV soubor
            <input type="file" id="imp-csv" accept=".csv,text/csv,text/plain" multiple style="display:none" onchange="impCsv(this.files)"></label>
          <div class="mp-or"><span>nebo</span></div>
          <div class="cmp-sub" style="margin-bottom:6px"><b>Napiš / vlož seznam akcií</b> – na řádek jedna${FEATURE_QTY ? ', počet kusů je nepovinný (např. <i>Apple</i>, <i>NVDA 12</i>, <i>Microsoft 3,5 ks</i>)' : ' (např. <i>Apple</i>, <i>NVDA</i>, <i>Microsoft</i>)'}:</div>
          <textarea id="imp-text" class="mp-textarea" rows="4" placeholder="Apple&#10;Microsoft&#10;NVDA 12&#10;iShares Core S&P 500"></textarea>
          <button class="btn-primary" style="margin-top:8px" onclick="impText()"><i class="ti ti-list-search"></i> Najít akcie</button>`;
        btns.innerHTML = `<button class="btn-ghost" onclick="closeImport()">Zavřít</button>`;
        const drop = document.getElementById('imp-drop');
        drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
        drop.addEventListener('dragleave', () => drop.classList.remove('over'));
        drop.addEventListener('drop', e => {
            e.preventDefault(); drop.classList.remove('over');
            const fs = Array.from(e.dataTransfer.files || []);
            if (fs.length && fs.every(f => /\.csv$/i.test(f.name) || /csv/i.test(f.type))) impCsv(fs); else impFiles(fs);
        });
        return;
    }
    if (impState.step === 'reading') {
        body.innerHTML = `<div class="mp-progress"><i class="ti ti-loader spin"></i> Čtu obrázky… ${impState.done} / ${impState.total}</div>
          ${impState.note ? `<div class="mp-note warn" style="margin-top:0"><i class="ti ti-clock"></i><span>${mpEsc(impState.note)}</span></div>` : ''}
          <div class="cmp-sub">Každý obrázek trvá pár vteřin – AI přečte pozice a appka je ověří proti cenám na burze.</div>`;
        btns.innerHTML = '';
        return;
    }
    // review
    const rows = impState.rows;
    const ok = rows.filter(r => r.include && r.ticker).length;
    const noQty = rows.filter(r => r.include && r.ticker && !(r.qty > 0)).length;
    let h = '';
    if (impState.errors.length) h += `<div class="mp-note warn" style="margin-top:0"><i class="ti ti-alert-triangle"></i><span>${impState.errors.map(mpEsc).join('<br>')}</span></div>`;
    if (impState.csvNote) h += `<div class="mp-note" style="margin-top:0"><i class="ti ti-file-spreadsheet"></i><span>${mpEsc(impState.csvNote)}</span></div>`;
    if (!rows.length) {
        h += `<div class="mp-note warn" style="margin-top:0"><i class="ti ti-eye-off"></i><span><b>AI na obrázku nenašla žádnou akcii.</b> ${impState.preview ? `<span style="color:var(--text3)">Odpověď AI: ${mpEsc(impState.preview)}</span><br>` : ''}Zkus ostřejší screenshot seznamu, nebo si akcie <b>napiš jako text</b> – na to AI nepotřebuje.</span></div>
          <button class="btn-primary" style="margin-top:10px" onclick="openImport()"><i class="ti ti-list-search"></i> Napsat seznam akcií</button>`;
    } else {
        h += FEATURE_QTY
          ? `<div class="cmp-sub" style="margin-bottom:10px">Našel jsem <b>${rows.length}</b> pozic. Zkontroluj hlavně řádky se žlutým rámečkem. <b>Počet kusů ani měsíc nákupu nejsou povinné</b> – bez počtu appka ví, že titul vlastníš (hlídá k němu zprávy a nedoporučí ho jako nový), jen ho nemůže ocenit. Měsíc nákupu slouží ke hlídání 3letého testu (bez daně).</div>
          <div class="mp-rv-hd"><div></div><div>Ze screenshotu</div><div>Ticker</div><div>Počet kusů (nepovinné)</div><div>Koupeno (měsíc)</div></div>`
          : `<div class="cmp-sub" style="margin-bottom:10px">Našel jsem <b>${rows.length}</b> ${rows.length === 1 ? 'akcii' : (rows.length < 5 ? 'akcie' : 'akcií')}. Zkontroluj hlavně řádky se žlutým rámečkem – stačí, aby seděl <b>ticker</b>. Odškrtnutím akcii vynecháš.</div>
          <div class="mp-rv-hd names"><div></div><div>Ze screenshotu</div><div>Ticker</div></div>`;
        rows.forEach((r, i) => {
            const cls = !r.include ? 'off' : (r.status === 'unresolved' ? 'bad' : (r.status === 'check' ? 'check' : ''));
            const opts = (r.candidates || []).map(c => `<option value="${mpEsc(c.ticker)}" ${c.ticker === r.ticker ? 'selected' : ''}>${mpEsc(c.ticker)} · ${mpEsc(c.exchange || c.currency || '')}${c.implied_czk ? ' · ' + mpKc(c.implied_czk) : ''}</option>`).join('');
            const tick = r.status === 'unresolved' || !(r.candidates || []).length
                ? `<div style="display:flex;gap:4px"><input type="text" id="imp-q-${i}" placeholder="ticker / název" value="${mpEsc(r.raw_ticker || '')}"><button class="mp-icon-btn" style="flex:none" onclick="impSearch(${i})"><i class="ti ti-search"></i></button></div>`
                : `<select onchange="impPick(${i}, this.value)">${opts}</select>`;
            h += `<div class="mp-rv ${cls}${FEATURE_QTY ? '' : ' names'}">
              <input type="checkbox" ${r.include ? 'checked' : ''} onchange="impState.rows[${i}].include=this.checked; renderImport()">
              <div style="min-width:0"><div class="raw" title="${mpEsc(r.name)}">${mpEsc(r.name || r.raw_ticker || '?')}</div>${impCheckHTML(r)}</div>
              <div style="min-width:0">${tick}</div>
              ${FEATURE_QTY ? `<label class="mp-rv-f"><span>Počet kusů (nepovinné)</span><input type="number" step="any" min="0" placeholder="nepovinné" value="${r.qty != null ? r.qty : ''}" onchange="impQty(${i}, this.value)"></label>
              <label class="mp-rv-f"><span>Koupeno (měsíc)</span><input type="month" value="${mpEsc(r.buy_month || '')}" max="${mpThisMonth()}" onchange="impState.rows[${i}].buy_month=this.value"></label>` : ''}
            </div>`;
        });
        if (planLots.length) {
            h += `<div class="mp-mode">
                <label><input type="radio" name="imp-mode" ${impState.mode === 'replace' ? 'checked' : ''} onchange="impState.mode='replace'"> Nahradit současné portfolio (screenshot = celý stav)</label>
                <label><input type="radio" name="imp-mode" ${impState.mode === 'add' ? 'checked' : ''} onchange="impState.mode='add'"> Přidat k současným akciím</label></div>`;
        }
    }
    body.innerHTML = h;
    btns.innerHTML = `<button class="btn-ghost" onclick="openImport()">Jiné obrázky</button>
      <button class="btn-primary" ${ok ? '' : 'disabled style="opacity:.5"'} onclick="impSave()"><i class="ti ti-check"></i> Uložit ${ok} ${ok === 1 ? 'akcii' : (ok >= 2 && ok <= 4 ? 'akcie' : 'akcií')}</button>`;
}

function impCheckHTML(r) {
    if (r.status === 'unresolved') return `<div class="chk bad">✗ nenašel jsem – napiš ticker a hledej</div>`;
    if (!FEATURE_QTY) {                                   // bez kusů stačí vědět, že titul existuje
        const c = (r.candidates || []).find(x => x.ticker === r.ticker) || {};
        let t = r.status === 'ok' ? `<div class="chk ok">✓ nalezeno: ${mpEsc(c.name || r.ticker)}</div>`
                                  : `<div class="chk check">⚠ zkontroluj, že je to ta správná firma: ${mpEsc(c.name || r.ticker)}</div>`;
        if (r.dup) t += `<div class="chk check">${mpEsc(r.dup)}</div>`;
        return t;
    }
    let t = '';
    if (!(r.qty > 0) && !r.value_czk) {
        const c = (r.candidates || []).find(x => x.ticker === r.ticker) || {};
        return r.status === 'ok' ? `<div class="chk ok">✓ nalezeno: ${mpEsc(c.name || r.ticker)} · počet kusů neznámý</div>`
                                 : `<div class="chk check">⚠ zkontroluj, že je to ta správná firma · počet kusů neznámý</div>`;
    }
    if (r.value_czk && r.implied_czk) {
        const diff = Math.abs(r.implied_czk - r.value_czk) / r.value_czk;
        t = diff <= 0.15 ? `<div class="chk ok">✓ sedí s hodnotou ${mpKc(r.value_czk)}</div>`
            : `<div class="chk check">⚠ screenshot ${mpKc(r.value_czk)}, vychází ${mpKc(r.implied_czk)} – zkontroluj kusy/ticker</div>`;
    } else if (r.implied_czk) t = `<div class="chk check">≈ ${mpKc(r.implied_czk)} · hodnotu ze screenshotu neznám</div>`;
    if (r.qty_derived) t += `<div class="chk check">počet kusů dopočítán z hodnoty</div>`;
    if (r.dup) t += `<div class="chk check">${mpEsc(r.dup)}</div>`;
    return t;
}

// iOS: s `image/*` Safari fotku z knihovny sám převede na JPEG; s `.heic` v accept by poslal originál. Jinde `.heic` přidáme, ať ho výběr souborů nabídne.
const impAccept = () => (/iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)) ? 'image/*' : 'image/*,.heic,.heif';
const impIsHeic = f => /image\/hei[cf]/i.test(f.type) || /\.hei[cf]$/i.test(f.name || '');
function impReadDataURL(blob) {
    return new Promise((res, rej) => { const fr = new FileReader(); fr.onload = () => res(fr.result); fr.onerror = () => rej(new Error('nelze přečíst')); fr.readAsDataURL(blob); });
}
async function impDecode(file) {
    // CSP (img-src) blokuje <img src="blob:…"> → 1) createImageBitmap (iOS 15+, Chrome), 2) <img> z data URL
    if (window.createImageBitmap) { try { return await createImageBitmap(file); } catch (e) {} }
    const du = await impReadDataURL(file);
    return await new Promise((res, rej) => { const im = new Image(); im.onload = () => res(im); im.onerror = () => rej(new Error('nelze dekódovat')); im.src = du; });
}
async function impPrep(file) {
    // zmenšit (a dlouhé screenshoty rozřezat), ať je text čitelný a request malý
    let img;
    try { img = await impDecode(file); }
    catch (e) {
        if (impIsHeic(file)) {
            // převod HEIC v prohlížeči by vyžadoval povolit eval/worker v CSP → nedělá se; Safari/iOS HEIC čtou samy
            throw new Error('tenhle prohlížeč formát HEIC neumí – otevři appku v Safari, nebo vyber JPG/PNG');
        } else {
            // prohlížeč obrázek neotevře → pošli originál, AI ho přečte sama
            const du = await impReadDataURL(file);
            const m = /^data:([^;]*);base64,/.exec(du) || [];
            const typ = /png/i.test(m[1] || file.name) ? 'png' : (/webp/i.test(m[1] || file.name) ? 'webp' : 'jpeg');
            const fixed = du.replace(/^data:[^;]*;base64,/, `data:image/${typ};base64,`);
            if (fixed.length > 3_900_000) throw new Error('obrázek je moc velký');
            return [fixed];
        }
    }
    try {
        const w = img.naturalWidth || img.width, h = img.naturalHeight || img.height, out = [];
        if (!w || !h) throw new Error('prázdný obrázek');
        const chunkH = h / w > 2.6 ? Math.round(w * 2.2) : h;
        for (let y = 0; y < h; y += Math.max(1, chunkH - Math.round(w * 0.25))) {
            const ch = Math.min(chunkH, h - y);
            const s = Math.min(1, 1500 / Math.max(w, ch));          // menší obrázek = míň tokenů (bezplatný limit AI)
            const c = document.createElement('canvas');
            c.width = Math.round(w * s); c.height = Math.round(ch * s);
            const ctx = c.getContext('2d');
            ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, c.width, c.height);
            ctx.drawImage(img, 0, y, w, ch, 0, 0, c.width, c.height);
            out.push(c.toDataURL('image/jpeg', 0.82));
            if (y + ch >= h) break;
        }
        return out;
    } finally {
        if (img && img.close) img.close();
    }
}

async function impFiles(files) {
    files = Array.from(files || []).filter(f => /^image\//.test(f.type) || impIsHeic(f) || /\.(jpe?g|png|webp)$/i.test(f.name || '')).slice(0, 10);
    if (!files.length) { showToast('Vyber obrázek (JPG/PNG/HEIC)', 'down', 'ti-alert-circle'); return; }
    impState.step = 'reading'; impState.errors = []; impState.rows = []; impState.preview = null;
    impState.done = 0; impState.total = files.length; renderImport();
    let images = [];
    for (const f of files) {
        try { images = images.concat(await impPrep(f)); }
        catch (e) { impState.errors.push(`${f.name}: obrázek nejde otevřít (${(e && e.message) || 'neznámá chyba'}).`); }
    }
    impState.total = images.length; impState.done = 0;
    renderImport();
    const found = [];
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    // po jednom (bezplatný limit AI je malý); při limitu appka počká a zkusí znovu
    for (let my = 0; my < images.length; my++) {
        let last = null;
        for (let attempt = 1; attempt <= 4; attempt++) {
            try {
                const r = await fetch(`${BACKEND_URL}/api/holdings/import`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ image: images[my] }) });
                const d = await r.json().catch(() => ({ ok: false, error: `Server neodpověděl (HTTP ${r.status}).` }));
                if (d.ok) {
                    found.push(...(d.positions || []).map(p => Object.assign(p, { _img: my })));
                    if (d.preview) impState.preview = d.preview;
                    last = null; break;
                }
                last = d;
                if (d.kind === 'limit' && attempt < 4 && (Number(d.retry_after) || 20) <= 90) {
                    const w = Math.min(Math.max(Math.round(Number(d.retry_after) || 20), 8), 45);
                    for (let t = w; t > 0; t--) {
                        impState.note = `Bezplatný limit AI – čekám ${t} s a zkusím obrázek ${my + 1} znovu…`; renderImport(); await sleep(1000);
                    }
                    impState.note = ''; continue;
                }
                break;
            } catch (e) { last = { error: 'spojení selhalo' }; if (attempt < 2) { await sleep(2000); continue; } break; }
        }
        if (last) impState.errors.push(`Obrázek ${my + 1}: ${last.error || 'nepodařilo se přečíst'}${last.detail ? ' (' + last.detail + ')' : ''}`);
        impState.done++; renderImport();
    }
    impFinish(found);
}

// Text místo fotky: seznam akcií (názvy / tickery, počet kusů nepovinný)
async function impCsv(files) {
    files = Array.from(files || []).slice(0, 6);
    if (!files.length) return;
    impState.step = 'reading'; impState.errors = []; impState.rows = []; impState.preview = null; impState.total = 1; impState.done = 0;
    impState.note = 'Čtu CSV a hledám tickery…';
    renderImport();
    let text = '';
    for (const f of files) {
        if (f.size > 3_000_000) { impState.errors.push(`${f.name}: soubor je moc velký (max ~3 MB).`); continue; }
        try { text += (text ? '\n' : '') + (await f.text()).replace(/^\uFEFF/, ''); }
        catch (e) { impState.errors.push(`${f.name}: soubor nejde přečíst.`); }
    }
    const found = [];
    if (text) {
        try {
            const r = await fetch(`${BACKEND_URL}/api/holdings/import`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ csv: text }) });
            const d = await r.json().catch(() => ({ ok: false, error: `Server neodpověděl (HTTP ${r.status}).` }));
            if (d.ok) {
                found.push(...(d.positions || []).map(p => Object.assign(p, { _img: 0 })));
                const c = d.csv || {};
                if (c.kind === 'history') {
                    const pl = (n, one, few, many) => n === 1 ? one : (n >= 2 && n <= 4 ? few : many);
                    const bits = [`držíš ${c.held} ${pl(c.held, 'titul', 'tituly', 'titulů')}`];
                    if (c.sold) bits.push(`${c.sold} ${pl(c.sold, 'prodaný jsem vynechal', 'prodané jsem vynechal', 'prodaných jsem vynechal')}`);
                    if (c.negative) bits.push(`${c.negative} ${pl(c.negative, 'titul má', 'tituly mají', 'titulů má')} v exportu víc prodejů než nákupů (export nejspíš nezačíná prvním nákupem) – vynechal jsem ${pl(c.negative, 'ho', 'je', 'je')}`);
                    impState.csvNote = 'Z historie obchodů: ' + bits.join(', ') + '.';
                }
                if (d.note) impState.errors.push(d.note);
            } else impState.errors.push(d.error || 'CSV se nepodařilo zpracovat.');
        } catch (e) { impState.errors.push('Spojení selhalo.'); }
    }
    impState.note = '';
    impState.done = 1;
    impFinish(found);
}

async function impText() {
    const text = ((document.getElementById('imp-text') || {}).value || '').trim();
    if (text.length < 2) { showToast('Napiš aspoň jednu akcii', 'down', 'ti-alert-circle'); return; }
    impState.step = 'reading'; impState.errors = []; impState.rows = []; impState.preview = null; impState.total = 1; impState.done = 0;
    renderImport();
    const found = [];
    try {
        const r = await fetch(`${BACKEND_URL}/api/holdings/import`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ text }) });
        const d = await r.json();
        if (d.ok) found.push(...(d.positions || []).map(p => Object.assign(p, { _img: 0 })));
        else impState.errors.push(d.error || 'Seznam se nepodařilo zpracovat.');
    } catch (e) { impState.errors.push('Spojení selhalo.'); }
    impState.done = 1;
    impFinish(found);
}

function impFinish(found) {
    // sloučit: stejná pozice na dvou screenshotech (překryv) = jedna pozice
    const byKey = {};
    found.sort((a, b) => a._img - b._img).forEach(p => {
        const key = p.ticker || ('?' + (p.raw_ticker || p.name));
        const ex = byKey[key];
        if (!ex) { byKey[key] = Object.assign(p, { include: !!p.ticker, buy_month: '', qty0: p.qty }); return; }
        if (p.qty && ex.qty && Math.abs(p.qty - ex.qty) / ex.qty > 0.01) {
            ex.dup = `na jiném obrázku ${mpQty(p.qty)} ks – vyber správný počet`;
            ex.status = 'check';
        }
    });
    impState.rows = Object.values(byKey);
    impState.step = 'review';
    renderImport();
}

function impPick(i, t) {
    const r = impState.rows[i], c = (r.candidates || []).find(x => x.ticker === t);
    r.ticker = t;
    if (c) {
        r.implied_czk = (c.implied_czk && r.qty0 && r.qty) ? c.implied_czk / r.qty0 * r.qty : c.implied_czk;
        if (r.value_czk && c.implied_czk) r.status = Math.abs(c.implied_czk - r.value_czk) / r.value_czk <= 0.15 ? 'ok' : 'check';
    }
    renderImport();
}
function impQty(i, v) {
    const r = impState.rows[i], q = parseFloat(String(v).replace(',', '.'));
    if (r.qty && r.implied_czk && q > 0) r.implied_czk = r.implied_czk / r.qty * q;
    r.qty = q > 0 ? q : null; r.qty_derived = false; r.no_qty = !(q > 0);
    if (r.value_czk && r.implied_czk) r.status = Math.abs(r.implied_czk - r.value_czk) / r.value_czk <= 0.15 ? 'ok' : 'check';
    renderImport();
}
async function impSearch(i) {
    const r = impState.rows[i], q = (document.getElementById('imp-q-' + i) || {}).value || '';
    if (!q.trim()) return;
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/holdings/resolve?q=${encodeURIComponent(q.trim())}${r.qty ? '&qty=' + r.qty : ''}${r.value_czk ? '&value=' + r.value_czk : ''}`)).json();
        if (d.ok && d.ticker) {
            Object.assign(r, { ticker: d.ticker, candidates: d.candidates, implied_czk: d.implied_czk, status: d.status, include: true });
            if (!r.qty && d.qty) r.qty = d.qty;
            r.qty0 = r.qty;
        } else showToast('Nic jsem nenašel – zkus jiný ticker', 'down', 'ti-search');
    } catch (e) { showToast('Hledání selhalo', 'down', 'ti-alert-circle'); }
    renderImport();
}

function impSave() {
    const lots = impState.rows.filter(r => r.include && r.ticker)
        .map(r => ({ ticker: r.ticker, qty: FEATURE_QTY && r.qty > 0 ? r.qty : null, buy_month: FEATURE_QTY ? (r.buy_month || null) : null, name: r.name || '' }));
    if (!lots.length) return;
    planLots = (impState.mode === 'add' && planLots.length) ? planLots.concat(lots) : lots;
    if (!FEATURE_QTY) planLots = [...new Map(planLots.map(l => [l.ticker, l])).values()];     // každá akcie jednou
    saveLots(); planData = null;
    closeImport();
    showToast(`Uloženo ${lots.length} akcií`, 'up', 'ti-check');
    planRefresh();
}

// ---------- ruční přidání, úprava nákupů, „Nakoupeno" ----------
let lotState = null;

function openLot(ticker) {
    if (ticker) {
        const lots = planLots.filter(l => l.ticker === ticker).map(l => ({ qty: l.qty == null ? '' : l.qty, buy_month: l.buy_month || '' }));
        const row = planData && planData.rows.find(r => r.ticker === ticker);
        lotState = { mode: 'edit', ticker, name: (row && row.name) || (planLots.find(l => l.ticker === ticker) || {}).name || '', lots: lots.length ? lots : [{ qty: '', buy_month: '' }] };
    } else {
        lotState = { mode: 'add', ticker: null, name: '', found: null, lots: [{ qty: '', buy_month: '' }] };
    }
    renderLot();
    document.getElementById('lotModal').classList.add('open');
}
function closeLot() { document.getElementById('lotModal').classList.remove('open'); }

function openBought() {
    const dep = planData && planData.deposit;
    if (!dep) return;
    lotState = { mode: 'bought', lines: dep.lines.map(l => ({ ticker: l.ticker, name: l.name, qty: l.shares || '', on: true })) };
    renderLot();
    document.getElementById('lotModal').classList.add('open');
}

function renderLot() {
    const s = lotState, title = document.getElementById('lot-title'), body = document.getElementById('lot-body'), btns = document.getElementById('lot-btns');
    const lotRows = () => s.lots.map((l, i) => `<div class="mp-lot">
        <div class="mp-field"><label>${i ? '' : 'Počet kusů'}</label><input type="number" inputmode="decimal" step="any" min="0" placeholder="nepovinné" value="${l.qty}" oninput="lotState.lots[${i}].qty=this.value"></div>
        <div class="mp-field"><label>${i ? '' : 'Koupeno (měsíc)'}</label><input type="month" max="${mpThisMonth()}" value="${mpEsc(l.buy_month || '')}" oninput="lotState.lots[${i}].buy_month=this.value"></div>
        <div class="mp-field"><label>${i ? '' : '&nbsp;'}</label><button class="mp-icon-btn" style="height:42px;width:36px" onclick="lotState.lots.splice(${i},1); if(!lotState.lots.length) lotState.lots.push({qty:'',buy_month:''}); renderLot()"><i class="ti ti-x"></i></button></div>
      </div>`).join('') + `<a style="color:var(--accent);cursor:pointer;font-size:13px" onclick="lotState.lots.push({qty:'',buy_month:''}); renderLot()"><i class="ti ti-plus"></i> další nákup (jiný měsíc)</a>
      <div class="cmp-sub" style="margin-top:10px">Každý nákup zvlášť kvůli 3letému testu – kusy koupené před víc než 3 lety jsou při prodeji v ČR bez daně. Počet ani měsíc nevíš? Nech prázdné – appka si zapamatuje, že akcii vlastníš.</div>`;
    if (s.mode === 'bought' && !FEATURE_QTY) {
        title.innerHTML = `<i class="ti ti-circle-check" style="color:var(--accent)"></i> Koupil jsem – přidat do portfolia`;
        body.innerHTML = `<div class="cmp-sub" style="margin-bottom:12px">Zaškrtni, co jsi koupil. Appka to přidá mezi tvoje akcie a bude je hodnotit – počty kusů nepotřebuju.</div>` +
            s.lines.map((l, i) => `<label class="mp-lot" style="grid-template-columns:22px 1fr; align-items:center; cursor:pointer">
                <input type="checkbox" ${l.on ? 'checked' : ''} onchange="lotState.lines[${i}].on=this.checked">
                <div><b>${mpEsc(mpBase(l.ticker))}</b> <span style="color:var(--text2);font-size:12.5px">${mpEsc(l.name || '')}</span></div></label>`).join('');
        btns.innerHTML = `<button class="btn-ghost" onclick="closeLot()">Zrušit</button><button class="btn-primary" onclick="lotSave()"><i class="ti ti-check"></i> Přidat</button>`;
        return;
    }
    if (s.mode === 'bought') {
        title.innerHTML = `<i class="ti ti-circle-check" style="color:var(--accent)"></i> Nakoupeno – připsat do portfolia`;
        body.innerHTML = `<div class="cmp-sub" style="margin-bottom:12px">Uprav počty kusů podle skutečných nákupů v Trading 212. Měsíc nákupu se uloží jako ${mpMonth(mpThisMonth())}.</div>` +
            s.lines.map((l, i) => `<div class="mp-lot" style="grid-template-columns:22px 1fr 130px; align-items:center">
                <input type="checkbox" ${l.on ? 'checked' : ''} onchange="lotState.lines[${i}].on=this.checked">
                <div><b>${mpEsc(mpBase(l.ticker))}</b> <span style="color:var(--text2);font-size:12.5px">${mpEsc(l.name || '')}</span></div>
                <input type="number" step="any" min="0" value="${l.qty}" oninput="lotState.lines[${i}].qty=this.value" style="padding:8px 10px;background:var(--surface);border:1px solid var(--border2);border-radius:9px;color:var(--text)"></div>`).join('');
        btns.innerHTML = `<button class="btn-ghost" onclick="closeLot()">Zrušit</button><button class="btn-primary" onclick="lotSave()"><i class="ti ti-check"></i> Připsat</button>`;
        return;
    }
    if (s.mode === 'add') {
        title.innerHTML = `<i class="ti ti-plus" style="color:var(--accent)"></i> ${FEATURE_QTY ? 'Přidat pozici' : 'Přidat akcii'}`;
        body.innerHTML = `<div class="mp-field"><label>Ticker nebo název</label>
            <div style="display:flex;gap:8px"><input type="text" id="lot-q" placeholder="např. AAPL, SXR8, Microsoft" onkeydown="if(event.key==='Enter')lotFind()"><button class="tool-btn" style="flex:none" onclick="lotFind()"><i class="ti ti-search"></i> Najít</button></div></div>
          <div class="mp-search-res" id="lot-res">${s.found ? `<b>${mpEsc(s.found.ticker)}</b> · ${mpEsc(s.found.name)} · ${mpQty(s.found.price)} ${mpEsc(s.found.currency)}${(s.found.candidates || []).length > 1 ? `<br><select style="margin-top:6px;padding:6px 8px;background:var(--surface);border:1px solid var(--border2);border-radius:8px;color:var(--text)" onchange="lotPickCand(this.value)">${s.found.candidates.map(c => `<option value="${mpEsc(c.ticker)}" ${c.ticker === s.found.ticker ? 'selected' : ''}>${mpEsc(c.ticker)} · ${mpEsc(c.exchange || '')} · ${mpQty(c.price)} ${mpEsc(c.currency)}</option>`).join('')}</select>` : ''}` : ''}</div>
          ${s.found ? (FEATURE_QTY ? lotRows() : '<div class="cmp-sub" style="margin-top:10px">Přidá se mezi tvoje akcie a appka ji bude hodnotit. Počet kusů nepotřebuju.</div>') : ''}`;
        btns.innerHTML = `<button class="btn-ghost" onclick="closeLot()">Zrušit</button><button class="btn-primary" ${s.found ? '' : 'disabled style="opacity:.5"'} onclick="lotSave()"><i class="ti ti-check"></i> Přidat</button>`;
        return;
    }
    title.innerHTML = `<i class="ti ti-pencil" style="color:var(--accent)"></i> ${mpEsc(mpBase(s.ticker))} <span style="font-size:14px;color:var(--text2);font-weight:400">${mpEsc(s.name)}</span>`;
    body.innerHTML = lotRows();
    btns.innerHTML = `<button class="btn-ghost" style="color:var(--down);flex:none" onclick="lotDelete()"><i class="ti ti-trash"></i></button><button class="btn-ghost" onclick="closeLot()">Zrušit</button><button class="btn-primary" onclick="lotSave()"><i class="ti ti-check"></i> Uložit</button>`;
}

async function lotFind() {
    const q = ((document.getElementById('lot-q') || {}).value || '').trim();
    if (!q) return;
    const res = document.getElementById('lot-res');
    if (res) res.innerHTML = '<i class="ti ti-loader spin"></i> Hledám…';
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/holdings/resolve?q=${encodeURIComponent(q)}`)).json();
        if (d.ok && d.ticker) {
            const c = (d.candidates || []).find(x => x.ticker === d.ticker) || {};
            lotState.found = { ticker: d.ticker, name: c.name || d.name, price: c.price, currency: c.currency, candidates: d.candidates };
            renderLot();
            const qi = document.getElementById('lot-q'); if (qi) qi.value = q;
        } else if (res) res.textContent = 'Nic jsem nenašel. Zkus ticker (např. AAPL) nebo přesnější název.';
    } catch (e) { if (res) res.textContent = 'Hledání selhalo.'; }
}
function lotPickCand(t) {
    const c = (lotState.found.candidates || []).find(x => x.ticker === t);
    if (c) Object.assign(lotState.found, { ticker: c.ticker, name: c.name || lotState.found.name, price: c.price, currency: c.currency });
    renderLot();
}

function lotSave() {
    const s = lotState;
    if (s.mode === 'bought') {
        const m = mpThisMonth();
        const have = new Set(planLots.map(l => l.ticker));
        const add = FEATURE_QTY
            ? s.lines.filter(l => l.on && parseFloat(l.qty) > 0).map(l => ({ ticker: l.ticker, qty: parseFloat(l.qty), buy_month: m, name: l.name || '' }))
            : s.lines.filter(l => l.on && !have.has(l.ticker)).map(l => ({ ticker: l.ticker, qty: null, buy_month: null, name: l.name || '' }));
        if (!add.length) { closeLot(); return; }
        planLots = planLots.concat(add);
        planShowDeposit = false; planAmount = '';
        try { localStorage.removeItem('ma_plan_amount'); } catch (e) {}
        saveLots(); closeLot();
        showToast(FEATURE_QTY ? `Připsáno ${add.length} nákupů` : `Přidáno ${add.length} ${add.length === 1 ? 'akcie' : (add.length < 5 ? 'akcie' : 'akcií')} do portfolia`, 'up', 'ti-check');
        planRefresh();
        return;
    }
    const ticker = s.mode === 'add' ? (s.found && s.found.ticker) : s.ticker;
    if (!ticker) return;
    const nm = s.mode === 'add' ? s.found.name : s.name;
    let lots = s.lots.map(l => ({ ticker, qty: parseFloat(String(l.qty).replace(',', '.')), buy_month: l.buy_month || null, name: nm }))
        .filter(l => l.qty > 0);
    if (!lots.length) lots = [{ ticker, qty: null, buy_month: null, name: nm }];   // jen „vlastním", počet neznám
    planLots = planLots.filter(l => (s.mode === 'add' && FEATURE_QTY) || l.ticker !== ticker).concat(lots);
    saveLots(); closeLot();
    if (!FEATURE_QTY) showToast(`${mpBase(ticker)} přidána – hodnotím ji`, 'up', 'ti-check');
    planRefresh();
}
function lotDelete() {
    const t = lotState.ticker;
    if (!confirm(`Odebrat ${mpBase(t)} z portfolia?`)) return;
    planLots = planLots.filter(l => l.ticker !== t);
    saveLots(); closeLot();
    planRefresh();
}
