// ====================== DENNÍ BRIEFING („Co se děje") ======================
// Ne seznam odkazů, ale: co se stalo → proč je to důležité → koho se to týká
// → co s tím. „Co s tím" neříká AI – plyne z dlouhodobého plánu (zprávy ho nemění).
let digestData = null, digestTs = 0, digestMine = null, digestMineKey = '';
const DG_IMPACT = { 'pozitivní': ['up', 'Pozitivní'], 'negativní': ['down', 'Negativní'], 'smíšené': ['mix', 'Smíšené'] };

async function loadDigest(force) {
    if (!force && digestData && Date.now() - digestTs < 10 * 60 * 1000) return digestData;
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/news/digest`)).json();
        if (d.ok) { digestData = d; digestTs = Date.now(); }
    } catch (e) {}
    return digestData;
}

// Moje pozice pro briefing: titul, sektor (česky), váha. Jádro = „celý trh".
function dgMyHoldings() {
    const rows = (planData && planData.rows) || [];
    return rows.map(r => ({
        ticker: r.ticker, base: mpBase(r.ticker).replace('-', '.'), name: r.name, weight: r.weight,
        status: r.status, sector: r.status === 'core' ? 'celý trh' : (MP_SECTOR_CZ[r.sector] || ''),
    }));
}

async function loadDigestMine() {
    const mine = dgMyHoldings().filter(h => h.status !== 'core').sort((a, b) => b.weight - a.weight).slice(0, 8);
    if (!mine.length) { digestMine = null; return null; }
    const key = mine.map(h => h.ticker).join(',');
    if (digestMine && digestMineKey === key) return digestMine;
    try {
        const names = {}; mine.forEach(h => { names[h.ticker] = h.name; });
        const d = await (await fetch(`${BACKEND_URL}/api/news/mine`, { method: 'POST', headers: authHeaders(),
            body: JSON.stringify({ tickers: mine.map(h => h.ticker), names }) })).json();
        if (d.ok) { digestMine = d; digestMineKey = key; }
    } catch (e) {}
    return digestMine;
}

// Komu z mého portfolia se událost týká + co s tím (z plánu, ne z AI)
function dgForMe(ev) {
    const my = dgMyHoldings();
    if (!my.length) return null;
    const tick = new Set((ev.tickers || []).map(t => t.toUpperCase().replace('-', '.')));
    const secs = new Set(ev.sectors || []);
    const hit = my.filter(h => tick.has(h.base.toUpperCase()) || (h.sector && secs.has(h.sector)));
    if (!hit.length) return { hit: [], text: 'Tvého portfolia se to přímo netýká.' };
    const w = hit.reduce((s, h) => s + (h.weight || 0), 0);
    const names = hit.filter(h => h.status !== 'core').map(h => mpBase(h.ticker));
    const core = hit.some(h => h.status === 'core');
    let who = [];
    if (names.length) who.push(names.slice(0, 5).join(', ') + (names.length > 5 ? ' a další' : ''));
    if (core) who.push('tvé indexové jádro');
    let act;
    if (ev.impact === 'negativní') {
        act = hit.some(h => h.status === 'plan' || h.status === 'core')
            ? (FEATURE_QTY ? 'Nic neprodávej – plán se kvůli zprávám nemění. Kdyby cena spadla, další měsíční vklad nakoupí levněji.' : 'Nic neprodávej – hodnocení se kvůli jedné zprávě nemění. Kdyby cena spadla, další vklad nakoupí levněji.')
            : (FEATURE_QTY ? 'Nic neprodávej v panice. Tyhle tituly stejně nedokupuješ; o prodeji rozhoduje jen pravidlo po 3 letech.' : 'Nic neprodávej v panice. Tyhle tituly stejně nedokupuješ; jeden titulek hodnocení nemění.');
    } else if (ev.impact === 'pozitivní') {
        act = FEATURE_QTY ? 'Nic nedokupuj navíc kvůli titulku – vklady jdou dál podle plánu tam, kde chybí do cíle.' : 'Nic nedokupuj navíc kvůli titulku – nové peníze jdou dál do koše tipů.';
    } else {
        act = 'Nic dělat nemusíš – sleduje se to, ale plán to nemění.';
    }
    return { hit, text: `Týká se: <b>${mpEsc(who.join(' + '))}</b>${w > 0 ? ` (≈ ${mpPct(w, 0)} portfolia)` : ''}. ${act}` };
}


function dgStoryHTML(story, withDrivers) {
    if (!story || !story.what) return '';
    const lvl = story.level || 'mixed';
    const drv = (story.drivers || []);
    let h = `<div class="dg-story ${lvl}">
        <div class="dg-mood"><span class="dot"></span> Trh je dnes ${mpEsc(story.label || '')}</div>
        <div class="dg-what">${mpEsc(story.what)}</div>
        ${story.why ? `<div class="dg-why">${mpEsc(story.why)}</div>` : ''}`;
    if (!story.ai && story.note) {
        h += `<div class="dg-quote"><b>Co k tomu píšou zdroje:</b> „${mpEsc(story.note.title)}“ – ${mpEsc(story.note.text)}
            <a href="${mpEsc(story.note.link)}" target="_blank" rel="noopener noreferrer" style="color:var(--text3)">(${mpEsc(story.note.src)})</a></div>`;
    }
    h += `</div>`;
    if (withDrivers && drv.length) {
        h += `<div class="dg-h"><i class="ti ti-flame"></i> Co dnes trh hýbe</div>` + drv.map(d => {
            const src = (d.sources || []).filter((x, i, a) => a.findIndex(y => y.src === x.src) === i)
                .map(x => `<a href="${mpEsc(x.link)}" target="_blank" rel="noopener noreferrer">${mpEsc(x.src)}</a>`).join(' · ');
            return `<div class="dg-drv"><b>${mpEsc(d.title)}</b>${mpEsc(d.text)}${src ? `<div class="s">Zdroj: ${src}</div>` : ''}</div>`;
        }).join('');
    }
    return h;
}

function dgFactsHTML(mk) {
    if (!mk || !(mk.facts || []).length) return '';
    const badUp = { '^VIX': 1, '^TNX': 1 };   // u strachu a výnosů je růst spíš špatná zpráva
    return `<div class="dg-facts">` + mk.facts.map(f => {
        const good = badUp[f.key] ? f.chg <= 0 : f.chg >= 0;
        const col = Math.abs(f.chg) < 0.05 ? 'var(--text2)' : (good ? 'var(--up)' : 'var(--down)');
        return `<div class="dg-fact"><div class="l">${mpEsc(f.label)}</div><div class="v">${Number(f.price).toLocaleString('cs-CZ', { maximumFractionDigits: 2 })}${mpEsc(f.unit || '')}</div>
            <div class="c" style="color:${col}">${f.chg >= 0 ? '+' : '−'}${mpPct(Math.abs(f.chg), 2)}</div></div>`;
    }).join('') + `</div>` + (mk.best && mk.worst ? `<div class="dg-sec">Nejvíc roste <b>${mpEsc(mk.best.label)}</b> <span style="color:var(--up)">+${mpPct(mk.best.chg, 1)}</span> · nejvíc klesá <b>${mpEsc(mk.worst.label)}</b> <span style="color:var(--down)">−${mpPct(Math.abs(mk.worst.chg), 1)}</span></div>` : '');
}

function dgEventHTML(ev, compact) {
    const im = DG_IMPACT[ev.impact] || DG_IMPACT['smíšené'];
    const me = compact ? null : dgForMe(ev);
    const myBases = new Set(((me && me.hit) || []).map(h => h.base.toUpperCase()));
    const mySecs = new Set(((me && me.hit) || []).map(h => h.sector));
    const chips = (ev.sectors || []).map(s => `<span class="dg-chip ${mySecs.has(s) ? 'mine' : ''}">${mpEsc(s)}</span>`).join('') +
        (ev.tickers || []).map(t => `<span class="dg-chip t ${myBases.has(t.toUpperCase().replace('-', '.')) ? 'mine' : ''}">${mpEsc(t)}</span>`).join('');
    if (compact) {
        return `<div class="dg-mini" onclick="openNews()"><span class="dg-dot ${im[0]}"></span><div><b>${mpEsc(ev.title)}</b><div class="w">${mpEsc(ev.why || ev.what)}</div></div></div>`;
    }
    const src = (ev.sources || []).filter((s, i, a) => a.findIndex(x => x.src === s.src) === i).map(s => `<a href="${mpEsc(s.link)}" target="_blank" rel="noopener noreferrer">${mpEsc(s.src)}</a>`).join(' · ');
    return `<div class="dg-ev">
      <div class="dg-ev-h"><span class="dg-imp ${im[0]}">${im[1]}</span><b>${mpEsc(ev.title)}</b></div>
      <div class="dg-row"><span class="k">Co se stalo</span><span>${mpEsc(ev.what)}</span></div>
      ${ev.why ? `<div class="dg-row"><span class="k">Proč je to důležité</span><span>${mpEsc(ev.why)}</span></div>` : ''}
      ${chips ? `<div class="dg-row"><span class="k">Koho se to týká</span><span class="dg-chips">${chips}</span></div>` : ''}
      ${me ? `<div class="dg-row me"><span class="k">Pro tebe</span><span>${me.text}</span></div>` : ''}
      ${src ? `<div class="dg-src">Zdroj: ${src}</div>` : ''}
    </div>`;
}

function dgMineHTML() {
    const m = digestMine;
    if (!m) return '';
    let body = '';
    if (m.ai && (m.items || []).length) {
        body = m.items.map(it => {
            const im = DG_IMPACT[it.impact] || DG_IMPACT['smíšené'];
            const src = (it.sources || []).map(s => `<a href="${mpEsc(s.link)}" target="_blank" rel="noopener noreferrer">${mpEsc(s.src)}</a>`).join(' · ');
            return `<div class="dg-ev"><div class="dg-ev-h"><span class="dg-imp ${im[0]}">${im[1]}</span><b>${mpEsc(mpBase(it.ticker))}</b></div>
              <div class="dg-row"><span class="k">Co se stalo</span><span>${mpEsc(it.what)}</span></div>
              ${it.why ? `<div class="dg-row"><span class="k">Proč je to důležité</span><span>${mpEsc(it.why)}</span></div>` : ''}
              ${src ? `<div class="dg-src">Zdroj: ${src}</div>` : ''}</div>`;
        }).join('');
    } else if (m.ai) {
        body = `<div class="dg-note">U tvých akcií za poslední 3 dny nic podstatného – žádné výsledky, akvizice ani velké zprávy.</div>`;
    } else if ((m.headlines || []).length) {
        // Titulky z Yahoo jsou anglicky – bez českého AI shrnutí je neukazujeme, jen které akcie mají nové zprávy.
        const cnt = {}; m.headlines.forEach(x => { const b = mpBase(x.ticker); cnt[b] = (cnt[b] || 0) + 1; });
        body = `<div class="dg-note warn"><i class="ti ti-info-circle"></i> České shrnutí zpráv se teď nepodařilo vytvořit (bezplatná AI má limit). Nové zprávy jsou u: ${Object.entries(cnt).map(([k, v]) => `<b>${mpEsc(k)}</b> (${v})`).join(', ')}. Jsou anglicky, proto je tu neukazuju – <a style="color:var(--accent);cursor:pointer" onclick="dgRetryMine()">zkusit znovu</a>.</div>`;
    } else {
        body = `<div class="dg-note">U tvých akcií za poslední 3 dny žádné zprávy.</div>`;
    }
    return `<div class="dg-h"><i class="ti ti-briefcase"></i> Tvoje akcie ve zprávách</div>${body}`;
}

async function dgRetryMine() {
    digestMine = null; digestMineKey = '';
    const list = document.getElementById('market-news-list');
    if (list) list.insertAdjacentHTML('afterbegin', '<div class="dg-note"><i class="ti ti-loader spin"></i> Zkouším znovu vytvořit české shrnutí…</div>');
    await loadDigestMine();
    renderDigestModal();
}

function renderDigestModal() {
    const list = document.getElementById('market-news-list');
    if (!list) return;
    const d = digestData;
    if (!d) { list.innerHTML = '<div class="dg-note">Briefing se teď nepodařilo načíst.</div>'; return; }
    let h = dgStoryHTML(d.story, true);
    h += dgFactsHTML(d.market);
    if (d.ai && (d.events || []).length) {
        h += `<div class="dg-h"><i class="ti ti-bolt"></i> Další důležité události</div>` + d.events.map(e => dgEventHTML(e)).join('');
    }
    if (!d.ai && d.story && d.story.what) {
        const why = { no_key: 'AI shrnutí není zapnuté (chybí klíč ANALYSIS_API_KEY).', llm_failed: 'AI shrnutí se teď nepodařilo získat – zkusíme to znovu za pár minut.',
                      no_valid: 'AI shrnutí neprošlo kontrolou zdrojů – zobrazuji jen ověřená čísla a témata.', skipped: '' }[d.ai_status] || '';
        h += `<div class="dg-note warn"><i class="ti ti-info-circle"></i> Příčiny jsou odvozené z nejčastějších témat ve zprávách.${why ? ' ' + why : ''}</div>`;
    } else if (!d.ai) {
        h += `<div class="dg-note warn"><i class="ti ti-info-circle"></i> AI shrnutí teď není k dispozici – níže jsou nejdůležitější titulky s perexem.</div>` +
            (d.headlines || []).slice(0, 8).map(n => `<a class="dg-hl" href="${mpEsc(n.link)}" target="_blank" rel="noopener noreferrer"><b>${mpEsc(n.title)}</b>${n.text ? `<span>${mpEsc(n.text)}</span>` : ''}<small>${mpEsc(n.src)}</small></a>`).join('');
    }
    h += dgMineHTML();
    h += `<div class="dg-lt"><i class="ti ti-compass"></i><div><b>Pro dlouhodobý plán:</b> ${d.long_term ? mpEsc(d.long_term) + ' ' : ''}Zprávy jsou kontext, ne signál. Plán nakupuje jednou měsíčně podle hodnoty a momenta – kvůli dnešním titulkům nic neměň.</div></div>`;
    if ((d.headlines || []).length) {
        h += `<details class="dg-all"><summary>Všechny titulky (${d.headlines.length})</summary>` +
            d.headlines.map(n => `<a class="dg-hl" href="${mpEsc(n.link)}" target="_blank" rel="noopener noreferrer"><b>${mpEsc(n.title)}</b><small>${mpEsc(n.src)}</small></a>`).join('') + `</details>`;
    }
    if (d.generated) h += `<div class="dg-upd">Aktualizováno ${new Date(d.generated * 1000).toLocaleTimeString('cs-CZ', { hour: '2-digit', minute: '2-digit' })} · obnovuje se každé 3 hodiny</div>`;
    list.innerHTML = h;
}

async function loadMarketNews() {
    const st = document.getElementById('market-status');
    const list = document.getElementById('market-news-list');
    list.innerHTML = '<div class="dg-note"><i class="ti ti-loader spin"></i> Připravuji briefing – čtu zprávy a počítám, koho se týkají…</div>';
    // stav burzy (rychlý, nezávislý)
    fetch(`${BACKEND_URL}/api/market-news`).then(r => r.json()).then(d => {
        if (d.market && st) {
            st.className = 'market-status ' + (d.market.open ? 'open' : 'closed');
            st.innerHTML = `<i class="ti ti-${d.market.open ? 'point-filled' : 'moon'}"></i> ${escapeHtml(d.market.label)}`;
        }
    }).catch(() => {});
    if (planLots.length && !planData && !planLoading) { try { await planRefresh(); } catch (e) {} }
    await Promise.all([loadDigest(), loadDigestMine()]);
    renderDigestModal();
}

async function fetchOverviewNews() {
    const box = document.getElementById('ov-news');
    if (!box) return;
    const d = await loadDigest();
    const st = d && d.story;
    if (st && st.what) {
        const drv = (st.drivers || []).slice(0, 3).map(x => `<span class="dg-chip">${mpEsc(x.title)}</span>`).join('');
        box.innerHTML = `<div class="dg-ov-mood ${st.level || 'mixed'}"><span class="dot"></span> Trh je dnes ${mpEsc(st.label || '')}</div>
            <div class="dg-headline sm" style="margin:0 0 8px">${mpEsc(st.what)}</div>
            ${st.why ? `<div style="font-size:12.5px;color:var(--text2);line-height:1.5">${mpEsc(st.why)}</div>` : ''}
            ${drv ? `<div class="dg-chips2">${drv}</div>` : ''}
            <button class="tool-btn" onclick="openNews()" style="width:100%; justify-content:center; margin-top:10px;"><i class="ti ti-news"></i> Proč? – celý briefing</button>`;
        return;
    }
    const list = (d && d.headlines) || [];
    if (!list.length) { box.innerHTML = '<div class="ov-empty">Novinky teď nelze načíst.</div>'; return; }
    box.innerHTML = list.slice(0, 5).map(n => `<div class="ov-news-row" onclick="window.open('${String(n.link || '').replace(/'/g, "\\'")}', '_blank', 'noopener')">
        <div class="meta">${n.ts ? formatNewsTime(n.ts) : ''}</div><div class="txt">${escapeHtml(n.title)}</div></div>`).join('') +
        `<button class="tool-btn" onclick="openNews()" style="width:100%; justify-content:center; margin-top:8px;"><i class="ti ti-news"></i> Briefing</button>`;
}

// Patička až POD detailem akcie (dřív byla v DOM před ním → po otevření akcie
// se nejdřív scrollovalo přes celou patičku, na telefonu skoro dvě obrazovky).
(function () {
    const f = document.querySelector('.app-footer'), d = document.getElementById('detail-view');
    if (f && d && f.parentNode === d.parentNode) d.parentNode.appendChild(f);
})();

// ---- Telefon: zamčená stránka pod oknem, spodní lišta mizí při psaní, odložené překreslení ----
(function () {
    const isField = el => el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) && !/^(checkbox|radio|button|submit|file)$/.test(el.type || '');
    document.addEventListener('focusin', e => { if (isField(e.target)) document.body.classList.add('kb-open'); });
    document.addEventListener('focusout', () => {
        setTimeout(() => {
            if (!isField(document.activeElement)) document.body.classList.remove('kb-open');
            if (planPending && !planTyping()) renderPlan();
        }, 150);
    });
    setInterval(() => { if (planPending && !planTyping()) renderPlan(); }, 1000);   // pojistka, kdyby focusout nepřišel
    const syncModal = () => document.body.classList.toggle('modal-open', !!document.querySelector('.pl-modal.open'));
    new MutationObserver(syncModal).observe(document.body, { subtree: true, attributes: true, attributeFilter: ['class'] });
    syncModal();
})();

// ---------- hodnocení portfolia a tipy (režim „Jen akcie") ----------
function planEvalHTML(d) {
    const ev = d.evaluation;
    if (!ev || !ev.items.length) return '';
    const by = lvl => ev.items.filter(e => e.level === lvl).map(e => mpEsc(mpBase(e.ticker)));
    const warn = ev.items.filter(e => (e.warn || []).length);
    const line = (cls, label, list) => list.length ? `<div class="mp-ev-line"><span class="mp-verdict ${cls}">${label}</span> <b>${list.join(', ')}</b></div>` : '';
    return `<div class="mp-card"><h4><i class="ti ti-report-analytics"></i> Hodnocení mého portfolia</h4>
      <div class="dg-headline sm" style="margin:6px 0 12px">${mpEsc(ev.headline)}</div>
      ${line('strong', 'Silné', by('strong'))}${line('hold', 'Drž', by('hold'))}${line('weak', 'Slabší – nedokupuj', by('weak'))}
      ${warn.length ? warn.map(e => `<div class="mp-flag" style="margin-top:6px">⚠ <b>${mpEsc(mpBase(e.ticker))}</b>: ${mpEsc(e.warn.join(' '))}</div>`).join('') : ''}
      ${planSectorsHTML(d)}
      <div class="mp-note"><i class="ti ti-info-circle"></i><span>Hodnocení = pořadí akcie mezi ~500 firmami S&amp;P 500 podle kombinace „levná podle zisků a cash flow" a „roste". <b>Prodávat podle něj nedoporučuji</b> – na historii slabší akcie spolehlivě nezaostávaly. Spolehlivé je jen tohle: nové peníze dávat do silných akcií a tipů, ne do slabých.</span></div>
    </div>`;
}

function planSetPicks(n) {
    planPicksN = n === 6 ? 6 : 10;
    try { localStorage.setItem('ma_picks_n', String(planPicksN)); } catch (e) {}
    // Hned přepni tlačítko a oříznutý koš (prvních n tipů je totéž, co server pošle); přesná čísla doběhnou po odpovědi serveru.
    if (planData && planData.picks) {
        const pk = planData.picks;
        planData.picks = Object.assign({}, pk, { n: planPicksN, loading: true, basket: (pk.basket || []).slice(0, planPicksN) });
    }
    if (planLots.length) planRefresh(); else renderPlan();
    if (document.getElementById('opp-picks-root')) loadOppPicks();
    if (typeof fetchOverviewSignal === 'function') { fetchOverviewSignal(); fetchOverviewOpps(); }
}

// ---------- koš tipů: jediný seznam „co koupit“ (Příležitosti, Přehled, Moje portfolio, e-maily) ----------
let picksCache = {};
async function fetchPicksData(n, force) {
    n = n === 6 ? 6 : 10;
    const c = picksCache[n];
    if (c && !force && Date.now() - c.ts < 5 * 60 * 1000) return c.d;
    const d = await (await fetch(`${BACKEND_URL}/api/picks?n=${n}`)).json();
    if (d && d.ok && d.picks) picksCache[n] = { ts: Date.now(), d };
    return d;
}
function picksHeldRows() { return [...new Set((typeof planLots !== 'undefined' ? planLots : []).map(l => l.ticker))].map(t => ({ ticker: t })); }

async function loadOppPicks(force) {
    const root = document.getElementById('opp-picks-root');
    if (!root) return;
    if (!hasFeature('pro')) {
        root.innerHTML = lockedCardHTML('Co koupit teď — koš tipů', 'pro', 'Měsíční koš nejsilnějších akcií S&amp;P 500, ověřený na historii.');
        return;
    }
    if (!picksCache[planPicksN] || force) root.innerHTML = '<div class="opp-loading"><i class="ti ti-loader spin"></i> Načítám koš tipů…</div>';
    try {
        const d = await fetchPicksData(planPicksN, force);
        if (!d || !d.ok || !d.picks) {
            root.innerHTML = `<div class="mp-card"><div class="mp-sub">${d && d.scanning ? 'Ranní sken ještě běží – koš tipů bude za chvíli.' : 'Koš tipů se nepodařilo načíst.'} <a style="color:var(--accent);cursor:pointer" onclick="loadOppPicks(true)">Zkusit znovu</a></div></div>`;
            return;
        }
        const dd = Object.assign({ rows: picksHeldRows() }, d);
        root.innerHTML = planPicksHTML(dd, '-opp') + picksMoreHTML(dd);
    } catch (e) {
        root.innerHTML = '<div class="mp-card"><div class="mp-sub">Koš tipů se nepodařilo načíst. <a style="color:var(--accent);cursor:pointer" onclick="loadOppPicks(true)">Zkusit znovu</a></div></div>';
    }
}

// Další kandidáti za košem (pořadí 11+ v tomto měsíci) – jako informace, ne jako další nákupní seznam
function picksMoreHTML(d) {
    const more = d.more || [];
    if (!more.length) return '';
    const held = new Set((d.rows || []).map(r => String(r.ticker).replace('.', '-')));
    const rows = more.map(p => `<div class="mp-more-row" onclick="loadStockDetail('${mpEsc(p.ticker)}')">
        <div><b>${mpEsc(mpBase(p.ticker))}</b>${held.has(p.ticker) ? ' <span class="mp-pick-src own">Už máš</span>' : ''} <span class="mp-more-n">${mpEsc(p.name)}</span></div>
        <div class="mp-more-w">${mpEsc(p.why)}${mpEarnHTML(d.earnings && d.earnings[p.ticker])}${mpShortHTML(d.short && d.short[p.ticker])}</div>
      </div>`).join('');
    return `<div class="mp-card"><h4><i class="ti ti-list-details"></i> Další kandidáti za košem</h4>
      <div class="mp-sub">Akcie hned za hranicí koše (stejné pořadí, jen se do koše nevešly). <b>Nejsou to další doporučení k nákupu</b> – slouží k porovnání, nebo jako náhrada, kdyby tip z koše vypadl kvůli vážné zprávě.</div>
      <div class="mp-more">${rows}</div></div>`;
}

function planPicksHTML(d, sfx) {
    sfx = sfx || '';
    const pk = d.picks;
    if (!pk || !(pk.basket || []).length) return '';
    const st = pk.stats || {}, tr = st.train || {}, ho = st.holdout || {};
    const tog = n => `<button class="risk-pill ${pk.n === n ? 'active' : ''}" onclick="planSetPicks(${n})">${n} tipů</button>`;
    const heldT = new Set((d.rows || []).map(r => String(r.ticker).replace('.', '-')));
    const nHeld = pk.basket.filter(p => heldT.has(p.ticker)).length;
    const cards = pk.basket.map(p => `<div class="mp-pick${heldT.has(p.ticker) ? ' held' : ''}" onclick="loadStockDetail('${mpEsc(p.ticker)}')">
        <div class="mp-pick-h"><b>${mpEsc(mpBase(p.ticker))}</b>${heldT.has(p.ticker) ? '<span class="mp-pick-src own">Už máš</span>' : ''}<span class="mp-pick-src ${p.source}">${p.source === 'vm' ? 'Levná + roste' : 'Silné momentum'}</span></div>
        <div class="mp-pick-n">${mpEsc(p.name)}${p.sector ? ' · ' + mpEsc(MP_SECTOR_CZ[p.sector] || p.sector) : ''}</div>
        <div class="mp-pick-w">${mpEsc(p.why)}</div>
        ${p.price ? `<div class="mp-pick-p">${mpQty(p.price)} ${mpEsc(p.currency || '')}${p.chg != null ? ` <span style="color:var(--${p.chg >= 0 ? 'up' : 'down'})">${p.chg >= 0 ? '+' : ''}${mpPct(p.chg, 1)}</span>` : ''}</div>` : ''}
        ${mpEarnHTML(d.earnings && d.earnings[p.ticker])}${mpShortHTML(d.short && d.short[p.ticker])}
        ${heldT.has(p.ticker) ? '' : planTipNewsHTML(p.ticker, 'buy')}
      </div>`).join('');
    return `<div class="mp-card" id="mp-picks${sfx}"><h4><i class="ti ti-target-arrow"></i> Co koupit teď</h4>
      <div class="mp-sub">${pk.month ? `Koš na <b>${MP_MONTH_CZ[parseInt(pk.month.slice(5, 7), 10) - 1]}</b> (sestaven ${mpDate(pk.since)}) – ` : ''}${pk.n} nejsilnějších akcií z S&amp;P 500: půlka je <b>levná a zároveň roste</b> (hodnota + momentum), půlka má <b>nejsilnější momentum</b>. Mění se jednou měsíčně, mezitím zůstává stejný. ${nHeld ? `${pk.basket.filter(p => heldT.has(p.ticker)).map(p => '<b>' + mpEsc(mpBase(p.ticker)) + '</b>').join(', ')} už máš – peníze ${nHeld > 1 ? 'dostanou' : 'dostane'}, jen když ${nHeld > 1 ? 'jich' : 'jí'} máš méně než ostatních. ` : ''}Klikni na akcii pro detail.</div>
      <div class="mp-riskrow" style="margin:0 0 12px">${tog(10)}${tog(6)}<span class="mp-pill-sep">${pk.loading ? '<i class="ti ti-loader spin"></i> přepočítávám…' : (pk.n === 6 ? 'koncentrovanější, víc kolísá' : 'doporučeno – stabilnější')}</span></div>
      ${d.basket_perf ? `<div class="mp-bperf"><i class="ti ti-activity"></i><span>Živý záznam koše od ${mpDate(d.basket_perf.since)}: <b style="color:var(--${d.basket_perf.ret >= d.basket_perf.spy ? 'up' : 'amber'})">${mpSigned(d.basket_perf.ret)}</b> · S&amp;P 500 ${mpSigned(d.basket_perf.spy)}. Krátké období je hlavně náhoda – smysl to dává po měsících.</span></div>` : ''}
      <div class="mp-picks">${cards}</div>
      <div class="mp-acc"><i class="ti ti-chart-dots"></i><div><b>Jak přesné to je (ověřeno na historii S&amp;P 500):</b> koš ${pk.n} tipů porazil index za 12 měsíců v <b>${tr.beat_pct} %</b> případů v letech 2015–2022 (průměrně o +${mpPct(tr.excess, 1)}) a v <b>${ho.beat_pct} %</b> v letech 2023–2026, na kterých se nic neladilo (+${mpPct(ho.excess, 1)}). Jednotlivý tip ale porazí index jen asi v ${ho.tip_pct} % – <b>kupuj celý koš, ne jednu akcii</b>. Žádný model nezaručí výsledek a akcie s nejsilnějším momentem umí i prudce spadnout.</div></div>
      ${!pk.mix ? '<div class="mp-note"><i class="ti ti-info-circle"></i><span>Dnes jsou tipy jen podle hodnoty + momenta – pořadí podle momenta doplní ranní sken.</span></div>' : ''}
      ${(pk.paused || []).length ? `<div class="mp-note warn"><i class="ti ti-player-pause"></i><span>Vynecháno kvůli vážné zprávě: ${pk.paused.map(x => '<b>' + mpEsc(mpBase(x.ticker)) + '</b>').join(', ')}.</span></div>` : ''}
    </div>`;
}
