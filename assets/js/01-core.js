// Na Vercelu běží frontend i backend na stejné domeně -> relativní cesty ("").
// Lokálně (file:// nebo localhost) se automaticky napojí na Flask na :5001.
const BACKEND_URL = (
    location.protocol === 'file:' ||
    location.hostname === 'localhost' ||
    location.hostname === '127.0.0.1'
) ? 'http://127.0.0.1:5001' : '';

// Výchozí sledované akcie
// NVDA, Lithium Argentina (LAR), Colt CZ / CSG (COLT.PR), Alphabet (GOOGL),
// Navitas (NVTS), Zlato (GC=F), Stříbro (SI=F), Měď (HG=F)
const DEFAULT_WATCHLIST = ['NVDA', 'LAR', 'COLT.PR', 'GOOGL', 'NVTS', 'GC=F', 'SI=F', 'HG=F'];
let myStocks = JSON.parse(localStorage.getItem('mystocks_portfolio')) || DEFAULT_WATCHLIST;
// Jednorázový seed: i prohlížečům se starým uloženým seznamem nastaví nový (jen jednou).
if (!localStorage.getItem('mystocks_seed_v3')) {
    myStocks = DEFAULT_WATCHLIST.slice();
    localStorage.setItem('mystocks_portfolio', JSON.stringify(myStocks));
    localStorage.setItem('mystocks_seed_v3', '1');
}
// Portfolio pozice: { "NVDA": { qty: 10, avgPrice: 450.00 }, ... }
const myPositions = {};   // pozice odstraněny – appka je asistent, ne broker

let currentStockData = null;
let currentChartData = null;
let chartInstance = null;
let autoRefreshTimer = null;
let searchTimeout = null;

// Nastavení grafu
let chartType = 'area';
let chartPeriod = '6mo';
let showMA = false;
let volChartInstance = null;
let allocChartInstance = null;
let lastWatchlistData = [];
let alertDir = 'above';

// Cenová upozornění: [{ticker, dir:'above'|'below', price}]
let myAlerts = JSON.parse(localStorage.getItem('mystocks_alerts')) || [];

// Účet & cloud synchronizace
let authToken = localStorage.getItem('mystocks_token') || null;
let authUser = localStorage.getItem('mystocks_user') || null;
let cloudEnabled = false;
let btProof = null;   // poslední backtest (důkaz úspěšnosti) z /api/backtest
let billingEnabled = false;   // jsou nastavené platby (Stripe)?
let billingPlans = [];
let authTab = 'login';
let syncTimer = null;

// Úroveň investora (začátečník/pokročilý) + diverzifikované začátečnické portfolio
let investorLevel = localStorage.getItem('mystocks_level') || 'advanced';
let investorRisk = localStorage.getItem('mystocks_risk') || 'balanced';
let investorSimple = localStorage.getItem('mystocks_simple') === '1';
// Diverzifikované začátečnické sady – každá akcie jiný sektor, dle agresivity
const STARTER_SETS = {
    conservative: [
        { t: 'SXR8.DE', s: 'Index S&P 500', n: 'iShares Core S&P 500 UCITS ETF' },
        { t: 'JNJ', s: 'Zdravotnictví', n: 'Johnson & Johnson' },
        { t: 'PG', s: 'Spotřební zboží', n: 'Procter & Gamble' },
        { t: 'KO', s: 'Nápoje', n: 'Coca-Cola' },
        { t: 'NEE', s: 'Energetika / utility', n: 'NextEra Energy' },
    ],
    balanced: [
        { t: 'AAPL', s: 'Technologie', n: 'Apple' },
        { t: 'JNJ', s: 'Zdravotnictví', n: 'Johnson & Johnson' },
        { t: 'JPM', s: 'Finance', n: 'JPMorgan Chase' },
        { t: 'PG', s: 'Spotřební zboží', n: 'Procter & Gamble' },
        { t: 'XOM', s: 'Energie', n: 'Exxon Mobil' },
    ],
    aggressive: [
        { t: 'NVDA', s: 'Polovodiče', n: 'Nvidia' },
        { t: 'TSLA', s: 'Elektromobily', n: 'Tesla' },
        { t: 'AMZN', s: 'E-commerce', n: 'Amazon' },
        { t: 'CRWD', s: 'Kyberbezpečnost', n: 'CrowdStrike' },
        { t: 'AMD', s: 'Čipy', n: 'AMD' },
    ],
};
const RISK_LABELS = { conservative: 'Opatrná', balanced: 'Vyvážená', aggressive: 'Agresivní' };

// Předplatné
let mySub = null;                       // {plan, effective, trial_ends, days_left, is_admin, ...}
let myNotif = { morning: false };       // nastavení notifikací
let myName = '';                        // jméno k oslovení (nastavuje si uživatel; nikdy ne z e-mailu)
const PLAN_RANK = { none: -1, start: 0, pro: 1, elite: 2 };
const PLAN_NAMES = { none: 'Bez přístupu', start: 'Start', pro: 'Pro', elite: 'Elite' };

// Aktivní plán: bez účtů (cloud vypnutý) = vše odemčeno (dev); nepřihlášen = bez přístupu
function effPlan() {
    if (!cloudEnabled) return 'elite';
    if (!authToken) return 'none';
    if (mySub && mySub.is_admin) return 'elite';   // admin má vždy plný přístup
    return (mySub && mySub.effective) || 'none';
}
function hasAccess() { return effPlan() !== 'none'; }
function hasFeature(minPlan) {
    return (PLAN_RANK[effPlan()] ?? -1) >= PLAN_RANK[minPlan];
}
function lockedCardHTML(title, minPlan, desc) {
    return `<div class="locked-card">
        <div class="locked-icon"><i class="ti ti-lock"></i></div>
        <h3>${title}</h3>
        <p>${desc || ''} Dostupné v plánu <b>${PLAN_NAMES[minPlan]}</b> a vyšším.</p>
        <button class="btn-primary" style="display:inline-flex;align-items:center;gap:8px;" onclick="openProfile('plans')"><i class="ti ti-crown"></i> Zobrazit předplatné</button>
    </div>`;
}
// Historie AI chatu pro aktuální akcii
let chatHistory = [];

function updateMarketWidget() {
    const el = document.getElementById('mkt-status'); if (!el) return;
    const parts = new Intl.DateTimeFormat('en-GB', { timeZone: 'Europe/Prague', weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false }).formatToParts(new Date());
    const get = t => (parts.find(p => p.type === t) || {}).value;
    const wd = { Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6, Sun: 0 }[get('weekday')];
    const mins = parseInt(get('hour'), 10) * 60 + parseInt(get('minute'), 10);
    const OPEN = 15 * 60 + 30, CLOSE = 22 * 60;
    const weekday = wd >= 1 && wd <= 5;
    const open = weekday && mins >= OPEN && mins < CLOSE;
    document.querySelectorAll('.mkt-dot').forEach(d => d.className = 'mkt-dot ' + (open ? 'open' : 'closed'));
    const hm = t => String(Math.floor(t / 60)).padStart(2, '0') + ':' + String(t % 60).padStart(2, '0');
    const pill = document.getElementById('mkt-pill-label');
    if (open) {
        if (pill) pill.textContent = 'US trh otevřen';
        el.innerHTML = '<b style="color:var(--up)">Otevřeno</b> · zavírá ve ' + hm(CLOSE);
    } else {
        if (pill) pill.textContent = 'US trh zavřen';
        let when;
        if (weekday && mins < OPEN) when = 'dnes';
        else if (wd === 5 && mins >= CLOSE) when = 'v pondělí';
        else if (wd === 6) when = 'v pondělí';
        else when = 'zítra';
        el.innerHTML = '<b style="color:var(--down)">Zavřeno</b> · otevírá ' + when + ' v ' + hm(OPEN);
    }
}
function initMarketWidget() { updateMarketWidget(); setInterval(updateMarketWidget, 30000); }

/* ---- VSTUPNÍ ZÁMEK ---------------------------------------------------
   Server je jediná autorita (/api/gate + HttpOnly cookie). localStorage klíč
   je jen kosmetika, aby odemčenému uživateli neproblikla zamykací obrazovka. */
const SL_HINT = 'ma_gate_ok';

function slHide(){
    const el = document.getElementById('siteLock');
    if (el) el.style.display = 'none';
}

async function slSubmit(){
    const inp = document.getElementById('slInput');
    const btn = document.getElementById('slBtn');
    const err = document.getElementById('slErr');
    const pw = (inp.value || '').trim();
    if (!pw) { inp.focus(); return; }
    btn.disabled = true; err.textContent = '';
    try {
        const r = await fetch('/api/gate', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({password: pw})
        });
        const d = await r.json().catch(() => ({}));
        if (r.ok && d.ok) {
            localStorage.setItem(SL_HINT, '1');
            location.reload();     // čistý boot appky už za odemčenou bránou
            return;
        }
        err.textContent = d.error || 'Nesprávné heslo.';
    } catch (e) {
        err.textContent = 'Nepodařilo se ověřit heslo.';
    }
    btn.disabled = false;
    inp.value = ''; inp.focus();
}

function initSiteLock(){
    const el = document.getElementById('siteLock');
    if (!el) return;
    // Odemčeného uživatele nenecháme koukat na zámek, než doběhne fetch.
    if (localStorage.getItem(SL_HINT) === '1') el.style.display = 'none';

    document.getElementById('slBtn').addEventListener('click', slSubmit);
    document.getElementById('slInput').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') slSubmit();
    });

    fetch('/api/gate').then(r => r.json()).then(d => {
        if (d && d.open) { localStorage.setItem(SL_HINT, '1'); slHide(); }
        else {
            localStorage.removeItem(SL_HINT);   // cookie vypršela → zamknout zpět
            el.style.display = 'flex';
            document.getElementById('slInput').focus();
        }
    }).catch(() => {});
}

document.addEventListener("DOMContentLoaded", () => {
    initSiteLock();
    initResetFromUrl();
    initGate();
    initMarketWidget();

    const searchInput = document.getElementById('searchInput');
    const searchDropdown = document.getElementById('searchDropdown');

    searchInput.addEventListener('input', (e) => {
        clearTimeout(searchTimeout);
        const query = e.target.value.trim();
        if(query.length === 0) { showPopularMarkets(); return; }
        searchDropdown.style.display = 'flex';
        searchDropdown.innerHTML = '<div style="padding:15px; color:var(--text2); text-align:center;"><i class="ti ti-loader spin"></i> Hledám...</div>';
        searchTimeout = setTimeout(() => fetchSearchResults(query), 300);
    });
    searchInput.addEventListener('focus', () => {
        if (!searchInput.value.trim()) showPopularMarkets();
    });

    document.addEventListener('click', (e) => {
        if(!searchInput.contains(e.target) && !searchDropdown.contains(e.target)) searchDropdown.style.display = 'none';
    });
    
    // Close modals on backdrop click
    document.getElementById('alertModal').addEventListener('click', (e) => {
        if(e.target === document.getElementById('alertModal')) closeAlertModal();
    });
    document.getElementById('profileModal').addEventListener('click', (e) => {
        if(e.target === document.getElementById('profileModal')) closeProfile();
    });

    // Klávesové zkratky
    document.addEventListener('keydown', (e) => {
        if(e.key === '/' && document.activeElement.tagName !== 'INPUT') {
            e.preventDefault(); searchInput.focus();
        }
        if(e.key === 'Escape') {
            const pk = document.getElementById('cmp-picker');
            if(pk && pk.style.display === 'flex') { closeCmpPicker(); return; }
            if(document.getElementById('legalModal').classList.contains('open')) { closeLegal(); return; }
            if(document.getElementById('compareModal').classList.contains('open')) { closeCompare(); return; }
            if(document.getElementById('alertModal').classList.contains('open')) { closeAlertModal(); return; }
            if(document.getElementById('profileModal').classList.contains('open')) { closeProfile(); return; }
            if(document.getElementById('detail-view').style.display === 'block') closeDetail();
            searchDropdown.style.display = 'none';
        }
    });
});

const SEARCH_TYPE_CZ = { EQUITY: 'Akcie', ETF: 'ETF', INDEX: 'Index', CRYPTOCURRENCY: 'Krypto', FUTURE: 'Komodita', CURRENCY: 'Měna', MUTUALFUND: 'Fond' };
const SEARCH_TYPE_COL = { INDEX: '#29B6F6', CRYPTOCURRENCY: '#AB47BC', FUTURE: '#FFC400', CURRENCY: '#26A69A', ETF: '#7C4DFF' };
function searchItemHTML(item) {
    const t = item.type || 'EQUITY';
    const badge = `<span class="si-type" style="background:${(SEARCH_TYPE_COL[t] || 'var(--text2)')}22;color:${SEARCH_TYPE_COL[t] || 'var(--text2)'}">${SEARCH_TYPE_CZ[t] || 'Akcie'}</span>`;
    return `<div class="search-item" onclick="selectSearchItem('${item.ticker}')">
        <div style="display:flex; align-items:center; gap:10px; min-width:0;"><span class="si-ticker">${item.ticker}</span><span class="si-name">${escapeHtml(niceName(item.ticker, item.name))}</span></div>
        <div style="display:flex; align-items:center; gap:8px; flex:none;">${badge}<span class="si-exch">${item.exchange || ''}</span></div>
    </div>`;
}

const POPULAR_MARKETS = [
    { h: 'Indexy' },
    { ticker: '^GSPC', type: 'INDEX' }, { ticker: '^IXIC', type: 'INDEX' }, { ticker: '^DJI', type: 'INDEX' }, { ticker: '^GDAXI', type: 'INDEX' },
    { h: 'Komodity' },
    { ticker: 'GC=F', type: 'FUTURE' }, { ticker: 'SI=F', type: 'FUTURE' }, { ticker: 'CL=F', type: 'FUTURE' }, { ticker: 'HG=F', type: 'FUTURE' },
    { h: 'Krypto' },
    { ticker: 'BTC-USD', type: 'CRYPTOCURRENCY' }, { ticker: 'ETH-USD', type: 'CRYPTOCURRENCY' },
    { h: 'Populární akcie (USA)' },
    { ticker: 'AAPL', type: 'EQUITY', name: 'Apple' }, { ticker: 'NVDA', type: 'EQUITY', name: 'NVIDIA' }, { ticker: 'MSFT', type: 'EQUITY', name: 'Microsoft' },
    { ticker: 'TSLA', type: 'EQUITY', name: 'Tesla' }, { ticker: 'AMZN', type: 'EQUITY', name: 'Amazon' }, { ticker: 'GOOGL', type: 'EQUITY', name: 'Alphabet' },
    { h: 'České akcie 🇨🇿 (BCPP)' },
    { ticker: 'CEZ.PR', type: 'EQUITY', name: 'ČEZ' }, { ticker: 'KOMB.PR', type: 'EQUITY', name: 'Komerční banka' },
    { ticker: 'MONET.PR', type: 'EQUITY', name: 'Moneta Money Bank' }, { ticker: 'PHILIP.PR', type: 'EQUITY', name: 'Philip Morris ČR' },
    { ticker: 'KOFOL.PR', type: 'EQUITY', name: 'Kofola' }, { ticker: 'COLT.PR', type: 'EQUITY', name: 'Colt CZ Group' },
    { h: 'Evropské akcie 🇪🇺' },
    { ticker: 'SAP.DE', type: 'EQUITY', name: 'SAP' }, { ticker: 'ASML.AS', type: 'EQUITY', name: 'ASML' },
    { ticker: 'ULVR.L', type: 'EQUITY', name: 'Unilever' }, { ticker: 'MBG.DE', type: 'EQUITY', name: 'Mercedes-Benz' },
    { h: 'Měnové páry' },
    { ticker: 'EURCZK=X', type: 'CURRENCY' }, { ticker: 'USDCZK=X', type: 'CURRENCY' }, { ticker: 'EURUSD=X', type: 'CURRENCY' },
];
function showPopularMarkets() {
    const dropdown = document.getElementById('searchDropdown');
    dropdown.style.display = 'flex';
    dropdown.innerHTML = '<div class="search-head">Populární trhy</div>' + POPULAR_MARKETS.map(x =>
        x.h ? `<div class="search-grp">${x.h}</div>` : searchItemHTML(x)).join('');
}

async function fetchSearchResults(query) {
    const dropdown = document.getElementById('searchDropdown');
    try {
        const res = await fetch(`${BACKEND_URL}/api/search?q=${encodeURIComponent(query)}`);
        const data = await res.json();
        if(!data.ok || data.results.length === 0) {
            dropdown.innerHTML = '<div style="padding:15px; color:var(--text2); text-align:center;">Nic nenalezeno</div>'; return;
        }
        dropdown.innerHTML = data.results.map(searchItemHTML).join('');
    } catch(e) { dropdown.innerHTML = '<div style="padding:15px; color:var(--down); text-align:center;">Chyba připojení k serveru.</div>'; }
}

function selectSearchItem(ticker) {
    document.getElementById('searchDropdown').style.display = 'none';
    document.getElementById('searchInput').value = '';
    loadStockDetail(ticker);
}

function formatNum(num) {
    if(!num || num === "N/A") return "N/A";
    if(num >= 1e12) return (num / 1e12).toFixed(2) + " T";
    if(num >= 1e9) return (num / 1e9).toFixed(2) + " B";
    if(num >= 1e6) return (num / 1e6).toFixed(2) + " M";
    return num.toLocaleString();
}

// --- ROBUSTNÍ JSON PARSER: Vytáhne JSON i z obalených odpovědí modelu ---

// Server-side AI: zavolá náš endpoint (uživatel nezadává žádný klíč). Vrací data nebo null.
async function serverAI(path, body) {
    try {
        const r = await fetch(`${BACKEND_URL}/api/ai/${path}`, {
            method: 'POST', headers: authHeaders(), body: JSON.stringify(body || {})
        });
        const d = await r.json();
        if (!d.ok) { d._status = r.status; return d; }
        return d;
    } catch (e) {
        return { ok: false, error: 'Chyba spojení se serverem.' };
    }
}

// --- PORTFOLIO & DASHBOARD AI ---
async function loadWatchlist() {
    const watchGrid = document.getElementById('watch-grid');
    const badge = document.getElementById('live-badge');
    const rbtn = document.getElementById('refresh-btn');
    const rico = document.getElementById('refresh-ico');
    if (rico) rico.classList.add('spin');
    if (rbtn) rbtn.disabled = true;

    if(myStocks.length === 0) {
        watchGrid.innerHTML = '';
        document.getElementById('watch-empty').style.display = 'block';
        if (rico) rico.classList.remove('spin');
        if (rbtn) rbtn.disabled = false;
        return;
    }

    if(!watchGrid.innerHTML) watchGrid.innerHTML = '<div style="color:var(--accent)"><i class="ti ti-loader spin"></i> Připojuji se k burze...</div>';

    try {
        const res = await fetch(`${BACKEND_URL}/api/watchlist?tickers=${myStocks.join(',')}`);
        const data = await res.json();
        if(!data.ok) throw new Error();

        badge.style.display = 'inline-block';
        lastWatchlistData = data.results;
        renderWatchCards(data.results);
        checkAlerts(data.results);
        if (typeof renderOverviewPortfolio === 'function') renderOverviewPortfolio();
        if (typeof renderOverviewEvents === 'function') renderOverviewEvents();

        if(!autoRefreshTimer) {
            autoRefreshTimer = setInterval(() => {
                if(document.getElementById('dashboard').style.display !== 'none') loadWatchlistSilent();
            }, 10000);
        }
        showToast('Portfolio aktualizováno', 'up', 'ti-refresh');
    } catch(e) {
        badge.style.display = 'none';
        watchGrid.innerHTML = `<div style="color:var(--down); padding:20px; border:1px solid var(--down); border-radius:10px;">Chyba spojení s burzou.</div>`;
        showToast('Aktualizace se nezdařila', 'down', 'ti-x');
    } finally {
        if (rico) rico.classList.remove('spin');
        if (rbtn) rbtn.disabled = false;
    }
}

async function loadWatchlistSilent() {
    try {
        const res = await fetch(`${BACKEND_URL}/api/watchlist?tickers=${myStocks.join(',')}`);
        const data = await res.json();
        if(!data.ok) return;
        lastWatchlistData = data.results;
        renderWatchCards(data.results);
        checkAlerts(data.results);
        if (typeof renderOverviewPortfolio === 'function') renderOverviewPortfolio();
    } catch(e) {}
}
