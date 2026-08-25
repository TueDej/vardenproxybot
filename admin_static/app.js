const $ = s => document.querySelector(s);
let currentTab = 'orders';
let ordersPage = 1, usersPage = 1;
let totalOrdersPages = 1, totalUsersPages = 1;

function fmtAmount(n){ return (n||0).toLocaleString('en-US') + ' T'; }
function fmtDate(s){ if(!s) return '—'; try{ const d=new Date(s); return d.toLocaleString('en-GB',{dateStyle:'short',timeStyle:'short'}); }catch{ return s; } }
function pill(status){ return `<span class="pill ${status}">${status}</span>`; }

async function fetchJSON(url){
  const res = await fetch(url, {credentials:'include'});
  if(res.status===401){ document.body.innerHTML = '<div style="padding:40px;text-align:center"><h2>401 Unauthorized</h2><p>Check ADMIN_PANEL_USER/PASS in .env</p></div>'; throw new Error('auth'); }
  if(!res.ok) throw new Error(await res.text());
  return res.json();
}

async function loadStats(){
  try{
    const s = await fetchJSON('/admin/api/stats');
    $('#stat-total').textContent = s.total_orders;
    $('#stat-pending').textContent = s.by_status.pending||0;
    $('#stat-approved').textContent = s.by_status.approved||0;
    $('#stat-revenue').textContent = fmtAmount(s.total_revenue);
    $('#stat-users').textContent = s.total_users;
    $('#stat-today').textContent = s.today_orders;
  }catch(e){ console.error(e); }
}

function buildQuery(page, limit){
  const q = $('#q').value.trim();
  const status = $('#status').value;
  const pkg = $('#package').value;
  const from = $('#from').value;
  const to = $('#to').value;
  const params = new URLSearchParams();
  if(q) params.set('q', q);
  if(status) params.set('status', status);
  if(pkg) params.set('package', pkg);
  if(from) params.set('from', from);
  if(to) params.set('to', to);
  params.set('page', page);
  params.set('limit', limit||50);
  // populate package filter once
  return params.toString();
}

async function loadOrders(){
  const qs = buildQuery(ordersPage, 50);
  const data = await fetchJSON('/admin/api/orders?'+qs);
  const tbody = $('#orders-body');
  if(!data.items.length){ tbody.innerHTML = '<tr><td colspan="9" class="muted">No orders</td></tr>'; }
  else{
    tbody.innerHTML = data.items.map(o=> `
      <tr>
        <td><code>#${o.id}</code></td>
        <td>
          <div>${o.first_name? escapeHtml(o.first_name):'—'} <span class="muted">${o.username?'@'+o.username:''}</span></div>
          <div class="muted"><code>${o.telegram_id||'—'}</code></div>
        </td>
        <td>${escapeHtml(o.package_label)}</td>
        <td>${fmtAmount(o.amount_toomans)}</td>
        <td>${pill(o.status)}</td>
        <td>${o.payment_ref_id? `<code>${o.payment_ref_id}</code><span class="copy" onclick="copyText('${o.payment_ref_id}')">copy</span>`: '<span class="muted">—</span>'}</td>
        <td><code title="${o.payment_authority||''}">${o.payment_authority? o.payment_authority.slice(0,12)+'…':'—'}</code>${o.payment_authority? `<span class="copy" onclick="copyText('${o.payment_authority}')">copy</span>`:''}</td>
        <td><code>${o.panel_email||'—'}</code></td>
        <td>${fmtDate(o.created_at)}</td>
      </tr>
    `).join('');
  }
  // packages dropdown populate once
  const pkgSel = $('#package');
  if(pkgSel.options.length===1){
    const pkgs = [...new Set(data.items.map(x=>x.package_label))];
    pkgs.forEach(p=>{ const opt=document.createElement('option'); opt.value=p; opt.textContent=p; pkgSel.appendChild(opt); });
  }
  renderPag('orders', data.total, data.page, data.limit);
}

async function loadUsers(){
  const qs = buildQuery(usersPage, 50);
  const data = await fetchJSON('/admin/api/users?'+qs);
  const tbody = $('#users-body');
  if(!data.items.length){ tbody.innerHTML = '<tr><td colspan="6" class="muted">No users</td></tr>'; }
  else{
    tbody.innerHTML = data.items.map(u=> `
      <tr>
        <td><code>${u.id}</code></td>
        <td><code>${u.telegram_id}</code></td>
        <td>${u.username? '@'+u.username:'—'}</td>
        <td>${escapeHtml(u.first_name||'—')}</td>
        <td>${u.order_count}</td>
        <td>${fmtDate(u.created_at)}</td>
      </tr>
    `).join('');
  }
  renderPag('users', data.total, data.page, data.limit);
}

function renderPag(kind, total, page, limit){
  const el = kind==='orders'? $('#orders-pag'): $('#users-pag');
  const pages = Math.max(1, Math.ceil(total/limit));
  if(kind==='orders') totalOrdersPages=pages; else totalUsersPages=pages;
  el.innerHTML = `
    <button ${page<=1?'disabled':''} onclick="${kind}Page(${page-1})">Prev</button>
    <span class="muted">Page ${page} / ${pages} — ${total} total</span>
    <button ${page>=pages?'disabled':''} onclick="${kind}Page(${page+1})">Next</button>
  `;
}
window.ordersPage = n=>{ ordersPage=n; loadOrders(); };
window.usersPage = n=>{ usersPage=n; loadUsers(); };
window.copyText = t=>{ navigator.clipboard.writeText(t).then(()=>{ const el=document.createElement('div'); el.textContent='Copied'; el.style.cssText='position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#0f172a;color:#fff;padding:8px 12px;border-radius:10px;font-size:13px'; document.body.appendChild(el); setTimeout(()=>el.remove(),900); }); };
function escapeHtml(s){ return String(s).replace(/[&<>\"]/g,c=>({ '&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;' }[c])); }

$('#apply').addEventListener('click', ()=>{ ordersPage=1; usersPage=1; currentTab==='orders'? loadOrders(): loadUsers(); });
$('#clear').addEventListener('click', ()=>{ $('#q').value=''; $('#status').value=''; $('#package').value=''; $('#from').value=''; $('#to').value=''; ordersPage=1; usersPage=1; loadOrders(); loadUsers(); });
$('#q').addEventListener('keydown', e=>{ if(e.key==='Enter'){ ordersPage=1; usersPage=1; currentTab==='orders'? loadOrders(): loadUsers(); }});
let qTimer;
$('#q').addEventListener('input', ()=>{ clearTimeout(qTimer); qTimer=setTimeout(()=>{ ordersPage=1; usersPage=1; currentTab==='orders'? loadOrders(): loadUsers(); }, 400); });

document.querySelectorAll('.tab').forEach(btn=>{
  btn.addEventListener('click', ()=>{
    document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    currentTab = btn.dataset.tab;
    $('#orders-panel').classList.toggle('hidden', currentTab!=='orders');
    $('#users-panel').classList.toggle('hidden', currentTab!=='users');
    if(currentTab==='orders') loadOrders(); else loadUsers();
  });
});

// init
loadStats(); loadOrders(); loadUsers();
setInterval(loadStats, 30000);
