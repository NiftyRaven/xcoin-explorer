const $ = (sel, el = document) => el.querySelector(sel);
const app = $("#app");
const DEFAULT_BLOCK_TIME_SECONDS = 60;
let STATUS = null;
let pollTimer = null;
let lastTipKey = "";
let lastIndexing = null;
let liveRefreshInFlight = false;

async function api(path) {
  const r = await fetch("/api" + path);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const err = new Error(body.error || r.statusText);
    err.status = r.status;
    throw err;
  }
  const data = await r.json();
  rememberLabels(data);
  return data;
}

// Address -> X handles with the proof for each (from the API's `labels`). See explorer/links.py.
const LABELS = new Map();
const PROOF_NAMES = {
  claim: "holds the claimed root",
  wallet: "wallet-confirmed",
  owner: "owner token",
  cospend: "same wallet (spent together)",
};

function rememberLabels(data) {
  const labels = data && typeof data === "object" ? data.labels : null;
  if (!labels || typeof labels !== "object") return;
  for (const [addr, rows] of Object.entries(labels)) {
    if (Array.isArray(rows)) LABELS.set(addr, rows);
  }
}

function labelHint(row) {
  return `@${row.handle}: ${PROOF_NAMES[row.proof] || row.proof}${row.detail ? ` (${row.detail})` : ""}`;
}

function handleChips(addr) {
  const rows = LABELS.get(addr) || [];
  if (!rows.length) return "";
  const chips = rows.map((row) => `<a class="addr-handle" href="https://x.com/${encodeURIComponent(row.handle)}" target="_blank" rel="noopener noreferrer" title="${esc(labelHint(row))}">@${esc(row.handle)}</a>`);
  return `<span class="addr-handles">${chips.join(" ")}<span class="faint"> · </span></span>`;
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

function clipMiddle(value, left = 10, right = 8) {
  const s = value == null ? "" : String(value);
  if (s.length <= left + right + 1) return s;
  return s.slice(0, left) + "…" + s.slice(-right);
}

function copyButton(text) {
  const full = text == null ? "" : String(text);
  if (!full) return "";
  return `<button type="button" class="copy-btn" data-copy="${esc(full)}">Copy</button>`;
}

function idBody(value, cls, href) {
  return href
    ? `<a class="${cls}" href="${esc(href)}">${esc(value)}</a>`
    : `<span class="${cls}">${esc(value)}</span>`;
}

function idHtml(text, opts = {}) {
  const full = text == null ? "" : String(text);
  if (!full) return `<span class="faint">—</span>`;
  const href = opts.href ? String(opts.href) : "";
  const shown = clipMiddle(full, opts.left || 10, opts.right || 8);
  if (opts.full || shown === full) {
    return `<span class="id-line id-open"><span class="id-full">${idBody(full, "id-full-text break-anywhere", href)}</span>${copyButton(full)}</span>`;
  }
  return `<span class="id-line can-expand">${idBody(shown, "mono-clip", href)}<span class="id-full">${idBody(full, "id-full-text break-anywhere", href)}</span><button type="button" class="copy-btn id-toggle" data-id-toggle aria-expanded="false">Full</button>${copyButton(full)}</span>`;
}

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve) => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch { /* ignore */ }
    ta.remove();
    resolve();
  });
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

const PINATA_VIEW_GATEWAY = "xfer.mypinata.cloud";

function pinataViewUrl(cid) {
  return "https://" + PINATA_VIEW_GATEWAY + "/ipfs/" + cid;
}

function ipfsContentUrl(cid) {
  return "/api/ipfs/content/" + encodeURIComponent(cid);
}

function defaultGateways(cid) {
  return [
    pinataViewUrl(cid),
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
  add(pinataViewUrl(cid));
  add(ipfsContentUrl(cid));
  (extra || []).forEach(add);
  defaultGateways(cid).forEach(add);
  return out;
}

function explorerAssetCid(a) {
  if (typeof assetCid === "function") return assetCid(a);
  if (!a) return "";
  return a.ipfs_cid || a.ipfs || (a.rpc && (a.rpc.ipfs_hash || a.rpc.ipfs)) || "";
}

function assetListIpfsCell(a) {
  const cid = explorerAssetCid(a);
  if (!cid) return '<span class="faint">—</span>';
  const srcs = ipfsSources(cid);
  const name = a && a.name ? String(a.name) : "";
  const href = name ? "#/asset/" + encodeURIComponent(name) : ipfsContentUrl(cid);
  return (
    `<a class="asset-ipfs" href="${esc(href)}">` +
    `<img class="asset-thumb" src="${esc(srcs[0])}" alt="" data-ipfs-json="${esc(cid)}"${fallbackAttr(srcs)} />` +
    `<span class="badge ipfs">IPFS</span></a>`
  );
}

async function resolveThumb(img, cid) {
  try {
    const info = await api("/ipfs/inspect?cid=" + encodeURIComponent(cid));
    const media = mediaCidFromInfo(info, cid);
    if (!media || media === cid) {
      img.hidden = true;
      return;
    }
    const urls = ipfsSources(media);
    img.hidden = false;
    img.dataset.fallbacks = JSON.stringify(urls.slice(1));
    img.src = urls[0];
  } catch {
    img.hidden = true;
  }
}

function mediaCidFromInfo(info, jsonCid) {
  if (!info) return "";
  if (info.kind === "image") return jsonCid || info.cid || "";
  const viewCid = info.view && info.view.image && info.view.image.cid;
  if (viewCid) return viewCid;
  const nftCid = info.nft && info.nft.image && info.nft.image.cid;
  return nftCid || "";
}

function mediaSrc(ref) {
  if (!ref) return "";
  if (typeof ref === "object") {
    if (ref.cid) return ipfsSources(ref.cid)[0];
    const src = ref.src || "";
    if (src.startsWith("/api/ipfs/")) return src;
    return safeHttpClient(src);
  }
  if (/^https?:\/\//i.test(ref)) return safeHttpClient(ref);
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
function linkTx(id, opts = {}) {
  if (!id) return `<span class="faint">—</span>`;
  return idHtml(id, { href: "#/tx/" + encodeURIComponent(id), full: !!opts.full });
}
function linkAddr(a, opts = {}) {
  if (!a) return `<span class="faint">—</span>`;
  return handleChips(a) + idHtml(a, { href: "#/address/" + encodeURIComponent(a), left: 8, right: 6, full: !!opts.full });
}
function assetApiPath(name) {
  return "/asset/" + String(name || "").split("/").map((part) => encodeURIComponent(part)).join("/");
}
function linkAsset(n) {
  if (!n) return "—";
  const full = String(n);
  return idHtml(full, { href: "#/asset/" + encodeURIComponent(full), left: 16, right: 8 });
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
          || (href === "#/stats" && hash.startsWith("#/stats"))
    );
  });
}

function copyable(text) {
  return idHtml(text, { full: true });
}

document.addEventListener("click", (e) => {
  const toggle = e.target.closest("[data-id-toggle]");
  if (toggle) {
    e.preventDefault();
    e.stopPropagation();
    const line = toggle.closest(".id-line");
    if (!line) return;
    const open = !line.classList.contains("id-open");
    line.classList.toggle("id-open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.textContent = open ? "Hide" : "Full";
    return;
  }
  const el = e.target.closest("[data-copy], [data-copy-from]");
  if (el) {
    e.preventDefault();
    e.stopPropagation();
    let text = el.dataset.copy || "";
    const from = el.getAttribute("data-copy-from");
    if (from) {
      const node = document.getElementById(from);
      text = node ? node.textContent || "" : "";
    }
    copyText(text).then(() => {
      const prev = el.textContent;
      el.textContent = "Copied";
      el.classList.add("ok");
      setTimeout(() => {
        el.textContent = prev === "Copied" ? "Copy" : prev;
        el.classList.remove("ok");
      }, 900);
    }).catch(() => {});
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
  if (!el || (el.tagName !== "IMG" && el.tagName !== "VIDEO" && el.tagName !== "AUDIO")) return;
  if (advanceIfPossible(el)) {
    e.stopImmediatePropagation();
    if (el.hasAttribute("data-lightbox")) el.setAttribute("data-lightbox", el.src);
    return;
  }
  if (el.classList && el.classList.contains("asset-thumb")) {
    const cid = el.getAttribute("data-ipfs-json") || "";
    if (cid && !el.dataset.metaTried) {
      el.dataset.metaTried = "1";
      resolveThumb(el, cid);
      return;
    }
    el.hidden = true;
    return;
  }
  const stage = el.closest && el.closest("#asset-media");
  if (!stage) return;
  if (el.tagName === "IMG" && !el.dataset.triedVideo) {
    const cid = el.getAttribute("alt") || "";
    const urls = ipfsSources(normalizeIpfsClient(cid) || cid);
    if (urls.length && normalizeIpfsClient(cid)) {
      stage.innerHTML = `<div class="media-stage video"><video controls playsinline src="${esc(urls[0])}"${fallbackAttr(urls)}></video></div>`;
      return;
    }
  }
  if (!stage.querySelector(".media-empty")) {
    stage.innerHTML = `<div class="media-stage">${ipfsUnavailable("This file did not load. Try a link below.")}</div>`;
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

function blockTimeSeconds() {
  const n = Number(STATUS?.block_time_seconds ?? STATUS?.slot_seconds);
  return Number.isFinite(n) && n > 0 ? n : DEFAULT_BLOCK_TIME_SECONDS;
}

function applyStatusPills(s) {
  if (!s) return;
  const pill = $("#pill-rpc");
  const h = $("#pill-height");
  if (pill) {
    if (s.rpc_connected) {
      if (s.network_label || s.network) {
        pill.textContent = `node · ${s.network_label || s.network}`;
      }
      pill.className = "pill ok";
    } else {
      pill.textContent = "node offline";
      pill.className = "pill bad";
    }
  }
  if (h) {
    const height = s.tip ?? s.height ?? s.indexed_height;
    h.textContent = `height ${height < 0 || height == null ? "—" : height}${s.indexing ? " …" : ""}`;
  }
  if (s.network_label) $("#foot-net").textContent = s.network_label;
}

async function refreshStatus() {
  try {
    STATUS = await api("/status");
    applyStatusPills(STATUS);
  } catch (e) {
    $("#pill-rpc").textContent = "explorer error";
    $("#pill-rpc").className = "pill bad";
  }
}

function tipKey(tip) {
  return `${tip.hash || ""}:${tip.height ?? ""}`;
}

function livePagesNeedReload() {
  const h = location.hash || "#/";
  return h === "#/" || h === "#/lottery" || (h.startsWith("#/members") && document.activeElement?.id !== "member-q");
}

async function pollTipAndMaybeReload() {
  if (liveRefreshInFlight) return;
  liveRefreshInFlight = true;
  try {
    const tip = await api("/tip");
    if (STATUS) {
      STATUS.tip = tip.height ?? STATUS.tip;
      STATUS.indexed_height = tip.indexed_height ?? STATUS.indexed_height;
      STATUS.indexing = tip.indexing;
      STATUS.best_hash = tip.hash || STATUS.best_hash;
      STATUS.slot_seconds = tip.slot_seconds || STATUS.slot_seconds;
      STATUS.block_time_seconds = tip.block_time_seconds || STATUS.block_time_seconds;
      if (typeof tip.rpc_connected === "boolean") STATUS.rpc_connected = tip.rpc_connected;
    }
    applyStatusPills({
      ...(STATUS || {}),
      height: tip.height,
      indexed_height: tip.indexed_height,
      indexing: tip.indexing,
      rpc_connected: tip.rpc_connected,
    });
    const key = tipKey(tip);
    const caughtUp = lastIndexing === true && tip.indexing === false;
    if (!lastTipKey) {
      lastTipKey = key;
      lastIndexing = tip.indexing;
      return;
    }
    if (key !== lastTipKey || caughtUp) {
      lastTipKey = key;
      lastIndexing = tip.indexing;
      await refreshStatus();
      if (livePagesNeedReload()) await route();
      return;
    }
    lastIndexing = tip.indexing;
  } catch {
    /* next slot retries */
  } finally {
    liveRefreshInFlight = false;
  }
}

function msUntilNextBlockPoll() {
  const spacing = blockTimeSeconds() * 1000;
  const rem = spacing - (Date.now() % spacing);
  return Math.max(250, rem + 500);
}

function nextLiveRefreshMs() {
  if (STATUS?.indexing) return Math.min(5000, blockTimeSeconds() * 1000);
  return msUntilNextBlockPoll();
}

function scheduleLiveRefresh() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    await pollTipAndMaybeReload();
    scheduleLiveRefresh();
  }, nextLiveRefreshMs());
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
      <div class="card stat"><span>Latest block</span><b>${s.tip ?? "—"}</b></div>
      <div class="card stat"><span>XFER issued</span><b>${atomsToXfer(s.supply_atoms)}</b></div>
      <div class="card stat"><span>Next reward</span><b>${atomsToXfer(s.subsidy_atoms)}</b></div>
      <div class="card stat"><span>Assets</span><b>${counts.assets ?? 0}</b></div>
      <div class="card stat"><span>Draws paid</span><b>${counts.wins ?? 0}</b></div>
      <div class="card stat"><span>Nodes connected</span><b>${s.peer_count ?? "—"}</b></div>
    </div>`;
}

function winnerWho(w) {
  if (w && w.xaccount) return handle(w.xaccount);
  return `<span class="faint">no name on the payout</span>`;
}

function blockWinner(b) {
  if (b && b.winner_handle) return handle(b.winner_handle);
  if (b && b.height === 0) return "—";
  return `<span class="faint">no name on the payout</span>`;
}

function txIdentityHtml(t) {
  if (t.coinbase) {
    if (t.winner_handle) return handle(t.winner_handle);
    if (t.lottery_handles && t.lottery_handles.length) {
      return t.lottery_handles.map((h) => handle(h)).join(" ");
    }
    return `<span class="faint">no XVA1</span>`;
  }
  if (t.host_share) {
    const host = t.host_share.host_handle ? handle(t.host_share.host_handle) : "";
    return `${host} <span class="badge guest">guest share</span>`.trim();
  }
  return t.xid_handle ? handle(t.xid_handle) : "—";
}

function lotteryCard(live, nodes, winnerHandles, activeCount) {
  const L = live || {};
  const rewards = L.rewards || [];
  const mapped = winnerHandles || [];
  const n = Math.max(mapped.length, (L.winners || []).length);
  const rows = n
    ? Array.from({ length: n }, (_, i) => {
        const who = mapped[i] ? handle(mapped[i]) : `<span class="faint">no name on the payout</span>`;
        return `<div class="winner"><div class="who"><span class="badge lottery">winner</span> ${who}</div><div class="amt">${atomsToXfer(rewards[i] || 0)}</div></div>`;
      }).join("")
    : `<div class="empty">No eligible nodes in this draw yet.</div>`;
  const fromApi = Number(activeCount);
  const fromWallet = Number(L.active_nodes);
  const activeN = Number.isFinite(fromApi)
    ? fromApi
    : Number.isFinite(fromWallet)
      ? fromWallet
      : ((nodes || []).length || 0);
  const slot = L.slot;
  const winnerN = L.winner_count ?? 1;
  const people = activeN === 1 ? "1 person is" : activeN === 0 ? "No one is" : `${activeN} people are`;
  const paid = Number(winnerN) === 1 ? "This draw pays 1 person" : `This draw pays ${winnerN} people`;
  return `
    <div class="card">
      <h2>This minute’s draw</h2>
      <div class="countdown" id="cd">—:—</div>
      <p class="muted">Block ${L.height ?? "—"}. ${people} in this draw. ${paid}.</p>
      ${rows}
      <details class="raw-json"><summary>Draw numbers</summary><p class="muted">Slot ${slot ?? "—"} · ${activeN} active · ${winnerN} winner${Number(winnerN) === 1 ? "" : "s"}</p></details>
      <div class="row-actions">
        <a href="#/lottery">Open this draw</a>
        <a href="#/members">See who can be paid</a>
      </div>
    </div>`;
}

function tickCountdown() {
  const slot = blockTimeSeconds();
  const rem = slot - (Math.floor(Date.now() / 1000) % slot);
  const el = $("#cd");
  if (el) el.textContent = `0:${String(rem).padStart(2, "0")}`;
  const sec = $("#obs-sec");
  if (sec) sec.textContent = String(rem).padStart(2, "0");
  const arc = $("#obs-sec-arc");
  if (arc) {
    const c = 2 * Math.PI * 52;
    const spent = slot - rem;
    arc.setAttribute("stroke-dasharray", `${(spent / slot) * c} ${c}`);
  }
}

function handleHue(h) {
  let n = 0;
  for (const c of String(h || "")) n = (n * 33 + c.charCodeAt(0)) >>> 0;
  const hues = [38, 214, 152, 280, 18, 190, 330];
  return hues[n % hues.length];
}

function ringClock(progress, remLabel) {
  const rLife = 70;
  const rMin = 52;
  const cLife = 2 * Math.PI * rLife;
  const cMin = 2 * Math.PI * rMin;
  const life = Math.max(0, Math.min(1, Number(progress) || 0));
  return `<div class="obs-clock">
    <svg viewBox="0 0 180 180" aria-hidden="true">
      <circle cx="90" cy="90" r="${rLife}" fill="none" stroke="#1c2333" stroke-width="10"/>
      <circle cx="90" cy="90" r="${rMin}" fill="none" stroke="#161b27" stroke-width="8"/>
      <circle cx="90" cy="90" r="${rLife}" fill="none" stroke="#e2b34a" stroke-width="10"
        stroke-linecap="round" transform="rotate(-90 90 90)"
        stroke-dasharray="${life * cLife} ${cLife}"/>
      <circle id="obs-sec-arc" cx="90" cy="90" r="${rMin}" fill="none" stroke="#4c8dff" stroke-width="8"
        stroke-linecap="round" transform="rotate(-90 90 90)"
        stroke-dasharray="0 ${cMin}"/>
    </svg>
    <div class="obs-clock-label">
      <b id="obs-sec">${esc(remLabel)}</b>
      <span>this minute</span>
      <span>${(life * 100).toFixed(4)}% of all emission</span>
    </div>
  </div>`;
}

function eraStairs(eras, height) {
  if (!eras || !eras.length) return `<div class="empty">Shows after genesis.</div>`;
  const maxSub = Math.max(...eras.map((e) => e.subsidy_atoms || 0), 1);
  return `<div class="era-track">${eras.map((e) => {
    const h = Math.max(8, Math.round((e.subsidy_atoms / maxSub) * 100));
    const on = height >= e.start && height <= e.end;
    return `<div class="era-col${on ? " on" : ""}" style="height:${h}%" title="Era ${e.era}: ${e.start}–${e.end}">
      <span>${e.era === 0 ? "now" : "½"}</span>
    </div>`;
  }).join("")}</div>`;
}

function hatConstellation(handles) {
  const rows = (handles || []).slice(0, 16);
  if (!rows.length) return `<div class="empty">No handles indexed yet.</div>`;
  const size = 360;
  const cx = 180;
  const cy = 180;
  const maxW = Math.max(...rows.map((h) => h.wins || 0), 1);
  const nodes = rows.map((h, i) => {
    const ang = (i / rows.length) * Math.PI * 2 - Math.PI / 2;
    const rad = 56 + ((h.wins || 0) / maxW) * 88;
    return {
      ...h,
      x: cx + Math.cos(ang) * rad,
      y: cy + Math.sin(ang) * rad,
      r: 7 + ((h.wins || 0) / maxW) * 10,
      hue: handleHue(h.handle),
    };
  });
  const lines = nodes.map((n) => `<line x1="${cx}" y1="${cy}" x2="${n.x}" y2="${n.y}" stroke="hsla(${n.hue},70%,58%,0.28)" stroke-width="1.2"/>`).join("");
  const dots = nodes.map((n) => `<a href="#/identity/${encodeURIComponent(n.handle)}">
    <circle cx="${n.x}" cy="${n.y}" r="${n.r}" fill="hsl(${n.hue} 70% 58%)" />
    <text x="${n.x}" y="${n.y + n.r + 12}" text-anchor="middle" fill="#8d97ab" font-size="10">@${esc(n.handle)}</text>
  </a>`).join("");
  return `<svg class="hat-map" viewBox="0 0 ${size} ${size}">
    <circle cx="${cx}" cy="${cy}" r="22" fill="#11151f" stroke="#e2b34a" stroke-width="1.4"/>
    <text x="${cx}" y="${cy + 4}" text-anchor="middle" fill="#f0d48a" font-size="11">hat</text>
    ${lines}${dots}
  </svg>`;
}

function pulseChart(points) {
  const rows = points || [];
  if (rows.length < 2) return `<div class="empty">Shows after a few lottery blocks.</div>`;
  const w = 640;
  const h = 140;
  const pad = 12;
  const ys = rows.map((p) => Number(p.n) || 0);
  const min = Math.min(...ys);
  const max = Math.max(...ys);
  const span = Math.max(1, max - min);
  const step = (w - pad * 2) / (rows.length - 1);
  const xy = rows.map((p, i) => {
    const x = pad + i * step;
    const y = h - pad - ((Number(p.n) - min) / span) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const last = rows[rows.length - 1];
  return `<svg class="pulse-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <polyline fill="none" stroke="#4c8dff" stroke-width="2" points="${xy.join(" ")}"/>
    <text x="${w - 8}" y="16" text-anchor="end" fill="#8d97ab" font-size="11">${last.n} in hat @ ${last.height}</text>
  </svg>`;
}

function luckRows(handles) {
  const rows = (handles || []).filter((h) => (h.hat_blocks || 0) > 0).slice(0, 12);
  if (!rows.length) return `<div class="empty">Shows after a few hats are indexed.</div>`;
  const max = Math.max(...rows.flatMap((h) => [h.wins || 0, h.expected_wins || 0]), 1);
  return rows.map((h) => {
    const act = (h.wins || 0) / max * 100;
    const exp = (h.expected_wins || 0) / max * 100;
    const luck = h.luck == null ? "—" : (h.luck >= 1 ? `+${((h.luck - 1) * 100).toFixed(0)}%` : `${((h.luck - 1) * 100).toFixed(0)}%`);
    return `<div class="luck-row">
      <div>${handle(h.handle)}</div>
      <div class="luck-bars" title="gold = minutes won · blue = fair share if every hat was equal">
        <i class="luck-exp" style="width:${exp}%"></i>
        <i class="luck-act" style="width:${act}%"></i>
      </div>
      <div class="muted">${esc(luck)}</div>
    </div>`;
  }).join("");
}

function homeLead(blocks, lottery) {
  const latest = (lottery.history || [])[0];
  const paid = latest && (latest.winners || [])[0];
  if (latest && paid && (paid.amount || paid.xaccount)) {
    return `The latest paid minute is block ${linkBlock(latest.height)}. ${winnerWho(paid)} received ${atomsToXfer(paid.amount)}. Open the block to see that payout, or open Trades to see buys and sells.`;
  }
  const tip = (blocks.items || [])[0];
  if (tip) return `The latest block is ${linkBlock(tip.height)}. Open it to see who was paid and which payments are inside.`;
  return "No blocks are indexed yet.";
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
    <p class="sub home-lead">${homeLead(blocks, lottery)}</p>
    ${statsRow()}
    <div class="grid home" style="margin-top:16px">
      <div class="card">
        <h2>Latest blocks</h2>
        <p class="muted">Each row is one minute. Open a block to see who was paid.</p>
        <table>
          <thead><tr><th>Block</th><th>When</th><th>Payments</th><th>Paid</th></tr></thead>
          <tbody>
            ${(blocks.items || []).map((b) => `<tr>
              <td>${linkBlock(b.height)}</td>
              <td class="muted">${timeAgo(b.time)}</td>
              <td>${b.tx_count}</td>
              <td>${blockWinner(b)}</td>
            </tr>`).join("") || `<tr><td colspan="4" class="empty">No blocks yet.</td></tr>`}
          </tbody>
        </table>
        <div class="row-actions"><a href="#/blocks">See every block</a></div>
      </div>
      ${lotteryCard(lottery.live, lottery.nodes, lottery.winner_handles, lottery.active_count)}
    </div>
    <div class="card" style="margin-top:16px">
      <h2>Who was paid</h2>
      <p class="muted">The draw pays one or more @handles each minute. Open a block for the full payout.</p>
      <table>
        <thead><tr><th>Block</th><th>Paid to</th><th>Amount</th><th>When</th></tr></thead>
        <tbody>${recentWins || `<tr><td colspan="4" class="empty">Payouts start at block 1.</td></tr>`}</tbody>
      </table>
    </div>`;
  tickCountdown();
}

async function pageBlocks() {
  const state = pageState(hashQuery());
  const data = await api(`/blocks?limit=${state.size}&offset=${state.offset}`);
  if (clampPage(data.total || 0, state)) return;
  app.innerHTML = `
    <h1 class="page-title">Blocks</h1>
    <p class="sub">One block per minute. Height 0 is genesis and pays nothing. Height 1 is the first draw.</p>
    <div class="card">
      <table>
        <thead><tr><th>Height</th><th>Hash</th><th>Time</th><th>Tx</th><th>Winners</th><th>Winner</th></tr></thead>
        <tbody>
          ${(data.items || []).map((b) => `<tr>
            <td>${linkBlock(b.height)}</td>
            <td>${idHtml(b.hash, { href: "#/block/" + encodeURIComponent(b.hash) })}</td>
            <td class="muted">${fmtTime(b.time)}</td>
            <td>${b.tx_count}</td>
            <td>${b.winner_count || (b.height === 0 ? "—" : "0")}</td>
            <td>${blockWinner(b)}</td>
          </tr>`).join("") || `<tr><td colspan="6" class="empty">No blocks yet.</td></tr>`}
        </tbody>
      </table>
      ${pagerHtml("blocks-pager", data.total || 0, state)}
    </div>`;
  bindPagers();
}

function receiptCard(lines, where) {
  const items = (lines || []).filter(Boolean);
  const body = items.length
    ? `<ul class="receipt-lines">${items.map((html) => `<li>${html}</li>`).join("")}</ul>`
    : `<p class="receipt-line">The explorer has no payment it can describe here.</p>`;
  return `<div class="card receipt"><h2>What happened</h2>${body}${where ? `<p class="receipt-where">${where}</p>` : ""}</div>`;
}

function chainDetail(body, summary) {
  const label = summary || "Inputs, outputs, and chain detail";
  return `<details class="raw-json chain-detail"><summary>${label}</summary><div class="chain-detail-body">${body}</div></details>`;
}

function assetLinkPlain(name) {
  const full = String(name || "");
  if (!full) return "—";
  return `<a class="break-anywhere" href="#/asset/${encodeURIComponent(full)}">${esc(full)}</a>`;
}

function personHtml(address, handleName) {
  if (address) {
    const shown = clipMiddle(address, 8, 6);
    const named = handleName && !(LABELS.get(address) || []).some((row) => row.handle === handleName) ? `${handle(handleName)} · ` : "";
    return `${named}${handleChips(address)}<a class="mono-clip" href="#/address/${encodeURIComponent(address)}" title="${esc(address)}">${esc(shown)}</a>`;
  }
  if (handleName) return handle(handleName);
  return `<span class="faint">an address this explorer does not have</span>`;
}

function legAmount(row) {
  if (!row) return "";
  if (row.asset) return `${formatAssetAmount(row.asset_amount, "")} ${assetLinkPlain(row.asset)}`;
  if (Number(row.value)) return atomsToXfer(row.value);
  return "";
}

function readableNote(text) {
  const s = String(text || "").trim();
  if (!s || s.length > 160) return "";
  if (s.includes("|")) return "";
  const hex = s.replace(/\s/g, "");
  if (/^[0-9a-fA-F]+$/.test(hex) && hex.length >= 16) return "";
  return s;
}

function indexedReceipt(t) {
  const lines = [];
  if (t.coinbase) {
    if (Number(t.height) === 0) {
      lines.push("This is the first block. It paid nothing.");
    } else {
      if (t.winner_handle) lines.push(`The draw named ${personHtml(null, t.winner_handle)}.`);
      const outs = (t.vout || []).filter((v) => v.asset || Number(v.value));
      if (!outs.length) lines.push("This payout did not include an amount the explorer could read.");
      for (const v of outs) {
        const amt = legAmount(v);
        const who = v.address ? personHtml(v.address) : (t.winner_handle ? personHtml(null, t.winner_handle) : `<span class="faint">no name on the payout</span>`);
        if (amt) lines.push(`Paid ${amt} to ${who}.`);
      }
    }
  } else if (t.host_share) {
    const s = t.host_share;
    const host = s.host_handle ? personHtml(null, s.host_handle) : personHtml(s.host_address);
    const fromBlock = s.host_height != null ? linkBlock(s.host_height) : "an earlier block";
    lines.push(`${host} shared ${esc(s.guest_percent)}% of a win from block ${fromBlock}. ${esc(s.guest_count)} guest${Number(s.guest_count) === 1 ? "" : "s"} received ${atomsToXfer(s.pot_amount)} altogether.`);
    for (const g of s.guests || []) {
      const who = g.handle ? personHtml(null, g.handle) : personHtml(g.address);
      lines.push(`${who} received ${atomsToXfer(g.amount)}.`);
    }
  } else {
    const sent = new Map();
    for (const v of t.vin || []) {
      if (v.coinbase) continue;
      const amt = legAmount(v);
      if (!amt) continue;
      const key = v.address || "";
      if (!sent.has(key)) sent.set(key, []);
      sent.get(key).push(amt);
    }
    for (const [addr, amts] of sent) {
      lines.push(`${personHtml(addr)} sent ${amts.join(", ")}.`);
    }
    for (const v of t.vout || []) {
      if (v.script_type === "nulldata") {
        const note = readableNote(v.op_return);
        lines.push(note ? `A note on this transaction says “${esc(note)}”.` : "A note was written on this transaction. The exact text is in chain detail.");
        continue;
      }
      const amt = legAmount(v);
      if (!amt && !v.address) continue;
      const who = personHtml(v.address);
      lines.push(amt ? `${who} received ${amt}.` : `${who} is named, with no amount recorded.`);
    }
    if (t.xid_handle) lines.push(`This transaction names ${personHtml(null, t.xid_handle)}.`);
    if (Number(t.fee) > 0) lines.push(`The fee was ${atomsToXfer(t.fee)}.`);
  }
  const where = t.height != null
    ? `In block ${linkBlock(t.height)}${t.time ? ` · ${esc(fmtTime(t.time))}` : ""}. Open the block, or any name above.`
    : `Not in a block yet${t.time ? ` · ${esc(fmtTime(t.time))}` : ""}. Open any name above.`;
  return receiptCard(lines, where);
}

function rpcReceipt(rpc) {
  const lines = [];
  const vins = rpc.vin || [];
  const vouts = rpc.vout || [];
  if (vins.some((v) => v.coinbase)) lines.push("These are new coins from a block. This explorer has not indexed the transaction yet.");
  for (const v of vins) {
    if (v.coinbase) continue;
    if (v.txid) lines.push(`This spent an output from ${linkTx(v.txid)}. The amount is in chain detail.`);
  }
  for (const v of vouts) {
    const spk = v.scriptPubKey || {};
    if (spk.type === "nulldata") {
      lines.push("A note was written on this transaction. The exact text is in chain detail.");
      continue;
    }
    const asset = spk.asset && spk.asset.name;
    const who = rpcOutAddress(v) ? personHtml(rpcOutAddress(v)) : `<span class="faint">an address this explorer does not have</span>`;
    if (asset && spk.asset.amount != null && spk.asset.amount !== "") {
      lines.push(`${who} received ${esc(String(spk.asset.amount))} ${linkAsset(asset)}.`);
    } else if (asset) {
      lines.push(`${who} received ${linkAsset(asset)}.`);
    } else if (v.value != null) {
      lines.push(`${who} received ${esc(v.value)} XFER.`);
    }
  }
  if (!lines.length) lines.push("The node returned this transaction, and the explorer has not indexed it yet.");
  return receiptCard(lines, "Open chain detail for every input and output.");
}

function blockReceipt(b) {
  const wins = (b.lottery && b.lottery.winners) || [];
  const lines = [];
  if (Number(b.height) === 0) {
    lines.push("This is the first block. It paid nothing.");
  } else if (!wins.length) {
    lines.push("No one was paid on this block.");
  } else {
    for (const w of wins) {
      lines.push(`The draw paid ${atomsToXfer(w.amount)} to ${winnerWho(w)}.`);
    }
  }
  const n = (b.txs || []).length || Number(b.tx_count) || 0;
  if (n) lines.push(`${n} payment${n === 1 ? "" : "s"} ${n === 1 ? "is" : "are"} in this block. Open one below to see who sent what.`);
  else lines.push("No payments are in this block.");
  const when = b.time ? `${esc(fmtTime(b.time))} · ${timeAgo(b.time)}` : "";
  return receiptCard(lines, when);
}

async function pageBlock(key) {
  const b = await api("/block/" + encodeURIComponent(key));
  const wins = (b.lottery && b.lottery.winners) || [];
  const named = wins.filter((w) => w.xaccount).map((w) => handle(w.xaccount));
  app.innerHTML = `
    <h1 class="page-title">Block ${b.height}</h1>
    ${blockReceipt(b)}
    <div class="card" style="margin-bottom:16px">
      <h2>Who was paid</h2>
      ${wins.length ? wins.map((w) => `<div class="winner">
        <div class="who">${w.rank === 0 ? `<span class="badge lottery">paid</span>` : `<span class="badge">also paid</span>`}
          ${winnerWho(w)}
        </div>
        <div class="amt">${atomsToXfer(w.amount)}</div>
      </div>`).join("") : `<div class="empty">${b.height === 0 ? "The first block paid nothing." : "No one was paid on this block."}</div>`}
    </div>
    <div class="card" style="margin-bottom:16px">
      <h2>Payments in this block</h2>
      <p class="muted">Open a transaction to read who sent what.</p>
      <table>
        <thead><tr><th>#</th><th>Transaction</th><th>What</th><th>XFER out</th></tr></thead>
        <tbody>
          ${(b.txs || []).map((t) => `<tr>
            <td>${t.n}</td>
            <td>${linkTx(t.txid)}</td>
            <td>${t.coinbase ? `<span class="badge lottery">Block reward</span>` : ""}${t.identity ? ` <span class="badge asset">${handle(t.xid_handle)}</span>` : ""}${!t.coinbase && !t.identity ? `<span class="faint">—</span>` : ""}</td>
            <td>${atomsToXfer(t.xfer_out)}</td>
          </tr>`).join("") || `<tr><td colspan="4" class="empty">No transactions in this block.</td></tr>`}
        </tbody>
      </table>
    </div>
    ${chainDetail(`<div class="card"><div class="kv">
        <b>Hash</b><div>${idHtml(b.hash, { href: "#/block/" + encodeURIComponent(b.hash), full: true })}</div>
        <b>Previous</b><div>${b.prev ? idHtml(b.prev, { href: "#/block/" + encodeURIComponent(b.prev), full: true }) : "—"}</div>
        <b>Slot</b><div>${b.lottery_slot ?? "—"}</div>
        <b>Seed</b><div>${b.lottery_seed ? idHtml(b.lottery_seed, { full: true }) : "—"}</div>
        <b>Names</b><div>${named.length ? named.join(" ") : `<span class="faint">no XVA1</span>`}</div>
        <b>Reward</b><div>${atomsToXfer(b.subsidy)}</div>
        <b>Fees</b><div>${atomsToXfer(b.fees)}</div>
        <b>Size</b><div>${b.size} bytes · ${b.tx_count} tx</div>
      </div></div>`, "Chain detail")}`;
}

function rpcOutAddress(vout) {
  const spk = (vout && vout.scriptPubKey) || {};
  const addrs = spk.addresses || [];
  return addrs[0] || "";
}

async function pageTx(id) {
  const t = await api("/tx/" + encodeURIComponent(id));
  if (t.unindexed) {
    const rpc = t.rpc || {};
    const txid = rpc.txid || id;
    const vin = (rpc.vin || []).map((v) => {
      if (v.coinbase) return `<div><span class="badge lottery">Block reward</span></div>`;
      const prev = v.txid ? `${linkTx(v.txid, { full: true })}<span class="faint">:${esc(v.vout ?? "")}</span>` : `<span class="faint">input</span>`;
      return `<div>${prev}</div>`;
    }).join("");
    const vout = (rpc.vout || []).map((v) => {
      const spk = v.scriptPubKey || {};
      const asset = spk.asset && spk.asset.name;
      const who = spk.type === "nulldata" ? `<span class="badge">Note</span> ${spk.asm ? `<span class="break-anywhere">${esc(spk.asm)}</span>` : ""}` : linkAddr(rpcOutAddress(v), { full: true });
      const amt = asset ? esc(String(spk.asset.amount ?? "") + " " + asset) : (v.value != null ? esc(v.value) + " XFER" : "");
      return `<div>${who}<div class="muted">${amt}</div></div>`;
    }).join("");
    app.innerHTML = `
      <h1 class="page-title">Transaction</h1>
      <p class="id-hero">${copyable(txid)}</p>
      <p class="sub">The node has this transaction. The explorer index does not, yet.</p>
      ${rpcReceipt(rpc)}
      ${chainDetail(`<div class="io">
        <div class="card"><h2>Inputs</h2>${vin || `<div class="empty">No inputs.</div>`}</div>
        <div class="arrow">→</div>
        <div class="card"><h2>Outputs</h2>${vout || `<div class="empty">No outputs.</div>`}</div>
      </div>
      <details class="raw-json" style="margin-top:16px"><summary>Raw transaction</summary><pre class="media-text break-anywhere">${esc(JSON.stringify(rpc, null, 2))}</pre></details>`)}`;
    return;
  }
  const vin = (t.vin || []).map((v) => `<div>${v.coinbase ? `<span class="badge lottery">Block reward</span>` : linkAddr(v.address, { full: true })}
    <div class="muted">${v.asset ? linkAsset(v.asset) + " · " + formatAssetAmount(v.asset_amount, v.asset) : atomsToXfer(v.value)}</div>
    ${v.spent_txid ? `<div class="faint">from ${linkTx(v.spent_txid)}:${v.spent_n}</div>` : ""}</div>`).join("");
  const vout = (t.vout || []).map((v) => `<div>${v.script_type === "nulldata" ? `<span class="badge">Note</span> ${v.op_return ? `<span class="break-anywhere">${esc(v.op_return)}</span>` : ""}` : linkAddr(v.address, { full: true })}
    <div class="muted">${v.asset ? linkAsset(v.asset) + " · " + formatAssetAmount(v.asset_amount, v.asset) + (v.asset_kind ? ` <span class="badge asset">${esc(v.asset_kind)}</span>` : "") : atomsToXfer(v.value)}</div></div>`).join("");
  app.innerHTML = `
    <h1 class="page-title">Transaction</h1>
    <p class="id-hero">${copyable(t.txid)}</p>
    ${indexedReceipt(t)}
    ${chainDetail(`
    <div class="card" style="margin-bottom:16px">
      <div class="kv">
        <b>Block</b><div>${t.height != null ? linkBlock(t.height) : "not in a block yet"} ${t.block_hash ? idHtml(t.block_hash, { href: "#/block/" + encodeURIComponent(t.block_hash), full: true }) : ""}</div>
        <b>Time</b><div>${fmtTime(t.time)}</div>
        <b>Fee</b><div>${t.coinbase ? "—" : atomsToXfer(t.fee)}</div>
        <b>Identity</b><div>${txIdentityHtml(t)}</div>
        ${t.host_share ? `<b>Guest share</b><div>${t.host_share.guest_percent}% of a mature win at ${linkBlock(t.host_share.host_height)} · ${t.host_share.guest_count} guest${t.host_share.guest_count === 1 ? "" : "s"} · pot ${atomsToXfer(t.host_share.pot_amount)}</div>` : ""}
      </div>
    </div>
    ${t.host_share ? `<div class="card" style="margin-bottom:16px">
      <h2>Guests</h2>
      ${(t.host_share.guests || []).map((g) => `<div class="winner"><div>${g.handle ? handle(g.handle) : linkAddr(g.address, { full: true })}</div><div class="amt">${atomsToXfer(g.amount)}</div></div>`).join("") || `<div class="empty">No guest outputs.</div>`}
    </div>` : ""}
    <div class="io">
      <div class="card"><h2>Inputs</h2>${vin || `<div class="empty">No inputs.</div>`}</div>
      <div class="arrow">→</div>
      <div class="card"><h2>Outputs</h2>${vout || `<div class="empty">No outputs.</div>`}</div>
    </div>`)}`;
}

function addressTxNet(t) {
  const received = Number(t.addr_received || 0);
  const sent = Number(t.addr_sent || 0);
  if (!received && !sent) return `<span class="muted">${atomsToXfer(t.xfer_out)}</span>`;
  const net = received - sent;
  return `<span class="${net < 0 ? "amt-out" : "amt-in"}">${atomsToXfer(net)}</span>`;
}

function addressReceipt(a) {
  const lines = [];
  if (a.identity && a.identity.handle) lines.push(`This address is ${handle(a.identity.handle)}.`);
  for (const row of LABELS.get(a.address) || []) {
    if (a.identity && row.handle === a.identity.handle) continue;
    lines.push(`This address belongs to ${handle(row.handle)}: ${esc(PROOF_NAMES[row.proof] || row.proof)}${row.detail ? `, ${esc(row.detail)}` : ""}${row.txid ? ` (${linkTx(row.txid)})` : ""}.`);
  }
  if (a.burn) lines.push(`This is a special chain address: ${esc(a.burn)}.`);
  lines.push(`It holds ${atomsToXfer(a.balance_atoms)}.`);
  const assets = a.assets || [];
  if (!assets.length) lines.push("It holds no named assets.");
  for (const x of assets) lines.push(`It holds ${formatAssetAmount(x.amount, "")} ${assetLinkPlain(x.name)}.`);
  lines.push(`Altogether it has received ${atomsToXfer(a.received_atoms)} and sent ${atomsToXfer(a.sent_atoms)}.`);
  const wins = (a.lottery_wins || []).length;
  if (wins) lines.push(`The draw paid this address ${wins} time${wins === 1 ? "" : "s"}. Open a block in the list to see that minute.`);
  const n = a.tx_total ?? (a.txs || []).length;
  if (n) lines.push(`${n.toLocaleString()} payment${n === 1 ? "" : "s"} below. Open one to see who sent what.`);
  else lines.push("No payments for this address are indexed yet.");
  return receiptCard(lines, "A positive amount means this address received more XFER than it sent in that payment.");
}

async function pageAddress(addr) {
  const state = pageState(hashQuery());
  const a = await api(`/address/${encodeURIComponent(addr)}?limit=${state.size}&offset=${state.offset}`);
  if (clampPage(a.tx_total || 0, state)) return;
  app.innerHTML = `
    <h1 class="page-title">Address</h1>
    <p class="id-hero">${handleChips(a.address)}${copyable(a.address)}</p>
    ${addressReceipt(a)}
    <div class="grid two">
      <div class="card">
        <h2>Payments</h2>
        <p class="muted">Open a transaction for the full receipt.</p>
        <table>
          <thead><tr><th>Transaction</th><th>Block</th><th>When</th><th>For this address</th></tr></thead>
          <tbody>${(a.txs || []).map((t) => `<tr><td>${linkTx(t.txid)}</td><td>${t.height != null ? linkBlock(t.height) : "—"}</td><td class="muted">${timeAgo(t.time)}</td><td>${addressTxNet(t)}</td></tr>`).join("") || `<tr><td colspan="4" class="empty">No transactions for this address.</td></tr>`}</tbody>
        </table>
        ${pagerHtml("address-pager", a.tx_total || 0, state)}
      </div>
      <div>
        <div class="card" style="margin-bottom:16px">
          <h2>Assets held here</h2>
          ${(a.assets || []).map((x) => `<div class="winner"><div>${linkAsset(x.name)}</div><div class="amt">${formatAssetAmount(x.amount, x.name)}</div></div>`).join("") || `<div class="empty">No named assets.</div>`}
        </div>
        <div class="card">
          <h2>Draw payouts</h2>
          ${(a.lottery_wins || []).map((w) => `<div class="winner"><div>${linkBlock(w.height)} ${winnerWho(w)}</div><div class="amt">${atomsToXfer(w.amount)}</div></div>`).join("") || `<div class="empty">This address has not been paid by the draw.</div>`}
        </div>
        ${(a.guest_shares || []).length ? `<div class="card" style="margin-top:16px">
          <h2>Guest payments received</h2>
          <p class="muted">A host sent part of a mature win. Open the transaction to see the split.</p>
          ${a.guest_shares.map((s) => `<div class="winner"><div>${linkTx(s.txid)} ${s.host_handle ? handle(s.host_handle) : ""} <span class="badge guest">${s.guest_percent}%</span></div><div class="amt">${atomsToXfer(s.amount)}</div></div>`).join("")}
        </div>` : ""}
      </div>
    </div>`;
  bindPagers();
}

const ASSET_KINDS = [["", "All"], ["root", "Roots"], ["sub", "Subs"], ["unique", "Uniques"]];

async function pageAssets() {
  const params = hashQuery();
  const q = (params.get("q") || "").trim();
  const kind = ASSET_KINDS.some(([k]) => k && k === params.get("kind")) ? params.get("kind") : "";
  const state = pageState(params);
  const data = await api(`/assets?q=${encodeURIComponent(q)}&kind=${kind}&limit=${state.size}&offset=${state.offset}`);
  if (clampPage(data.total || 0, state)) return;
  const filterHash = (nextKind, nextQ) => withParams("#/assets", { q: nextQ, kind: nextKind, size: state.size });
  app.innerHTML = `
    <h1 class="page-title">Assets</h1>
    <p class="sub">Roots come from Sign in with X. Subs are NAME/CHILD. Uniques are NAME#tag.</p>
    <div class="card">
      <div class="toolbar">
        <form class="search" id="asset-search">
          <input id="asset-q" type="search" value="${esc(q)}" placeholder="Search name or @handle" autocomplete="off" />
          <button type="submit">Search</button>
        </form>
        <div class="tabs" id="asset-kinds">
          ${ASSET_KINDS.map(([k, label]) => `<a class="tab ${k === kind ? "on" : ""}" href="${esc(filterHash(k, q))}">${label}</a>`).join("")}
        </div>
      </div>
      <p class="member-meta">${(data.total || 0).toLocaleString()} ${data.total === 1 ? "asset" : "assets"}${q ? ` for “${esc(q)}”` : ""}</p>
      <table class="click-rows">
        <thead><tr><th>Name</th><th>Kind</th><th>Amount</th><th>IPFS</th><th>X</th><th>Created</th></tr></thead>
        <tbody>
          ${(data.items || []).map((a) => `<tr data-href="#/asset/${encodeURIComponent(a.name)}">
            <td>${linkAsset(a.name)}</td>
            <td><span class="badge asset">${esc(a.kind || "")}</span></td>
            <td>${formatAssetAmount(a.amount, a.name, a.units)}</td>
            <td>${assetListIpfsCell(a)}</td>
            <td>${a.x_handle ? handle(a.x_handle) : "—"}</td>
            <td>${a.created_height != null ? linkBlock(a.created_height) : "—"}</td>
          </tr>`).join("") || `<tr><td colspan="6" class="empty">${q || kind ? "No asset matches that search." : "No assets yet."}</td></tr>`}
        </tbody>
      </table>
      ${pagerHtml("assets-pager", data.total || 0, state)}
    </div>`;
  $("#asset-search").addEventListener("submit", (e) => {
    e.preventDefault();
    location.hash = filterHash(kind, $("#asset-q").value.trim());
  });
  bindPagers();
}

const GATEWAY_CHIPS = [
  ["xfer.mypinata.cloud", "View"],
  ["ipfs.io", "ipfs.io"],
  ["dweb.link", "dweb"],
  ["w3s.link", "w3s"],
  ["cloudflare-ipfs.com", "Cloudflare"],
  ["gateway.pinata.cloud", "Gateway"],
];

function gatewayChipsHtml(cid) {
  const id = normalizeIpfsClient(cid);
  if (!id) return "";
  return `<span class="gw-row">${GATEWAY_CHIPS.map(([host, label]) => {
    const href = "https://" + host + "/ipfs/" + id;
    return `<a class="gw-chip" href="${esc(href)}" target="_blank" rel="noopener noreferrer" title="${esc(host)}">${esc(label)}</a>`;
  }).join("")}</span>`;
}

function plainClient(value) {
  return String(value ?? "").replace(/<[^>]*>/g, "");
}

function safeHttpClient(value) {
  const t = plainClient(value).trim();
  if (!/^https?:\/\//i.test(t)) return "";
  try {
    const u = new URL(t);
    if (u.protocol !== "http:" && u.protocol !== "https:") return "";
    if (u.username || u.password) return "";
    return t;
  } catch {
    return "";
  }
}

function kindLabel(kind) {
  return {
    image: "Picture",
    video: "Video",
    audio: "Audio",
    json: "Token file",
    text: "Text",
  }[kind] || "File";
}

function captionHtml(cid, kind) {
  const label = kind ? `<div class="muted">${esc(kindLabel(kind))}</div>` : `<div class="muted">File id</div>`;
  return `${label}${idHtml(cid)}${gatewayChipsHtml(cid)}`;
}

function traitGrid(attrs) {
  if (!attrs || !attrs.length) return "";
  const cells = attrs.map((at) => {
    if (!at || typeof at !== "object") return "";
    const label = plainClient(at.trait_type || at.trait || at.type || "");
    const value = plainClient(at.value ?? "");
    if (!label || !value) return "";
    return `<div class="attr"><span>${esc(label)}</span><b>${esc(value)}</b></div>`;
  }).join("");
  if (!cells) return "";
  return `<div class="trait-label">Traits</div><div class="attr-grid">${cells}</div>`;
}

function renderMetadataCard(info) {
  if (!info || info.kind !== "json") return "";
  const view = info.view || {};
  const nft = info.nft || {};
  const name = plainClient(view.name || nft.name || "").trim();
  const description = plainClient(view.description || nft.description || "").trim();
  const external = safeHttpClient(view.external_url || nft.external_url || "");
  const image = (view.image && typeof view.image === "object") ? view.image : (nft.image || null);
  const attrs = (view.attributes && view.attributes.length) ? view.attributes : (nft.attributes || []);
  const raw = info.raw_json || "";
  const picture = image && image.cid
    ? `<p class="muted">Picture ${idHtml(image.cid)}</p>`
    : (image && safeHttpClient(image.src) ? `<p class="muted">Picture <a href="${esc(safeHttpClient(image.src))}" target="_blank" rel="noopener noreferrer">${esc(safeHttpClient(image.src))}</a></p>` : "");
  const fields = [
    name ? `<h3 class="nft-name">${esc(name)}</h3>` : "",
    description ? `<p class="asset-desc">${esc(description)}</p>` : "",
    external ? `<p><span class="muted">Website</span> <a href="${esc(external)}" target="_blank" rel="noopener noreferrer">${esc(external)}</a></p>` : "",
    picture,
    traitGrid(attrs),
  ].join("");
  const rawBlock = raw
    ? `<details class="raw-json"><summary>Raw JSON</summary><div class="raw-json-tools"><button type="button" class="copy-btn" data-copy-from="raw-json-body">Copy</button></div><pre id="raw-json-body" class="media-text">${esc(raw)}</pre></details>`
    : "";
  if (!fields && !rawBlock) return "";
  return `<div class="card metadata-card" style="margin-bottom:16px"><h2>Metadata</h2>${fields}${rawBlock}</div>`;
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
          <p>Loading…</p>
        </div>
      </div>`;
  }
  const nft = info.nft || {};
  if (info.view && info.view.image && !nft.image) nft.image = info.view.image;
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
    const srcs = info.kind === "audio" ? ipfsSources(cid, info.gateways) : mediaFallbacks(nft.audio, info.gateways);
    const src = srcs[0] || (info.kind === "audio" ? info.url : audio);
    return `
      <div class="media-stage">
        <div class="media-empty">
          <audio controls src="${esc(src)}"${fallbackAttr(srcs)}></audio>
        </div>
      </div>`;
  }
  if (info.kind === "json") {
    return `<div class="media-stage">${ipfsUnavailable("No picture in this file.")}</div>`;
  }
  if (info.kind === "text") {
    return `<pre class="media-text">${esc(info.text || "")}</pre>`;
  }
  return `
    <div class="media-stage">
      <div class="media-empty">
        <span class="badge">${esc(info.content_type || "file")}</span>
        <p><a href="${esc(info.url)}" target="_blank" rel="noopener noreferrer">Open this file</a></p>
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
    if (cap) cap.innerHTML = captionHtml(cid, info.kind);
    if (metaBox) metaBox.innerHTML = renderMetadataCard(info);
  } catch (e) {
    stage.innerHTML = showGatewayImage(cid, urls);
    const cap = $("#asset-media-cap");
    if (cap) cap.innerHTML = captionHtml(cid, "");
    if (metaBox) metaBox.innerHTML = "";
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

function assetKindWords(kind) {
  const k = String(kind || "").toLowerCase();
  if (k === "root") return "This is a main asset";
  if (k === "sub") return "This is a sub-asset";
  if (k === "unique") return "This is a one-of-a-kind token";
  if (k === "owner") return "This is an owner token";
  if (kind) return `This is ${esc(kind)}`;
  return "This is an asset";
}

function activityWords(kind) {
  const k = String(kind || "").toLowerCase();
  if (k === "new") return "Created";
  if (k === "reissue") return "More issued";
  if (k === "transfer") return "Sent";
  if (k === "owner") return "Owner token";
  if (!kind) return "—";
  return esc(kind);
}

async function pageAsset(name) {
  const params = hashQuery();
  const hState = pageState(params, "holders");
  const aState = pageState(params, "history");
  const enc = encodeURIComponent(name);
  const [a, holderPage, activityPage] = await Promise.all([
    api(assetApiPath(name)),
    api(`/asset-holders/${enc}?limit=${hState.size}&offset=${hState.offset}`),
    api(`/asset-activity/${enc}?limit=${aState.size}&offset=${aState.offset}`),
  ]);
  if (clampPage(holderPage.total || 0, hState, "holders") || clampPage(activityPage.total || 0, aState, "history")) return;
  const meta = a.rpc || {};
  const units = meta.units ?? a.units ?? 0;
  const amountAtoms = meta.amount != null ? Math.round(Number(meta.amount) * 1e8) : a.amount;
  const cid = assetCid(a);
  const created = a.created_height != null ? linkBlock(a.created_height) : "";
  const holders = a.holder_count ?? (a.holders || []).length;
  const lines = [];
  lines.push(`${assetKindWords(a.kind)} named ${esc(a.name)}. ${formatAssetAmount(amountAtoms, "", units)} exist.`);
  if (a.x_handle) lines.push(`It is tied to ${handle(a.x_handle)}.`);
  if (a.identity?.address) lines.push(`That name’s address is ${linkAddr(a.identity.address, { full: true })}.`);
  if (a.issuer) lines.push(`Created by ${linkAddr(a.issuer, { full: true })}${created ? ` in block ${created}` : ""}.`);
  else if (created) lines.push(`Created in block ${created}.`);
  if (a.created_txid) lines.push(`The creation transaction is ${linkTx(a.created_txid, { full: true })}.`);
  lines.push(holders ? `${holders} ${holders === 1 ? "address holds" : "addresses hold"} it. Open one below.` : "No address holds it right now.");
  if (!cid) lines.push("No file is attached.");
  app.innerHTML = `
    <p class="crumb"><a href="#/assets">Assets</a> / ${esc(a.name)}</p>
    <h1 class="page-title">${esc(a.name)}</h1>
    <div class="asset-hero">
      <div class="card media-card">
        <div id="asset-media">${cid ? renderMediaStage(null, cid) : `
          <div class="media-stage">
            <div class="media-empty">
              <span class="badge">No file</span>
              <p>This asset has no file attached.</p>
            </div>
          </div>`}</div>
        <div class="media-caption" id="asset-media-cap">${cid ? captionHtml(cid, "") : "No file attached."}</div>
      </div>
      ${receiptCard(lines, "Open a holder to see that address, or a row under History to open the transaction.")}
    </div>
    <div id="asset-nft"></div>
    ${chainDetail(`<div class="card"><div class="kv">
      <b>Kind</b><div>${esc(a.kind || "—")}</div>
      <b>Units</b><div>${esc(units)}</div>
      <b>More can be issued</b><div>${yesNo(meta.reissuable ?? a.reissuable)}</div>
      <b>File id</b><div>${cid ? copyable(cid) : `<span class="faint">none</span>`}</div>
    </div></div>`, "Chain detail")}
    <div class="grid two">
      <div class="card" id="asset-holders">
        <h2>Who holds it</h2>
        <table>
          <thead><tr><th>Address</th><th>Amount</th></tr></thead>
          <tbody>${(holderPage.items || []).map((h) => `<tr><td>${linkAddr(h.address)}</td><td>${formatAssetAmount(h.amount, a.name, units)}</td></tr>`).join("") || `<tr><td colspan="2" class="empty">No holders.</td></tr>`}</tbody>
        </table>
        ${pagerHtml("holders-pager", holderPage.total || 0, hState, "holders")}
      </div>
      <div class="card">
        <h2>History</h2>
        <p class="muted">Open a transaction to see who sent this asset.</p>
        <table>
          <thead><tr><th>Block</th><th>What</th><th>Transaction</th></tr></thead>
          <tbody>${(activityPage.items || []).map((x) => `<tr><td>${x.height != null ? linkBlock(x.height) : "—"}</td><td>${activityWords(x.kind)}</td><td>${linkTx(x.txid)}</td></tr>`).join("") || `<tr><td colspan="3" class="empty">No activity.</td></tr>`}</tbody>
        </table>
        ${pagerHtml("history-pager", activityPage.total || 0, aState, "history")}
      </div>
    </div>`;
  bindPagers();
  if (cid) loadAssetMedia(cid, a.ipfs_gateways);
}

function hashQuery() {
  const h = location.hash || "";
  const i = h.indexOf("?");
  return new URLSearchParams(i >= 0 ? h.slice(i + 1) : "");
}

// Long lists page on the server. The page lives in the hash (?page=2&size=50) so links and Back work.
const PAGE_SIZES = [25, 50, 100];
let scrollToPager = "";

function hashPath() {
  const h = location.hash || "#/";
  const i = h.indexOf("?");
  return i >= 0 ? h.slice(0, i) : h;
}

function pageState(params, key = "page") {
  const size = PAGE_SIZES.includes(Number(params.get("size"))) ? Number(params.get("size")) : PAGE_SIZES[0];
  const page = Math.max(1, Math.floor(Number(params.get(key))) || 1);
  return { page, size, offset: (page - 1) * size };
}

function withParams(path, values) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(values)) {
    if (v === "" || v == null || (k === "size" && Number(v) === PAGE_SIZES[0]) || (/page$|^holders$|^history$/.test(k) && Number(v) === 1)) continue;
    p.set(k, v);
  }
  const qs = p.toString();
  return path + (qs ? "?" + qs : "");
}

// Same list, other page: keeps every other hash param (scope, q, kind, ...).
function pageHref(key, page, size) {
  const params = Object.fromEntries(hashQuery());
  params[key] = page;
  if (size) params.size = size;
  return withParams(hashPath(), params);
}

function pageNumbers(page, last) {
  const want = new Set([1, last, page - 1, page, page + 1]);
  if (page <= 3) [2, 3, 4].forEach((n) => want.add(n));
  if (page >= last - 2) [last - 1, last - 2, last - 3].forEach((n) => want.add(n));
  const nums = [...want].filter((n) => n >= 1 && n <= last).sort((a, b) => a - b);
  const out = [];
  nums.forEach((n, i) => {
    if (i && n - nums[i - 1] > 1) out.push(null);
    out.push(n);
  });
  return out;
}

function pagerHtml(id, total, state, key = "page") {
  const { page, size, offset } = state;
  const last = Math.max(1, Math.ceil(total / size));
  const from = total ? offset + 1 : 0;
  const to = Math.min(total, offset + size);
  const link = (n, label, cls = "") => `<a class="pg-btn ${cls}" data-pager-link href="${esc(pageHref(key, n))}"${n === page ? ' aria-current="page"' : ""}>${label}</a>`;
  const off = (label) => `<span class="pg-btn off" aria-disabled="true">${label}</span>`;
  const nums = pageNumbers(page, last).map((n) => (n == null ? `<span class="pg-gap">…</span>` : link(n, n.toLocaleString(), n === page ? "on" : ""))).join("");
  return `<nav class="pager" id="${id}" data-key="${key}" data-last="${last}" aria-label="Pages">
    <span class="pg-count">${total ? `${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()}` : "0 results"}</span>
    ${last > 1 ? `<span class="pg-nav">
      ${page > 1 ? link(page - 1, "‹ Prev", "pg-step") : off("‹ Prev")}
      <span class="pg-nums">${nums}</span>
      <span class="pg-here">Page ${page} of ${last}</span>
      ${page < last ? link(page + 1, "Next ›", "pg-step") : off("Next ›")}
    </span>
    <form class="pg-jump"><label>Page <input type="number" inputmode="numeric" min="1" max="${last}" value="${page}" aria-label="Go to page" /></label><button type="submit">Go</button></form>` : ""}
    <label class="pg-size">Show <select aria-label="Rows per page">${PAGE_SIZES.map((n) => `<option value="${n}"${n === size ? " selected" : ""}>${n}</option>`).join("")}</select></label>
  </nav>`;
}

// Wire the jump box and size picker of every pager on the page.
function bindPagers() {
  document.querySelectorAll(".pager").forEach((nav) => {
    const key = nav.dataset.key;
    const last = Number(nav.dataset.last);
    const state = pageState(hashQuery(), key);
    nav.querySelectorAll("[data-pager-link]").forEach((a) => a.addEventListener("click", () => { scrollToPager = nav.id; }));
    const jump = nav.querySelector(".pg-jump");
    if (jump) {
      jump.addEventListener("submit", (e) => {
        e.preventDefault();
        const n = Math.min(last, Math.max(1, Math.floor(Number(jump.querySelector("input").value)) || 1));
        scrollToPager = nav.id;
        location.hash = pageHref(key, n);
      });
    }
    nav.querySelector(".pg-size select").addEventListener("change", (e) => {
      const size = Number(e.target.value);
      // Keep the first row on screen visible on the new page size.
      location.hash = pageHref(key, Math.floor(state.offset / size) + 1, size);
    });
  });
  const target = scrollToPager && document.getElementById(scrollToPager);
  scrollToPager = "";
  if (target) {
    const list = target.closest(".card") || target;
    list.scrollIntoView({ block: "start" });
  }
}

// A page past the end (fewer rows now, or a bigger size) goes to the last page instead of an empty table.
function clampPage(total, state, key = "page") {
  const last = Math.max(1, Math.ceil(total / state.size));
  if (state.page <= last) return false;
  location.replace(pageHref(key, last));
  return true;
}

function membersHash(scope, q) {
  const size = pageState(hashQuery()).size;
  return withParams("#/members", { scope: scope === "eligible" ? "" : scope, q, size });
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
    <p class="sub">${p.found ? "The @handle is the lottery identity. Payout addresses change. Guest shares are later sends from a mature win. Guests are not in the hat." : "This handle is not in the hat, the live eligible set, or any guest share yet."}</p>
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
        <b>Address</b><div>${p.address ? linkAddr(p.address, { full: true }) : `<span class="faint">—</span>`}</div>
        <b>Asset root</b><div>${p.asset ? linkAsset(p.asset) : `<span class="faint">—</span>`}</div>
        <b>All addresses</b><div>${(p.addresses || []).map((row) => `<div title="${esc(labelHint({ handle: name, proof: row.proof, detail: row.detail }))}">${linkAddr(row.address)} <span class="faint">${esc(PROOF_NAMES[row.proof] || row.proof)}</span></div>`).join("") || `<span class="faint">—</span>`}</div>
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
            ${(p.wins_recent || []).map((w) => `<tr><td>${linkBlock(w.height)}</td><td>${atomsToXfer(w.amount)}</td></tr>`).join("") || `<tr><td colspan="2" class="empty">No wins for this handle.</td></tr>`}
          </tbody>
        </table>
      </div>
      <div class="card">
        <h2>Recent hat</h2>
        <table>
          <thead><tr><th>Block</th><th>n</th></tr></thead>
          <tbody>
            ${(p.appearances || []).map((a) => `<tr><td>${linkBlock(a.height)}</td><td>${a.n ?? "—"}</td></tr>`).join("") || `<tr><td colspan="2" class="empty">Not seen in a hat.</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>
    <div class="grid two" style="margin-top:16px">
      <div class="card">
        <h2>Shares sent</h2>
        <p class="muted">A percent of a mature win, split equally.</p>
        <table>
          <thead><tr><th>Tx</th><th>%</th><th>Guests</th><th>Pot</th></tr></thead>
          <tbody>
            ${(p.shares_sent || []).map((s) => `<tr><td>${linkTx(s.txid)}</td><td>${s.guest_percent}%</td><td>${s.guest_count}</td><td>${atomsToXfer(s.pot_amount)}</td></tr>`).join("") || `<tr><td colspan="4" class="empty">No shares sent.</td></tr>`}
          </tbody>
        </table>
      </div>
      <div class="card">
        <h2>Guest receipts</h2>
        <p class="muted">Paid by a host. This handle was not in the hat.</p>
        <table>
          <thead><tr><th>Tx</th><th>Host</th><th>Paid</th></tr></thead>
          <tbody>
            ${(p.shares_received || []).map((s) => `<tr><td>${linkTx(s.txid)}</td><td>${s.host_handle ? handle(s.host_handle) : "—"}</td><td>${atomsToXfer(s.amount)}</td></tr>`).join("") || `<tr><td colspan="3" class="empty">No guest receipts.</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>`;
}

async function pageMembers() {
  const params = hashQuery();
  const q = (params.get("q") || "").trim();
  const scope = params.get("scope") === "all" ? "all" : "eligible";
  const state = pageState(params);
  const data = await api(`/lottery/members?q=${encodeURIComponent(q)}&scope=${scope}&limit=${state.size}&offset=${state.offset}`);
  if (clampPage(data.total || 0, state)) return;
  const items = data.items || [];
  app.innerHTML = `
    <h1 class="page-title">Eligible members</h1>
    <p class="sub">X Verified <code>@handles</code> in this minute’s hat, or with a live heartbeat. Search also finds handles that were eligible before and are offline now.</p>
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
      <p class="member-meta">${(data.total || 0).toLocaleString()} ${data.total === 1 ? "handle" : "handles"}${q ? ` for “${esc(q)}”` : ""} · ${scope === "eligible" ? "live eligible only" : "every indexed handle"}</p>
      <table class="click-rows">
        <thead><tr><th>Handle</th><th>Now</th><th>Wins</th><th>Earned</th><th>Last hat</th></tr></thead>
        <tbody>
          ${items.map((m) => `<tr data-href="#/identity/${encodeURIComponent(m.handle)}">
            <td>${handle(m.handle)}</td>
            <td>${memberStatusBadges(m)}</td>
            <td>${m.wins || 0}</td>
            <td>${atomsToXfer(m.earned || 0)}</td>
            <td>${m.last_hat != null ? linkBlock(m.last_hat) : "—"}</td>
          </tr>`).join("") || `<tr><td colspan="5" class="empty">${q ? "No handle matches that search." : (scope === "eligible" ? "No one is eligible this minute. Try All handles." : "No handles indexed yet.")}</td></tr>`}
        </tbody>
      </table>
      ${pagerHtml("members-pager", data.total || 0, state)}
    </div>`;
  bindPagers();
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
        ? `<div class="empty">No handles in this minute’s hat yet.</div>`
        : `<div class="empty">Wallet is offline. History below is from blocks already indexed.</div>`);
  app.innerHTML = `
    ${nodeBanner()}
    <h1 class="page-title">Lottery</h1>
    <p class="sub">Each block is one minute and one draw among X Verified nodes in the hat. The winner is the @handle paid on that block. Height is the block, slot is the minute, and active is how many nodes are in the draw. The countdown is time left in the minute. Height 0 pays nothing; the first draw is height 1. A host can later send a percent of a mature win to invited guests, split equally. Guests are not in the hat.</p>
    <div class="grid two">
      ${lotteryCard(live, nodes, L.winner_handles, L.active_count)}
      <div class="card">
        <h2>Active now${(L.active_count != null || live.active_nodes != null) ? ` · ${L.active_count ?? live.active_nodes}` : ""}</h2>
        ${liveHint}
        <p class="muted" style="margin-top:12px">This wallet: eligible ${live.local_eligible ? "yes" : "no"} · verified ${live.local_x_verified ? "yes" : "no"} · @${esc(live.local_xaccount || "—")}</p>
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
            }).join("") || `<tr><td colspan="3" class="empty">No draws yet. Height 0 is genesis and is not a win. Height 1 is the first draw.</td></tr>`}
          </tbody>
        </table>
      </div>
      <div class="card">
        <h2>Leaderboard</h2>
        <table>
          <thead><tr><th>Who</th><th>Wins</th><th>Earned</th></tr></thead>
          <tbody>
            ${(L.leaders || []).map((x) => `<tr>
              <td>${winnerWho(x)}</td>
              <td>${x.wins}</td><td>${atomsToXfer(x.earned)}</td>
            </tr>`).join("") || `<tr><td colspan="3" class="empty">No winners yet.</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>
    <div class="grid two" style="margin-top:16px">
      <div class="card">
        <h2>Guest shares</h2>
        <p class="muted">A host sends 1–100% of a mature win, split equally among guests.</p>
        <table>
          <thead><tr><th>Tx</th><th>Host</th><th>%</th><th>Guests</th></tr></thead>
          <tbody>
            ${(L.recent_shares || []).map((s) => `<tr>
              <td>${linkTx(s.txid)}</td>
              <td>${s.host_handle ? handle(s.host_handle) : "—"}</td>
              <td>${s.guest_percent}%</td>
              <td>${s.guest_count}</td>
            </tr>`).join("") || `<tr><td colspan="4" class="empty">None yet. These show up after a mature win is split.</td></tr>`}
          </tbody>
        </table>
      </div>
      <div class="card">
        <h2>This wallet</h2>
        ${L.wallet_share && typeof L.wallet_share === "object" && !L.wallet_share.error ? `
          <p class="muted">${L.wallet_share.enabled ? `Sharing ${L.wallet_share.guest_percent || 0}% of each mature win.` : "Share lottery wins is off on the connected wallet."}${L.wallet_share.assetindex ? "" : " Asset index is off. Turn it on once in the wallet so guest roots can be looked up."}</p>
          ${(L.wallet_share.guests || []).map((g) => `<div class="winner"><div>${handle(g.handle)} ${g.ready ? `<span class="badge ok">ready</span>` : `<span class="badge">no holder</span>`}</div></div>`).join("") || `<div class="empty">No guests invited on this wallet.</div>`}
        ` : `<div class="empty">Connect a local wallet to see its guest list. This list is not the hat.</div>`}
      </div>
    </div>`;
  tickCountdown();
}

async function pageRich() {
  const state = pageState(hashQuery());
  const data = await api(`/rich?limit=${state.size}&offset=${state.offset}`);
  if (clampPage(data.total || 0, state)) return;
  app.innerHTML = `
    <h1 class="page-title">XFER holders</h1>
    <div class="card">
      <table>
        <thead><tr><th>#</th><th>Address</th><th>Balance</th></tr></thead>
        <tbody>
          ${(data.items || []).map((r, i) => `<tr><td>${state.offset + i + 1}</td><td>${linkAddr(r.address)}</td><td>${atomsToXfer(r.balance)}</td></tr>`).join("") || `<tr><td colspan="3" class="empty">No balances yet.</td></tr>`}
        </tbody>
      </table>
      ${pagerHtml("rich-pager", data.total || 0, state)}
    </div>`;
  bindPagers();
}

async function pageMempool() {
  const m = await api("/mempool");
  const offline = m.connected === false;
  const summary = offline
    ? "The node is offline, so unconfirmed transactions cannot be read."
    : (m.info ? `${m.info.size || m.count || 0} tx · ${m.info.bytes || 0} bytes` : "Unconfirmed transactions.");
  app.innerHTML = `
    <h1 class="page-title">Mempool</h1>
    <p class="sub">${summary}</p>
    <div class="card">
      <table>
        <thead><tr><th>Txid</th><th>Vin</th><th>Vout</th></tr></thead>
        <tbody>
          ${(m.txs || []).map((t) => `<tr><td>${linkTx(t.txid)}</td><td>${t.vin ?? "—"}</td><td>${t.vout ?? "—"}</td></tr>`).join("") || `<tr><td colspan="3" class="empty">${offline ? "No node is connected." : "No unconfirmed transactions."}</td></tr>`}
        </tbody>
      </table>
    </div>`;
}

function fmtXferShort(atoms) {
  const n = Number(atoms || 0) / 1e8;
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)}B XFER`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M XFER`;
  if (n >= 1e3) return `${n.toLocaleString(undefined, { maximumFractionDigits: 0 })} XFER`;
  return atomsToXfer(atoms);
}

async function pageStats() {
  const S = await api("/stats?pulse=240");
  const rem = blockTimeSeconds() - (Math.floor(Date.now() / 1000) % blockTimeSeconds());
  const kinds = S.assets && S.assets.by_kind ? S.assets.by_kind : [];
  app.innerHTML = `
    <h1 class="page-title">Observatory</h1>
    <p class="sub">Each height is one minute. Height 0 paid nothing. The gold ring is emission so far (about ${esc(S.years_of_emission)} years). The blue ring is this minute.</p>
    <div class="obs-hero">
      <div class="card">${ringClock(S.emission_progress, String(rem).padStart(2, "0"))}</div>
      <div class="grid stats">
        <div class="card stat"><span>Minutes</span><b>${(S.minutes_lived || 0).toLocaleString()}</b></div>
        <div class="card stat"><span>Issued</span><b>${fmtXferShort(S.issued_atoms)}</b></div>
        <div class="card stat"><span>Lifetime</span><b>${fmtXferShort(S.lifetime_atoms)}</b></div>
        <div class="card stat"><span>Next halving</span><b>${(S.years_to_halving || 0).toLocaleString()} yr</b></div>
        <div class="card stat"><span>Draws</span><b>${(S.paydays || 0).toLocaleString()}</b></div>
        <div class="card stat"><span>Winners</span><b>${S.unique_winners || 0}</b></div>
        <div class="card stat"><span>Top handle</span><b>${((S.top_handle_share || 0) * 100).toFixed(1)}%</b></div>
        <div class="card stat" title="Win concentration. Near 1 means one handle won most minutes."><span>HHI</span><b>${(S.hhi || 0).toFixed(3)}</b></div>
      </div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <h2>Subsidy schedule</h2>
      <p class="muted">The subsidy halves every 2,100,000 minutes (about 4 years). The lit step is the current era. Each halving adds one winner that minute.</p>
      ${eraStairs(S.eras, S.height)}
      <p class="muted" style="margin-top:10px">Era ${S.era} · ${fmtXferShort(S.subsidy_atoms)} / minute · next halving at height ${(S.next_halving_height || 0).toLocaleString()} · last paying height ${Number(S.last_paying_height || 0).toLocaleString()}</p>
      <div class="launch-strip" title="Height 0 is genesis and pays nothing. Height 1 is the first draw."><i></i><i></i></div>
      <p class="muted">Black is genesis (no payout). Gold is every later minute, a public draw.</p>
    </div>
    <div class="grid two">
      <div class="card">
        <h2>The hat</h2>
        <p class="muted">Farther from the center means more wins. Each dot is one @handle. Click a name.</p>
        ${hatConstellation(S.handles)}
      </div>
      <div class="card">
        <h2>Luck</h2>
        <p class="muted">Blue is the fair share: 1 divided by hat size, for each minute in the hat. Gold is minutes actually paid.</p>
        ${luckRows(S.handles)}
      </div>
    </div>
    <div class="grid two" style="margin-top:16px">
      <div class="card">
        <h2>Hat pulse</h2>
        <p class="muted">Handles in the hat over the last ${ (S.hat_pulse || []).length } minutes.</p>
        ${pulseChart(S.hat_pulse)}
      </div>
      <div class="card">
        <h2>Assets</h2>
        <div class="kind-pills">
          ${kinds.map((k) => `<div class="kind-pill"><b>${k.n}</b><span>${esc(k.kind || "asset")}</span></div>`).join("") || `<div class="empty">No assets yet.</div>`}
          <div class="kind-pill"><b>${(S.assets && S.assets.ipfs) || 0}</b><span>IPFS</span></div>
        </div>
      </div>
    </div>`;
  tickCountdown();
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
        <b>P2P id</b><div>XFER. Four bytes on each peer message, so this chain stays separate from Bitcoin and Ravencoin.</div>
        <b>RPC</b><div>${s?.rpc_connected ? `connected :${s.rpc_port}` : "offline"}</div>
        <b>Best hash</b><div>${idHtml(chain.bestblockhash || s?.best_hash || "", { full: true })}</div>
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
            <td class="break-anywhere">${esc(x.addr)}</td>
            <td>${esc(x.subver)}</td>
            <td>${x.inbound ? "in" : "out"}</td>
            <td>${x.synced_blocks ?? x.startingheight ?? "—"}</td>
          </tr>`).join("") || `<tr><td colspan="4" class="empty">${p.connected === false ? "The node is offline, so peers cannot be listed." : "No peers connected."}</td></tr>`}
        </tbody>
      </table>
    </div>`;
}

function recordHref(r) {
  const id = r && r.id != null ? String(r.id) : "";
  if (r.type === "tx") return "#/tx/" + encodeURIComponent(id);
  if (r.type === "block") return "#/block/" + encodeURIComponent(id);
  if (r.type === "address") return "#/address/" + encodeURIComponent(id);
  if (r.type === "asset") return "#/asset/" + encodeURIComponent(id);
  if (r.type === "identity") return "#/identity/" + encodeURIComponent(id);
  if (r.type === "node") {
    if (r.address) return "#/address/" + encodeURIComponent(r.address);
    const label = String(r.label || "");
    if (label.startsWith("@") && label.length > 1) return "#/identity/" + encodeURIComponent(label.slice(1));
  }
  return "";
}

function searchWords(r) {
  if (r.type === "tx") return "Transaction";
  if (r.type === "address") return "Address";
  if (r.type === "block") return "Block";
  if (r.type === "asset") return "Asset";
  if (r.type === "identity") return "@handle";
  if (r.type === "node") return "Payout";
  return "Result";
}

function searchBlurb(r) {
  if (r.type === "tx") return "Open it to see who sent what, and which asset moved.";
  if (r.type === "address") return "Open it to see the balance, the assets, and the payments.";
  if (r.type === "block") return "Open it to see who was paid this minute.";
  if (r.type === "asset") return "Open it to see how many exist and who holds them.";
  if (r.type === "identity") return "Open this name to see wins and the address.";
  if (r.type === "node") return "Open the address or @handle for this payout.";
  return "Open this record.";
}

async function pageSearch(q) {
  const data = await api("/search?q=" + encodeURIComponent(q));
  const results = data.results || [];
  if (results.length === 1) {
    const href = recordHref(results[0]);
    if (href) {
      location.hash = href;
      return;
    }
  }
  app.innerHTML = `
    <h1 class="page-title">Search</h1>
    <p class="sub">${results.length ? `${results.length} match${results.length === 1 ? "" : "es"} for “${esc(q)}”. Open one.` : `Nothing matched “${esc(q)}”. Try a block number, an address, an asset name, or an @handle.`}</p>
    <div class="card">
      ${results.map((r) => {
        const href = recordHref(r);
        const primary = (r.type === "tx" || r.type === "address" || r.type === "block") ? (r.id || r.label) : (r.label || r.id);
        const shown = (r.type === "tx" || r.type === "address")
          ? idHtml(primary, { href, full: true })
          : (href ? `<a class="break-anywhere" href="${href}">${esc(r.label || primary)}</a>` : `<span class="break-anywhere">${esc(r.label || primary)}</span>`);
        const open = href ? `<a class="search-open" href="${href}">Open</a>` : "";
        return `<div class="winner search-hit"><div><div><span class="badge">${searchWords(r)}</span></div>${shown}<p class="muted">${searchBlurb(r)}</p></div>${open}</div>`;
      }).join("") || `<div class="empty">Nothing matched that search.</div>`}
    </div>
    ${chainDetail(`<p class="muted">Search read this as ${esc(data.kind || "a query")}.</p>`, "How the search was read")}`;
}

const routes = [
  [/^#\/?$/, pageHome],
  [/^#\/blocks(?:\?.*)?$/, pageBlocks],
  [/^#\/block\/(.+)$/, (m) => pageBlock(decodeURIComponent(m[1]))],
  [/^#\/tx\/(.+)$/, (m) => pageTx(decodeURIComponent(m[1]))],
  [/^#\/address\/([^?]+)(?:\?.*)?$/, (m) => pageAddress(decodeURIComponent(m[1]))],
  [/^#\/assets(?:\?.*)?$/, pageAssets],
  [/^#\/asset\/([^?]+)(?:\?.*)?$/, (m) => pageAsset(decodeURIComponent(m[1]))],
  [/^#\/identity\/(.+)$/, (m) => pageIdentity(decodeURIComponent(m[1]))],
  [/^#\/members(?:\?.*)?$/, pageMembers],
  [/^#\/lottery$/, pageLottery],
  [/^#\/stats$/, pageStats],
  [/^#\/rich(?:\?.*)?$/, pageRich],
  [/^#\/trades(?:\?.*)?$/, pageTrades],
  [/^#\/mempool$/, pageMempool],
  [/^#\/network$/, pageNetwork],
  [/^#\/search\/(.+)$/, (m) => pageSearch(decodeURIComponent(m[1]))],
];

let tradePollTimer = null;
let tradeView = null;

function stopTradePoll() {
  if (tradePollTimer) {
    clearTimeout(tradePollTimer);
    tradePollTimer = null;
  }
}

function tradesHash(side, q) {
  const p = new URLSearchParams();
  if (side && side !== "all") p.set("side", side);
  if (q) p.set("q", q);
  const qs = p.toString();
  return "#/trades" + (qs ? "?" + qs : "");
}

function tradeQuery(side, q) {
  const p = new URLSearchParams();
  if (side && side !== "all") p.set("side", side);
  if (q) p.set("q", q);
  const qs = p.toString();
  return "/trades" + (qs ? "?" + qs : "");
}

function tradeWho(t) {
  const addr = t.trader || "";
  if (!addr) return t.trader_handle ? handle(t.trader_handle) : `<span class="faint">someone</span>`;
  const shown = clipMiddle(addr, 6, 4);
  const named = t.trader_handle && !(LABELS.get(addr) || []).some((row) => row.handle === t.trader_handle) ? `${handle(t.trader_handle)} · ` : "";
  return `${named}${handleChips(addr)}<a class="mono-clip" href="#/address/${encodeURIComponent(addr)}" title="${esc(addr)}">${esc(shown)}</a>`;
}

function tradeAssetLink(name) {
  const full = String(name || "");
  if (!full) return "—";
  const shown = full.length > 28 ? clipMiddle(full, 16, 8) : full;
  return `<a href="#/asset/${encodeURIComponent(full)}" title="${esc(full)}">${esc(shown)}</a>`;
}

function tradeSentence(t) {
  const who = tradeWho(t);
  const qty = formatAssetAmount(t.asset_atoms, "");
  const asset = tradeAssetLink(t.asset);
  const xfer = atomsToXfer(t.xfer_atoms);
  if (t.side === "sell") return `${who} sold ${qty} ${asset} for ${xfer}`;
  return `${who} bought ${qty} ${asset} for ${xfer}`;
}

function tradeMoved(t) {
  const asset = `${formatAssetAmount(t.asset_atoms, "")} ${tradeAssetLink(t.asset)}`;
  const xfer = atomsToXfer(t.xfer_atoms);
  if (t.side === "sell") return `They received ${xfer} and sent ${asset}.`;
  if (t.tokens_pending) return `They paid ${xfer}. The ${asset} are not delivered yet.`;
  return `They paid ${xfer} and received ${asset}.`;
}

function tradePriceDetail(t) {
  const price = atomsToXfer(t.price_atoms);
  const fee = atomsToXfer(t.fee_atoms);
  return `<details class="raw-json"><summary>Price and fee</summary><p class="muted">${price} per unit · Fee ${fee}</p></details>`;
}

function tradeStatus(t) {
  if (t.tokens_pending) {
    return `<span class="trade-status" title="Buy is in. Tokens are not delivered yet."><i class="dot wait"></i> Tokens on the way</span>`;
  }
  if (t.confirmed) {
    const n = t.confirmations ? ` · ${t.confirmations}` : "";
    return `<span class="trade-status" title="In a block."><i class="dot ok"></i> Confirmed${n}</span>`;
  }
  return `<span class="trade-status" title="Not in a block yet."><i class="dot wait"></i> Confirming</span>`;
}

function tradeCard(t, extra) {
  const when = t.time
    ? `<span class="trade-when" title="${esc(fmtTime(t.time))}">${esc(timeAgo(t.time))}</span>`
    : `<span class="trade-when">time unknown</span>`;
  const block = t.height != null ? linkBlock(t.height) : `<span class="faint">not in a block yet</span>`;
  const whoLabel = t.side === "sell" ? "Seller" : "Buyer";
  const cls = "card trade-card" + (extra ? " " + extra : "");
  return `<article class="${cls}" id="trade-${esc(t.txid)}">
    <div class="trade-top">
      <span class="badge ${t.side === "sell" ? "sell" : "buy"}">${t.side === "sell" ? "SELL" : "BUY"}</span>
      ${t.venue === "book" ? `<span class="badge" title="Filled by the Launch order book.">BOOK</span>` : ""}
      <div class="trade-sentence">${tradeSentence(t)}</div>
      ${tradeStatus(t)}
    </div>
    <p class="trade-moved">${tradeMoved(t)}</p>
    ${tradePriceDetail(t)}
    <div class="trade-links">
      ${when}
      <div class="trade-link stack"><span class="faint">Tx</span>${linkTx(t.txid, { full: true })}</div>
      <div class="trade-link"><span class="faint">Block</span><span class="id-line">${block}</span></div>
      <div class="trade-link"><span class="faint">Asset</span>${linkAsset(t.asset)}</div>
      <div class="trade-link stack"><span class="faint">${whoLabel}</span>${linkAddr(t.trader, { full: true })}</div>
    </div>
  </article>`;
}

function tradeStatsHtml(s) {
  const stats = s || {};
  return `<div class="grid stats" id="trade-stats">
    <div class="card stat"><span>Trades today</span><b>${stats.trades_today ?? 0}</b></div>
    <div class="card stat"><span>XFER volume today</span><b>${atomsToXfer(stats.volume_today_atoms)}</b></div>
    <div class="card stat"><span>Pending</span><b>${stats.pending ?? 0}</b></div>
  </div>`;
}

function tradeRecency(t) {
  const txid = String(t.txid || "");
  const time = Number(t.time) || 0;
  const n = t.n == null || t.n === "" ? -1 : Number(t.n);
  const nKey = Number.isFinite(n) ? n : -1;
  if (t.height == null || t.height === "") return [1, time, nKey, txid];
  const height = Number(t.height);
  return [0, Number.isFinite(height) ? height : -1, nKey, txid];
}

function compareTradesNewest(a, b) {
  const ka = tradeRecency(a);
  const kb = tradeRecency(b);
  for (let i = 0; i < 3; i++) {
    if (ka[i] !== kb[i]) return kb[i] - ka[i];
  }
  if (ka[3] === kb[3]) return 0;
  return ka[3] < kb[3] ? 1 : -1;
}

function sortTradesNewest(items) {
  return (items || []).slice().sort(compareTradesNewest);
}

function renderTradeList(freshIds) {
  const list = $("#trade-list");
  if (!list || !tradeView) return;
  const items = sortTradesNewest(tradeView.items || []);
  tradeView.items = items;
  const fresh = freshIds instanceof Set ? freshIds : new Set();
  list.innerHTML = items.length
    ? items.map((t) => tradeCard(t, fresh.has(t.txid) ? "trade-in" : "")).join("")
    : `<div class="card"><div class="empty">${tradeView.q ? "No Launch trades match that search." : "No Launch trades yet today."}</div></div>`;
}

async function refreshTradeHead() {
  if (!tradeView) return;
  const hash = location.hash || "";
  if (!hash.startsWith("#/trades")) return;
  try {
    const data = await api(tradeQuery(tradeView.side, tradeView.q));
    if ((location.hash || "") !== hash) return;
    const stats = $("#trade-stats");
    if (stats) stats.outerHTML = tradeStatsHtml(data.stats);
    const prev = new Set((tradeView.items || []).map((t) => t.txid));
    const incoming = data.items || [];
    const fresh = new Set();
    for (const t of incoming) {
      if (t.txid && !prev.has(t.txid)) fresh.add(t.txid);
    }
    tradeView.items = incoming;
    renderTradeList(fresh);
  } catch {
    /* next poll retries */
  }
}

function scheduleTradePoll() {
  stopTradePoll();
  tradePollTimer = setTimeout(async () => {
    await refreshTradeHead();
    if ((location.hash || "").startsWith("#/trades")) scheduleTradePoll();
  }, 7500);
}

async function pageTrades() {
  stopTradePoll();
  const params = hashQuery();
  const side = params.get("side") === "buy" || params.get("side") === "sell" ? params.get("side") : "all";
  const q = (params.get("q") || "").trim();
  tradeView = { side, q, items: [] };
  app.innerHTML = `
    <h1 class="page-title">Trades</h1>
    <p class="sub">Today’s Launch trades, since midnight ET. Buys and sells are listed together, newest first. Use Buys or Sells to filter. Older ones stay on the chain. Confirmed means the trade is in a block. Tokens on the way means the tokens are not delivered yet. BOOK means the order book filled it.</p>
    <div id="trade-stats-slot">${tradeStatsHtml(null)}</div>
    <div class="toolbar">
      <div class="tabs" id="trade-tabs">
        <button type="button" data-side="all" class="${side === "all" ? "on" : ""}">All</button>
        <button type="button" data-side="buy" class="${side === "buy" ? "on" : ""}">Buys</button>
        <button type="button" data-side="sell" class="${side === "sell" ? "on" : ""}">Sells</button>
      </div>
      <form class="search" id="trade-search">
        <input id="trade-q" type="search" value="${esc(q)}" placeholder="Asset, @handle, address, or tx" autocomplete="off" />
        <button type="submit">Filter</button>
      </form>
    </div>
    <div class="trade-list" id="trade-list"><div class="card"><div class="loading">Loading trades…</div></div></div>`;
  document.querySelectorAll("#trade-tabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const next = btn.getAttribute("data-side") || "all";
      const typed = ($("#trade-q") && $("#trade-q").value.trim()) || "";
      location.hash = tradesHash(next, typed || q);
    });
  });
  const form = $("#trade-search");
  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const typed = ($("#trade-q") && $("#trade-q").value.trim()) || "";
      location.hash = tradesHash(side, typed);
    });
  }
  try {
    const data = await api(tradeQuery(side, q));
    tradeView.items = data.items || [];
    const slot = $("#trade-stats-slot");
    if (slot) slot.innerHTML = tradeStatsHtml(data.stats);
    renderTradeList();
  } catch (e) {
    const list = $("#trade-list");
    if (list) list.innerHTML = `<div class="card"><h2>Could not load trades</h2><p class="err">${esc(e.message || "Try again.")}</p></div>`;
  }
  if ((location.hash || "").startsWith("#/trades")) scheduleTradePoll();
}

async function route() {
  stopTradePoll();
  setNav();
  const hash = location.hash || "#/";
  app.innerHTML = `<div class="card"><div class="loading">Loading…</div></div>`;
  try {
    await refreshStatus();
    for (const [re, fn] of routes) {
      const m = hash.match(re);
      if (m) {
        await fn(m);
        return;
      }
    }
    app.innerHTML = `<div class="card"><h2>Page not found</h2><p class="err">That page is not part of this explorer.</p></div>`;
  } catch (e) {
    const msg = e && e.status === 404 ? "Nothing was found for that link." : (e.message || "Something went wrong. Try again.");
    app.innerHTML = `<div class="card"><h2>Could not load this page</h2><p class="err">${esc(msg)}</p></div>`;
  }
}

$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#q").value.trim();
  if (!q) return;
  location.hash = "#/search/" + encodeURIComponent(q);
});

window.addEventListener("hashchange", route);
route().finally(() => scheduleLiveRefresh());
setInterval(() => {
  tickCountdown();
}, 1000);
