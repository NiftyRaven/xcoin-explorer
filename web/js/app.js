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
    a.classList.toggle("active", href === "#/" ? hash === "#/" : hash.startsWith(href));
  });
}

function copyable(text) {
  return `<code class="copy" title="Copy" data-copy="${esc(text)}">${esc(text)}</code>`;
}

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-copy]");
  if (!el) return;
  navigator.clipboard.writeText(el.dataset.copy).catch(() => {});
  el.classList.add("ok");
  setTimeout(() => el.classList.remove("ok"), 600);
});

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

function lotteryCard(live, nodes) {
  const L = live || {};
  const winners = L.winners || [];
  const rewards = L.rewards || [];
  const nodeMap = {};
  (nodes || []).forEach((n) => { nodeMap[(n.id || "").toLowerCase()] = n; });
  const rows = winners.map((id, i) => {
    const n = nodeMap[String(id).toLowerCase()] || {};
    const who = n.xaccount ? handle(n.xaccount) : `<span class="hash">${shortHash(id)}</span>`;
    const tag = i === 0 ? `<span class="badge lottery">producer</span>` : `<span class="badge">winner</span>`;
    return `<div class="winner"><div class="who">${tag} ${who}<span class="faint hash">${esc(n.address || id)}</span></div><div class="amt">${atomsToXfer(rewards[i] || 0)}</div></div>`;
  }).join("") || `<div class="empty">No eligible X Verified nodes in the live draw yet.</div>`;
  const slot = L.slot;
  return `
    <div class="card">
      <h2>This minute’s lottery</h2>
      <div class="countdown" id="cd">—:—</div>
      <p class="muted">Height ${L.height ?? "—"} · slot ${slot ?? "—"} · ${L.active_nodes ?? 0} active · ${L.winner_count ?? 1} winner(s)</p>
      ${rows}
      <div class="row-actions"><a href="#/lottery">Full lottery →</a></div>
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
    const who = w.xaccount ? handle(w.xaccount) : `<span class="faint">no XVA1</span>`;
    return `<tr><td>${linkBlock(h.height)}</td><td>${who}</td><td>${atomsToXfer(w.amount)}</td><td class="muted">${timeAgo(h.time)}</td></tr>`;
  }).join("");
  app.innerHTML = `
    ${nodeBanner()}
    ${statsRow()}
    <div class="grid home" style="margin-top:16px">
      <div class="card">
        <h2>Latest blocks</h2>
        <table>
          <thead><tr><th>Height</th><th>Time</th><th>Tx</th><th>Producer</th></tr></thead>
          <tbody>
            ${(blocks.items || []).map((b) => `<tr>
              <td>${linkBlock(b.height)}</td>
              <td class="muted">${timeAgo(b.time)}</td>
              <td>${b.tx_count}</td>
              <td>${linkAddr(b.producer)}</td>
            </tr>`).join("") || `<tr><td colspan="4" class="empty">No blocks indexed yet. Genesis appears once the node is up.</td></tr>`}
          </tbody>
        </table>
        <div class="row-actions"><a href="#/blocks">All blocks →</a></div>
      </div>
      ${lotteryCard(lottery.live, lottery.nodes)}
    </div>
    <div class="card" style="margin-top:16px">
      <h2>Recent lottery producers</h2>
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
        <thead><tr><th>Height</th><th>Hash</th><th>Time</th><th>Tx</th><th>Winners</th><th>Producer</th></tr></thead>
        <tbody>
          ${(data.items || []).map((b) => `<tr>
            <td>${linkBlock(b.height)}</td>
            <td class="hash"><a class="hash" href="#/block/${b.hash}">${shortHash(b.hash)}</a></td>
            <td class="muted">${fmtTime(b.time)}</td>
            <td>${b.tx_count}</td>
            <td>${b.winner_count || (b.height === 0 ? "—" : "0")}</td>
            <td>${linkAddr(b.producer)}</td>
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
        <div class="who">${w.is_producer ? `<span class="badge lottery">winner</span>` : `<span class="badge">#${(w.rank || 0) + 1}</span>`}
          ${w.xaccount ? handle(w.xaccount) : `<span class="faint">no XVA1 handle</span>`}
          <span class="faint">${w.address ? linkAddr(w.address) : ""}</span>
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
    <div class="muted">${v.asset ? linkAsset(v.asset) + " · " + atomsToXfer(v.asset_amount) : atomsToXfer(v.value)}</div>
    ${v.spent_txid ? `<div class="faint">from ${linkTx(v.spent_txid)}:${v.spent_n}</div>` : ""}</div>`).join("");
  const vout = (t.vout || []).map((v) => `<div>${v.script_type === "nulldata" ? `<span class="badge">OP_RETURN</span>` : linkAddr(v.address)}
    <div class="muted">${v.asset ? linkAsset(v.asset) + " · " + atomsToXfer(v.asset_amount) + ` <span class="badge asset">${esc(v.asset_kind || "")}</span>` : atomsToXfer(v.value)}</div></div>`).join("");
  app.innerHTML = `
    <h1 class="page-title">Transaction</h1>
    <p class="sub">${copyable(t.txid)}</p>
    <div class="card" style="margin-bottom:16px">
      <div class="kv">
        <b>Block</b><div>${t.height != null ? linkBlock(t.height) : "mempool"} ${t.block_hash ? `<span class="hash">${shortHash(t.block_hash)}</span>` : ""}</div>
        <b>Time</b><div>${fmtTime(t.time)}</div>
        <b>Fee</b><div>${t.coinbase ? "—" : atomsToXfer(t.fee)}</div>
        <b>Identity</b><div>${t.xid_handle ? handle(t.xid_handle) : "—"}</div>
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
          ${(a.assets || []).map((x) => `<div class="winner"><div>${linkAsset(x.name)}</div><div class="amt">${atomsToXfer(x.amount)}</div></div>`).join("") || `<div class="empty">No assets.</div>`}
        </div>
        <div class="card">
          <h2>Lottery</h2>
          ${(a.lottery_wins || []).map((w) => `<div class="winner"><div>${linkBlock(w.height)} ${w.is_producer ? `<span class="badge lottery">producer</span>` : ""}</div><div class="amt">${atomsToXfer(w.amount)}</div></div>`).join("") || `<div class="empty">No wins.</div>`}
        </div>
      </div>
    </div>`;
}

async function pageAssets() {
  const data = await api("/assets?limit=80");
  app.innerHTML = `
    <h1 class="page-title">Assets</h1>
    <p class="sub">Identity roots are protocol-assigned from Sign in with X. Subs are NAME/CHILD. Uniques are NAME#tag.</p>
    <div class="card">
      <table>
        <thead><tr><th>Name</th><th>Kind</th><th>Amount</th><th>X</th><th>Created</th></tr></thead>
        <tbody>
          ${(data.items || []).map((a) => `<tr>
            <td>${linkAsset(a.name)}</td>
            <td><span class="badge asset">${esc(a.kind || "")}</span></td>
            <td>${atomsToXfer(a.amount)}</td>
            <td>${a.x_handle ? handle(a.x_handle) : "—"}</td>
            <td>${a.created_height != null ? linkBlock(a.created_height) : "—"}</td>
          </tr>`).join("") || `<tr><td colspan="5" class="empty">No assets yet.</td></tr>`}
        </tbody>
      </table>
    </div>`;
}

async function pageAsset(name) {
  const a = await api("/asset/" + encodeURIComponent(name));
  const meta = a.rpc || {};
  app.innerHTML = `
    <h1 class="page-title">${esc(a.name)}</h1>
    <p class="sub"><span class="badge asset">${esc(a.kind)}</span> ${a.x_handle ? handle(a.x_handle) : ""} ${a.identity?.address ? linkAddr(a.identity.address) : ""}</p>
    <div class="grid stats">
      <div class="card stat"><span>Amount</span><b>${atomsToXfer(meta.amount != null ? Math.round(meta.amount * 1e8) : a.amount)}</b></div>
      <div class="card stat"><span>Holders</span><b>${a.holder_count ?? (a.holders || []).length}</b></div>
      <div class="card stat"><span>Units</span><b>${meta.units ?? a.units ?? 0}</b></div>
      <div class="card stat"><span>Reissuable</span><b>${(meta.reissuable ?? a.reissuable) ? "yes" : "no"}</b></div>
    </div>
    <div class="grid two" style="margin-top:16px">
      <div class="card">
        <h2>Holders</h2>
        <table>
          ${(a.holders || []).map((h) => `<tr><td>${linkAddr(h.address)}</td><td>${atomsToXfer(h.amount)}</td></tr>`).join("") || `<tr><td class="empty">No holders in the UTXO index.</td></tr>`}
        </table>
      </div>
      <div class="card">
        <h2>Activity</h2>
        <table>
          ${(a.activity || []).map((x) => `<tr><td>${linkBlock(x.height)}</td><td>${esc(x.kind)}</td><td>${linkTx(x.txid)}</td></tr>`).join("") || `<tr><td class="empty">No activity.</td></tr>`}
        </table>
      </div>
    </div>`;
}

async function pageIdentity(handleName) {
  const q = await api("/search?q=" + encodeURIComponent("@" + handleName));
  const ident = (q.results || []).find((r) => r.type === "identity");
  if (ident && ident.address) {
    location.hash = "#/address/" + encodeURIComponent(ident.address);
    return;
  }
  if (ident && ident.asset) {
    location.hash = "#/asset/" + encodeURIComponent(ident.asset);
    return;
  }
  app.innerHTML = `<h1 class="page-title">@${esc(handleName)}</h1><div class="card empty">No on-chain identity root indexed for this handle yet.</div>`;
}

async function pageLottery() {
  const L = await api("/lottery");
  const live = L.live || {};
  const nodes = L.nodes || [];
  const liveHint = L.rpc_connected
    ? (nodes.length
        ? nodes.map((n) => `<div class="winner">
          <div class="who">${n.local ? `<span class="badge ok">local</span>` : `<span class="badge">node</span>`}
            ${n.xaccount ? handle(n.xaccount) : `<span class="hash">${shortHash(n.id)}</span>`}
            <span class="faint">${linkAddr(n.address)}</span>
          </div>
          <div class="muted">${n.lastseen ? timeAgo(n.lastseen) : ""}</div>
        </div>`).join("")
        : `<div class="empty">No heartbeats in the last 180s. Lottery needs a signed-in X Verified wallet on a connected peer.</div>`)
    : `<div class="empty">Live heartbeats need a connected wallet (see docs/SETUP.md). History below is from blocks already indexed.</div>`;
  app.innerHTML = `
    ${nodeBanner()}
    <h1 class="page-title">Lottery</h1>
    <p class="sub">Every minute, X Verified running nodes are drawn. Height ≥ 1 coinbase carries <code>XVA1</code> (handle + id + stamp) — history and the leaderboard use that handle, not the payout address (addresses change; @handle does not). Live “active now” is this minute only. Height 0 is not a payday.</p>
    <div class="grid two">
      ${lotteryCard(live, nodes)}
      <div class="card">
        <h2>Active nodes now</h2>
        ${liveHint}
        <p class="muted" style="margin-top:12px">Eligible locally: ${live.local_eligible ? "yes" : "no"} · verified: ${live.local_x_verified ? "yes" : "no"} · @${esc(live.local_xaccount || "—")}</p>
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
              return `<tr><td>${linkBlock(h.height)}</td><td>${w.xaccount ? handle(w.xaccount) : `<span class="faint">no XVA1</span>`}</td><td>${atomsToXfer(w.amount)}</td></tr>`;
            }).join("") || `<tr><td colspan="3" class="empty">No lottery blocks yet. Height 0 is genesis (unspendable, not a win). Height 1 is the first draw.</td></tr>`}
          </tbody>
        </table>
      </div>
      <div class="card">
        <h2>Leaderboard</h2>
        <table>
          <thead><tr><th>Who</th><th>Wins</th><th>Produced</th><th>Earned</th></tr></thead>
          <tbody>
            ${(L.leaders || []).map((x) => `<tr>
              <td>${x.xaccount ? handle(x.xaccount) : esc(x.who || "")}</td>
              <td>${x.wins}</td><td>${x.produced}</td><td>${atomsToXfer(x.earned)}</td>
            </tr>`).join("") || `<tr><td colspan="4" class="empty">No XVA1 winners indexed.</td></tr>`}
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
  if (h === "#/" || h === "#/lottery") route();
}, 8000);
