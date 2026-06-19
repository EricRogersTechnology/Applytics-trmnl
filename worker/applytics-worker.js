// Applytics — Cloudflare Worker backend for the TRMNL plugin.
//
// TRMNL polls this Worker (POST) with a user's App Store Connect credentials
// (entered in the plugin's Form Fields). The Worker signs an ES256 JWT, pulls
// the daily Sales reports in parallel, and returns the merge variables the
// Applytics templates render. STATELESS — nothing is stored or logged.
//
// Deploy: Cloudflare dashboard → Workers & Pages → Create Worker → paste → Deploy.

const ASC = 'https://api.appstoreconnect.apple.com/v1/salesReports';
const EMPTY = {
  has_data: false, updated_at: 'No data yet',
  app_name: '', app_icon: '', app_rating: '-', app_ratings_count: 0,
  downloads_day: 0, downloads_7d: 0, downloads_30d: 0,
  revenue_day: '0.00', revenue_7d: '0.00', revenue_30d: '0.00',
  currency: 'USD', apps: [],
};

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === 'GET' && !url.search) {
      return json({ ok: true, service: 'applytics-worker',
        usage: 'POST {key_id, issuer_id, private_key, vendor_number, app_store_url?}' });
    }
    try {
      const cfg = await readConfig(request);
      for (const k of ['key_id', 'issuer_id', 'private_key', 'vendor_number']) {
        if (!cfg[k]) return json({ ...EMPTY, updated_at: 'Setup needed', error: `Missing ${k}` });
      }
      return json(await fetchAppStore(cfg));
    } catch (e) {
      return json({ ...EMPTY, updated_at: 'Error', error: String((e && e.message) || e) });
    }
  },
};

const json = (o) => new Response(JSON.stringify(o),
  { headers: { 'content-type': 'application/json', 'cache-control': 'no-store' } });

async function readConfig(request) {
  let c = {};
  if (request.method === 'POST') {
    const body = await request.text();
    try { c = JSON.parse(body); }
    catch {
      try { c = JSON.parse(body.replace(/\r?\n/g, "\\n")); } // tolerate a multi-line .p8 inside the JSON body
      catch { c = Object.fromEntries(new URLSearchParams(body)); }
    }
  }
  for (const [k, v] of new URL(request.url).searchParams) if (c[k] === undefined) c[k] = v;
  const pick = (...ks) => ks.map((k) => c[k]).find((v) => v != null && v !== '');
  return {
    key_id: pick('key_id', 'asc_key_id'),
    issuer_id: pick('issuer_id', 'asc_issuer_id'),
    private_key: String(pick('private_key', 'asc_private_key') || '').replace(/\\n/g, '\n').trim(),
    vendor_number: pick('vendor_number', 'asc_vendor_number'),
    app_store_url: pick('app_store_url', 'asc_app_url') || '',
    app_icon_url: pick('app_icon_url', 'icon') || '',
  };
}

// --- ES256 JWT via Web Crypto ---
const b64urlBytes = (buf) => {
  let s = ''; const b = new Uint8Array(buf);
  for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
  return btoa(s).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
};
const b64urlStr = (s) => btoa(unescape(encodeURIComponent(s)))
  .replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
function pemToDer(pem) {
  const b = atob(pem.replace(/-----[^-]+-----/g, '').replace(/\s+/g, ''));
  const u = new Uint8Array(b.length); for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i);
  return u.buffer;
}
async function signToken(keyId, issuerId, pem) {
  const key = await crypto.subtle.importKey('pkcs8', pemToDer(pem),
    { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']);
  const header = b64urlStr(JSON.stringify({ alg: 'ES256', kid: keyId, typ: 'JWT' }));
  const now = Math.floor(Date.now() / 1000);
  const payload = b64urlStr(JSON.stringify({ iss: issuerId, iat: now, exp: now + 1100, aud: 'appstoreconnect-v1' }));
  const si = `${header}.${payload}`;
  const sig = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, key, new TextEncoder().encode(si));
  return `${si}.${b64urlBytes(sig)}`;
}

// --- reports ---
async function getDaily(token, vendor, date) {
  const qs = new URLSearchParams({
    'filter[frequency]': 'DAILY', 'filter[reportType]': 'SALES', 'filter[reportSubType]': 'SUMMARY',
    'filter[vendorNumber]': vendor, 'filter[reportDate]': date,
  });
  const r = await fetch(`${ASC}?${qs}`, { headers: { Authorization: `Bearer ${token}`, Accept: 'application/a-gzip' } });
  if (r.status === 404) return null;
  if (r.status !== 200) throw new Error(`Apple HTTP ${r.status}`);
  return parseTSV(await new Response(r.body.pipeThrough(new DecompressionStream('gzip'))).text());
}
function parseTSV(text) {
  const lines = text.split('\n').filter((l) => l.trim());
  if (!lines.length) return [];
  const h = lines[0].split('\t');
  return lines.slice(1).map((line) => { const c = line.split('\t'); const row = {}; h.forEach((k, i) => { row[k] = c[i]; }); return row; });
}
const isDownload = (pt) => { pt = (pt || '').toUpperCase(); return !pt.startsWith('IA') && !pt.startsWith('7'); };
function aggregate(rows, appFilter) {
  let downloads = 0, revenue = 0; const cur = {}; const apps = {};
  for (const r of (rows || [])) {
    const id = (r['Apple Identifier'] || '').trim();
    if (appFilter && id !== appFilter) continue;
    const units = parseInt(r['Units'] || '0', 10) || 0;
    const proceeds = (parseFloat(r['Developer Proceeds'] || '0') || 0) * units;
    const c = (r['Currency of Proceeds'] || 'USD').trim() || 'USD';
    const dl = isDownload(r['Product Type Identifier']);
    if (dl) downloads += units;
    revenue += proceeds; cur[c] = (cur[c] || 0) + proceeds;
    if (id) {
      apps[id] = apps[id] || { name: (r['Title'] || '').trim(), downloads: 0, revenue: 0 };
      if ((r['Title'] || '').trim()) apps[id].name = (r['Title'] || '').trim();
      if (dl) apps[id].downloads += units;
      apps[id].revenue += proceeds;
    }
  }
  const currency = Object.keys(cur).sort((a, b) => cur[b] - cur[a])[0] || 'USD';
  return { downloads, revenue, currency, apps };
}
const money = (n) => n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const ymd = (ms) => new Date(ms).toISOString().slice(0, 10);

// iTunes app metadata (icon + rating). Apple rate-limits this from datacenter
// IPs and may return 403; the icon then falls back to the optional app_icon_url
// form field. Looked up "cold" (before the Sales calls) to improve odds.
async function lookupITunes(id) {
  for (let i = 0; i < 3; i++) {
    try {
      const r = await fetch(`https://itunes.apple.com/lookup?id=${id}&country=us`, {
        headers: { 'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15', 'Accept': 'application/json' },
      });
      if (r.ok) { const j = await r.json(); return (j.results && j.results[0]) || null; }
    } catch (e) {}
  }
  return null;
}

async function fetchAppStore(cfg) {
  const token = await signToken(cfg.key_id, cfg.issuer_id, cfg.private_key);
  let appFilter = null;
  if (cfg.app_store_url) { const m = String(cfg.app_store_url).match(/id(\d+)/); if (m) appFilter = m[1]; }
  const primaryMeta = appFilter ? await lookupITunes(appFilter) : null;
  const day = 86400000, today = Date.now();
  const dates = []; for (let o = 1; o <= 32; o++) dates.push(ymd(today - o * day));
  const results = await Promise.all(dates.map(async (d) => {
    try { return { date: d, rows: await getDaily(token, cfg.vendor_number, d) }; }
    catch (e) { return { date: d, err: String(e.message || e) }; }
  }));
  const errs = results.filter((r) => r.err);
  if (errs.length === dates.length) throw new Error(errs[0].err); // likely auth/crypto
  const byDate = {};
  for (const r of results) if (r.rows) byDate[r.date] = aggregate(r.rows, appFilter);
  const have = Object.keys(byDate).sort();
  if (!have.length) return { ...EMPTY };
  const latest = have[have.length - 1];
  const end = new Date(latest + 'T00:00:00Z').getTime();
  const win = (n) => { let dl = 0, rev = 0; for (let i = 0; i < n; i++) { const dd = ymd(end - i * day); if (byDate[dd]) { dl += byDate[dd].downloads; rev += byDate[dd].revenue; } } return { dl, rev }; };
  const t = win(1), w7 = win(7), w30 = win(30);
  const at = {};
  for (let i = 0; i < 30; i++) { const dd = ymd(end - i * day); const a = byDate[dd]; if (!a) continue;
    for (const [id, info] of Object.entries(a.apps)) { at[id] = at[id] || { name: info.name, downloads: 0, revenue: 0 }; if (info.name) at[id].name = info.name; at[id].downloads += info.downloads; at[id].revenue += info.revenue; } }
  const ranked = Object.entries(at).sort((a, b) => b[1].downloads - a[1].downloads).slice(0, 6);
  const meta = {};
  await Promise.all(ranked.map(async ([id]) => {
    const x = (appFilter && id === appFilter) ? primaryMeta : await lookupITunes(id);
    if (x) {
      meta[id] = {
        r: (x.averageUserRating != null) ? Number(x.averageUserRating).toFixed(1) : "-",
        c: x.userRatingCount || 0,
        icon: x.artworkUrl512 || x.artworkUrl100 || "",
        name: (x.trackName || "").trim(),
      };
    }
  }));
  const apps = ranked.map(([id, a]) => ({
    name: (meta[id] && meta[id].name) || a.name,
    icon: (meta[id] && meta[id].icon) || "",
    downloads_30d: a.downloads, revenue_30d: money(a.revenue),
    rating: (meta[id] && meta[id].r) || "-", ratings_count: (meta[id] && meta[id].c) || 0,
  }));
  const primary = apps[0] || { name: "", icon: "", rating: "-", ratings_count: 0 };
  return {
    has_data: true, updated_at: latest,
    app_name: primary.name, app_icon: cfg.app_icon_url || primary.icon,
    app_rating: primary.rating, app_ratings_count: primary.ratings_count,
    downloads_day: t.dl, downloads_7d: w7.dl, downloads_30d: w30.dl,
    revenue_day: money(t.rev), revenue_7d: money(w7.rev), revenue_30d: money(w30.rev),
    currency: byDate[latest].currency || 'USD', apps,
  };
}
