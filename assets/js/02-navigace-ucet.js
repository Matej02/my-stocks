// ====================== NAVIGACE SEKCÍ ======================
let activeTab = 'overview';
function switchTab(name) {
    activeTab = name;
    ['overview', 'portfolio', 'opportunities', 'results', 'ai'].forEach(s => {
        const sec = document.getElementById('section-' + s);
        const tab = document.getElementById('dtab-' + s);
        if (sec) sec.style.display = (s === name) ? 'block' : 'none';
        if (tab) tab.classList.toggle('active', s === name);
    });
    if (name === 'overview') {
        loadOverview();
        renderOverviewPlan();
        // Po otevření Přehledu uživatel události "viděl" – vynulujeme dot
        markNotifSeen();
    }
    if (name === 'results') {
        loadResults();
    }
    // Portfolio: překresli začátečnické portfolio
    if (name === 'portfolio') {
        renderStarter();
        planOpen();
    }
    // Příležitosti: signál „Sleva v trendu“ + podhodnocené s momentem (zamčené bez Pro) a pod nimi měsíční koš tipů
    if (name === 'opportunities') {
        if (!hasFeature('pro')) {
            document.getElementById('signals-grid').innerHTML = lockedCardHTML('Co koupit teď — ověřený signál', 'pro',
                'Akcie, které právě teď splňují náš backtestem prověřený nákupní signál.');
            document.getElementById('opp-grid').innerHTML = lockedCardHTML('Podhodnocené s momentem', 'pro',
                'Nejlevnější akcie S&amp;P 500, které zároveň mají roční momentum — ověřená kombinace.');
        } else {
            loadSignals();
            if (!oppData.length && !oppLoading) loadOpportunities();
        }
        loadOppPicks();
    }
    if (name === 'ai') {
        const lock = document.getElementById('ai-lock');
        if (!hasFeature('elite')) {
            lock.innerHTML = lockedCardHTML('Doporučení', 'elite',
                'Hodnocení každé tvé akcie, co se změnilo od včera, živý výkon tvých akcií i koše tipů, shrnutí od AI a track record verdiktů.');
            lock.style.display = 'block';
            document.getElementById('rec-root').innerHTML = '';
            document.getElementById('track-box').style.display = 'none';
        } else {
            lock.style.display = 'none';
            renderRecommendations();
            loadTrackRecord();
        }
    }
    // Scroll: skoč na začátek tab-baru (ne úplně nahoru), ať uživatel po přepnutí
    // vždycky vidí taby + nový obsah. Instantně, bez smooth animace — jinak vypadá
    // jako "poskakování" když se mění výška obsahu.
    requestAnimationFrame(() => {
        const tabs = document.querySelector('.dash-tabs');
        if (!tabs) return;
        if (getComputedStyle(tabs).position === 'fixed') { window.scrollTo(0, 0); return; }
        const y = tabs.getBoundingClientRect().top + window.scrollY - 20;
        if (window.scrollY > y) window.scrollTo(0, Math.max(0, y));
    });
}

// ====================== ÚČET & CLOUD SYNCHRONIZACE ======================
function authHeaders() {
    const h = { 'Content-Type': 'application/json' };
    if (authToken) h['Authorization'] = 'Bearer ' + authToken;
    return h;
}

// Nastaví lokální stav z cloud portfolia (BEZ zpětného pushe do cloudu)
function applyPortfolio(pf) {
    pf = pf || {};
    myStocks = Array.isArray(pf.watchlist) ? pf.watchlist : DEFAULT_WATCHLIST.slice();
    myAlerts = Array.isArray(pf.alerts) ? pf.alerts : [];
    localStorage.setItem('mystocks_portfolio', JSON.stringify(myStocks));
    localStorage.setItem('mystocks_alerts', JSON.stringify(myAlerts));
}

// Odešle aktuální portfolio do cloudu (jen když jsi přihlášen), s debounce
function cloudSync() {
    if (!authToken) return;
    clearTimeout(syncTimer);
    syncTimer = setTimeout(async () => {
        try {
            await fetch(`${BACKEND_URL}/api/portfolio`, {
                method: 'POST', headers: authHeaders(),
                body: JSON.stringify({ watchlist: myStocks, alerts: myAlerts })
            });
        } catch (e) {}
    }, 600);
}

// Zjistí, zda je cloud nastavený, a případně natáhne portfolio přihlášeného uživatele
async function initAccount() {
    try {
        const r = await fetch(`${BACKEND_URL}/api/account/status`);
        cloudEnabled = !!(await r.json()).cloud;
    } catch (e) { cloudEnabled = false; }

    // Backtest „důkaz" (neblokující) – pro banner v analýze
    fetch(`${BACKEND_URL}/api/backtest`).then(r => r.json())
        .then(d => { if (d.ok) btProof = d.result; }).catch(() => {});
    // Stav plateb (Stripe) – jestli zobrazit „Předplatit"
    fetch(`${BACKEND_URL}/api/billing/config`).then(r => r.json())
        .then(d => { if (d.ok) { billingEnabled = !!d.enabled; billingPlans = d.plans || []; } }).catch(() => {});

    if (cloudEnabled && authToken) {
        try {
            const r = await fetch(`${BACKEND_URL}/api/portfolio`, { headers: authHeaders() });
            if (r.ok) {
                const d = await r.json();
                applyPortfolio(d.portfolio);
                syncLotsFromCloud();
                // Stav předplatného
                try {
                    const me = await (await fetch(`${BACKEND_URL}/api/me`, { headers: authHeaders() })).json();
                    if (me.ok) { mySub = me.subscription; if (me.notif) myNotif = me.notif; myName = me.display_name || ''; myNameVoc = me.display_name_voc || ''; }
                    if (mySub && mySub.is_admin) setTimeout(adminDailyKick, 4000);   // admin: denní aktualizace 1× za den
                } catch (e) {}
            } else {
                // Token neplatný → odhlásit lokálně
                authToken = null; authUser = null; mySub = null;
                localStorage.removeItem('mystocks_token');
                localStorage.removeItem('mystocks_user');
            }
        } catch (e) {}
    }
    updateProfileBtn();
    renderOverviewGreeting();
}

function updateProfileBtn() {
    const btn = document.getElementById('profileBtn');
    const label = document.getElementById('profileBtnLabel');
    if (authToken && authUser) {
        btn.classList.add('logged');
        label.textContent = myName || 'Můj účet';
    } else {
        btn.classList.remove('logged');
        label.textContent = cloudEnabled ? 'Přihlásit' : 'Portfolio';
    }
}

let pendingPfView = 'portfolio';
// Při otevření modalu z úvodní stránky schováme gate, ať je modal vždy navrchu a klikatelný.
function _hideGate() { var g = document.getElementById('gateWall'); if (g) g.style.display = 'none'; }
function openProfile(view) {
    pendingPfView = view || 'portfolio';
    _hideGate();
    document.getElementById('profileModal').classList.add('open');
    renderProfileView();
}
function closeProfile() {
    document.getElementById('profileModal').classList.remove('open');
    refreshGate();
}
/* Který tier backtestu ukazovat: TOP když má vzorky, jinak Koupit.
   Bylo to na 4 místech zkopírované – jedna změna pravidla se musela dělat 4x. */
function btBest() {
    return btProof && (btProof.top && btProof.top.count ? btProof.top : btProof.buy);
}

function openAbout() {
    _hideGate();
    document.getElementById('aboutModal').classList.add('open');
    const el = document.getElementById('about-proof');
    if (!el) return;
    const bp = btBest();
    if (bp && bp.win_rate != null) {
        const alpha = (bp.alpha != null) ? ` a porážel index o <b style="color:var(--up)">+${bp.alpha} b.</b>` : '';
        el.innerHTML = `Náš nákupní signál měl na <b>${btProof.period || '10letém backtestu'}</b> (bez nahlížení do budoucna) <b style="color:var(--up)">${bp.win_rate}% úspěšnost</b>${alpha} na ~${btProof.horizon_days || 10}denním horizontu — napříč ${btProof.ticker_count || 'desítkami'} akciemi${btProof.sample_count ? ' a ' + btProof.sample_count + ' obchody' : ''}, <b>včetně propadu 2022</b>. K tomu vedeme veřejný <b>track record</b> reálných verdiktů. Výsledky ukazujeme otevřeně, i slabší období.`;
    } else {
        el.innerHTML = `Náš systém je ověřený <b>5letým backtestem</b> (bez nahlížení do budoucna, vč. propadu 2022) a doplněný veřejným <b>track recordem</b> reálných verdiktů. Konkrétní čísla najdeš přímo u analýz.`;
    }
}
function closeAbout() { document.getElementById('aboutModal').classList.remove('open'); refreshGate(); }
/* ---- RANNÍ PŘEHLED (web + archiv) -----------------------------------
   Stejná data jako ranní e-mail, jen se dají prolistovat zpětně.
   Zprávy jsou VEDLE pohybů jako kontext, ne jako jejich vysvětlení. */
let briefDates = [], briefIdx = 0;

function openBrief() {
    _hideGate();
    document.getElementById('briefModal').classList.add('open');
    loadBriefDates();
}
function closeBrief() {
    document.getElementById('briefModal').classList.remove('open');
    refreshGate();
}
function briefStep(delta) {
    const i = briefIdx + delta;
    if (i < 0 || i >= briefDates.length) return;
    briefIdx = i;
    loadBrief(briefDates[i]);
}
async function loadBriefDates() {
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/brief/dates`)).json();
        briefDates = (d && d.dates) || [];
    } catch (e) { briefDates = []; }
    briefIdx = 0;
    loadBrief(briefDates[0]);
}
async function loadBrief(date) {
    const body = document.getElementById('brief-body');
    body.innerHTML = '<div style="color:var(--text2);font-size:13px"><i class="ti ti-loader spin"></i> Načítám přehled…</div>';
    document.getElementById('brief-prev').disabled = briefIdx >= briefDates.length - 1;
    document.getElementById('brief-next').disabled = briefIdx <= 0;
    try {
        const url = `${BACKEND_URL}/api/brief?risk=${encodeURIComponent(investorRisk)}` + (date ? `&date=${encodeURIComponent(date)}` : '');
        const d = await (await fetch(url, { headers: authHeaders() })).json();
        renderBrief(d);
    } catch (e) {
        body.innerHTML = '<div style="color:var(--text2);font-size:13px">Přehled se teď nepodařilo načíst.</div>';
    }
}
function briefDateLabel(iso, isToday) {
    if (!iso) return '–';
    const p = iso.split('-');
    const dt = new Date(+p[0], +p[1] - 1, +p[2]);
    const txt = dt.toLocaleDateString('cs-CZ', { weekday: 'long', day: 'numeric', month: 'long' });
    return isToday ? `Dnes — ${txt}` : txt.charAt(0).toUpperCase() + txt.slice(1);
}
function renderBrief(d) {
    const body = document.getElementById('brief-body');
    document.getElementById('brief-date').textContent = briefDateLabel(d.date, d.is_today);
    if (!d.ok) {
        body.innerHTML = `<div style="color:var(--text2);font-size:13px">${escapeHtml(d.error || 'Pro tenhle den nemáme přehled.')}</div>`;
        return;
    }
    const m = d.market || {};
    let h = '';
    if (m.mood) h += `<p class="brief-mood">Trh je <b>${escapeHtml(m.mood)}</b>.</p>`;
    if ((m.macro || []).length) {
        // Barva podle REŽIMU, ne podle znaménka: rostoucí VIX nebo výnosy jsou
        // špatná zpráva, i když je změna kladná. Zelená u „VIX +8 %" by lhala.
        const regCol = {calm: 'var(--up)', stress: 'var(--down)', high: 'var(--amber)'};
        h += '<div class="brief-macro">' + m.macro.filter(x => x.price != null).map(x => {
            const c = regCol[x.regime] || 'var(--text2)';
            return `<span>${escapeHtml(x.label)} <b style="color:${c}">${x.price}${escapeHtml(x.unit || '')}</b>
                <span style="opacity:.75">${(x.chg || 0) >= 0 ? '+' : ''}${x.chg} %</span></span>`;
        }).join('') + '</div>';
    }
    if (m.best && m.worst) {
        h += `<p class="brief-sec">Nejvíc roste <b>${escapeHtml(m.best.label)}</b>
              <span style="color:var(--up)">${m.best.chg > 0 ? '+' : ''}${m.best.chg} %</span>,
              nejvíc klesá <b>${escapeHtml(m.worst.label)}</b>
              <span style="color:var(--down)">${m.worst.chg} %</span>.</p>`;
    }
    // Pořadí musí sedět s ranním e-mailem: trh → co se píše → TVÉ PORTFOLIO
    // (+ proč) → co dnes udělat → zbytek. Dřív tu portfolio chybělo úplně
    // a akce byla vražená mezi trh a zprávy.
    if (d.digest && d.digest.story && d.digest.story.what) {
        h += '<div class="brief-h">Co se dnes stalo a proč</div>' + dgStoryHTML(d.digest.story, true) +
             ((d.digest.events || []).length ? '<div class="brief-h">Další události</div>' + d.digest.events.map(e => dgEventHTML(e)).join('') : '');
    } else if ((m.news || []).length) {
        h += '<div class="brief-h">Co se k tomu píše</div><div class="brief-news">' +
             m.news.map(n => `<a href="${escapeHtml(n.link)}" target="_blank" rel="noopener">
                <span class="bn-title">${escapeHtml(n.title)}</span>
                ${n.blurb ? `<span class="bn-blurb">${escapeHtml(n.blurb)}</span>` : ''}
             </a>`).join('') +
             '</div>';
    }

    // Nahrané dlouhodobé portfolio (z plánu) – hodnota v Kč, změna, úkoly, zprávy k akciím
    if (typeof planLots !== 'undefined' && planLots.length && typeof planData !== 'undefined' && planData && planData.rows) {
        const rows = planData.rows, tot = planData.total_czk || 0;
        const day = rows.reduce((sm, r) => sm + (r.chg != null ? r.value_czk * r.chg / (100 + r.chg) : 0), 0);
        const base = tot - day, up = day >= 0, acts = (planData.plan.actions || []);
        const news = (tipNews && tipNews.ai && tipNews.items) ? tipNews.items.slice(0, 3) : [];
        if (!FEATURE_QTY) {
            const ev = planData.evaluation || {}, c = ev.counts || {};
            h += '<div class="brief-h">Tvoje akcie</div>' +
                `<div class="dg-story calm"><div class="dg-what">${rows.length} ${rows.length === 1 ? 'akcie' : (rows.length < 5 ? 'akcie' : 'akcií')} · silné ${c.strong || 0} · drž ${c.hold || 0} · slabší ${c.weak || 0}</div>
                  <div class="dg-why">${mpEsc(ev.headline || '')}</div></div>` +
                news.map(it => `<div class="mp-news ${{ 'pozitivní': 'up', 'negativní': 'down' }[it.impact] || 'mix'}"><b>${mpEsc(mpBase(it.ticker))}</b> – ${mpEsc(it.what)}</div>`).join('');
        } else
        h += '<div class="brief-h">Tvoje portfolio</div>' +
            `<div class="dg-story ${up ? 'calm' : 'mixed'}"><div class="dg-what">${mpKc(tot)}
                <span style="font-size:14px;font-weight:600;color:var(--${up ? 'up' : 'down'})">${up ? '+' : '−'}${mpKc(Math.abs(day))} (${up ? '+' : '−'}${mpPct(Math.abs(base ? day / base * 100 : 0), 2)}) za poslední obchodní den</span></div>
                <div class="dg-why">${acts.length ? acts.map(a => `${a.type === 'trim' ? 'Oříznout' : 'Prodat'} <b>${mpEsc(mpBase(a.ticker))}</b> za ≈ ${mpKc(a.amount_czk)}`).join(' · ') : 'Dnes nemusíš nic dělat – plán drží.'}</div></div>` +
            news.map(it => `<div class="mp-news ${{ 'pozitivní': 'up', 'negativní': 'down' }[it.impact] || 'mix'}"><b>${mpEsc(mpBase(it.ticker))}</b> – ${mpEsc(it.what)}</div>`).join('');
    }

    const pos = d.positions;
    if (pos) {
        h += '<div class="brief-h">' + ((typeof planLots !== 'undefined' && planLots.length) ? 'Krátkodobé signály – otevřené pozice' : 'Tvé portfolio') + '</div>';
        const items = pos.items || [];
        if (items.length) {
            const tag = {target:'Cíl', stop:'Stop', expired:'Horizont', open:'Drží se'};
            h += items.slice(0, 6).map(i => {
                const col = (i.return_pct || 0) >= 0 ? 'var(--up)' : 'var(--down)';
                return `<div class="pos-row">
                    <span class="pos-t">${escapeHtml(i.ticker)}</span>
                    <span class="pos-tag ${i.status}">${tag[i.status] || i.status}</span>
                    <span class="pos-lbl">${i.days_held} dní</span>
                    <span class="pos-ret" style="color:${col}">${(i.return_pct||0) >= 0 ? '+' : ''}${i.return_pct}%</span>
                </div>`;
            }).join('');
            h += allocHTML(pos.allocation, false);
        } else {
            h += '<div class="alloc-note" style="margin:2px 0 6px">Žádná otevřená pozice — kapitál patří do indexového jádra.</div>';
        }
        if ((d.why || []).length) {
            h += '<div style="color:var(--text2);font-size:12px;margin:12px 0 4px">Proč se to hýbe:</div>' +
                 d.why.map(w => `<div style="padding:5px 0;font-size:13px">
                    <b>${escapeHtml(w.ticker)}</b> · <span style="color:${w.severity === 'warn' ? 'var(--amber)' : 'var(--text2)'}">${escapeHtml(w.title || '')}</span>
                    <div style="color:var(--text2);font-size:12px">${escapeHtml(w.hint || '')}</div></div>`).join('');
        }
    }

    // s nahraným portfoliem rozhoduje dlouhodobý plán (viz výše), ne krátkodobý signál
    if (d.action && !(typeof planLots !== 'undefined' && planLots.length)) h += `<div class="brief-act">${escapeHtml(d.action)}</div>`;
    if ((d.signals || []).length) {
        h += '<div class="brief-h">Signály toho dne</div>' + d.signals.map(sg =>
            `<div class="brief-sig"><b>${escapeHtml(sg.ticker || '')}</b>
             <span style="color:var(--text2);flex:1">${escapeHtml(sg.name || '')}</span>
             <span style="color:var(--up)">skóre ${sg.score != null ? sg.score : '–'}</span></div>`).join('');
    }
    if (!d.is_today) {
        h += '<div class="alloc-note" style="margin-top:14px">Archivní přehled — pozice a doporučená akce se ukazují jen u dnešního dne.</div>';
    }
    body.innerHTML = h || '<div style="color:var(--text2);font-size:13px">Pro tenhle den nemáme data.</div>';
}

function openNews() { _hideGate(); document.getElementById('newsModal').classList.add('open'); loadMarketNews(); }
function closeNews() { document.getElementById('newsModal').classList.remove('open'); refreshGate(); }
