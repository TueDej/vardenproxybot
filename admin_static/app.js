const $ = (s) => document.querySelector(s);
let currentTab = "orders";
let ordersPage = 1,
  usersPage = 1;

// ── helpers ──
function fmtAmount(n) {
  return (n || 0).toLocaleString("en-US") + " T";
}
function fmtDate(s) {
  if (!s) return "—";
  try {
    const d = new Date(s);
    return d.toLocaleString("en-GB", { dateStyle: "short", timeStyle: "short" });
  } catch {
    return s;
  }
}
function pill(status) {
  return `<span class="pill ${status}">${status}</span>`;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(s) {
  return String(s).replace(/['"\\]/g, (c) => ({ "'": "&#39;", '"': "&quot;", "\\": "&#92;" }[c]));
}
async function fetchJSON(url, opts = {}) {
  const res = await fetch(url, { credentials: "include", ...opts });
  if (res.status === 401) {
    document.body.innerHTML =
      '<div style="padding:40px;text-align:center"><h2>401 Unauthorized</h2><p>Check ADMIN_PANEL_USER/PASS in .env</p></div>';
    throw new Error("auth");
  }
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// ── stats ──
async function loadStats() {
  try {
    const s = await fetchJSON("/admin/api/stats");
    $("#stat-total").textContent = s.total_orders;
    $("#stat-pending").textContent = s.by_status.pending || 0;
    $("#stat-approved").textContent = s.by_status.approved || 0;
    $("#stat-revenue").textContent = fmtAmount(s.total_revenue);
    $("#stat-users").textContent = s.total_users;
    $("#stat-today").textContent = s.today_orders;
    const sb = $("#sidebar-stats");
    if (sb) sb.textContent = `${s.total_orders} orders • ${s.total_users} users`;
  } catch (e) {
    console.error(e);
  }
}

// ── orders/users query ──
function buildQuery(page, limit) {
  const q = $("#q").value.trim();
  const status = $("#status").value;
  const pkg = $("#package").value;
  const from = $("#from").value;
  const to = $("#to").value;
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (status) params.set("status", status);
  if (pkg) params.set("package", pkg);
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  params.set("page", page);
  params.set("limit", limit || 50);
  return params.toString();
}

async function loadOrders() {
  const qs = buildQuery(ordersPage, 50);
  const data = await fetchJSON("/admin/api/orders?" + qs);
  const tbody = $("#orders-body");
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="muted">No orders</td></tr>';
  } else {
    tbody.innerHTML = data.items
      .map(
        (o) => `
      <tr>
        <td><code>#${o.id}</code></td>
        <td>
          <div>${o.first_name ? escapeHtml(o.first_name) : "—"} <span class="muted">${o.username ? "@" + escapeHtml(o.username) : ""}</span></div>
          <div class="muted"><code>${o.telegram_id || "—"}</code></div>
        </td>
        <td>${escapeHtml(o.package_label)}</td>
        <td>${fmtAmount(o.amount_toomans)}</td>
        <td>${pill(o.status)}</td>
        <td>${o.payment_ref_id ? `<code>${escapeHtml(o.payment_ref_id)}</code><span class="copy" data-copy="${escapeAttr(o.payment_ref_id)}" onclick="copyText(this.dataset.copy)">copy</span>` : '<span class="muted">—</span>'}</td>
        <td><code title="${escapeAttr(o.payment_authority || "")}">${o.payment_authority ? escapeHtml(o.payment_authority.slice(0, 12)) + "…" : "—"}</code>${o.payment_authority ? `<span class="copy" data-copy="${escapeAttr(o.payment_authority)}" onclick="copyText(this.dataset.copy)">copy</span>` : ""}</td>
        <td><code>${escapeHtml(o.panel_email || "—")}</code></td>
        <td>${fmtDate(o.created_at)}</td>
      </tr>
    `
      )
      .join("");
  }
  const pkgSel = $("#package");
  if (pkgSel.options.length === 1) {
    const pkgs = [...new Set(data.items.map((x) => x.package_label))];
    pkgs.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = p;
      pkgSel.appendChild(opt);
    });
  }
  renderPag("orders", data.total, data.page, data.limit);
}

async function loadUsers() {
  const qs = buildQuery(usersPage, 50);
  const data = await fetchJSON("/admin/api/users?" + qs);
  const tbody = $("#users-body");
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="muted">No users</td></tr>';
  } else {
    tbody.innerHTML = data.items
      .map(
        (u) => `
      <tr>
        <td><code>${u.id}</code></td>
        <td><code>${u.telegram_id}</code></td>
        <td>${u.username ? "@" + escapeHtml(u.username) : "—"}</td>
        <td>${escapeHtml(u.first_name || "—")}</td>
        <td>${u.order_count}</td>
        <td>${fmtDate(u.created_at)}</td>
      </tr>
    `
      )
      .join("");
  }
  renderPag("users", data.total, data.page, data.limit);
}

function renderPag(kind, total, page, limit) {
  const el = kind === "orders" ? $("#orders-pag") : $("#users-pag");
  const pages = Math.max(1, Math.ceil(total / limit));
  el.innerHTML = `
    <button ${page <= 1 ? "disabled" : ""} onclick="${kind}Page(${page - 1})">Prev</button>
    <span class="muted">Page ${page} / ${pages} — ${total} total</span>
    <button ${page >= pages ? "disabled" : ""} onclick="${kind}Page(${page + 1})">Next</button>
  `;
}
window.ordersPage = (n) => {
  ordersPage = n;
  loadOrders();
};
window.usersPage = (n) => {
  usersPage = n;
  loadUsers();
};
window.copyText = (t) => {
  navigator.clipboard.writeText(t).then(() => {
    const el = document.createElement("div");
    el.textContent = "Copied";
    el.style.cssText =
      "position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#16201b;color:#dff0e3;padding:8px 12px;border-radius:10px;font-size:13px;border:1px solid #25352c";
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 900);
  });
};

// ── packages ──
let packagesState = null;
let originalPackagesState = null;

function calcDiscount(gb) {
  if (gb <= 10 || gb === 0) return 0;
  return Math.min(0.28, 0.1 + (gb - 20) * 0.003);
}
function calcPrice(base, gb, manual) {
  if (gb === 0) return manual != null ? manual : 500000;
  const d = calcDiscount(gb);
  const raw = base * gb * (1 - d);
  return Math.max(5000, Math.floor(raw / 5000) * 5000);
}

function cloneState(s) {
  return JSON.parse(JSON.stringify(s));
}

async function loadPackages() {
  try {
    const data = await fetchJSON("/admin/api/packages");
    packagesState = cloneState(data);
    originalPackagesState = cloneState(data);
    renderPackages();
  } catch (e) {
    console.error(e);
    $("#packages-body").innerHTML = '<tr><td colspan="5" class="muted">Failed to load packages</td></tr>';
  }
}

let dragIdx = null;

function renderPackages() {
  if (!packagesState) return;
  $("#base-price").value = packagesState.base_price_per_gb;
  $("#payments-paused").checked = !!packagesState.payments_paused;
  const tbody = $("#packages-body");
  tbody.innerHTML = packagesState.packages
    .map((p, idx) => {
      const disc = p.data_gb === 0 ? "—" : (calcDiscount(p.data_gb) * 100).toFixed(1) + "%";
      const price = p.data_gb === 0 ? p.price : calcPrice(packagesState.base_price_per_gb, p.data_gb, p.price);
      const priceCell =
        p.data_gb === 0
          ? `<input type="number" data-idx="${idx}" data-field="price" value="${p.price}" min="1000" max="10000000" step="1000">`
          : `<span>${fmtAmount(price)}</span> <span class="muted small">[${disc} off]</span>`;
      return `
      <tr data-idx="${idx}" draggable="true">
        <td class="drag-handle" title="Drag to reorder">⋮⋮</td>
        <td><input type="text" data-idx="${idx}" data-field="label" value="${escapeHtml(p.label)}" placeholder="10GB"></td>
        <td><input type="number" data-idx="${idx}" data-field="data_gb" value="${p.data_gb}" min="0" max="10000" step="1"></td>
        <td class="muted">${disc}</td>
        <td>${priceCell}</td>
        <td><button class="btn" onclick="removePkg(${idx})">✕</button></td>
      </tr>
    `;
    })
    .join("");

  // live recalc on base change
  tbody.querySelectorAll('input').forEach((inp) => {
    inp.addEventListener('input', onPkgInput);
  });

  // drag & drop for reordering
  tbody.querySelectorAll('tr').forEach((tr) => {
    tr.addEventListener('dragstart', (e) => {
      dragIdx = parseInt(tr.dataset.idx, 10);
      tr.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      // Required for Firefox
      e.dataTransfer.setData('text/plain', String(dragIdx));
      // Delay hide
      setTimeout(() => (tr.style.opacity = '0.4'), 0);
    });
    tr.addEventListener('dragend', () => {
      tr.classList.remove('dragging');
      tr.style.opacity = '';
      dragIdx = null;
      tbody.querySelectorAll('tr').forEach((r) => r.classList.remove('drag-over'));
    });
    tr.addEventListener('dragover', (e) => {
      e.preventDefault();
      tr.classList.add('drag-over');
    });
    tr.addEventListener('dragleave', () => {
      tr.classList.remove('drag-over');
    });
    tr.addEventListener('drop', (e) => {
      e.preventDefault();
      tr.classList.remove('drag-over');
      const targetIdx = parseInt(tr.dataset.idx, 10);
      if (dragIdx === null || targetIdx === dragIdx) return;
      const [moved] = packagesState.packages.splice(dragIdx, 1);
      // Insert at drop target position
      const insertAt = Math.max(0, Math.min(targetIdx, packagesState.packages.length));
      packagesState.packages.splice(insertAt, 0, moved);
      renderPackages();
    });
  });
}

function onPkgInput(e) {
  const target = e.target;
  const idx = parseInt(target.dataset.idx, 10);
  const field = target.dataset.field;
  const val = target.value;

  if (field === "label") {
    packagesState.packages[idx][field] = val;
    return;
  }

  if (field === "price") {
    packagesState.packages[idx][field] = parseInt(val || "0", 10);
    return;
  }

  if (field === "data_gb") {
    const newGb = parseInt(val || "0", 10);
    packagesState.packages[idx][field] = Number.isNaN(newGb) ? 0 : newGb;

    // Update discount and price cells inline without destroying focused input
    const row = target.closest("tr");
    if (row) {
      const discCell = row.cells[3];
      const priceCell = row.cells[4];
      const gb = packagesState.packages[idx].data_gb;
      const disc = gb === 0 ? "—" : (calcDiscount(gb) * 100).toFixed(1) + "%";
      discCell.textContent = disc;
      discCell.className = "muted";
      if (gb === 0) {
        // Switched to Unlimited — need price input
        priceCell.innerHTML = `<input type="number" data-idx="${idx}" data-field="price" value="${packagesState.packages[idx].price || 500000}" min="1000" max="10000000" step="1000">`;
        const newPriceInput = priceCell.querySelector('input');
        if (newPriceInput) newPriceInput.addEventListener('input', onPkgInput);
      } else {
        const price = calcPrice(packagesState.base_price_per_gb, gb);
        priceCell.innerHTML = `<span>${fmtAmount(price)}</span> <span class="muted small">[${disc} off]</span>`;
      }
    } else {
      // Fallback: full rerender if row not found
      renderPackages();
    }
  }
}

function collectPackagesFromDOM() {
  // already in packagesState via input listeners, just read base/paused from inputs
  const base = parseInt($("#base-price").value || "0", 10);
  const paused = $("#payments-paused").checked;
  // packagesState already holds edits, but ensure data_gb is int
  const pkgs = packagesState.packages.map((p) => ({
    label: String(p.label).trim(),
    data_gb: parseInt(p.data_gb, 10) || 0,
    price: p.data_gb === 0 ? parseInt(p.price, 10) || 0 : calcPrice(base, parseInt(p.data_gb, 10)),
  }));
  return { base_price_per_gb: base, packages: pkgs, payments_paused: paused };
}

window.removePkg = (idx) => {
  packagesState.packages.splice(idx, 1);
  renderPackages();
};

$("#pkg-add")?.addEventListener("click", () => {
  packagesState.packages.push({ label: "NewPack", data_gb: 10, price: calcPrice(packagesState.base_price_per_gb, 10) });
  renderPackages();
});

$("#base-price")?.addEventListener("input", () => {
  const base = parseInt($("#base-price").value || "0", 10);
  packagesState.base_price_per_gb = Number.isNaN(base) ? 0 : base;
  // Inline update all price cells without full rerender to keep focus on base input and avoid churn
  const rows = document.querySelectorAll("#packages-body tr[data-idx]");
  rows.forEach((row) => {
    const idx = parseInt(row.dataset.idx, 10);
    const p = packagesState.packages[idx];
    if (!p) return;
    const disc = p.data_gb === 0 ? "—" : (calcDiscount(p.data_gb) * 100).toFixed(1) + "%";
    row.cells[3].textContent = disc;
    const priceCell = row.cells[4];
    if (p.data_gb === 0) return; // manual price, keep input
    const price = calcPrice(base, p.data_gb);
    priceCell.innerHTML = `<span>${fmtAmount(price)}</span> <span class="muted small">[${disc} off]</span>`;
  });
});

$("#pkg-save")?.addEventListener("click", async () => {
  const payload = collectPackagesFromDOM();
  const btn = $("#pkg-save");
  btn.disabled = true;
  btn.textContent = "Saving…";
  $("#pkg-msg").textContent = "";
  try {
    const res = await fetch("/admin/api/packages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    packagesState = cloneState(data);
    originalPackagesState = cloneState(data);
    renderPackages();
    $("#pkg-msg").textContent = "Saved ✓ — new packages active";
    $("#pkg-msg").style.color = "#5fb68a";
    // refresh orders filter packages
    setTimeout(() => (document.querySelector('#package').innerHTML = '<option value="">All packages</option>'), 0);
  } catch (e) {
    $("#pkg-msg").textContent = "Save failed: " + e.message;
    $("#pkg-msg").style.color = "#e07a7a";
  } finally {
    btn.disabled = false;
    btn.textContent = "Save";
  }
});

$("#pkg-discard")?.addEventListener("click", () => {
  packagesState = cloneState(originalPackagesState);
  renderPackages();
  $("#pkg-msg").textContent = "Discarded — reverted to last saved";
  $("#pkg-msg").style.color = "#8aa098";
});

$("#payments-paused")?.addEventListener("change", () => {
  // update state locally, will be saved on Save
  packagesState.payments_paused = $("#payments-paused").checked;
});

// ── nav ──
function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll(".nav-item, .tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === tab);
  });
  $("#orders-panel").classList.toggle("hidden", tab !== "orders");
  $("#users-panel").classList.toggle("hidden", tab !== "users");
  $("#packages-panel").classList.toggle("hidden", tab !== "packages");
  $("#page-title").textContent = tab.charAt(0).toUpperCase() + tab.slice(1);
  document.querySelector("#toolbar-orders").style.display = tab === "packages" ? "none" : "flex";
  if (tab === "orders") loadOrders();
  else if (tab === "users") loadUsers();
  else if (tab === "packages") loadPackages();
}

document.querySelectorAll(".nav-item, .tab").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

// ── toolbar events ──
$("#apply")?.addEventListener("click", () => {
  ordersPage = 1;
  usersPage = 1;
  currentTab === "orders" ? loadOrders() : loadUsers();
});
$("#clear")?.addEventListener("click", () => {
  $("#q").value = "";
  $("#status").value = "";
  $("#package").value = "";
  $("#from").value = "";
  $("#to").value = "";
  ordersPage = 1;
  usersPage = 1;
  loadOrders();
  loadUsers();
});
$("#q")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    ordersPage = 1;
    usersPage = 1;
    currentTab === "orders" ? loadOrders() : loadUsers();
  }
});
let qTimer;
$("#q")?.addEventListener("input", () => {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => {
    ordersPage = 1;
    usersPage = 1;
    currentTab === "orders" ? loadOrders() : loadUsers();
  }, 400);
});

// init
loadStats();
loadOrders();
loadUsers();
setInterval(loadStats, 30000);
