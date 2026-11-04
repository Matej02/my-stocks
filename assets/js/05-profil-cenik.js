// ====================== ÚROVEŇ INVESTORA + ZAČÁTEČNICKÉ PORTFOLIO ======================
function renderLevelButtons() {
    document.getElementById('lvl-beginner').classList.toggle('active', investorLevel === 'beginner');
    document.getElementById('lvl-advanced').classList.toggle('active', investorLevel === 'advanced');
    document.getElementById('level-hint').textContent = investorLevel === 'beginner'
        ? 'V záložce „Moje portfolio" ti připravíme 5 ověřených akcií napříč sektory.'
        : 'Plný režim bez doporučení pro začátečníky.';
}

function setLevel(lvl) {
    investorLevel = lvl;
    localStorage.setItem('mystocks_level', lvl);
    renderLevelButtons();
    renderStarter();
    showToast(lvl === 'beginner' ? 'Úroveň: Začátečník' : 'Úroveň: Pokročilý', 'up', 'ti-stairs');
}

const RISK_DESC = {
    // Čísla = VALIDATION v backendu (2015–2026, max 5 pozic, max 2 na sektor).
    conservative: 'Pozice po 10 %, a když je trh rozkolísaný, část kapitálu v hotovosti. Historicky poloviční propady: −18 % místo −34 % u indexu, za cenu o něco nižšího výnosu.',
    balanced: 'Pozice po 10 %, zbytek v indexu. Historicky +15,6 % ročně proti +13,9 % u indexu, propady podobné jako index.',
    aggressive: 'Pozice po 20 %, v signálech až celý kapitál. Historicky nejvyšší výnos (+17 % ročně), ale v letech tažených pár giganty víc kolísá vůči indexu.',
};

function setRisk(r) {
    investorRisk = r;
    localStorage.setItem('mystocks_risk', r);
    renderRiskButtons();
    renderStarter();
    // Při změně profilu znovu načti, co na něm závisí. (Dřív se tu volalo
    // neexistující renderOpps() – příležitosti se proto nikdy nepřekreslily.)
    if (oppData && oppData.length) renderOpportunities();
    if (typeof signalsLoaded !== 'undefined' && signalsLoaded) loadSignals(true);
    if (picksCache && Object.keys(picksCache).length) { picksCache = {}; loadOppPicks(true); }
    if (typeof fetchOverviewPositions === 'function') fetchOverviewPositions();
    planOnRisk();
    if (authToken) fetch(`${BACKEND_URL}/api/profile/risk`, {
        method: 'POST', headers: authHeaders(), body: JSON.stringify({ risk: r })
    }).catch(() => {});
    showToast('Tolerance rizika: ' + ({conservative:'Konzervativní', balanced:'Vyvážená', aggressive:'Agresivní'})[r], 'up', 'ti-shield-half');
}

function renderRiskButtons() {
    ['conservative','balanced','aggressive'].forEach(k => {
        const b = document.getElementById('risk-' + k);
        if (b) b.classList.toggle('active', investorRisk === k);
    });
    const hint = document.getElementById('risk-hint');
    if (hint) hint.textContent = ({
        conservative: 'Skener vyfiltruje vysokou volatilitu a malé firmy. Jen zavedené, stabilní tituly.',
        balanced: 'Standardní mix bez agresivních spekulací. Doporučujeme většině uživatelů.',
        aggressive: 'Bez filtru — uvidíš i růstové small-caps s vyšší volatilitou a vyšším potenciálem.',
    })[investorRisk] || '';
}

// Filtr podle rizikového profilu (frontend) – vrací true, pokud titul projde
function riskFilterPass(o) {
    if (investorRisk === 'aggressive' || !investorRisk) return true;
    const vol = o.volatility_pct;
    const mcap = o.market_cap;
    if (investorRisk === 'conservative') {
        if (typeof vol === 'number' && vol > 35) return false;
        if (typeof mcap === 'number' && mcap < 10e9) return false;  // < 10 mld $
        return true;
    }
    if (investorRisk === 'balanced') {
        if (typeof vol === 'number' && vol > 55) return false;
        if (typeof mcap === 'number' && mcap < 2e9) return false;   // < 2 mld $
        return true;
    }
    return true;
}

// ===== In-app notifikace (dot na profilu) + browser Notification API =====
let _notifLastCount = 0;
let _notifBrowserGranted = false;
function updateNotifPushBtn() {
    const btn = document.getElementById('notif-push-btn');
    if (!btn) return;
    if (!("Notification" in window)) {
        btn.innerHTML = '<i class="ti ti-alert-circle"></i> Nepodporováno';
        btn.disabled = true;
        return;
    }
    const p = Notification.permission;
    if (p === 'granted') {
        btn.innerHTML = '<i class="ti ti-check"></i> Zapnuto';
        btn.style.color = 'var(--up)';
        btn.style.borderColor = 'var(--up)';
    } else if (p === 'denied') {
        btn.innerHTML = '<i class="ti ti-x"></i> Zablokováno v prohlížeči';
        btn.disabled = true;
    } else {
        btn.innerHTML = '<i class="ti ti-bell"></i> Zapnout';
    }
}

async function handleNotifPushClick() {
    const ok = await requestBrowserNotifPerm();
    updateNotifPushBtn();
    if (ok) {
        try {
            new Notification('MY ADVANTAGE ✅', {
                body: 'Notifikace zapnuté. Uvidíš je, když se u tvých akcií něco stane.',
                icon: '/logo/icon-192.png',
            });
        } catch(e) {}
        showToast('Notifikace zapnuty', 'up', 'ti-bell-ringing');
    }
}

async function requestBrowserNotifPerm() {
    if (!("Notification" in window)) return false;
    if (Notification.permission === 'granted') { _notifBrowserGranted = true; return true; }
    if (Notification.permission === 'denied') return false;
    try {
        const r = await Notification.requestPermission();
        _notifBrowserGranted = r === 'granted';
        return _notifBrowserGranted;
    } catch(e) { return false; }
}

async function pollNotifUnread() {
    if (!authToken) return;
    try {
        const r = await fetch(`${BACKEND_URL}/api/notifications/unread`, { headers: authHeaders() });
        const d = await r.json();
        const cnt = (d && d.count) || 0;
        const dot = document.getElementById('notif-dot');
        if (dot) {
            if (cnt > 0) { dot.style.display = 'inline-flex'; dot.textContent = cnt > 9 ? '9+' : cnt; }
            else { dot.style.display = 'none'; }
        }
        // Nová událost od minulého pollu → browser notifikace, když má user povoleno
        if (_notifBrowserGranted && cnt > _notifLastCount && d.items && d.items.length) {
            const top = d.items[0];
            try {
                const n = new Notification('MY ADVANTAGE — ' + (top.ticker || ''), {
                    body: (top.title || '') + '\n' + (top.hint || ''),
                    icon: '/logo/icon-192.png',
                    badge: '/logo/icon-96.png',
                    tag: 'ma-event-' + (top.ticker || ''),
                });
                n.onclick = () => { window.focus(); if (top.ticker) loadStockDetail(top.ticker); };
            } catch(e) {}
        }
        _notifLastCount = cnt;
    } catch(e) {}
}

async function markNotifSeen() {
    if (!authToken) return;
    try {
        await fetch(`${BACKEND_URL}/api/notifications/seen`, {
            method: 'POST', headers: authHeaders()
        });
        _notifLastCount = 0;
        const dot = document.getElementById('notif-dot');
        if (dot) dot.style.display = 'none';
    } catch(e) {}
}

// ===== Právní modal (ToS / Privacy / Disclaimer) =====
const LEGAL_TEXTS = {
    terms: `
        <h3>Obchodní podmínky</h3>
        <p><b>Poskytovatel:</b> MY ADVANTAGE, provozovatel webu <b>myadvantage.site</b>. Kontakt: <a href="mailto:podpora@myadvantage.site">podpora@myadvantage.site</a>.</p>
        <h4>1. Předmět služby</h4>
        <p>MY ADVANTAGE poskytuje online <b>informační a analytický nástroj</b> pro individuální investory. Služba zahrnuje sledování portfolia, tržní data, hloubkovou analýzu akcií, přehled událostí a novinek. <b>Nejsme licencovaný investiční poradce ani broker.</b></p>
        <h4>2. Předplatné a platby</h4>
        <p>Platby probíhají měsíčně přes Stripe. Předplatné se automaticky obnovuje, dokud ho zákazník nezruší v profilu. Zrušení je účinné k datu konce zaplaceného období.</p>
        <h4>3. Vrácení peněz</h4>
        <p><b>Platby jsou nevratné.</b> Uživatel má možnost vyzkoušet zdarma dostupné funkce před předplatným. Uzavřením předplatného výslovně souhlasí s tím, že plnění začíná ihned a nárok na odstoupení od smlouvy dle §&nbsp;1837 písm. l) občanského zákoníku zaniká.</p>
        <h4>4. Odpovědnost</h4>
        <p>Poskytovatel neručí za investiční rozhodnutí uživatele ani za výnos či ztrátu z nich vzniklou. Data z třetích stran (Yahoo Finance, SEC EDGAR, Finnhub a další) jsou přebírána v dobré víře; nelze zaručit jejich úplnou přesnost nebo dostupnost.</p>
        <h4>5. Účet a bezpečnost</h4>
        <p>Uživatel odpovídá za bezpečnost svého hesla. Poskytovatel může účet zrušit v případě porušení podmínek, zneužití nebo pokusu o obcházení plánu.</p>
        <h4>6. Změny podmínek</h4>
        <p>Poskytovatel může podmínky měnit; uživatel bude informován v aplikaci. Pokračováním v používání služby uživatel se změnou souhlasí.</p>
        <p style="color:var(--text2); font-size:12px; margin-top:20px;">Verze podmínek: 2026-07-03</p>
    `,
    privacy: `
        <h3>Ochrana osobních údajů</h3>
        <p><b>Správce údajů:</b> MY ADVANTAGE, <a href="mailto:podpora@myadvantage.site">podpora@myadvantage.site</a>.</p>
        <h4>Jaké údaje zpracováváme</h4>
        <ul>
            <li><b>E-mail a heslo</b> — pro přihlášení (heslo je uložené jen v podobě bezpečného hashe se solí, my ho nikdy nevidíme).</li>
            <li><b>Watchlist a portfolio</b> — seznam sledovaných tickerů, poznámky, alerty, tagy skupin.</li>
            <li><b>Předplatné</b> — plán, historie plateb (Stripe drží kartová data, my je nemáme).</li>
            <li><b>Provozní údaje</b> — čas přihlášení, poslední aktivita, IP adresa v logu, User-Agent.</li>
        </ul>
        <h4>Právní základ</h4>
        <p>Zpracováváme údaje na základě <b>plnění smlouvy</b> (poskytování služby) a <b>oprávněného zájmu</b> (bezpečnost, zlepšování služby). Marketingové e-maily posíláme jen s explicitním souhlasem (opt-in v profilu — ranní souhrn, sobotní digest).</p>
        <h4>Předávání třetím stranám</h4>
        <ul>
            <li><b>Vercel</b> (hosting)</li>
            <li><b>Upstash</b> (KV databáze — účty, portfolio, cache)</li>
            <li><b>Stripe</b> (platby)</li>
            <li><b>Brevo</b> (odchozí e-maily)</li>
        </ul>
        <p>Údaje nikdy neprodáváme třetím stranám k marketingu.</p>
        <h4>Doba uchování</h4>
        <p>Údaje uchováváme po dobu aktivního účtu. Při zrušení účtu jsou smazány do 30 dnů (kromě daňových dokladů, které jsou uchovávány dle zákona).</p>
        <h4>Tvá práva</h4>
        <p>Máš právo na přístup, opravu, výmaz, přenositelnost údajů a podání stížnosti u ÚOOÚ (uoou.gov.cz). Kontaktuj nás na <a href="mailto:podpora@myadvantage.site">podpora@myadvantage.site</a>.</p>
        <p style="color:var(--text2); font-size:12px; margin-top:20px;">Verze: 2026-07-03</p>
    `,
    disclaimer: `
        <h3>Investiční upozornění</h3>
        <p><b>MY ADVANTAGE není licencovaný investiční poradce.</b> Aplikace slouží výhradně pro <b>informační a vzdělávací účely</b>. Neposkytujeme investiční doporučení ve smyslu zákona č.&nbsp;256/2004 Sb., o podnikání na kapitálovém trhu.</p>
        <h4>Rizika investování</h4>
        <ul>
            <li>Investice do akcií nesou <b>riziko ztráty</b> vloženého kapitálu.</li>
            <li><b>Historická výkonnost nezaručuje budoucí výsledky.</b> Ověřená trefnost ~59 % je poctivá historická statistika, ne příslib budoucích zisků.</li>
            <li>Přibližně <b>~41 % obchodů podle našeho modelu končí v mínusu</b> — proto u každého signálu počítáme stop-loss.</li>
            <li>Analýzy a signály jsou podklady k <b>tvému vlastnímu rozhodnutí</b>. Odpovědnost za obchodní rozhodnutí nese vždy uživatel.</li>
        </ul>
        <h4>Zdroje dat</h4>
        <p>Používáme data z Yahoo Finance, SEC EDGAR, Finnhubu a veřejných RSS zdrojů. Data jsou přebírána v dobré víře; nemůžeme zaručit jejich úplnou přesnost, aktuálnost nebo dostupnost. Chyby v datech mohou vést k chybným závěrům modelu.</p>
        <h4>Konflikt zájmů</h4>
        <p>MY ADVANTAGE <b>není odměňován za doporučení konkrétních akcií</b>. Nemáme partnerství s brokerem, emitentem ani investiční společností. Zisk pochází výhradně z předplatného uživatelů.</p>
        <h4>Před investováním</h4>
        <p>Před jakoukoli investicí zvaž vlastní finanční situaci, cíle a toleranci rizika. V případě pochybností konzultuj s <b>licencovaným investičním poradcem</b>. Nikdy neinvestuj peníze, jejichž ztráta by tě dostala do finančních potíží.</p>
        <p style="color:var(--text2); font-size:12px; margin-top:20px;">Verze: 2026-07-03</p>
    `,
};

function openLegal(tab) {
    setLegalTab(tab || 'terms');
    document.getElementById('legalModal').classList.add('open');
}
function closeLegal() {
    document.getElementById('legalModal').classList.remove('open');
}
function setLegalTab(tab) {
    ['terms','privacy','disclaimer'].forEach(t => {
        const btn = document.getElementById('ltab-' + t);
        if (btn) btn.classList.toggle('active', t === tab);
    });
    const body = document.getElementById('legal-body');
    if (body) { body.innerHTML = LEGAL_TEXTS[tab] || ''; body.scrollTop = 0; }
}

// ===== Referral program =====
async function loadReferral() {
    try {
        const r = await fetch(`${BACKEND_URL}/api/referral`, { headers: authHeaders() });
        const d = await r.json();
        if (!d.ok) return;
        const codeEl = document.getElementById('ref-code-val');
        const urlEl = document.getElementById('ref-share-url');
        const inv = document.getElementById('ref-invited');
        const paid = document.getElementById('ref-paid');
        const bonus = document.getElementById('ref-bonus');
        if (codeEl) codeEl.textContent = d.code || '—';
        if (urlEl) urlEl.value = d.share_url || '';
        if (inv) inv.textContent = d.invited_count ?? 0;
        if (paid) paid.textContent = d.paid_count ?? 0;
        if (bonus) bonus.textContent = (d.bonus_days_pending ?? 0) + ' dní';
    } catch(e) {}
}

async function copyRefLink() {
    const el = document.getElementById('ref-share-url');
    if (!el || !el.value) return;
    try {
        await navigator.clipboard.writeText(el.value);
        showToast('Odkaz zkopírován — nasdílej kamarádovi!', 'up', 'ti-copy');
    } catch(e) {
        el.select(); document.execCommand('copy');
        showToast('Odkaz zkopírován', 'up', 'ti-copy');
    }
}

// ===== Notifikace (ranní / týdenní shrnutí e-mailem) =====
async function saveDisplayName() {
    const el = document.getElementById('profile-name-input');
    if (!el || !authToken) return;
    try {
        const r = await fetch(`${BACKEND_URL}/api/profile/name`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ name: el.value }) });
        const d = await r.json().catch(() => ({ ok: false }));
        if (!d.ok) { showToast(d.error || 'Jméno se nepodařilo uložit', 'down', 'ti-x'); return; }
        myName = d.name || ''; myNameVoc = d.name_voc || ''; el.value = myName;
        updateProfileBtn(); renderOverviewGreeting();
        showToast(myName ? `Budu ti říkat ${myName}` : 'Oslovení vypnuto', 'up', 'ti-user-check');
    } catch (e) { showToast('Jméno se nepodařilo uložit', 'down', 'ti-x'); }
}

async function saveNotif() {
    const morning = !!document.getElementById('notif-morning').checked;
    const weeklyEl = document.getElementById('notif-weekly');
    const weekly = !!(weeklyEl && weeklyEl.checked);
    const alertsEl = document.getElementById('notif-alerts');
    const alerts = !!(alertsEl && alertsEl.checked);
    myNotif = { morning, weekly, alerts };
    try {
        await fetch(`${BACKEND_URL}/api/notifications`, {
            method: 'POST', headers: authHeaders(), body: JSON.stringify({ morning, weekly, alerts })
        });
        showToast('Nastavení notifikací uloženo', 'up', 'ti-mail');
    } catch (e) { showToast('Nepodařilo se uložit', 'down', 'ti-x'); }
}

// ===== Zjednodušený režim pro začátečníky =====
function setSimple(on) {
    investorSimple = !!on;
    localStorage.setItem('mystocks_simple', investorSimple ? '1' : '0');
    applySimple();
}

function applySimple() {
    document.body.classList.toggle('simple-mode', investorSimple);
    const banner = document.getElementById('simple-banner');
    if (banner) banner.style.display = investorSimple ? 'block' : 'none';
    const t = document.getElementById('simple-toggle');
    if (t) t.checked = investorSimple;
    if (investorSimple) switchTab('portfolio');
}

function renderStarter() {
    const box = document.getElementById('starter-card');
    if (!box) return;
    if (investorLevel !== 'beginner') { box.innerHTML = ''; return; }
    const set = STARTER_SETS[investorRisk] || STARTER_SETS.balanced;

    const pills = ['conservative', 'balanced', 'aggressive'].map(r =>
        `<button class="risk-pill ${r === investorRisk ? 'active' : ''}" onclick="setRisk('${r}')">${RISK_LABELS[r]}</button>`).join('');

    const items = set.map(x => {
        const owned = myStocks.includes(x.t);
        return `<div class="starter-item">
            <div class="starter-sector">${x.s}</div>
            <div class="starter-tkr">${x.t}</div>
            <div class="starter-name">${x.n}</div>
            <button class="starter-add ${owned ? 'added' : ''}" onclick="event.stopPropagation(); addStarter('${x.t}')">
                ${owned ? '<i class="ti ti-check"></i> Přidáno' : '<i class="ti ti-plus"></i> Přidat'}
            </button>
        </div>`;
    }).join('');

    const ownsVOO = myStocks.includes('SXR8.DE');
    box.innerHTML = `<div class="starter-box">
        <div class="starter-head"><i class="ti ti-seeding"></i> Pro začátečníky — průvodce</div>
        <div class="starter-sub">Nový v investování? V klidu, projdeme to krok po kroku.</div>

        <div class="bg-steps">
            <div class="bg-step"><span class="bg-num">1</span><div><b>Investuj jen to, co můžeš postrádat.</b> Klidně začni malou částkou, kterou roky nebudeš potřebovat.</div></div>
            <div class="bg-step"><span class="bg-num">2</span><div><b>Začni jedním ETF.</b> ETF = koš mnoha firem v jedné akcii. <b>SXR8</b> (iShares Core S&amp;P 500) = 500 největších firem USA najednou — okamžitá diverzifikace. Z ČR kupuj evropské <b>UCITS</b> verze; americká ETF jako VOO tu broker kvůli regulaci neprodá.</div></div>
            <div class="bg-step"><span class="bg-num">3</span><div><b>Časem přidej pár firem</b> z různých oborů, ať nejsi závislý jen na jedné.</div></div>
        </div>

        <div class="bg-first">
            <div><div class="bg-first-t">Tvůj první krok</div><div class="bg-first-d"><b>SXR8</b> — celý americký trh v jednom ETF</div></div>
            <button class="btn-primary" style="flex:none;" onclick="event.stopPropagation(); addStarter('SXR8.DE')">${ownsVOO ? '<i class="ti ti-check"></i> Přidáno' : '<i class="ti ti-plus"></i> Přidat SXR8'}</button>
        </div>

        <details class="bg-gloss"><summary><i class="ti ti-book"></i> Slovníček pojmů</summary>
            <p><b>Akcie</b> — podíl ve firmě.<br><b>ETF</b> — koš mnoha akcií v jednom (např. celý index).<br><b>Index (S&P 500)</b> — 500 největších amerických firem.<br><b>Dividenda</b> — část zisku vyplácená akcionářům.<br><b>Diverzifikace</b> — rozložení peněz do víc firem/oborů = nižší riziko.<br><b>Volatilita</b> — jak moc cena kolísá (vyšší = divočejší).</p>
        </details>

        <div class="starter-sub" style="margin-top:20px"><b>Až budeš chtít víc:</b> vyber styl a přidej 5 titulů z různých sektorů (diverzifikace).</div>
        <div class="risk-row">${pills}</div>
        <div class="risk-desc"><i class="ti ti-info-circle"></i> ${RISK_DESC[investorRisk]}</div>
        <div class="starter-grid">${items}</div>
        <div class="starter-actions">
            <button class="btn-primary" style="flex:none; display:inline-flex; align-items:center; gap:6px;" onclick="addAllStarter()"><i class="ti ti-stack-2"></i> Přidat všech 5</button>
        </div>
    </div>`;
}

function addStarter(ticker) {
    if (!myStocks.includes(ticker)) {
        myStocks.push(ticker);
        localStorage.setItem('mystocks_portfolio', JSON.stringify(myStocks));
        cloudSync();
        showToast(`${ticker} přidán do sledování`, 'up', 'ti-plus');
        loadWatchlist();
    }
    renderStarter();
}

function addAllStarter() {
    let added = 0;
    (STARTER_SETS[investorRisk] || STARTER_SETS.balanced).forEach(x => {
        if (!myStocks.includes(x.t)) { myStocks.push(x.t); added++; }
    });
    if (added) {
        localStorage.setItem('mystocks_portfolio', JSON.stringify(myStocks));
        cloudSync();
        showToast(`Přidáno ${added} akcií do sledování`, 'up', 'ti-stack-2');
        loadWatchlist();
    }
    renderStarter();
}

// ====================== CENÍK PŘEDPLATNÉHO ======================
const PLANS = [
    { id: 'start', name: 'Start', price: '5 €', period: '/ měsíc', feats: [
        ['Neomezené sledování', 1], ['Grafy, Signal Score & technický budík', 1], ['Aktuální zprávy', 1],
        ['Cenová upozornění + sync', 1], ['Koš tipů & analytici', 0], ['Hloubková analýza', 0] ] },
    { id: 'pro', name: 'Pro', price: '10 €', period: '/ měsíc', featured: true, feats: [
        ['Vše ze Start', 1], ['Měsíční koš tipů', 1], ['Doporučení analytiků & cílové ceny', 1],
        ['Fundamenty (P/E, kapitalizace)', 1], ['Hloubková analýza (5/den)', 1],
        ['Asistent, přehled & track record', 0] ] },
    { id: 'elite', name: 'Elite', price: '15 €', period: '/ měsíc', feats: [
        ['Vše z Pro', 1], ['Hloubková analýza (30/den)', 1], ['Asistent k akcii', 1],
        ['Tržní briefing & hodnocení akcií', 1], ['Zdůvodnění u tipů', 1],
        ['Track record signálů', 1] ] },
];

async function startCheckout(plan) {
    if (!authToken) { openProfile(); return; }
    showToast('Přesměrovávám na platbu…', 'up', 'ti-credit-card');
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/billing/checkout`, {
            method: 'POST', headers: authHeaders(), body: JSON.stringify({ plan })
        })).json();
        if (d.ok && d.url) { window.location.href = d.url; }
        else showToast(d.error || 'Platbu se nepodařilo spustit', 'down', 'ti-x');
    } catch (e) { showToast('Chyba spojení se serverem', 'down', 'ti-x'); }
}

function renderPlans() {
    const cur = effPlan();
    const nowS = Date.now() / 1000;
    const disc = (mySub && mySub.discount_pct && mySub.discount_until && mySub.discount_until > nowS) ? mySub.discount_pct : 0;
    const fmtCz = n => (Number.isInteger(n) ? String(n) : n.toFixed(2)).replace('.', ',');
    const cards = PLANS.map(pl => {
        const isCur = pl.id === cur;
        const feats = pl.feats.map(([t, ok]) =>
            `<li class="${ok ? '' : 'no'}"><i class="ti ti-${ok ? 'check' : 'x'}"></i> ${t}</li>`).join('');
        const tag = pl.featured ? 'Nejoblíbenější' : '';
        const cta = isCur
            ? '<div class="plan-cta active"><i class="ti ti-check"></i> Tvůj plán</div>'
            : (billingEnabled && billingPlans.includes(pl.id)
                ? `<button class="plan-cta plan-buy" onclick="startCheckout('${pl.id}')"><i class="ti ti-credit-card"></i> Předplatit</button>`
                : '<div class="plan-cta">Připravujeme platby</div>');
        let priceHtml = `${pl.price} <small>${pl.period}</small>`;
        if (disc) {
            const num = parseFloat(pl.price.replace(/[^\d.,]/g, '').replace(',', '.'));
            if (!isNaN(num)) {
                priceHtml = `<span style="text-decoration:line-through;color:var(--text2);font-size:17px;font-weight:600">${fmtCz(num)} €</span> ${fmtCz(num * (1 - disc / 100))} € <small>${pl.period}</small>`;
            }
        }
        return `<div class="plan-card ${pl.featured ? 'featured' : ''} ${isCur ? 'current' : ''}">
            <div class="plan-tag">${tag}</div>
            <div class="plan-name">${pl.name}</div>
            <div class="plan-price">${priceHtml}</div>
            <ul class="plan-feats">${feats}</ul>
            ${cta}
        </div>`;
    }).join('');
    const discNote = disc
        ? `<p class="auth-hint" style="margin-top:14px;color:var(--up)"><i class="ti ti-discount-2"></i> Máš slevu <b>${disc} %</b> na první 3 měsíce předplatného (z promo kódu) — ceny výše ji už zahrnují.</p>`
        : '';
    document.getElementById('pfview-plans').innerHTML = `
        <div class="plans-grid">${cards}</div>
        ${discNote}
        <p class="auth-hint" style="margin-top:10px;">
            <i class="ti ti-info-circle"></i> Online platby spustíme brzy. Do té doby plán aktivuje admin nebo zvací kód.
            Každý nový účet má prvních 7 dní plný <b>Pro</b> zdarma.
        </p>`;
}
