// ====================== ADMIN PANEL ======================
function fmtDate(ts) {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleDateString('cs-CZ');
}

async function loadAdmin() {
    const el = document.getElementById('pfview-admin');
    el.innerHTML = '<div style="color:var(--accent); padding:14px;"><i class="ti ti-loader spin"></i> Načítám admin data...</div>';
    try {
        const [usersR, invR] = await Promise.all([
            fetch(`${BACKEND_URL}/api/admin/users`, { headers: authHeaders() }).then(r => r.json()),
            fetch(`${BACKEND_URL}/api/admin/invites`, { headers: authHeaders() }).then(r => r.json())
        ]);
        if (!usersR.ok) { el.innerHTML = `<div style="color:var(--down); padding:14px;">${usersR.error || 'Chyba'}</div>`; return; }
        renderAdmin(usersR.users || [], invR.invites || []);
        loadEmailStatus();
    } catch (e) {
        el.innerHTML = '<div style="color:var(--down); padding:14px;">Chyba načítání admin dat.</div>';
    }
}

function renderAdmin(users, invites) {
    const planOpts = (cur) => ['none', 'trial', 'start', 'pro', 'elite', 'comped']
        .map(p => `<option value="${p}" ${p === cur ? 'selected' : ''}>${p}</option>`).join('');

    const rows = users.map(u => `
        <tr>
            <td><b>${u.username}</b>${u.is_admin ? ' <span style="color:var(--accent)">★</span>' : ''}</td>
            <td><span class="sub-badge ${u.effective}">${PLAN_NAMES[u.effective] || u.effective}</span></td>
            <td>${u.plan}${u.plan === 'trial' && u.days_left != null ? ` (${u.days_left}d)` : ''}</td>
            <td>${fmtDate(u.created)}</td>
            <td><select onchange="adminSetPlan('${u.username}', this.value)">${planOpts(u.plan)}</select></td>
            <td>${u.is_admin ? '' : `<button class="tool-btn" style="padding:5px 9px;" title="Smazat účet" onclick="adminDeleteUser('${u.username}')"><i class="ti ti-trash"></i></button>`}</td>
        </tr>`).join('');

    const comped = invites.filter(i => i.type === 'comped');
    const promos = invites.filter(i => i.type === 'promo');

    const compedChips = comped.length ? comped.map(i =>
        `<div class="invite-chip ${i.used_by ? 'used' : ''}">
            <span>${i.code}</span>
            ${i.used_by ? `<span style="font-size:11px;color:var(--text2)">${i.used_by}</span>`
                       : `<i class="ti ti-copy copy" title="Kopírovat" onclick="copyText('${i.code}')"></i>`}
            <i class="ti ti-trash" title="Smazat kód" style="cursor:pointer;color:var(--down)" onclick="deleteInvite('${i.code}')"></i>
        </div>`).join('') : '<span style="color:var(--text2); font-size:13px;">Žádné.</span>';

    const promoBlocks = promos.length ? promos.map(i => {
        const refs = (i.referrals || []).map(r =>
            `<div style="display:flex; justify-content:space-between; font-size:12px; padding:3px 0; border-top:1px solid var(--border);">
                <span>${r.email}</span><span class="sub-badge ${r.plan}">${PLAN_NAMES[r.plan] || r.plan}</span></div>`).join('');
        return `<div class="promo-block">
            <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
                <b style="font-family:monospace; font-size:14px;">${i.code}</b>
                <i class="ti ti-copy copy" onclick="copyText('${i.code}')" style="cursor:pointer; color:var(--accent)"></i>
                <span style="font-size:11px; color:var(--text2);">7 dní Pro${i.discount_pct ? ` + ${i.discount_pct}% sleva` : ''}</span>
                <span class="promo-count">${i.referral_count} pozvaných</span>
                <i class="ti ti-trash" title="Smazat kód" style="cursor:pointer;color:var(--down);margin-left:6px" onclick="deleteInvite('${i.code}')"></i>
            </div>
            ${refs || '<div style="font-size:12px; color:var(--text2); margin-top:4px;">Zatím nikdo nepřišel.</div>'}
        </div>`;
    }).join('') : '<span style="color:var(--text2); font-size:13px;">Žádné promo kódy.</span>';

    document.getElementById('pfview-admin').innerHTML = `
        <div class="admin-sub"><i class="ti ti-mail-cog" style="color:var(--accent)"></i> <b>Diagnostika e-mailů</b> <span style="font-size:11px;color:var(--text2)">(reset hesla, welcome, ranní souhrn)</span></div>
        <div style="display:flex; gap:8px; margin:8px 0; flex-wrap:wrap; align-items:center;">
            <input id="email-test-to" placeholder="komu poslat test (výchozí: ty)" class="admin-input" style="flex:1; min-width:180px;">
            <button class="btn-primary admin-mk" onclick="adminEmailTest()"><i class="ti ti-send"></i> Poslat test</button>
            <button class="tool-btn" onclick="adminMorningTest()"><i class="ti ti-sun"></i> Ranní souhrn (test)</button>
            <button class="tool-btn" onclick="adminAiTest()"><i class="ti ti-sparkles"></i> Otestovat AI</button>
            <button class="tool-btn" onclick="adminVisionTest()"><i class="ti ti-photo-search"></i> Otestovat čtení fotky</button>
        </div>
        <div id="email-test-result" style="font-size:12px; color:var(--text2); margin-bottom:10px;"></div>
        <div id="email-status" style="font-size:12.5px; line-height:1.7; margin-bottom:18px; padding:10px 12px; border-radius:12px; background:var(--surface); border:1px solid var(--border);"><i class="ti ti-loader spin"></i> Zjišťuji stav ranních e-mailů…</div>

        <div class="admin-sub"><i class="ti ti-radar" style="color:var(--accent)"></i> <b>Denní sken a verdikty</b> <span style="font-size:11px;color:var(--text2)">(běží sám v 5:00–6:30 UTC a při prvním přihlášení admina za den; ruční spuštění pokračuje tam, kde skončilo)</span></div>
        <div style="display:flex; gap:8px; margin:8px 0; flex-wrap:wrap; align-items:center;">
            <button class="btn-primary admin-mk" onclick="adminScan()"><i class="ti ti-player-play"></i> Spustit sken</button>
            <button class="tool-btn" onclick="adminDailyKick(true)"><i class="ti ti-refresh"></i> Denní aktualizace (sken + verdikty)</button>
            <span style="font-size:11px;color:var(--text2)">~20–40 s</span>
        </div>
        <div id="bt-result" style="font-size:12px; color:var(--text2); margin-bottom:18px;"></div>

        <div class="admin-sub"><i class="ti ti-infinity" style="color:var(--accent)"></i> <b>Kódy plného přístupu</b> <span style="font-size:11px;color:var(--text2)">(Elite natrvalo, rodina/kamarádi, 1 použití)</span></div>
        <div style="display:flex; gap:8px; margin:8px 0; flex-wrap:wrap;">
            <input id="invite-name" placeholder="vlastní název, např. tom" maxlength="24" class="admin-input"
                   onkeydown="if(event.key==='Enter') genNamedInvite('comped')">
            <button class="btn-primary admin-mk" onclick="genNamedInvite('comped')"><i class="ti ti-plus"></i> Vytvořit</button>
            <button class="tool-btn" onclick="genInvites('comped',1)"><i class="ti ti-dice"></i> Náhodný</button>
        </div>
        <div class="admin-invites">${compedChips}</div>

        <div class="admin-sub" style="margin-top:22px;"><i class="ti ti-discount-2" style="color:var(--accent)"></i> <b>Promo kódy ke sdílení</b> <span style="font-size:11px;color:var(--text2)">(7 dní Pro + 20% sleva 3 měsíce, vícenásobné)</span></div>
        <div style="display:flex; gap:8px; margin:8px 0; flex-wrap:wrap;">
            <input id="promo-name" placeholder="vlastní název, např. leto2026" maxlength="24" class="admin-input"
                   onkeydown="if(event.key==='Enter') genNamedInvite('promo')">
            <button class="btn-primary admin-mk" onclick="genNamedInvite('promo')"><i class="ti ti-plus"></i> Vytvořit promo</button>
        </div>
        <div style="display:flex; flex-direction:column; gap:10px;">${promoBlocks}</div>

        <div class="admin-sub" style="margin-top:24px;"><i class="ti ti-users" style="color:var(--accent)"></i> <b>Uživatelé (${users.length})</b></div>
        <div style="overflow-x:auto;">
        <table class="admin-table">
            <thead><tr><th>E-mail</th><th>Plán</th><th>Stav</th><th>Vytvořen</th><th>Změnit plán</th><th></th></tr></thead>
            <tbody>${rows || '<tr><td colspan="6" style="color:var(--text2)">Žádní uživatelé.</td></tr>'}</tbody>
        </table></div>`;
}

// Denní automatika: po přihlášení admina 1× za den (záloha k cronu) dokončí sken a přidá verdikty do track recordu.
let dailyKickRunning = false;
async function adminDailyKick(manual) {
    if (dailyKickRunning || !(authToken && mySub && mySub.is_admin)) return;
    const today = new Date().toISOString().slice(0, 10);          // UTC den = stejný jako na serveru
    let last = null; try { last = localStorage.getItem('ma_daily_kick2'); } catch (e) {}
    if (!manual && last === today) return;
    dailyKickRunning = true;
    const out = manual ? document.getElementById('bt-result') : null;
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    let added = 0, done = false, d = {};
    for (let round = 1; round <= 8 && !done; round++) {
        if (out) out.innerHTML = `<i class="ti ti-loader spin"></i> Denní aktualizace… (krok ${round})`;
        try {
            const r = await fetch(`${BACKEND_URL}/api/admin/daily`, { method: 'POST', headers: authHeaders() });
            d = await r.json();
            if (!d.ok) break;
            added = d.added || added; done = !!d.done;
            if (d.busy && !done) await sleep(5000);               // jiné spuštění (cron) právě pracuje
        } catch (e) { await sleep(2000); }                        // přerušené spuštění – další krok pokračuje
    }
    dailyKickRunning = false;
    if (done) {
        try { localStorage.setItem('ma_daily_kick2', today); } catch (e) {}
        signalsLoaded = false;
        if (added) showToast(`Denní aktualizace hotová: +${added} ${added === 1 ? 'verdikt' : (added < 5 ? 'verdikty' : 'verdiktů')} v track recordu`, 'up', 'ti-refresh');
        if (typeof activeTab !== 'undefined' && activeTab === 'ai') loadTrackRecord();
    }
    if (out) out.innerHTML = done
        ? `<span style="color:var(--up)">Hotovo: sken ${d.scan ? 'je aktuální' : '–'}, nových verdiktů ${added}${d.skipped ? ` (${d.skipped} titulů už mělo čerstvý verdikt)` : ''}.</span>`
        : `<span style="color:var(--amber)">${escapeHtml(d.error || 'Ještě to běží – klikni znovu za chvíli.')}</span>`;
    if (manual) loadEmailStatus();
}

async function loadEmailStatus() {
    const box = document.getElementById('email-status');
    if (!box) return;
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/admin/email-status`, { headers: authHeaders() })).json();
        if (!d.ok) { box.textContent = d.error || 'Stav se nepodařilo zjistit.'; return; }
        const ok = (c, txt, fix) => `<div>${c ? '<span style="color:var(--up)">✓</span>' : '<span style="color:var(--down)">✗</span>'} ${txt}${!c && fix ? ` <span style="color:var(--amber)">→ ${fix}</span>` : ''}</div>`;
        const when = x => x && x.ts ? new Date(x.ts * 1000).toLocaleString('cs-CZ', { day: 'numeric', month: 'numeric', hour: '2-digit', minute: '2-digit' }) : 'nikdy';
        const run = d.last_run_morning, snd = d.last_send_morning;
        box.innerHTML = `<b>Stav ranních e-mailů</b>` +
            ok(d.email_enabled, `Odesílání přes Brevo nastavené${d.sender ? ' (odesílatel ' + escapeHtml(d.sender) + ')' : ''}`, 'na Vercelu chybí BREVO_API_KEY nebo SENDER_EMAIL') +
            ok(d.digest_send, 'Rozesílání zapnuté (DIGEST_SEND=on)', 'Vercel → Settings → Environment Variables → přidej DIGEST_SEND = on (Production) a dej Redeploy') +
            ok(d.cron_secret, 'Ranní cron má heslo (CRON_SECRET)', 'na Vercelu chybí CRON_SECRET – bez něj cron nic neudělá') +
            (d.security ? ok(d.security.site_password, 'Heslo zámku webu je nastavené na Vercelu (SITE_PASSWORD)', 'bez něj platí veřejně známé výchozí heslo – nastav vlastní dlouhé heslo (Vercel → Environment Variables) a dej Redeploy') +
                ok(d.security.app_secret, 'Podpisový klíč přihlášení je nastavený (APP_SECRET)', 'nastav APP_SECRET (openssl rand -hex 32) a dej Redeploy; do té doby si appka klíč hlídá sama, ale změní se při smazání dat') : '') +
            ok(d.me.morning, `Ty máš ranní souhrn zapnutý (${escapeHtml(d.me.email)})`, 'zapni ho v Profil → Notifikace') +
            ok(d.ai_enabled, 'AI shrnutí zpráv zapnuté (ANALYSIS_API_KEY)', 'bez klíče briefing ukazuje jen čísla a témata, ne vysvětlení příčin') +
            (d.scan_last ? ok(d.scan_last.today, `Dnešní sken S&amp;P 500 hotový (${d.scan_last.scanned || '?'} titulů, ${d.scan_last.signals} signálů, ${escapeHtml(d.scan_last.date || '?')})`, 'poslední hotový sken je ze dne ' + escapeHtml(d.scan_last.date || '?') + ' – spusť „Denní aktualizace“ dole') : '') +
            (d.daily_last ? `<div style="color:var(--text2)">Denní automatika: <b style="color:var(--text)">${when(d.daily_last)}</b> · ${d.daily_last.done ? '<span style="color:var(--up)">hotovo</span>' : '<span style="color:var(--amber)">rozpracováno (' + escapeHtml(d.daily_last.stage || '') + ')</span>'} · nových verdiktů ${d.daily_last.added || 0}${d.daily_last.skipped ? ` (${d.daily_last.skipped} už mělo čerstvý)` : ''}${d.daily_last.eu_n ? ` · Evropa ${d.daily_last.eu_n} firem` : ''}${d.daily_last.user_changes ? ` · změn u uživatelů ${d.daily_last.user_changes}` : ''}${d.daily_last.alerts ? ` · upozornění odesláno ${d.daily_last.alerts}` : ''}</div>` : '<div style="color:var(--amber)">Denní automatika zatím neběžela – spustí se sama (cron nebo přihlášení admina), nebo ji spusť ručně dole.</div>') +
            `<div style="color:var(--text2)">Track record: <b style="color:var(--text)">${d.verdict_count || 0}</b> verdiktů · živý záznam signálů: <b style="color:var(--text)">${d.paper_count || 0}</b> obchodů</div>` +
            (d.llm_last ? `<div style="color:var(--text2)">Poslední volání AI: <b style="color:var(--text)">${when(d.llm_last)}</b> · ${d.llm_last.ok ? 'model <b style="color:var(--up)">' + escapeHtml(d.llm_last.model || '?') + '</b>' : '<span style="color:var(--down)">selhalo: ' + escapeHtml(d.llm_last.err || '?') + '</span>'}</div>` : '') +
            (d.import_last ? `<div style="color:var(--text2)">Poslední import z fotky/textu: <b style="color:var(--text)">${when(d.import_last)}</b> · ${d.import_last.ok ? `přečteno ${d.import_last.read || 0}, nalezeno ${d.import_last.resolved || 0}${d.import_last.note ? ' · <span style="color:var(--amber)">' + escapeHtml(d.import_last.note) + '</span>' : ''}` : ((d.import_last.kind === 'limit' || /HTTP 429|rate limit/i.test(d.import_last.error || '')) ? '<span style="color:var(--amber)">narazil na bezplatný limit AI – nová verze v takovém případě počká a zkusí to znovu; ověř tlačítkem „Otestovat čtení fotky“</span>' : '<span style="color:var(--down)">chyba: ' + escapeHtml(d.import_last.error || '?') + '</span>')}</div>` : '') +
            (d.news_status ? `<div style="color:var(--text2)">Poslední briefing: <b style="color:var(--text)">${when(d.news_status)}</b> · nálada ${escapeHtml(d.news_status.mood || '')} · AI ${d.news_status.ai ? 'ano' : 'ne (' + escapeHtml(d.news_status.ai_status || '?') + ')'}</div>` : '') +
            `<div style="color:var(--text2)">Odběratelé: ranní ${d.recipients.morning}, sobotní ${d.recipients.weekly}</div>` +
            `<div style="color:var(--text2)">Cron naposledy běžel: <b style="color:var(--text)">${when(run)}</b>${run ? ` (v Praze ${run.prague_hour}:xx, posílá se v ${run.send_hour}:xx)` : ' – pokud tu bude „nikdy" i zítra, cron se nespouští'}</div>` +
            `<div style="color:var(--text2)">Poslední rozesílka: <b style="color:var(--text)">${when(snd)}</b>${snd ? ` · příjemců ${snd.recipients}, odesláno ${snd.sent}, chyb ${snd.failed}${snd.error ? ` · <span style="color:var(--down)">${escapeHtml(snd.error)}</span>` : ''}` : ''}</div>`;
    } catch (e) { box.textContent = 'Stav se nepodařilo zjistit.'; }
}

// Vzorový „screenshot" seznamu akcií → skutečný import (AI čtení + rozpoznání). Přepíše i stav posledního importu.
async function adminVisionTest() {
    const out = document.getElementById('email-test-result');
    out.innerHTML = '<i class="ti ti-loader spin"></i> Posílám vzorový obrázek se 3 akciemi AI ke čtení…';
    const c = document.createElement('canvas'); c.width = 720; c.height = 380;
    const x = c.getContext('2d');
    x.fillStyle = '#101216'; x.fillRect(0, 0, c.width, c.height);
    x.fillStyle = '#fff'; x.font = 'bold 34px Arial'; x.fillText('Portfolio', 30, 60);
    [['Apple', 'AAPL', '12 400 Kč'], ['NVIDIA', 'NVDA', '8 150 Kč'], ['Advanced Micro Devices', 'AMD', '3 920 Kč']].forEach((r, i) => {
        const y = 135 + i * 85;
        x.fillStyle = '#fff'; x.font = 'bold 28px Arial'; x.fillText(r[0], 30, y);
        x.fillStyle = '#9ba1b0'; x.font = '22px Arial'; x.fillText(r[1], 30, y + 32);
        x.fillStyle = '#fff'; x.font = '26px Arial'; x.fillText(r[2], 520, y);
    });
    try {
        const t0 = Date.now();
        const r = await fetch(`${BACKEND_URL}/api/holdings/import`, { method: 'POST', headers: authHeaders(), body: JSON.stringify({ image: c.toDataURL('image/jpeg', 0.85) }) });
        const d = await r.json().catch(() => ({ ok: false, error: `HTTP ${r.status}` }));
        const s = ((Date.now() - t0) / 1000).toFixed(1).replace('.', ',');
        if (d.ok) {
            const got = (d.positions || []).map(p => p.ticker || ('?' + p.name));
            const good = ['AAPL', 'NVDA', 'AMD'].every(t => got.includes(t));
            out.innerHTML = `<span style="color:var(--${good ? 'up' : 'amber'})">${good ? '✓ Čtení fotky funguje' : '⚠ Přečteno jen částečně'}</span> – za ${s} s: ${escapeHtml(got.join(', ') || 'nic')}`;
        } else {
            out.innerHTML = `<span style="color:var(--down)">✗ ${escapeHtml(d.error || 'nepodařilo se')}</span>${d.detail ? ` <span style="color:var(--text3)">(${escapeHtml(String(d.detail).slice(0, 220))})</span>` : ''}`;
        }
    } catch (e) { out.innerHTML = '<span style="color:var(--down)">✗ spojení selhalo</span>'; }
    loadEmailStatus();
}

async function adminAiTest() {
    const out = document.getElementById('email-test-result');
    out.innerHTML = '<i class="ti ti-loader spin"></i> Zkouším AI modely (pár vteřin)…';
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/admin/ai-test`, { method: 'POST', headers: authHeaders() })).json();
        if (!d.ok) { out.innerHTML = `<span style="color:var(--down)">${escapeHtml(d.error || 'Test se nepodařil.')}</span>`; return; }
        const rows = (d.models || []).map(m => `<div>${m.ok ? '<span style="color:var(--up)">✓</span>' : '<span style="color:var(--down)">✗</span>'} <b>${escapeHtml(m.model)}</b> – ${m.ok ? 'odpovědělo za ' + (m.ms / 1000).toFixed(1).replace('.', ',') + ' s' : escapeHtml(m.err || '?')}</div>`).join('');
        const av = Array.isArray(d.available) ? d.available.join(', ') : (d.available || 'neznámé');
        const fbHint = { gemini: 'zdarma na aistudio.google.com → Get API key (umí i fotky; v bezplatném tarifu může Google data použít ke zlepšování služeb)',
                         cerebras: 'zdarma na cloud.cerebras.ai → API Keys', openrouter: 'zdarma na openrouter.ai → Keys (modely „:free")' };
        const fbs = (d.fallbacks || []).map(f => f.configured
            ? `<div>${f.test && f.test.ok ? '<span style="color:var(--up)">✓</span>' : '<span style="color:var(--down)">✗</span>'} záloha <b>${escapeHtml(f.id)}</b> – ${f.test ? (f.test.ok ? escapeHtml(f.test.model) + ' odpověděl za ' + (f.test.ms / 1000).toFixed(1).replace('.', ',') + ' s' : escapeHtml(f.test.err || '?')) : 'bez modelu'}${f.vision_models && f.vision_models.length ? ' · fotky: ' + escapeHtml(f.vision_models[0]) : ''}</div>`
            : `<div style="color:var(--text3)">○ záloha <b>${escapeHtml(f.id)}</b> není nastavená – klíč ${escapeHtml(f.env)} ${escapeHtml(fbHint[f.id] || '')}</div>`).join('');
        out.innerHTML = `<div style="margin:6px 0"><b>AI (${escapeHtml(d.provider || 'bez Groq')})</b>${rows || '<div style="color:var(--text3)">hlavní klíč (Groq) není nastavený</div>'}` +
            `<div style="color:var(--text3);margin-top:4px">Pro čtení fotek: ${escapeHtml((d.vision || []).join(', ') || '–')}</div>` +
            `<div style="margin-top:6px">${fbs}</div>` +
            `<div style="color:var(--text3);margin-top:4px">Zálohy se použijí samy, když Groq narazí na bezplatný limit nebo nefunguje. Klíč nastavíš ve Vercelu → Settings → Environment Variables a dáš Redeploy.</div>` +
            (d.provider ? `<div style="color:var(--text3);margin-top:4px;word-break:break-word">Nabízí Groq: ${escapeHtml(av)}</div>` : '') + `</div>`;
    } catch (e) { out.innerHTML = '<span style="color:var(--down)">Test se nepodařil (spojení).</span>'; }
}

async function adminMorningTest() {
    const out = document.getElementById('email-test-result');
    out.innerHTML = '<i class="ti ti-loader spin"></i> Posílám ranní souhrn na tvůj e-mail…';
    try {
        const d = await (await fetch(`${BACKEND_URL}/api/admin/morning-test`, { method: 'POST', headers: authHeaders() })).json();
        if (d.ok) { out.innerHTML = `<span style="color:var(--up)"><i class="ti ti-check"></i> Ranní souhrn odeslán na ${escapeHtml(d.sent_to || '')}. Zkontroluj schránku (i spam).</span>`; showToast('Ranní souhrn odeslán', 'up', 'ti-sun'); }
        else out.innerHTML = `<span style="color:var(--down)"><i class="ti ti-x"></i> Neodesláno: ${escapeHtml(d.error || 'neznámý důvod')}</span>`;
    } catch (e) { out.innerHTML = '<span style="color:var(--down)">Chyba spojení se serverem.</span>'; }
}

async function adminEmailTest() {
    const out = document.getElementById('email-test-result');
    const to = (document.getElementById('email-test-to').value || '').trim();
    out.innerHTML = '<i class="ti ti-loader spin"></i> Posílám testovací e-mail...';
    try {
        const r = await fetch(`${BACKEND_URL}/api/admin/email-test`, {
            method: 'POST', headers: authHeaders(), body: JSON.stringify({ to })
        });
        const d = await r.json();
        if (d.ok) {
            out.innerHTML = `<span style="color:var(--up)"><i class="ti ti-check"></i> Odesláno na ${d.sent_to}. Zkontroluj schránku (i spam).</span>`;
            showToast('Testovací e-mail odeslán', 'up', 'ti-mail');
        } else {
            out.innerHTML = `<span style="color:var(--down)"><i class="ti ti-x"></i> Neodesláno.</span><br>
                <span>Důvod: ${escapeHtml(d.error || 'neznámý')}</span><br>
                <span style="color:var(--text2)">email_enabled: ${d.email_enabled} · sender: ${escapeHtml(d.sender || '—')} · brevo_key: ${d.has_brevo_key} · app_url: ${escapeHtml(d.app_url || '—')}</span>`;
        }
    } catch (e) {
        out.innerHTML = '<span style="color:var(--down)">Chyba spojení se serverem.</span>';
    }
}

async function adminScan(round) {
    const out = document.getElementById('bt-result');
    round = round || 1;
    out.innerHTML = `<i class="ti ti-loader spin"></i> Skenuji S&amp;P 500… (krok ${round})`;
    try {
        const r = await fetch(`${BACKEND_URL}/api/admin/scan`, { method: 'POST', headers: authHeaders() });
        const d = await r.json();
        if (!d.ok) { out.innerHTML = `<span style="color:var(--down)">${escapeHtml(d.error || 'Nepodařilo se.')}</span>`; return; }
        if (!d.done && round < 8) return adminScan(round + 1);
        out.innerHTML = d.done
            ? `<span style="color:var(--up)">Hotovo: ${d.scanned} titulů, ${d.signals} ${d.signals === 1 ? 'signál' : 'signálů'} (${escapeHtml(d.updated || '')}).</span>`
            : 'Sken ještě běží – klikni znovu.';
        signalsLoaded = false;
    } catch (e) {
        out.innerHTML = '<span style="color:var(--down)">Chyba spojení se serverem.</span>';
    }
}

async function genInvites(type, count) {
    try {
        const r = await fetch(`${BACKEND_URL}/api/admin/invites`, {
            method: 'POST', headers: authHeaders(), body: JSON.stringify({ type, count })
        });
        const d = await r.json();
        if (d.ok) { showToast(`Vytvořeno ${d.created.length} kódů`, 'up', 'ti-ticket'); loadAdmin(); }
    } catch (e) { showToast('Nepodařilo se', 'down', 'ti-x'); }
}

async function genNamedInvite(type) {
    type = (type === 'promo') ? 'promo' : 'comped';
    const inp = document.getElementById(type === 'promo' ? 'promo-name' : 'invite-name');
    const name = (inp.value || '').trim().toLowerCase();
    if (!name) { showToast('Zadej název kódu', 'down', 'ti-x'); return; }
    const payload = (type === 'promo')
        ? { code: name, type: 'promo', trial_days: 7, discount_pct: 20, discount_months: 3 }
        : { code: name, type: 'comped' };
    try {
        const r = await fetch(`${BACKEND_URL}/api/admin/invites`, {
            method: 'POST', headers: authHeaders(), body: JSON.stringify(payload)
        });
        const d = await r.json();
        if (d.ok) { showToast(`Kód „${d.created[0]}" vytvořen`, 'up', 'ti-ticket'); inp.value = ''; loadAdmin(); }
        else showToast(d.error || 'Nepodařilo se', 'down', 'ti-x');
    } catch (e) { showToast('Chyba při vytváření kódu', 'down', 'ti-x'); }
}

async function adminSetPlan(username, plan) {
    try {
        const r = await fetch(`${BACKEND_URL}/api/admin/set-plan`, {
            method: 'POST', headers: authHeaders(), body: JSON.stringify({ username, plan })
        });
        const d = await r.json();
        if (d.ok) { showToast(`${username} → ${plan}`, 'up', 'ti-check'); loadAdmin(); }
        else showToast(d.error || 'Chyba', 'down', 'ti-x');
    } catch (e) { showToast('Chyba při změně plánu', 'down', 'ti-x'); }
}

async function adminDeleteUser(email) {
    if (!confirm(`Opravdu smazat účet ${email}? Tahle akce je nevratná.`)) return;
    try {
        const r = await fetch(`${BACKEND_URL}/api/admin/delete-user`, {
            method: 'POST', headers: authHeaders(), body: JSON.stringify({ email })
        });
        const d = await r.json();
        if (d.ok) { showToast(`Účet ${email} smazán`, 'down', 'ti-trash'); loadAdmin(); }
        else showToast(d.error || 'Chyba', 'down', 'ti-x');
    } catch (e) { showToast('Chyba při mazání', 'down', 'ti-x'); }
}

async function deleteInvite(code) {
    if (!confirm(`Smazat kód „${code}"? Existující účty zůstanou, jen kód už nepůjde použít.`)) return;
    try {
        const r = await fetch(`${BACKEND_URL}/api/admin/delete-invite`, {
            method: 'POST', headers: authHeaders(), body: JSON.stringify({ code })
        });
        const d = await r.json();
        if (d.ok) { showToast(`Kód „${code}" smazán`, 'down', 'ti-trash'); loadAdmin(); }
        else showToast(d.error || 'Chyba', 'down', 'ti-x');
    } catch (e) { showToast('Chyba při mazání kódu', 'down', 'ti-x'); }
}

function copyText(t) {
    navigator.clipboard?.writeText(t);
    showToast('Kód zkopírován', 'up', 'ti-copy');
}
