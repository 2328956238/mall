// ============================================================
// ⚡ Flash Sales Platform - Frontend Application
// ============================================================
// API 基础路径
const API_BASE = '/api/v1';
const ADMIN_BASE = '/admin/api';
const WS_BASE = `ws://${window.location.host}/ws`;

// ============================================================
// State Management
// ============================================================
const state = {
    token: localStorage.getItem('token') || null,
    user: JSON.parse(localStorage.getItem('user') || 'null'),
    currentPage: 'home',
    seckillFilter: 'all',
    adminTab: 'admin-products',
    websocket: null,
};

// ============================================================
// API Helpers
// ============================================================
async function apiRequest(url, options = {}) {
    const headers = { 'Content-Type': 'application/json', ...options.headers };
    if (state.token) {
        headers['Authorization'] = `Bearer ${state.token}`;
    }
    try {
        const response = await fetch(url, { ...options, headers });
        if (response.status === 401) {
            state.token = null;
            state.user = null;
            localStorage.removeItem('token');
            localStorage.removeItem('user');
            updateAuthUI();
            showToast('登录已过期，请重新登录', 'error');
            showPage('login');
            return null;
        }
        if (!response.ok) {
            const errData = await response.json().catch(() => ({}));
            throw new Error(errData.detail || `请求失败 (${response.status})`);
        }
        return await response.json();
    } catch (err) {
        if (err.name !== 'TypeError') throw err;
        throw new Error('网络错误：无法连接到服务器');
    }
}

// ============================================================
// Auth
// ============================================================
async function handleLogin(e) {
    e.preventDefault();
    const email = document.getElementById('loginEmail').value;
    const password = document.getElementById('loginPassword').value;
    const errorEl = document.getElementById('loginError');
    errorEl.style.display = 'none';

    try {
        const formData = new URLSearchParams();
        formData.append('username', email);
        formData.append('password', password);

        const response = await fetch(`${API_BASE}/auth/token`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: formData,
        });

        if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || '登录失败');
        }

        const data = await response.json();
        state.token = data.access_token;
        localStorage.setItem('token', data.access_token);

        await fetchCurrentUser();
        showToast('登录成功！', 'success');
        showPage('home');
    } catch (err) {
        errorEl.textContent = err.message;
        errorEl.style.display = 'block';
    }
}

async function fetchCurrentUser() {
    try {
        const data = await apiRequest(`${API_BASE}/users/me`);
        if (data && data.data) {
            state.user = data.data;
            localStorage.setItem('user', JSON.stringify(data.data));
            updateAuthUI();
            return data.data;
        }
    } catch (err) {
        console.error('Failed to fetch user:', err);
    }
    return null;
}

async function handleRegister(e) {
    e.preventDefault();
    const email = document.getElementById('regEmail').value;
    const name = document.getElementById('regName').value;
    const password = document.getElementById('regPassword').value;
    const errorEl = document.getElementById('registerError');
    errorEl.style.display = 'none';

    try {
        await apiRequest(`${API_BASE}/auth/register`, {
            method: 'POST',
            body: JSON.stringify({ email, full_name: name, password }),
        });
        showToast('注册成功！请登录', 'success');
        showPage('login');
    } catch (err) {
        errorEl.textContent = err.message;
        errorEl.style.display = 'block';
    }
}

function logout() {
    state.token = null;
    state.user = null;
    localStorage.removeItem('token');
    localStorage.removeItem('user');
    if (state.websocket) {
        state.websocket.close();
        state.websocket = null;
    }
    updateAuthUI();
    showToast('已退出登录', 'info');
    showPage('home');
}

function updateAuthUI() {
    const userInfo = document.getElementById('userInfo');
    const guestInfo = document.getElementById('guestInfo');
    const userNameDisplay = document.getElementById('userNameDisplay');
    const adminLink = document.querySelector('.admin-link');

    if (state.user) {
        userInfo.style.display = 'flex';
        guestInfo.style.display = 'none';
        userNameDisplay.textContent = state.user.full_name || state.user.email;
        adminLink.style.display = state.user.is_admin ? 'inline' : 'none';
        connectWebSocket();
    } else {
        userInfo.style.display = 'none';
        guestInfo.style.display = 'flex';
        adminLink.style.display = 'none';
    }
}

// ============================================================
// WebSocket
// ============================================================
function connectWebSocket() {
    if (!state.user || !state.token) return;
    if (state.websocket) return;

    const ws = new WebSocket(`${WS_BASE}/orders/${state.user.id}`);
    ws.onopen = () => console.log('WebSocket connected');
    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.event === 'order_status') {
                showToast(`订单 ${data.order_id.slice(0,8)}... 状态更新: ${data.status}`, 'info');
                if (state.currentPage === 'orders') loadOrders();
                if (state.currentPage === 'admin' && state.adminTab === 'admin-orders') loadAdminOrders();
            }
        } catch (e) {
            console.error('WebSocket message error:', e);
        }
    };
    ws.onclose = () => {
        state.websocket = null;
        setTimeout(connectWebSocket, 5000);
    };
    ws.onerror = () => ws.close();
    state.websocket = ws;
}

// ============================================================
// Page Navigation
// ============================================================
function showPage(page) {
    state.currentPage = page;
    document.querySelectorAll('.nav-link').forEach(link => {
        link.classList.toggle('active', link.dataset.page === page);
    });
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    const target = document.getElementById(`page-${page}`);
    if (target) {
        target.classList.add('active');
        switch (page) {
            case 'home': loadHomeData(); break;
            case 'products': loadProducts(); break;
            case 'seckill': loadSeckillProducts(); break;
            case 'orders': loadOrders(); break;
            case 'admin': loadAdminPage(); break;
        }
    }
}

// ============================================================
// Home Page
// ============================================================
async function loadHomeData() {
    try {
        const data = await apiRequest(`${API_BASE}/seckill/products/?limit=6`);
        const container = document.getElementById('homeSeckillList');
        const products = data?.data || data || [];

        if (products.length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="icon">🏷️</div><p>暂无秒杀活动</p></div>';
        } else {
            container.innerHTML = products.map(p => renderSeckillCard(p)).join('');
        }

        const allProducts = await apiRequest(`${API_BASE}/products/`).catch(() => ({ data: [] }));
        const totalProducts = allProducts?.data?.length || 0;
        const activeSeckill = products.filter(p => isSeckillActive(p)).length;
        const totalOrders = await apiRequest(`${ADMIN_BASE}/orders/stats/summary`).catch(() => null);
        document.getElementById('totalProducts').textContent = totalProducts;
        document.getElementById('activeSeckill').textContent = activeSeckill;
        document.getElementById('totalOrders').textContent = totalOrders?.data?.total || 0;
    } catch (err) {
        document.getElementById('homeSeckillList').innerHTML =
            `<div class="empty-state"><div class="icon">⚠️</div><p>数据加载失败: ${err.message}</p></div>`;
    }
}

// ============================================================
// Products Page
// ============================================================
async function loadProducts() {
    const container = document.getElementById('productList');
    container.innerHTML = '<div class="loading">加载中...</div>';
    try {
        const data = await apiRequest(`${API_BASE}/products/`);
        const products = data?.data || data || [];
        if (products.length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="icon">📦</div><p>暂无商品</p></div>';
        } else {
            container.innerHTML = products.map(p => `
                <div class="product-card">
                    <h3>${escapeHtml(p.name)}</h3>
                    <div class="price">¥${p.price.toFixed(2)}</div>
                    <div class="description">${escapeHtml(p.description || '暂无描述')}</div>
                    <div class="meta">
                        <span>库存: ${p.stock}</span>
                        <span class="badge ${p.is_on_sale ? 'badge-primary' : 'badge-warning'}">
                            ${p.is_on_sale ? '在售' : '下架'}
                        </span>
                    </div>
                </div>
            `).join('');
        }
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><div class="icon">⚠️</div><p>${err.message}</p></div>`;
    }
}

// ============================================================
// Seckill Page
// ============================================================
async function loadSeckillProducts() {
    const container = document.getElementById('seckillList');
    container.innerHTML = '<div class="loading">加载中...</div>';
    const countEl = document.getElementById('seckillCount');
    try {
        const data = await apiRequest(`${API_BASE}/seckill/products/`);
        let products = data?.data || data || [];
        if (!Array.isArray(products)) products = [];

        products = products.filter(p => {
            const now = new Date();
            const start = new Date(p.start_time);
            const end = new Date(p.end_time);
            switch (state.seckillFilter) {
                case 'active': return now >= start && now <= end;
                case 'upcoming': return now < start;
                case 'ended': return now > end;
                default: return true;
            }
        });

        countEl.textContent = `${products.length} 个活动`;
        if (products.length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="icon">⏰</div><p>暂无匹配的秒杀活动</p></div>';
        } else {
            container.innerHTML = products.map(p => renderSeckillCard(p, true)).join('');
        }
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><div class="icon">⚠️</div><p>${err.message}</p></div>`;
        countEl.textContent = '加载失败';
    }
}

function filterSeckill(filter) {
    state.seckillFilter = filter;
    document.querySelectorAll('.seckill-filter').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.filter === filter);
    });
    loadSeckillProducts();
}

function isSeckillActive(product) {
    const now = new Date();
    return now >= new Date(product.start_time) && now <= new Date(product.end_time);
}

function getSeckillStatus(product) {
    const now = new Date();
    const start = new Date(product.start_time);
    const end = new Date(product.end_time);
    if (now < start) return { label: '即将开始', className: '', timeLeft: start - now };
    if (now > end) return { label: '已结束', className: 'ended', timeLeft: 0 };
    return { label: '进行中', className: 'active', timeLeft: end - now };
}

function formatTimeLeft(ms) {
    if (ms <= 0) return '已结束';
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    const h = Math.floor(m / 60);
    if (h > 0) return `${h}小时${m % 60}分钟`;
    if (m > 0) return `${m}分钟${s % 60}秒`;
    return `${s}秒`;
}

function renderSeckillCard(product, showBuy = false) {
    const status = getSeckillStatus(product);
    const isActive = status.className === 'active';
    const price = product.seckill_price || 0;

    return `
        <div class="product-card">
            <h3>⚡ 秒杀活动 #${product.id}</h3>
            <div style="font-size:13px;color:var(--gray-500);margin-bottom:4px">商品 ID: #${product.product_id}</div>
            <div class="price">¥${price.toFixed(2)}</div>
            <div class="seckill-timer ${status.className}">
                ${status.label} ${status.timeLeft > 0 ? `· ${formatTimeLeft(status.timeLeft)}` : ''}
            </div>
            <div class="meta">
                <span>库存: ${product.stock_count || 0}</span>
                <span>开始: ${new Date(product.start_time).toLocaleDateString()}</span>
            </div>
            ${showBuy ? `
                ${isActive
                    ? `<button class="btn btn-primary btn-block" onclick="buySeckill(${product.id})"
                        ${!state.user ? 'disabled title="请先登录"' : ''}>⚡ 立即秒杀</button>`
                    : `<button class="btn btn-outline btn-block" disabled>${status.label}</button>`
                }
            ` : ''}
        </div>
    `;
}

// ============================================================
// Seckill + Payment: 秒杀成功后弹出支付窗口
// ============================================================

/** 秒杀抢购 - 成功后弹出支付窗口 */
async function buySeckill(seckillId) {
    if (!state.user) {
        showToast('请先登录', 'warning');
        showPage('login');
        return;
    }
    try {
        const data = await apiRequest(`${API_BASE}/seckill/${seckillId}`, { method: 'POST' });
        if (data && data.success) {
            const orderData = data.data || {};
            const orderId = orderData.id || data.order_id;
            showPaymentModal({
                orderId: orderId,
                amount: orderData.total_amount || data.amount || 0,
                seckillId: seckillId,
            });
            if (state.currentPage === 'orders') loadOrders();
        } else {
            showToast(data?.message || '秒杀失败', 'error');
        }
    } catch (err) {
        showToast(`❌ ${err.message}`, 'error');
    }
}

/** 当前选中的支付方式 */
let selectedPaymentMethod = 'alipay';

/** 弹出支付确认窗口 - 显示微信支付和支付宝选项 */
function showPaymentModal(orderInfo) {
    const overlay = document.getElementById('modalOverlay');
    selectedPaymentMethod = 'alipay'; // 默认支付宝
    const orderId = orderInfo.orderId || '';
    const amount = orderInfo.amount || 0;
    const displayId = orderId.length > 12 ? orderId.slice(0, 12) + '...' : orderId;

    document.getElementById('modalTitle').textContent = '💳 选择支付方式';
    document.getElementById('modalBody').innerHTML = `
        <div class="payment-info">
            <div class="payment-order-id">订单号: ${displayId}</div>
            <div class="payment-amount">¥${amount.toFixed(2)}</div>
            <div class="payment-tip">请在 15 分钟内完成支付，逾期将自动取消</div>
        </div>

        <!-- 支付方式选择 -->
        <div class="payment-methods">
            <div class="payment-method payment-method-alipay selected"
                 data-method="alipay" onclick="selectPaymentMethod('alipay')">
                <div class="payment-method-icon">
                    <svg viewBox="0 0 48 48" width="40" height="40">
                        <path fill="#1677FF" d="M24 4C12.95 4 4 12.95 4 24s8.95 20 20 20 20-8.95 20-20S35.05 4 24 4zm11.42 14.77c-3.65-1.2-7.6-1.5-11.42-1.5-2.1 0-4.2.13-6.28.38 3.2 1.85 5.6 4.2 7.15 7.15-.95.42-1.92.8-2.9 1.12-2.45.8-5.05 1.2-7.65 1.2-2.6 0-5.2-.4-7.65-1.2-.2-.06-.4-.1-.6-.1-.35 0-.7.15-.95.4-.3.3-.45.7-.45 1.1 0 3.4 2.7 6.65 6.05 7.6 1.35.38 2.75.55 4.15.55 2.35 0 4.65-.6 6.75-1.75-1.65 1.8-3.55 3.35-5.65 4.55-2.1 1.2-4.55 1.85-7.05 1.85-1.25 0-2.5-.15-3.7-.45-1.2-.3-2.35-.8-3.4-1.45-.25-.15-.55-.2-.8-.15-.45.1-.85.35-1.1.7-.35.5-.4 1.15-.1 1.7.3.55.8.95 1.35 1.2 2.55 1.2 5.35 1.85 8.2 1.85 3.35 0 6.65-.8 9.55-2.35 2.9-1.55 5.35-3.75 7.25-6.35h2.9c.55 0 1-.45 1-1v-2.05c0-.55-.45-1-1-1h-5.3c.3-.85.55-1.7.75-2.6.2-.9.35-1.8.45-2.7h4.1c.55 0 1-.45 1-1v-2.05c0-.55-.45-1-1-1h-6.9c-.05-.2-.1-.35-.15-.55-.55-1.85-1.35-3.65-2.4-5.3z"/>
                    </svg>
                </div>
                <div class="payment-method-label">支付宝</div>
                <div class="payment-method-check">✓</div>
            </div>

            <div class="payment-method payment-method-wechat"
                 data-method="wechat" onclick="selectPaymentMethod('wechat')">
                <div class="payment-method-icon">
                    <svg viewBox="0 0 48 48" width="40" height="40">
                        <path fill="#07C160" d="M24 4C12.95 4 4 12.95 4 24s8.95 20 20 20 20-8.95 20-20S35.05 4 24 4zm-5.5 11.5c-3.6 0-6.5 2.5-6.5 5.5 0 1.2.45 2.3 1.2 3.2.2.25.25.55.15.85l-.4 1.2c-.05.15-.15.25-.3.2l-1.1-.35c-.1-.05-.25-.05-.35 0-1.65.55-3.45.85-5.35.85-.25 0-.5-.25-.5-.5 0-.2.15-.35.35-.45 2.3-.9 3.5-2.35 3.5-4.05 0-3.85 4.15-6.95 9.3-6.95 4.1 0 7.6 1.95 8.85 4.7-4.75.15-8.65 2.65-8.65 5.65 0 1.15.45 2.2 1.2 3.05-3.4.15-6.1-.5-7.4-.85-.15-.05-.3-.05-.45 0l-1.35.45c-.15.05-.3-.05-.25-.2l.5-1.5c.1-.25.05-.55-.15-.75-.7-.75-1.15-1.7-1.15-2.7 0-2.45 2.9-4.45 6.5-5.05-.2-.45-.35-.95-.35-1.45 0-2.2 2.5-4 5.5-4 3.6 0 6.5 2.5 6.5 5.5s-2.9 5.5-6.5 5.5z"/>
                    </svg>
                </div>
                <div class="payment-method-label">微信支付</div>
                <div class="payment-method-check">✓</div>
            </div>
        </div>

        <!-- 操作按钮 -->
        <div style="display:flex;gap:12px;margin-top:24px">
            <button class="btn btn-outline btn-block" onclick="closeModal(); showToast('已取消支付', 'info')">稍后支付</button>
            <button class="btn btn-success btn-block" id="payNowBtn" onclick="handlePayment('${orderId}')">✅ 立即支付</button>
        </div>
    `;
    overlay.style.display = 'flex';
}

/** 选择支付方式 */
function selectPaymentMethod(method) {
    selectedPaymentMethod = method;
    document.querySelectorAll('.payment-method').forEach(el => {
        el.classList.toggle('selected', el.dataset.method === method);
    });
}

/** 处理支付请求 */
async function handlePayment(orderId) {
    const payBtn = document.getElementById('payNowBtn');
    if (!payBtn) return;
    payBtn.disabled = true;
    payBtn.textContent = '⏳ 支付处理中...';
    try {
        const result = await apiRequest(`${API_BASE}/pay/${orderId}`, {
            method: 'POST',
            body: JSON.stringify({ payment_method: selectedPaymentMethod }),
        });
        if (result) {
            closeModal();
            const methodLabel = result.data?.payment_method_label ||
                (selectedPaymentMethod === 'wechat' ? '微信支付' : '支付宝');
            const paymentId = result.data?.payment_id || '';
            showToast(`✅ ${methodLabel}支付成功！${paymentId ? '单号: ' + paymentId.slice(0, 12) + '...' : ''}`, 'success');
            if (state.currentPage === 'orders') loadOrders();
            if (state.currentPage === 'seckill') loadSeckillProducts();
        }
    } catch (err) {
        payBtn.disabled = false;
        payBtn.textContent = '✅ 立即支付';
        showToast(`❌ 支付失败: ${err.message}`, 'error');
    }
}

// ============================================================
// Orders Page
// ============================================================
async function loadOrders() {
    const container = document.getElementById('orderList');
    container.innerHTML = '<div class="loading">加载中...</div>';
    try {
        const data = await apiRequest(`${API_BASE}/orders/`);
        const orders = data?.data || [];
        if (orders.length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="icon">📋</div><p>暂无订单</p></div>';
        } else {
            container.innerHTML = orders.map(o => `
                <div class="order-card">
                    <div class="order-info">
                        <div class="order-id">订单号: ${o.id.slice(0, 12)}...</div>
                        <div class="order-product">秒杀活动 #${o.seckill_product_id}</div>
                        <div class="order-amount">¥${(o.total_amount || 0).toFixed(2)}</div>
                        <div style="font-size:12px;color:var(--gray-400);margin-top:4px">
                            ${new Date(o.created_at).toLocaleString()}
                        </div>
                        ${o.payment_id ? `<div style="font-size:11px;color:var(--gray-400);margin-top:2px">支付单号: ${o.payment_id.slice(0, 12)}...</div>` : ''}
                    </div>
                    <div style="display:flex;align-items:center;gap:12px;flex-shrink:0">
                        <span class="order-status badge-${o.status}">${getStatusLabel(o.status)}</span>
                        ${o.status === 'pending' ? `
                            <button class="btn btn-sm btn-primary" onclick="payOrder('${o.id}')">去支付</button>
                        ` : ''}
                        ${o.status === 'paid' ? `
                            <span style="font-size:11px;color:var(--success);display:flex;align-items:center;gap:4px">
                                ✅ 已付款
                            </span>
                        ` : ''}
                    </div>
                </div>
            `).join('');
        }
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><div class="icon">⚠️</div><p>${err.message}</p></div>`;
    }
}

/** 从订单列表点击"去支付" - 弹出支付窗口 */
async function payOrder(orderId) {
    try {
        const data = await apiRequest(`${API_BASE}/orders/${orderId}`).catch(() => null);
        const amount = data?.data?.total_amount || 0;
        showPaymentModal({ orderId, amount });
    } catch (err) {
        showPaymentModal({ orderId, amount: 0 });
    }
}

function getStatusLabel(status) {
    const labels = { pending: '待支付', paid: '已支付', shipped: '已发货', completed: '已完成', cancelled: '已取消' };
    return labels[status] || status;
}

// ============================================================
// Admin Page
// ============================================================
function switchAdminTab(tab) {
    state.adminTab = tab;
    document.querySelectorAll('.admin-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    document.querySelectorAll('.admin-tab-content').forEach(c => c.classList.remove('active'));
    document.getElementById(`tab-${tab}`).classList.add('active');

    switch (tab) {
        case 'admin-products': loadAdminProducts(); break;
        case 'admin-seckill': loadAdminSeckill(); break;
        case 'admin-orders': loadAdminOrders(); break;
    }
}

async function loadAdminPage() {
    if (!state.user) {
        showToast('请先登录', 'warning');
        showPage('login');
        return;
    }
    if (!state.user.is_admin) {
        showToast('需要管理员权限', 'error');
        showPage('home');
        return;
    }
    loadAdminProducts();
}

async function loadAdminProducts() {
    const container = document.getElementById('adminProductList');
    container.innerHTML = '<div class="loading">加载中...</div>';
    try {
        const data = await apiRequest(`${ADMIN_BASE}/products/`);
        const products = data?.data || [];
        if (products.length === 0) {
            container.innerHTML = '<div class="empty-state"><p>暂无商品，点击右上角新增</p></div>';
        } else {
            container.innerHTML = `
                <table class="admin-table">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>名称</th>
                            <th>价格</th>
                            <th>库存</th>
                            <th>状态</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${products.map(p => `
                            <tr>
                                <td>#${p.id}</td>
                                <td><strong>${escapeHtml(p.name)}</strong></td>
                                <td>¥${p.price.toFixed(2)}</td>
                                <td>${p.stock}</td>
                                <td><span class="badge ${p.is_on_sale ? 'badge-primary' : 'badge-warning'}">
                                    ${p.is_on_sale ? '在售' : '下架'}</span></td>
                                <td class="actions">
                                    <button class="btn btn-sm btn-outline" onclick="editProduct(${p.id})">编辑</button>
                                    <button class="btn btn-sm btn-danger" onclick="deleteProduct(${p.id})">删除</button>
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>${err.message}</p></div>`;
    }
}

function showAdminProductModal(product = null) {
    const isEdit = !!product;
    document.getElementById('modalTitle').textContent = isEdit ? '编辑商品' : '新增商品';
    document.getElementById('modalBody').innerHTML = `
        <form id="productForm" onsubmit="saveProduct(event, ${isEdit ? product.id : 'null'})">
            <div class="form-group">
                <label>商品名称</label>
                <input type="text" id="prodName" class="form-control" value="${isEdit ? escapeHtml(product.name) : ''}" required>
            </div>
            <div class="form-group">
                <label>描述</label>
                <textarea id="prodDesc" class="form-control" rows="3">${isEdit ? escapeHtml(product.description || '') : ''}</textarea>
            </div>
            <div class="form-group">
                <label>价格 (元)</label>
                <input type="number" id="prodPrice" class="form-control" step="0.01" min="0.01"
                    value="${isEdit ? product.price : ''}" required>
            </div>
            <div class="form-group">
                <label>库存</label>
                <input type="number" id="prodStock" class="form-control" min="0"
                    value="${isEdit ? product.stock : '100'}" required>
            </div>
            <div class="form-group">
                <label class="flex items-center gap-2">
                    <input type="checkbox" id="prodOnSale" ${!isEdit || product.is_on_sale ? 'checked' : ''}>
                    上架销售
                </label>
            </div>
            <button type="submit" class="btn btn-primary btn-block">${isEdit ? '保存修改' : '创建商品'}</button>
        </form>
    `;
    document.getElementById('modalOverlay').style.display = 'flex';
}

async function saveProduct(e, productId) {
    e.preventDefault();
    const data = {
        name: document.getElementById('prodName').value,
        description: document.getElementById('prodDesc').value,
        price: parseFloat(document.getElementById('prodPrice').value),
        stock: parseInt(document.getElementById('prodStock').value),
        is_on_sale: document.getElementById('prodOnSale').checked,
    };

    try {
        if (productId) {
            await apiRequest(`${ADMIN_BASE}/products/${productId}`, { method: 'PUT', body: JSON.stringify(data) });
            showToast('商品更新成功', 'success');
        } else {
            await apiRequest(`${ADMIN_BASE}/products/`, { method: 'POST', body: JSON.stringify(data) });
            showToast('商品创建成功', 'success');
        }
        closeModal();
        loadAdminProducts();
    } catch (err) {
        showToast(`操作失败: ${err.message}`, 'error');
    }
}

function editProduct(id) {
    apiRequest(`${API_BASE}/products/${id}`).then(data => {
        showAdminProductModal(data?.data || data);
    }).catch(err => showToast(`获取商品信息失败: ${err.message}`, 'error'));
}

async function deleteProduct(id) {
    if (!confirm('确定要删除该商品吗？')) return;
    try {
        await apiRequest(`${ADMIN_BASE}/products/${id}`, { method: 'DELETE' });
        showToast('商品已删除', 'success');
        loadAdminProducts();
    } catch (err) {
        showToast(`删除失败: ${err.message}`, 'error');
    }
}

async function loadAdminSeckill() {
    const container = document.getElementById('adminSeckillList');
    container.innerHTML = '<div class="loading">加载中...</div>';
    try {
        const data = await apiRequest(`${ADMIN_BASE}/seckill/`);
        const products = data?.data || [];
        if (products.length === 0) {
            container.innerHTML = '<div class="empty-state"><p>暂无秒杀活动</p></div>';
        } else {
            container.innerHTML = `
                <table class="admin-table">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>商品ID</th>
                            <th>秒杀价</th>
                            <th>库存</th>
                            <th>开始时间</th>
                            <th>结束时间</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${products.map(p => `
                            <tr>
                                <td>#${p.id}</td>
                                <td>#${p.product_id}</td>
                                <td><strong>¥${p.seckill_price.toFixed(2)}</strong></td>
                                <td>${p.stock_count}</td>
                                <td>${new Date(p.start_time).toLocaleString()}</td>
                                <td>${new Date(p.end_time).toLocaleString()}</td>
                                <td class="actions">
                                    <button class="btn btn-sm btn-danger" onclick="deleteSeckill(${p.id})">删除</button>
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>${err.message}</p></div>`;
    }
}

function showAdminSeckillModal() {
    document.getElementById('modalTitle').textContent = '新增秒杀活动';
    document.getElementById('modalBody').innerHTML = `
        <form id="seckillForm" onsubmit="saveSeckill(event)">
            <div class="form-group">
                <label>商品 ID</label>
                <input type="number" id="secProductId" class="form-control" min="1" required>
            </div>
            <div class="form-group">
                <label>秒杀价格 (元)</label>
                <input type="number" id="secPrice" class="form-control" step="0.01" min="0.01" required>
            </div>
            <div class="form-group">
                <label>秒杀库存</label>
                <input type="number" id="secStock" class="form-control" min="1" value="10" required>
            </div>
            <div class="form-group">
                <label>开始时间</label>
                <input type="datetime-local" id="secStart" class="form-control" required>
            </div>
            <div class="form-group">
                <label>结束时间</label>
                <input type="datetime-local" id="secEnd" class="form-control" required>
            </div>
            <button type="submit" class="btn btn-primary btn-block">创建秒杀活动</button>
        </form>
    `;
    document.getElementById('modalOverlay').style.display = 'flex';
}

async function saveSeckill(e) {
    e.preventDefault();
    const startTime = new Date(document.getElementById('secStart').value).toISOString();
    const endTime = new Date(document.getElementById('secEnd').value).toISOString();
    const data = {
        product_id: parseInt(document.getElementById('secProductId').value),
        seckill_price: parseFloat(document.getElementById('secPrice').value),
        stock_count: parseInt(document.getElementById('secStock').value),
        start_time: startTime,
        end_time: endTime,
    };

    try {
        await apiRequest(`${ADMIN_BASE}/seckill/`, { method: 'POST', body: JSON.stringify(data) });
        showToast('秒杀活动创建成功', 'success');
        closeModal();
        loadAdminSeckill();
    } catch (err) {
        showToast(`创建失败: ${err.message}`, 'error');
    }
}

async function deleteSeckill(id) {
    if (!confirm('确定要删除该秒杀活动吗？')) return;
    try {
        await apiRequest(`${ADMIN_BASE}/seckill/${id}`, { method: 'DELETE' });
        showToast('秒杀活动已删除', 'success');
        loadAdminSeckill();
    } catch (err) {
        showToast(`删除失败: ${err.message}`, 'error');
    }
}

async function loadAdminOrders() {
    const container = document.getElementById('adminOrderList');
    container.innerHTML = '<div class="loading">加载中...</div>';
    try {
        const [ordersData, statsData] = await Promise.all([
            apiRequest(`${ADMIN_BASE}/orders/`),
            apiRequest(`${ADMIN_BASE}/orders/stats/summary`),
        ]);

        const stats = statsData?.data;
        const statsContainer = document.getElementById('orderStats');
        if (stats) {
            statsContainer.innerHTML = `
                <div class="stat-mini"><span class="num">${stats.total}</span><span class="lbl">总计</span></div>
                <div class="stat-mini"><span class="num" style="color:var(--warning)">${stats.pending}</span><span class="lbl">待支付</span></div>
                <div class="stat-mini"><span class="num" style="color:var(--success)">${stats.paid}</span><span class="lbl">已支付</span></div>
                <div class="stat-mini"><span class="num" style="color:#3b82f6">${stats.shipped}</span><span class="lbl">已发货</span></div>
                <div class="stat-mini"><span class="num" style="color:var(--gray-500)">${stats.cancelled}</span><span class="lbl">已取消</span></div>
            `;
        }

        const orders = ordersData?.data || [];
        if (orders.length === 0) {
            container.innerHTML = '<div class="empty-state"><p>暂无订单</p></div>';
        } else {
            container.innerHTML = `
                <table class="admin-table">
                    <thead>
                        <tr>
                            <th>订单号</th>
                            <th>用户ID</th>
                            <th>金额</th>
                            <th>状态</th>
                            <th>时间</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${orders.map(o => `
                            <tr>
                                <td style="font-size:12px;font-family:monospace">${o.id.slice(0, 12)}...</td>
                                <td style="font-size:12px">${o.user_id.slice(0, 8)}...</td>
                                <td>¥${(o.total_amount || 0).toFixed(2)}</td>
                                <td><span class="order-status badge-${o.status}">${getStatusLabel(o.status)}</span></td>
                                <td style="font-size:12px">${new Date(o.created_at).toLocaleString()}</td>
                                <td class="actions">
                                    <select class="form-control" style="width:auto;padding:4px 8px" onchange="updateOrderStatus('${o.id}', this.value)">
                                        <option value="">变更状态</option>
                                        <option value="paid">已支付</option>
                                        <option value="shipped">已发货</option>
                                        <option value="completed">已完成</option>
                                        <option value="cancelled">已取消</option>
                                    </select>
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>${err.message}</p></div>`;
    }
}

async function updateOrderStatus(orderId, status) {
    if (!status) return;
    try {
        await apiRequest(`${ADMIN_BASE}/orders/${orderId}/status`, {
            method: 'PUT',
            body: JSON.stringify({ status }),
        });
        showToast(`订单状态已更新为: ${getStatusLabel(status)}`, 'success');
        loadAdminOrders();
    } catch (err) {
        showToast(`更新失败: ${err.message}`, 'error');
    }
}

// ============================================================
// Toast Notifications
// ============================================================
function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    const icons = { success: '✅', error: '❌', info: 'ℹ️', warning: '⚠️' };
    toast.innerHTML = `${icons[type] || ''} ${message}`;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        toast.style.transition = 'all 0.3s ease';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// ============================================================
// Modal
// ============================================================
function closeModal() {
    document.getElementById('modalOverlay').style.display = 'none';
}

document.getElementById('modalOverlay').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeModal();
});

// ============================================================
// Utility
// ============================================================
function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

// ============================================================
// Initialize
// ============================================================
document.addEventListener('DOMContentLoaded', async () => {
    updateAuthUI();
    if (state.token) {
        await fetchCurrentUser();
    }
    showPage('home');
});