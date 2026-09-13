const $ = (sel, el = document) => el.querySelector(sel);
const app = $("#app");
let STATUS = null;
let pollTimer = null;

async function api(path) {
  const r = await fetch("/api" + path);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const err = new Error(body.error || r.statusText);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function shortHash(h, n = 10) {
  if (!h) return "—";
  h = String(h);
  return h.length <= n * 2 + 1 ? h : `${h.slice(0, n)}…${h.slice(-n)}`;
}

function atomsToXfer(atoms) {
  const n = Number(atoms || 0);
  const sign = n < 0 ? "-" : "";
  const a = Math.abs(n);
  const whole = Math.floor(a / 1e8);
  const frac = String(a % 1e8).padStart(8, "0").replace(/0+$/, "");
  const body = frac ? `${whole.toLocaleString()}.${frac}` : whole.toLocaleString();
  return `${sign}${body} XFER`;
}

function formatAssetAmount(atoms, name, units) {
  const n = Number(atoms || 0);
  const sign = n < 0 ? "-" : "";
  const a = Math.abs(n);
  const u = Number(units);
  const decimals = Number.isFinite(u) ? Math.min(8, Math.max(0, u)) : 8;
  const whole = Math.floor(a / 1e8);
  const fracNum = a % 1e8;
  let body;
  if (decimals === 0) {
    body = whole.toLocaleString();
  } else {
    const frac = String(fracNum).padStart(8, "0").slice(0, decimals).replace(/0+$/, "");
    body = frac ? `${whole.toLocaleString()}.${frac}` : whole.toLocaleString();
  }
  return `${sign}${body}${name ? " " + name : ""}`;
}

function normalizeIpfsClient(value) {
  if (!value) return "";
  let s = String(value).trim();
  if (s.startsWith("ipfs://")) s = s.slice(7);
  if (s.startsWith("/ipfs/")) s = s.slice(6);
  s = s.split("/")[0].split("?")[0];
  if (/^Qm[1-9A-HJ-NP-Za-km-z]{44}$/.test(s)) return s;
  if (/^baf[a-z2-7]{50,}$/.test(s)) return s;
  return "";
}

function assetCid(a) {
  return a.ipfs_cid
    || normalizeIpfsClient(a.ipfs)
    || normalizeIpfsClient(a.rpc && (a.rpc.ipfs_hash || a.rpc.ipfs || a.rpc.message))
    || "";
}

function defaultGateways(cid) {
  return [
    "https://gateway.pinata.cloud/ipfs/" + cid,
    "https://dweb.link/ipfs/" + cid,
    "https://w3s.link/ipfs/" + cid,
    "https://cloudflare-ipfs.com/ipfs/" + cid,
    "https://ipfs.io/ipfs/" + cid,
  ];
}

function ipfsSources(cid, extra) {
  const seen = new Set();
  const out = [];
  const add = (u) => {
    if (!u || seen.has(u)) return;
    seen.add(u);
    out.push(u);
  };
  add("/api/ipfs/content/" + encodeURIComponent(cid));
  (extra || []).forEach(add);
  defaultGateways(cid).forEach(add);
  return out;
}

function mediaSrc(ref) {
  if (!ref) return "";
  if (typeof ref === "object") {
    if (ref.cid) return ipfsSources(ref.cid)[0];
    if (ref.src) return ref.src;
    return "";
  }
  if (/^https?:\/\//i.test(ref)) return ref;
  const cid = normalizeIpfsClient(ref);
  return cid ? ipfsSources(cid)[0] : "";
}

function mediaFallbacks(ref, extra) {
  if (ref && typeof ref === "object" && ref.cid) return ipfsSources(ref.cid, extra);
  const cid = normalizeIpfsClient(typeof ref === "string" ? ref : "");
  return cid ? ipfsSources(cid, extra) : [];
}

function fallbackAttr(urls) {
  if (!urls || urls.length < 2) return "";
  return ` data-fallbacks="${esc(JSON.stringify(urls.slice(1)))}"`;
}

function yesNo(v) {
  return v ? "yes" : "no";
}

function timeAgo(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function fmtTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toISOString().replace(".000", "").replace("T", " ") + " UTC";
}

function linkBlock(h) { return `<a href="#/block/${h}">${h}</a>`; }
function linkTx(id) { return `<a class="hash" href="#/tx/${id}">${shortHash(id)}</a>`; }
function linkAddr(a) {
  if (!a) return `<span class="faint">—</span>`;
  return `<a class="hash" href="#/address/${encodeURIComponent(a)}">${shortHash(a, 8)}</a>`;
}
function linkAsset(n) {
  if (!n) return "—";
  return `<a href="#/asset/${encodeURIComponent(n)}">${esc(n)}</a>`;
}
function handle(h) {
  if (!h) return "";
  return `<a href="#/identity/${encodeURIComponent(h)}">@${esc(h)}</a>`;
}

function setNav() {
  const hash = location.hash || "#/";
  document.querySelectorAll(".nav a").forEach((a) => {
    const href = a.getAttribute("href");
    a.classList.toggle(
      "active",
      href === "#/"
        ? hash === "#/"
        : hash.startsWith(href)
          || (href === "#/assets" && hash.startsWith("#/asset/"))
          || (href === "#/members" && hash.startsWith("#/identity/"))
    );
  });
}

function copyable(text) {
  return `<code class="copy" title="Copy" data-copy="${esc(text)}">${esc(text)}</code>`;
}

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-copy]");
  if (el) {
    navigator.clipboard.writeText(el.dataset.copy).catch(() => {});
    el.classList.add("ok");
    setTimeout(() => el.classList.remove("ok"), 600);
    return;
  }
  const shot = e.target.closest("[data-lightbox]");
  if (shot) {
    openLightbox(shot.getAttribute("data-lightbox") || shot.getAttribute("src"), shot.getAttribute("alt") || "");
    return;
  }
  if (e.target.closest("[data-lightbox-close]")) {
    closeLightbox();
    return;
  }
  if (e.target.closest("a, button, input, label, [data-copy]")) return;
  const row = e.target.closest("[data-href]");
  if (row) location.hash = row.getAttribute("data-href");
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeLightbox();
});

document.addEventListener("error", (e) => {
  const el = e.target;
  if (!el || !el.closest || !el.closest("#asset-media")) return;
  if (el.tagName !== "IMG" && el.tagName !== "VIDEO") return;
  let extras = [];
  try { extras = JSON.parse(el.dataset.fallbacks || "[]"); } catch { extras = []; }
  if (extras.length) {
    const next = extras.shift();
    el.dataset.fallbacks = JSON.stringify(extras);
    el.src = next;
    if (el.hasAttribute("data-lightbox")) el.setAttribute("data-lightbox", next);
    return;
  }
  if (el.tagName === "IMG" && !el.dataset.triedVideo) {
    const cid = el.getAttribute("alt") || "";
    const urls = ipfsSources(normalizeIpfsClient(cid) || cid);
    if (urls.length && normalizeIpfsClient(cid)) {
      const stage = el.closest("#asset-media");
      stage.innerHTML = `<div class="media-stage video"><video controls playsinline src="${esc(urls[0])}"${fallbackAttr(urls)}></video></div>`;
      return;
    }
  }
  const stage = el.closest("#asset-media");
  if (stage && !stage.querySelector(".media-empty")) {
    stage.innerHTML = `<div class="media-stage">${ipfsUnavailable("Could not render this IPFS object. Use a gateway link below.")}</div>`;
  }
}, true);

function openLightbox(src, alt) {
  closeLightbox();
  if (!src) return;
  const box = document.createElement("div");
  box.className = "lightbox on";
  box.id = "lightbox";
  box.setAttribute("data-lightbox-close", "1");
  box.innerHTML = `<img src="${esc(src)}" alt="${esc(alt)}" />`;
  document.body.appendChild(box);
}

function closeLightbox() {
  const box = $("#lightbox");
  if (box) box.remove();
}

async function refreshStatus() {
  try {
    STATUS = await api("/status");
    const pill = $("#pill-rpc");
    const h = $("#pill-height");
    if (STATUS.rpc_connected) {
      pill.textContent = `node · ${STATUS.network_label || STATUS.network}`;
      pill.className = "pill ok";
    } else {
      pill.textContent = "node offline";
      pill.className = "pill bad";
    }
    const height = STATUS.tip ?? STATUS.indexed_height;
    h.textContent = `height ${height < 0 ? "—" : height}${STATUS.indexing ? " …" : ""}`;
    $("#foot-net").textContent = STATUS.network_label || "";
  } catch (e) {
    $("#pill-rpc").textContent = "explorer error";
    $("#pill-rpc").className = "pill bad";
  }
}

function nodeBanner() {
  if (STATUS && STATUS.rpc_connected) return "";
  const err = STATUS?.rpc_error ? esc(STATUS.rpc_error) : "xcoind / xcoin-qt RPC not reachable";
  return `<div class="notice">${err}</div>`;
}

function statsRow() {
  const s = STATUS || {};
  const counts = s.counts || {};
  return `
    <div class="grid stats">
      <div class="card stat"><span>Height</span><b>${s.tip ?? "—"}</b></div>
      <div class="card stat"><span>Supply</span><b>${atomsToXfer(s.supply_atoms)}</b></div>
      <div class="card stat"><span>Next subsidy</span><b>${atomsToXfer(s.subsidy_atoms)}</b></div>
      <div class="card stat"><span>Assets</span><b>${counts.assets ?? 0}</b></div>
      <div class="card stat"><span>Lottery wins</span><b>${counts.wins ?? 0}</b></div>
      <div class="card stat"><span>Peers</span><b>${s.peer_count ?? "—"}</b></div>
    </div>`;
}

function winnerWho(w) {
  if (w && w.xaccount) return handle(w.xaccount);
  return `<span class="faint">no XVA1</span>`;
}

function blockWinner(b) {
  if (b && b.winner_handle) return handle(b.winner_handle);
  if (b && b.height === 0) return "—";
  return `<span class="faint">no XVA1</span>`;
}

function txIdentityHtml(t) {
  if (t.coinbase) {
    if (t.winner_handle) return handle(t.winner_handle);
    if (t.lottery_handles && t.lottery_handles.length) {
      return t.lottery_handles.map((h) => handle(h)).join(" ");
    }
    return `<span class="faint">no XVA1</span>`;
  }
  return t.xid_handle ? handle(t.xid_handle) : "—";
}

function lotteryCard(live, nodes, winnerHandles) {
  const L = live || {};
  const rewards = L.rewards || [];
  const mapped = winnerHandles || [];
  const n = Math.max(mapped.length, (L.winners || []).length);
  const rows = n
    ? Array.from({ length: n }, (_, i) => {
        const who = mapped[i] ? handle(mapped[i]) : `<span class="faint">no XVA1</span>`;
        return `<div class="winner"><div class="who"><span class="badge lottery">winner</span> ${who}</div><div class="amt">${atomsToXfer(rewards[i] || 0)}</div></div>`;
      }).join("")
    : `<div class="empty">No eligible X Verified nodes in the live draw yet.</div>`;
  const activeN = (nodes || []).length || L.active_nodes || 0;
  const slot = L.slot;
  return `
    <div class="card">
      <h2>This minute’s lottery</h2>
      <div class="countdown" id="cd">—:—</div>
      <p class="muted">Height ${L.height ?? "—"} · slot ${slot ?? "—"} · ${activeN} active · ${L.winner_count ?? 1} winner(s)</p>
      ${rows}
      <div class="row-actions">
        <a href="#/lottery">Full lottery →</a>
        <a href="#/members">Eligible members →</a>
      </div>
    </div>`;
}

function tickCountdown() {
  const el = $("#cd");
  if (!el) return;
  const rem = 60 - (Math.floor(Date.now() / 1000) % 60);
  el.textContent = `0:${String(rem).padStart(2, "0")}`;
}

async function pageHome() {
  const [blocks, lottery] = await Promise.all([
    api("/blocks?limit=12"),
    api("/lottery").catch(() => ({ live: null, nodes: [], history: [] })),
  ]);
  const recentWins = (lottery.history || []).slice(0, 6).map((h) => {
    const w = (h.winners || [])[0] || {};
    const who = winnerWho(w);
    return `<tr><td>${linkBlock(h.height)}</td><td>${who}</td><td>${atomsToXfer(w.amount)}</td><td class="muted">${timeAgo(h.time)}</td></tr>`;
  }).join("");
  app.innerHTML = `
    ${nodeBanner()}
    ${statsRow()}
    <div class="grid home" style="margin-top:16px">
      <div class="card">
        <h2>Latest blocks</h2>
        <table>
          <thead><tr><th>Height</th><th>Time</th><th>Tx</th><th>Winner</th></tr></thead>
          <tbody>
            ${(blocks.items || []).map((b) => `<tr>
              <td>${linkBlock(b.height)}</td>
              <td class="muted">${timeAgo(b.time)}</td>
              <td>${b.tx_count}</td>
              <td>${blockWinner(b)}</td>
            </tr>`).join("") || `<tr><td colspan="4" class="empty">No blocks indexed yet. Genesis appears once the node is up.</td></tr>`}
          </tbody>
        </table>
        <div class="row-actions"><a href="#/blocks">All blocks →</a></div>
      </div>
      ${lotteryCard(lottery.live, lottery.nodes, lottery.winner_handles)}
    </div>
    <div class="card" style="margin-top:16px">
      <h2>Recent lottery winners</h2>
      <table>
        <thead><tr><th>Block</th><th>Winner</th><th>Reward</th><th>When</th></tr></thead>
        <tbody>${recentWins || `<tr><td colspan="4" class="empty">Winners show up from height 1 coinbases (XHB1).</td></tr>`}</tbody>
      </table>
    </div>`;
  tickCountdown();
}

async function pageBlocks() {
  const data = await api("/blocks?limit=40");
  app.innerHTML = `
    <h1 class="page-title">Blocks</h1>
    <p class="sub">One height per minute. Height 0 is genesis (no payday). Height 1+ is the lottery coinbase.</p>
    <div class="card">
      <table>
        <thead><tr><th>Height</th><th>Hash</th><th>Time</th><th>Tx</th><th>Winners</th><th>Winner</th></tr></thead>
        <tbody>
          ${(data.items || []).map((b) => `<tr>
            <td>${linkBlock(b.height)}</td>
            <td class="hash"><a class="hash" href="#/block/${b.hash}">${shortHash(b.hash)}</a></td>
            <td class="muted">${fmtTime(b.time)}</td>
            <td>${b.tx_count}</td>
            <td>${b.winner_count || (b.height === 0 ? "—" : "0")}</td>
            <td>${blockWinner(b)}</td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

async function pageBlock(key) {
  const b = await api("/block/" + encodeURIComponent(key));
  const wins = (b.lottery && b.lottery.winners) || [];
  app.innerHTML = `
    <h1 class="page-title">Block ${b.height}</h1>
    <div class="card" style="margin-bottom:16px">
      <div class="kv">
        <b>Hash</b><div>${copyable(b.hash)}</div>
        <b>Time</b><div>${fmtTime(b.time)} · ${timeAgo(b.time)}</div>
        <b>Previous</b><div>${b.prev ? `<a class="hash" href="#/block/${b.prev}">${b.prev}</a>` : "—"}</div>
        <b>Slot</b><div>${b.lottery_slot ?? "—"}</div>
        <b>Seed</b><div class="hash">${b.lottery_seed || "—"}</div>
        <b>Subsidy</b><div>${atomsToXfer(b.subsidy)}</div>
        <b>Fees</b><div>${atomsToXfer(b.fees)}</div>
        <b>Size</b><div>${b.size} bytes · ${b.tx_count} tx</div>
      </div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <h2>Lottery winners</h2>
      ${wins.length ? wins.map((w) => `<div class="winner">
        <div class="who">${w.rank === 0 ? `<span class="badge lottery">winner</span>` : `<span class="badge">#${(w.rank || 0) + 1}</span>`}
          ${winnerWho(w)}
        </div>
        <div class="amt">${atomsToXfer(w.amount)}</div>
      </div>`).join("") : `<div class="empty">${b.height === 0 ? "Genesis has no lottery." : "No XHB1 winners decoded for this block."}</div>`}
    </div>
    <div class="card">
      <h2>Transactions</h2>
      <table>
        <thead><tr><th>#</th><th>Txid</th><th>Type</th><th>Out</th></tr></thead>
        <tbody>
          ${(b.txs || []).map((t) => `<tr>
            <td>${t.n}</td>
            <td>${linkTx(t.txid)}</td>
            <td>${t.coinbase ? `<span class="badge lottery">coinbase</span>` : ""} ${t.identity ? `<span class="badge asset">identity @${esc(t.xid_handle)}</span>` : ""}</td>
            <td>${atomsToXfer(t.xfer_out)}</td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

async function pageTx(id) {
  const t = await api("/tx/" + encodeURIComponent(id));
  if (t.unindexed) {
    app.innerHTML = `<h1 class="page-title">Transaction</h1><div class="card"><p>Seen via node RPC, not yet in the explorer index.</p><pre class="hash">${esc(JSON.stringify(t.rpc, null, 2))}</pre></div>`;
    return;
  }
  const vin = (t.vin || []).map((v) => `<div>${v.coinbase ? `<span class="badge lottery">coinbase</span>` : linkAddr(v.address)}
    <div class="muted">${v.asset ? linkAsset(v.asset) + " · " + formatAssetAmount(v.asset_amount, v.asset) : atomsToXfer(v.value)}</div>
    ${v.spent_txid ? `<div class="faint">from ${linkTx(v.spent_txid)}:${v.spent_n}</div>` : ""}</div>`).join("");
  const vout = (t.vout || []).map((v) => `<div>${v.script_type === "nulldata" ? `<span class="badge">OP_RETURN</span>` : linkAddr(v.address)}
    <div class="muted">${v.asset ? linkAsset(v.asset) + " · " + formatAssetAmount(v.asset_amount, v.asset) + ` <span class="badge asset">${esc(v.asset_kind || "")}</span>` : atomsToXfer(v.value)}</div></div>`).join("");
  app.innerHTML = `
    <h1 class="page-title">Transaction</h1>
    <p class="sub">${copyable(t.txid)}</p>
    <div class="card" style="margin-bottom:16px">
      <div class="kv">
        <b>Block</b><div>${t.height != null ? linkBlock(t.height) : "mempool"} ${t.block_hash ? `<span class="hash">${shortHash(t.block_hash)}</span>` : ""}</div>
        <b>Time</b><div>${fmtTime(t.time)}</div>
        <b>Fee</b><div>${t.coinbase ? "—" : atomsToXfer(t.fee)}</div>
        <b>Identity</b><div>${txIdentityHtml(t)}</div>
      </div>
    </div>
    <div class="io">
      <div class="card"><h2>Inputs</h2>${vin || `<div class="empty">None</div>`}</div>
      <div class="arrow">→</div>
      <div class="card"><h2>Outputs</h2>${vout || `<div class="empty">None</div>`}</div>
    </div>`;
}

async function pageAddress(addr) {
  const a = await api("/address/" + encodeURIComponent(addr));
  app.innerHTML = `
    <h1 class="page-title">Address</h1>
    <p class="sub">${copyable(a.address)} ${a.burn ? `<span class="badge warn">${esc(a.burn)}</span>` : ""} ${a.identity ? `<span class="badge asset">${handle(a.identity.handle)}</span>` : ""}</p>
    <div class="grid stats">
      <div class="card stat"><span>Balance</span><b>${atomsToXfer(a.balance_atoms)}</b></div>
      <div class="card stat"><span>Received</span><b>${atomsToXfer(a.received_atoms)}</b></div>
      <div class="card stat"><span>Sent</span><b>${atomsToXfer(a.sent_atoms)}</b></div>
      <div class="card stat"><span>Lottery wins</span><b>${(a.lottery_wins || []).length}</b></div>
    </div>
    <div class="grid two" style="margin-top:16px">
      <div class="card">
        <h2>Transactions</h2>
        <table>
          <tbody>${(a.txs || []).map((t) => `<tr><td>${linkTx(t.txid)}</td><td>${linkBlock(t.height)}</td><td>${atomsToXfer(t.xfer_out)}</td></tr>`).join("") || `<tr><td class="empty">No txs indexed.</td></tr>`}</tbody>
        </table>
      </div>
      <div>
        <div class="card" style="margin-bottom:16px">
          <h2>Assets</h2>
          ${(a.assets || []).map((x) => `<div class="winner"><div>${linkAsset(x.name)}</div><div class="amt">${formatAssetAmount(x.amount, x.name)}</div></div>`).join("") || `<div class="empty">No assets.</div>`}
        </div>
        <div class="card">
          <h2>Lottery</h2>
          ${(a.lottery_wins || []).map((w) => `<div class="winner"><div>${linkBlock(w.height)} ${w.xaccount ? handle(w.xaccount) : `<span class="faint">no XVA1</span>`}</div><div class="amt">${atomsToXfer(w.amount)}</div></div>`).join("") || `<div class="empty">No wins.</div>`}
        </div>
      </div>
    </div>`;
}

async function pageAssets() {
  const data = await api("/assets?limit=80");
  app.innerHTML = `
    <h1 class="page-title">Assets</h1>
    <p class="sub">Identity roots are protocol-assigned from Sign in with X. Subs are NAME/CHILD. Uniques are NAME#tag. Click an asset to open its page and IPFS media.</p>
    <div class="card">
      <table class="click-rows">
        <thead><tr><th>Name</th><th>Kind</th><th>Amount</th><th>IPFS</th><th>X</th><th>Created</th></tr></thead>
        <tbody>
          ${(data.items || []).map((a) => `<tr data-href="#/asset/${encodeURIComponent(a.name)}">
            <td>${linkAsset(a.name)}</td>
            <td><span class="badge asset">${esc(a.kind || "")}</span></td>
            <td>${formatAssetAmount(a.amount, a.name, a.units)}</td>
            <td>${a.has_ipfs || a.ipfs_cid || a.ipfs ? `<span class="badge ipfs">IPFS</span>` : `<span class="faint">—</span>`}</td>
            <td>${a.x_handle ? handle(a.x_handle) : "—"}</td>
            <td>${a.created_height != null ? linkBlock(a.created_height) : "—"}</td>
          </tr>`).join("") || `<tr><td colspan="6" class="empty">No assets yet.</td></tr>`}
        </tbody>
      </table>
    </div>`;
}

function gatewayLinks(cid, extra) {
  const urls = (extra && extra.length ? extra : defaultGateways(cid)).filter((u) => /^https?:\/\//i.test(u));
  return urls.map((u) => {
    let host = u;
    try { host = new URL(u).hostname; } catch { /* keep raw */ }
    return `<a href="${esc(u)}" target="_blank" rel="noreferrer">${esc(host)}</a>`;
  }).join(" · ");
}

function renderNftAttributes(attrs) {
  if (!attrs || !attrs.length) return "";
  const cells = attrs.map((at) => {
    if (at == null) return "";
    if (typeof at !== "object") return `<div class="attr"><b>${esc(at)}</b></div>`;
    const label = at.trait_type || at.traitType || at.type || at.name || "trait";
    const value = at.value ?? at.val ?? "";
    return `<div class="attr"><span>${esc(label)}</span><b>${esc(value)}</b></div>`;
  }).join("");
  return `<div class="card" style="margin-bottom:16px"><h2>Attributes</h2><div class="attr-grid">${cells}</div></div>`;
}

function ipfsUnavailable(message) {
  return `<div class="media-empty"><span class="badge">IPFS</span><p>${esc(message)}</p></div>`;
}

function renderMediaStage(info, cid) {
  if (!info) {
    return `
      <div class="media-stage">
        <div class="media-empty">
          <span class="badge ipfs">IPFS</span>
          <p>Resolving ${copyable(cid)}…</p>
        </div>
      </div>`;
  }
  const nft = info.nft || {};
  const poster = mediaSrc(nft.image);
  const anim = mediaSrc(nft.animation);
  const audio = mediaSrc(nft.audio);
  if (info.kind === "video" || (info.kind === "json" && anim)) {
    const srcs = info.kind === "video" ? ipfsSources(cid, info.gateways) : mediaFallbacks(nft.animation, info.gateways);
    const src = srcs[0] || (info.kind === "video" ? info.url : anim);
    return `
      <div class="media-stage video">
        <video controls playsinline preload="metadata"${poster ? ` poster="${esc(poster)}"` : ""} src="${esc(src)}"${fallbackAttr(srcs)}></video>
      </div>`;
  }
  if (info.kind === "image" || poster) {
    const srcs = info.kind === "image" ? ipfsSources(cid, info.gateways) : mediaFallbacks(nft.image, info.gateways);
    const src = srcs[0] || (info.kind === "image" ? info.url : poster);
    const alt = nft.name || cid;
    return `
      <figure class="media-frame">
        <img src="${esc(src)}" alt="${esc(alt)}" data-lightbox="${esc(src)}"${fallbackAttr(srcs)} />
      </figure>`;
  }
  if (info.kind === "audio" || audio) {
    const src = info.kind === "audio" ? (ipfsSources(cid)[0] || info.url) : audio;
    return `
      <div class="media-stage">
        <div class="media-empty">
          <audio controls src="${esc(src)}"></audio>
        </div>
      </div>`;
  }
  if (info.kind === "json") {
    return `<div class="media-stage">${ipfsUnavailable("IPFS document has no image or video.")}</div>`;
  }
  if (info.kind === "text") {
    return `<pre class="media-text">${esc(info.text || "")}</pre>`;
  }
  return `
    <div class="media-stage">
      <div class="media-empty">
        <span class="badge">${esc(info.content_type || "file")}</span>
        <p><a href="${esc(info.url)}" target="_blank" rel="noreferrer">Open IPFS content</a></p>
      </div>
    </div>`;
}

function showGatewayImage(cid, urls) {
  const srcs = urls && urls.length ? urls : ipfsSources(cid);
  return `
    <figure class="media-frame">
      <img src="${esc(srcs[0])}" alt="${esc(cid)}" data-lightbox="${esc(srcs[0])}"${fallbackAttr(srcs)} />
    </figure>`;
}

async function loadAssetMedia(cid, gateways) {
  const stage = $("#asset-media");
  const metaBox = $("#asset-nft");
  if (!stage) return;
  const urls = ipfsSources(cid, gateways);
  try {
    const info = await api("/ipfs/inspect?cid=" + encodeURIComponent(cid));
    stage.innerHTML = renderMediaStage(info, cid);
    const poster = mediaSrc((info.nft || {}).image);
    const vid = stage.querySelector("video");
    if (vid && poster) {
      vid.addEventListener("error", () => {
        if (advanceIfPossible(vid)) return;
        stage.innerHTML = showGatewayImage(cid, mediaFallbacks((info.nft || {}).image, urls));
      });
    }
    const cap = $("#asset-media-cap");
    if (cap) {
      cap.innerHTML = `${esc(info.kind)} · ${esc(info.content_type || "")} · ${gatewayLinks(cid, info.gateways || gateways)}`;
    }
    if (metaBox && info.nft) {
      const nft = info.nft;
      const desc = nft.description ? `<p class="asset-desc">${esc(nft.description)}</p>` : "";
      const title = nft.name ? `<h3 class="nft-name">${esc(nft.name)}</h3>` : "";
      const ext = nft.external_url ? `<p><a href="${esc(nft.external_url)}" target="_blank" rel="noreferrer">${esc(nft.external_url)}</a></p>` : "";
      metaBox.innerHTML = (title || desc || ext || (nft.attributes || []).length)
        ? `${title}${desc}${ext}${renderNftAttributes(nft.attributes)}`
        : "";
    }
  } catch (e) {
    stage.innerHTML = showGatewayImage(cid, urls);
    const cap = $("#asset-media-cap");
    if (cap) cap.innerHTML = `${gatewayLinks(cid, gateways || urls)}`;
  }
}

function advanceIfPossible(el) {
  let extras = [];
  try { extras = JSON.parse(el.dataset.fallbacks || "[]"); } catch { extras = []; }
  if (!extras.length) return false;
  const next = extras.shift();
  el.dataset.fallbacks = JSON.stringify(extras);
  el.src = next;
  return true;
}

async function pageAsset(name) {
  const a = await api("/asset/" + encodeURIComponent(name));
  const meta = a.rpc || {};
  const units = meta.units ?? a.units ?? 0;
  const amountAtoms = meta.amount != null ? Math.round(Number(meta.amount) * 1e8) : a.amount;
  const cid = assetCid(a);
  const created = a.created_height != null ? linkBlock(a.created_height) : "—";
  app.innerHTML = `
    <p class="crumb"><a href="#/assets">Assets</a> / ${esc(a.name)}</p>
    <h1 class="page-title">${esc(a.name)}</h1>
    <p class="sub">
      <span class="badge asset">${esc(a.kind || "")}</span>
      ${cid ? `<span class="badge ipfs">IPFS</span>` : ""}
      ${a.x_handle ? handle(a.x_handle) : ""}
      ${a.identity?.address ? linkAddr(a.identity.address) : ""}
    </p>
    <div class="asset-hero">
      <div class="card media-card">
        <div id="asset-media">${cid ? renderMediaStage(null, cid) : `
          <div class="media-stage">
            <div class="media-empty">
              <span class="badge">no IPFS</span>
              <p>This asset has no IPFS hash on chain.</p>
            </div>
          </div>`}</div>
        <div class="media-caption" id="asset-media-cap">${cid ? `CID ${copyable(cid)} · ${gatewayLinks(cid, a.ipfs_gateways)}` : "No media attached."}</div>
      </div>
      <div class="card">
        <h2>Asset</h2>
        <div class="kv">
          <b>Name</b><div>${esc(a.name)}</div>
          <b>Kind</b><div>${esc(a.kind || "—")}</div>
          <b>Amount</b><div>${formatAssetAmount(amountAtoms, a.name, units)}</div>
          <b>Holders</b><div>${a.holder_count ?? (a.holders || []).length}</div>
          <b>Units</b><div>${esc(units)}</div>
          <b>Reissuable</b><div>${yesNo(meta.reissuable ?? a.reissuable)}</div>
          <b>Created</b><div>${created}${a.created_txid ? " · " + linkTx(a.created_txid) : ""}</div>
          <b>Issuer</b><div>${a.issuer ? linkAddr(a.issuer) : "—"}</div>
          <b>IPFS</b><div>${cid ? copyable(cid) : `<span class="faint">none</span>`}</div>
        </div>
      </div>
    </div>
    <div id="asset-nft"></div>
    <div class="grid two">
      <div class="card">
        <h2>Holders</h2>
        <table>
          ${(a.holders || []).map((h) => `<tr><td>${linkAddr(h.address)}</td><td>${formatAssetAmount(h.amount, a.name, units)}</td></tr>`).join("") || `<tr><td class="empty">No holders in the UTXO index.</td></tr>`}
        </table>
      </div>
      <div class="card">
        <h2>Activity</h2>
        <table>
          ${(a.activity || []).map((x) => `<tr><td>${linkBlock(x.height)}</td><td>${esc(x.kind)}</td><td>${linkTx(x.txid)}</td></tr>`).join("") || `<tr><td class="empty">No activity.</td></tr>`}
        </table>
      </div>
    </div>`;
  if (cid) loadAssetMedia(cid, a.ipfs_gateways);
}

function hashQuery() {
  const h = location.hash || "";
  const i = h.indexOf("?");
  return new URLSearchParams(i >= 0 ? h.slice(i + 1) : "");
}

function membersHash(scope, q) {
  const p = new URLSearchParams();
  if (scope && scope !== "eligible") p.set("scope", scope);
  if (q) p.set("q", q);
  const qs = p.toString();
  return "#/members" + (qs ? "?" + qs : "");
}

function memberStatusBadges(m) {
  if (m.eligible) {
    return `${m.in_hat ? `<span class="badge lottery">in hat</span>` : ""} ${m.heartbeat ? `<span class="badge ok">heartbeat</span>` : ""}`.trim()
      || `<span class="badge ok">eligible</span>`;
  }
  return `<span class="badge">offline</span>`;
}

async function pageIdentity(handleName) {
  const p = await api("/handle/" + encodeURIComponent(handleName));
  const name = p.handle || handleName;
  app.innerHTML = `
    <p class="crumb"><a href="${membersHash("all", "")}">Members</a> / @${esc(name)}</p>
    <h1 class="page-title">@${esc(name)}</h1>
    <p class="sub">${p.found ? "Lottery identity from coinbase XVA1 (the handle stays; payout addresses change)." : "This handle is not in the indexed hat or live eligible set yet."}</p>
    <div class="grid stats">
      <div class="card stat"><span>Now</span><b>${p.eligible ? "eligible" : "offline"}</b></div>
      <div class="card stat"><span>Wins</span><b>${p.wins || 0}</b></div>
      <div class="card stat"><span>Earned</span><b>${atomsToXfer(p.earned || 0)}</b></div>
      <div class="card stat"><span>Hat blocks</span><b>${p.hat_blocks || 0}</b></div>
    </div>
    <div class="card" style="margin-top:16px">
      <h2>Identity</h2>
      <div class="kv">
        <b>Handle</b><div>${handle(name)} ${memberStatusBadges(p)}</div>
        <b>In this minute’s hat</b><div>${yesNo(p.in_hat)}</div>
        <b>Heartbeat</b><div>${yesNo(p.heartbeat)}</div>
        <b>Address</b><div>${p.address ? linkAddr(p.address) : `<span class="faint">—</span>`}</div>
        <b>Asset root</b><div>${p.asset ? linkAsset(p.asset) : `<span class="faint">—</span>`}</div>
        <b>First hat</b><div>${p.first_hat != null ? linkBlock(p.first_hat) : "—"}</div>
        <b>Last hat</b><div>${p.last_hat != null ? linkBlock(p.last_hat) : "—"}</div>
        <b>Last seen</b><div>${p.last_seen ? timeAgo(p.last_seen) : "—"}</div>
      </div>
    </div>
    <div class="grid two" style="margin-top:16px">
      <div class="card">
        <h2>Wins</h2>
        <table>
          <thead><tr><th>Block</th><th>Paid</th></tr></thead>
          <tbody>
            ${(p.wins_recent || []).map((w) => `<tr><td>${linkBlock(w.height)}</td><td>${atomsToXfer(w.amount)}</td></tr>`).join("") || `<tr><td colspan="2" class="empty">No XVA1 wins indexed for this handle.</td></tr>`}
          </tbody>
        </table>
      </div>
      <div class="card">
        <h2>Recent hat</h2>
        <table>
          <thead><tr><th>Block</th><th>n</th></tr></thead>
          <tbody>
            ${(p.appearances || []).map((a) => `<tr><td>${linkBlock(a.height)}</td><td>${a.n ?? "—"}</td></tr>`).join("") || `<tr><td colspan="2" class="empty">Not seen in an indexed XVA1 hat.</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>`;
}

async function pageMembers() {
  const params = hashQuery();
  const q = (params.get("q") || "").trim();
  const scope = params.get("scope") === "all" ? "all" : "eligible";
  const data = await api(`/lottery/members?q=${encodeURIComponent(q)}&scope=${scope}&limit=400`);
  const items = data.items || [];
  app.innerHTML = `
    <h1 class="page-title">Eligible members</h1>
    <p class="sub">X Verified <code>@handles</code> in this minute’s hat or with a live heartbeat. Search any handle — including people who were eligible earlier and are offline now.</p>
    <div class="card">
      <div class="toolbar">
        <form class="search" id="member-search">
          <input id="member-q" type="search" value="${esc(q)}" placeholder="Search @handle" autocomplete="off" />
          <button type="submit">Search</button>
        </form>
        <div class="tabs" id="member-tabs">
          <button type="button" data-scope="eligible" class="${scope === "eligible" ? "on" : ""}">Eligible now (${data.eligible_total || 0})</button>
          <button type="button" data-scope="all" class="${scope === "all" ? "on" : ""}">All handles (${data.known_total || 0})</button>
        </div>
      </div>
      <p class="member-meta">${items.length} shown${q ? ` for “${esc(q)}”` : ""} · ${scope === "eligible" ? "live eligible only" : "every indexed handle"}</p>
      <table class="click-rows">
        <thead><tr><th>Handle</th><th>Now</th><th>Wins</th><th>Earned</th><th>Last hat</th></tr></thead>
        <tbody>
          ${items.map((m) => `<tr data-href="#/identity/${encodeURIComponent(m.handle)}">
            <td>${handle(m.handle)}</td>
            <td>${memberStatusBadges(m)}</td>
            <td>${m.wins || 0}</td>
            <td>${atomsToXfer(m.earned || 0)}</td>
            <td>${m.last_hat != null ? linkBlock(m.last_hat) : "—"}</td>
          </tr>`).join("") || `<tr><td colspan="5" class="empty">${q ? "No handle matches that search." : (scope === "eligible" ? "No eligible members this minute. Try All handles, or wait for the next hat." : "No handles indexed yet.")}</td></tr>`}
        </tbody>
      </table>
    </div>`;
  const form = $("#member-search");
  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      location.hash = membersHash(scope, $("#member-q").value.trim());
    });
  }
  document.querySelectorAll("#member-tabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      location.hash = membersHash(btn.getAttribute("data-scope"), ($("#member-q") && $("#member-q").value.trim()) || q);
    });
  });
}

async function pageLottery() {
  const L = await api("/lottery");
  const live = L.live || {};
  const nodes = L.nodes || [];
  const active = L.active_handles || nodes.map((n) => n.xaccount).filter(Boolean);
  const liveHint = active.length
    ? active.map((h) => `<div class="winner">
          <div class="who"><span class="badge">hat</span> ${handle(h)}</div>
        </div>`).join("")
    : (L.rpc_connected
        ? `<div class="empty">No XVA1 handles this minute. The baked seed prints every main block; wait for the next coinbase.</div>`
        : `<div class="empty">Connect a local wallet (see docs/SETUP.md). History below is from blocks already indexed.</div>`);
  app.innerHTML = `
    ${nodeBanner()}
    <h1 class="page-title">Lottery</h1>
    <p class="sub">The baked seed prints every main block. Coinbase <code>XVA1</code> is the public roll (handle + id + stamp). The winner is who got paid — history and the leaderboard key on that <code>@handle</code>, not the payout address (addresses change; the handle does not). Live “active now” is <code>stamped_handles</code> when the node has it, otherwise the last indexed block’s XVA1 hat. Height 0 is not a payday.</p>
    <div class="grid two">
      ${lotteryCard(live, nodes, L.winner_handles)}
      <div class="card">
        <h2>Active now</h2>
        ${liveHint}
        <p class="muted" style="margin-top:12px">Eligible locally: ${live.local_eligible ? "yes" : "no"} · verified: ${live.local_x_verified ? "yes" : "no"} · @${esc(live.local_xaccount || "—")}</p>
        <div class="row-actions"><a href="#/members">Browse all eligible members →</a></div>
      </div>
    </div>
    <div class="grid two" style="margin-top:16px">
      <div class="card">
        <h2>History</h2>
        <table>
          <thead><tr><th>Block</th><th>Who</th><th>Paid</th></tr></thead>
          <tbody>
            ${(L.history || []).map((h) => {
              const w = (h.winners || [])[0] || {};
              return `<tr><td>${linkBlock(h.height)}</td><td>${winnerWho(w)}</td><td>${atomsToXfer(w.amount)}</td></tr>`;
            }).join("") || `<tr><td colspan="3" class="empty">No lottery blocks yet. Height 0 is genesis (unspendable, not a win). Height 1 is the first draw.</td></tr>`}
          </tbody>
        </table>
      </div>
      <div class="card">
        <h2>Leaderboard</h2>
        <table>
          <thead><tr><th>Who</th><th>Wins</th><th>Earned</th></tr></thead>
          <tbody>
            ${(L.leaders || []).map((x) => `<tr>
              <td>${x.xaccount ? handle(x.xaccount) : `<span class="faint">no XVA1</span>`}</td>
              <td>${x.wins}</td><td>${atomsToXfer(x.earned)}</td>
            </tr>`).join("") || `<tr><td colspan="3" class="empty">No XVA1 winners indexed.</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>`;
  tickCountdown();
}

async function pageRich() {
  const data = await api("/rich?limit=50");
  app.innerHTML = `
    <h1 class="page-title">XFER holders</h1>
    <div class="card">
      <table>
        <thead><tr><th>#</th><th>Address</th><th>Balance</th></tr></thead>
        <tbody>
          ${(data.items || []).map((r, i) => `<tr><td>${i + 1}</td><td>${linkAddr(r.address)}</td><td>${atomsToXfer(r.balance)}</td></tr>`).join("") || `<tr><td colspan="3" class="empty">UTXO set is empty until blocks are indexed.</td></tr>`}
        </tbody>
      </table>
    </div>`;
}

async function pageMempool() {
  const m = await api("/mempool");
  app.innerHTML = `
    <h1 class="page-title">Mempool</h1>
    <p class="sub">${m.info ? `${m.info.size || m.count || 0} tx · ${m.info.bytes || 0} bytes` : "Node mempool"}</p>
    <div class="card">
      <table>
        <thead><tr><th>Txid</th><th>Vin</th><th>Vout</th></tr></thead>
        <tbody>
          ${(m.txs || []).map((t) => `<tr><td>${linkTx(t.txid)}</td><td>${t.vin ?? "—"}</td><td>${t.vout ?? "—"}</td></tr>`).join("") || `<tr><td colspan="3" class="empty">Empty.</td></tr>`}
        </tbody>
      </table>
    </div>`;
}

async function pageNetwork() {
  const [p, s] = await Promise.all([api("/peers"), refreshStatus().then(() => STATUS)]);
  const chain = (s && s.chain) || {};
  app.innerHTML = `
    <h1 class="page-title">Network</h1>
    <div class="card" style="margin-bottom:16px">
      <div class="kv">
        <b>Chain</b><div>${esc(s?.network_label || "mainnet")}</div>
        <b>Ticker</b><div>XFER</div>
        <b>P2P id</b><div>XFER — 4 bytes on every peer message so this chain is not mixed with Bitcoin or Ravencoin</div>
        <b>RPC</b><div>${s?.rpc_connected ? `connected :${s.rpc_port}` : "offline"}</div>
        <b>Best hash</b><div class="hash">${esc(chain.bestblockhash || s?.best_hash || "—")}</div>
        <b>Verification</b><div>${chain.verificationprogress != null ? (chain.verificationprogress * 100).toFixed(2) + "%" : "—"}</div>
        <b>P2P</b><div>port 38443 · no DNS seeds · peers join with addnode/seednode</div>
      </div>
    </div>
    <div class="card">
      <h2>Peers</h2>
      <table>
        <thead><tr><th>Addr</th><th>Agent</th><th>In</th><th>Height</th></tr></thead>
        <tbody>
          ${(p.peers || []).map((x) => `<tr>
            <td class="hash">${esc(x.addr)}</td>
            <td>${esc(x.subver)}</td>
            <td>${x.inbound ? "in" : "out"}</td>
            <td>${x.synced_blocks ?? x.startingheight ?? "—"}</td>
          </tr>`).join("") || `<tr><td colspan="4" class="empty">No peers (or RPC off).</td></tr>`}
        </tbody>
      </table>
    </div>`;
}

async function pageSearch(q) {
  const data = await api("/search?q=" + encodeURIComponent(q));
  const results = data.results || [];
  if (results.length === 1) {
    const r = results[0];
    const map = { block: "block", tx: "tx", address: "address", asset: "asset", identity: "identity" };
    if (map[r.type]) {
      location.hash = `#/${map[r.type]}/${encodeURIComponent(r.id)}`;
      return;
    }
  }
  app.innerHTML = `
    <h1 class="page-title">Search</h1>
    <p class="sub">${esc(q)} · ${esc(data.kind)}</p>
    <div class="card">
      ${results.map((r) => `<div class="winner"><div><span class="badge">${esc(r.type)}</span>
        <a href="#/${r.type === "tx" ? "tx" : r.type === "block" ? "block" : r.type === "address" ? "address" : r.type === "asset" ? "asset" : "identity"}/${encodeURIComponent(r.id)}">${esc(r.label)}</a>
      </div></div>`).join("") || `<div class="empty">Nothing indexed matches that query.</div>`}
    </div>`;
}

const routes = [
  [/^#\/?$/, pageHome],
  [/^#\/blocks$/, pageBlocks],
  [/^#\/block\/(.+)$/, (m) => pageBlock(decodeURIComponent(m[1]))],
  [/^#\/tx\/(.+)$/, (m) => pageTx(decodeURIComponent(m[1]))],
  [/^#\/address\/(.+)$/, (m) => pageAddress(decodeURIComponent(m[1]))],
  [/^#\/assets$/, pageAssets],
  [/^#\/asset\/(.+)$/, (m) => pageAsset(decodeURIComponent(m[1]))],
  [/^#\/identity\/(.+)$/, (m) => pageIdentity(decodeURIComponent(m[1]))],
  [/^#\/members(?:\?.*)?$/, pageMembers],
  [/^#\/lottery$/, pageLottery],
  [/^#\/rich$/, pageRich],
  [/^#\/mempool$/, pageMempool],
  [/^#\/network$/, pageNetwork],
  [/^#\/search\/(.+)$/, (m) => pageSearch(decodeURIComponent(m[1]))],
];

async function route() {
  setNav();
  const hash = location.hash || "#/";
  app.innerHTML = `<div class="loading">Loading…</div>`;
  try {
    await refreshStatus();
    for (const [re, fn] of routes) {
      const m = hash.match(re);
      if (m) {
        await fn(m);
        return;
      }
    }
    app.innerHTML = `<div class="err">Unknown page.</div>`;
  } catch (e) {
    app.innerHTML = `<div class="err">${esc(e.message || e)}</div>`;
  }
}

$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#q").value.trim();
  if (!q) return;
  location.hash = "#/search/" + encodeURIComponent(q);
});

window.addEventListener("hashchange", route);
route();
setInterval(() => {
  tickCountdown();
}, 1000);
setInterval(() => {
  refreshStatus();
  const h = location.hash || "#/";
  if (h === "#/" || h === "#/lottery" || (h.startsWith("#/members") && document.activeElement?.id !== "member-q")) route();
}, 8000);
