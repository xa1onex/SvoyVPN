import UIKit
import WebKit

/// AuthViewController — экран авторизации.
/// Полный аналог Android-шного AuthActivity: тот же HTML/JS загружается в WKWebView,
/// JavaScript-мост называется «iOSAuth» (вместо «AndroidAuth»), методы идентичны.
final class AuthViewController: UIViewController {

    private var webView: WKWebView!

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(hex: "#18222d")
        setupWebView()
        loadAuthPage()
    }

    // MARK: – WKWebView setup

    private func setupWebView() {
        let controller = WKUserContentController()
        controller.add(AuthBridgeHandler(owner: self), name: "iOSAuth")

        let config = WKWebViewConfiguration()
        config.userContentController = controller
        // Required for inline video / JS
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []

        webView = WKWebView(frame: .zero, configuration: config)
        webView.scrollView.isScrollEnabled = true
        webView.navigationDelegate = self
        webView.backgroundColor = UIColor(hex: "#18222d")
        webView.isOpaque = false
        webView.translatesAutoresizingMaskIntoConstraints = false

        view.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.topAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor)
        ])
    }

    private func loadAuthPage() {
        let html = buildAuthHTML()
        // Load with baseURL so fetch() to same origin works
        webView.loadHTMLString(html, baseURL: URL(string: AppConfig.apiBaseURL))
    }

    // MARK: – Navigation to main screen

    func launchWebView() {
        DispatchQueue.main.async {
            let vc = WebViewViewController()
            vc.modalPresentationStyle = .fullScreen
            self.present(vc, animated: false)
        }
    }

    // MARK: – HTML Auth Page (identical to Android)

    private func buildAuthHTML() -> String {
        let botUsername = AppConfig.botUsername
        return """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no"/>
  <title>SvoyVPN</title>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #18222d;
      --section_bg_color: #21303f;
      --accent: #3aa8fc;
      --text: #ffffff;
      --muted: #8e9db0;
      --danger: #ff3b57;
      --success: #34c759;
      --input-bg: rgba(255,255,255,0.07);
      --border: rgba(255,255,255,0.08);
    }
    html, body {
      height: 100%;
      background: var(--bg);
      color: var(--text);
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      -webkit-font-smoothing: antialiased;
      overflow-x: hidden;
    }
    .page {
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 24px 20px 40px;
    }
    .logo-anim {
      position: relative; width: 176px; height: 176px;
      display: flex; align-items: center; justify-content: center;
      margin-bottom: 24px; flex-shrink: 0;
    }
    .logo-anim .ring {
      position: absolute; border-radius: 50%;
      border: 4px solid var(--accent);
      animation: pulse 2.4s ease-out infinite;
    }
    .logo-anim .ring:nth-child(1) { width:100%;height:100%;opacity:.15;animation-delay:0s; }
    .logo-anim .ring:nth-child(2) { width:75%;height:75%;opacity:.25;animation-delay:.4s; }
    .logo-anim .ring:nth-child(3) { width:50%;height:50%;opacity:.35;animation-delay:.8s; }
    @keyframes pulse {
      0%   { transform: scale(1); opacity: var(--ring-opacity,.2); }
      70%  { transform: scale(1.15); opacity: 0; }
      100% { transform: scale(1.15); opacity: 0; }
    }
    .logo-anim .logo-core {
      width: 88px; height: 88px; border-radius: 50%;
      background: var(--accent);
      display: flex; align-items: center; justify-content: center;
      z-index: 1; box-shadow: 0 8px 44px rgba(58,168,252,.35);
    }
    .logo-anim .logo-core svg { width:75px;height:75px;fill:currentColor;color:#fff; }
    .app-name { font-size:22px;font-weight:700;line-height:1.2;text-align:center;margin-bottom:4px; }
    .app-tagline { font-size:14px;font-weight:400;line-height:1.4;color:var(--muted);text-align:center;margin-bottom:24px; }
    .card { width:100%;max-width:360px;background:var(--section_bg_color);border-radius:14px;padding:14px 16px; }
    .tabs { display:flex;background:var(--input-bg);border-radius:12px;padding:4px;margin-bottom:24px; }
    .tab { flex:1;text-align:center;padding:10px;border-radius:9px;font-size:13px;font-weight:600;color:var(--muted);cursor:pointer;transition:all .2s;-webkit-tap-highlight-color:transparent; }
    .tab.active { background:var(--accent);color:#fff; }
    .panel { display:none; }
    .panel.active { display:block; }
    .tg-btn { width:100%;padding:14px;background:var(--accent);color:#fff;border:none;border-radius:12px;font-size:16px;font-weight:600;font-family:inherit;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:10px;transition:opacity .15s, transform .1s;-webkit-tap-highlight-color:transparent; }
    .tg-btn:active { transform:scale(.98);opacity:.85; }
    .tg-btn svg { width:22px;height:22px;fill:#fff;flex-shrink:0; }
    .tg-hint { margin-top:14px;font-size:12px;color:var(--muted);text-align:center;line-height:1.5; }
    .polling { display:none;margin-top:20px;padding:14px;background:rgba(58,168,252,.08);border:1px solid rgba(58,168,252,.2);border-radius:12px;text-align:center; }
    .polling.visible { display:block; }
    .polling-text { font-size:13px;color:var(--accent); }
    .dots::after { content:'';animation:dotdot 1.5s infinite; }
    @keyframes dotdot { 0%{content:'';}25%{content:'.';}50%{content:'..';}75%{content:'...';} }
    .input-group { margin-bottom:14px; }
    .input-label { font-size:13px;color:var(--muted);margin-bottom:6px;display:block;font-weight:500;margin-left:4px; }
    .input-field { width:100%;padding:14px 16px;background:var(--input-bg);border:1px solid transparent;border-radius:12px;color:var(--text);font-size:16px;font-family:inherit;outline:none;transition:all .2s;-webkit-appearance:none; }
    .input-field:focus { border-color:rgba(58,168,252,0.4);background:rgba(255,255,255,0.1); }
    .input-field::placeholder { color:var(--muted); }
    .sub-tabs { display:flex;gap:0;margin-bottom:20px;border-bottom:1px solid var(--border); }
    .sub-tab { flex:1;text-align:center;padding:10px;font-size:13px;font-weight:600;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;transition:all .2s; }
    .sub-tab.active { color:var(--accent);border-bottom-color:var(--accent); }
    .email-btn { width:100%;padding:14px;background:var(--accent);color:#fff;border:none;border-radius:12px;font-size:16px;font-weight:600;font-family:inherit;cursor:pointer;transition:opacity .15s, transform .1s;margin-top:4px;-webkit-tap-highlight-color:transparent; }
    .email-btn:active { transform:scale(.98);opacity:.85; }
    .error-msg { background:rgba(255,69,58,.1);border:1px solid rgba(255,69,58,.3);border-radius:10px;padding:12px;font-size:13px;color:#ff6b61;margin-bottom:14px;display:none; }
    .error-msg.visible { display:block; }
    .success-msg { background:rgba(52,199,89,.1);border:1px solid rgba(52,199,89,.3);border-radius:10px;padding:12px;font-size:13px;color:#4cd964;margin-bottom:14px;display:none;text-align:center; }
    .success-msg.visible { display:block; }
    .divider { display:flex;align-items:center;gap:12px;margin:20px 0 0;color:var(--muted);font-size:12px; }
    .divider::before,.divider::after { content:'';flex:1;height:1px;background:var(--border); }
  </style>
</head>
<body>
<div class="page">
  <div class="logo-anim">
    <div class="ring" style="--ring-opacity:.15;"></div>
    <div class="ring" style="--ring-opacity:.25;"></div>
    <div class="ring" style="--ring-opacity:.35;"></div>
    <div class="logo-core">
      <svg viewBox="0 0 1024 1024" xmlns="http://www.w3.org/2000/svg">
        <g transform="translate(0,1024) scale(0.1,-0.1)">
          <path d="M3033 7920 c-212 -22 -363 -95 -495 -240 -73 -79 -117 -161 -150 -276 -29 -103 -31 -305 -4 -434 54 -255 174 -534 386 -895 48 -82 100 -170 114 -195 57 -98 95 -179 122 -260 23 -73 28 -102 28 -200 0 -104 -3 -124 -32 -209 -32 -92 -84 -197 -149 -298 -18 -29 -33 -54 -33 -56 0 -2 -31 -56 -68 -118 -233 -387 -382 -727 -449 -1024 -24 -104 -24 -369 0 -455 72 -260 273 -452 555 -527 87 -23 115 -26 277 -26 164 -1 193 2 333 31 341 71 733 234 1167 483 224 129 350 210 580 375 402 289 582 443 985 840 315 310 347 334 453 334 129 0 207 -72 367 -340 213 -357 353 -774 320 -950 -22 -117 -80 -161 -221 -168 -104 -5 -185 7 -340 53 -265 78 -526 201 -772 362 -85 56 -132 82 -140 76 -7 -5 -52 -41 -102 -80 -105 -82 -448 -329 -467 -336 -16 -5 -16 -5 57 -54 260 -174 631 -360 955 -481 166 -61 203 -73 355 -111 197 -49 257 -56 465 -56 214 0 258 8 395 67 97 43 169 94 246 175 111 118 169 245 190 417 17 141 -6 336 -62 531 -34 116 -137 361 -219 520 -79 154 -96 183 -288 509 -61 104 -126 228 -144 275 -31 82 -32 93 -33 226 0 197 12 228 250 633 6 10 43 73 84 141 162 271 279 555 327 790 22 109 25 144 22 266 -4 121 -8 152 -33 224 -78 228 -251 382 -498 441 -170 41 -376 37 -657 -11 -247 -43 -676 -193 -985 -346 -91 -45 -395 -217 -395 -223 0 -3 56 -43 124 -89 68 -46 188 -132 267 -191 l144 -107 50 32 c268 171 612 316 895 375 125 27 292 32 363 11 57 -17 101 -63 118 -123 42 -155 -74 -531 -259 -843 -109 -184 -196 -291 -266 -327 -30 -16 -60 -22 -111 -22 -61 -1 -77 3 -126 31 -56 33 -82 55 -374 321 -747 679 -1516 1161 -2230 1397 -340 113 -652 160 -892 135z m369 -575 c108 -17 147 -26 322 -81 320 -100 663 -275 1071 -547 392 -262 846 -639 1000 -830 238 -295 263 -543 86 -845 -107 -184 -362 -435 -736 -728 -712 -557 -1562 -980 -1981 -987 -82 -2 -96 1 -149 28 -137 70 -146 207 -35 528 84 245 240 551 369 724 89 121 155 164 251 164 107 1 129 -15 400 -291 135 -137 286 -284 335 -326 50 -43 93 -81 96 -86 5 -8 106 57 194 125 117 90 295 241 293 247 -2 4 -52 50 -113 102 -149 129 -358 347 -423 440 -118 172 -167 335 -145 487 36 246 208 472 605 792 l97 78 -92 74 c-51 40 -163 122 -249 181 l-157 106 -58 -46 c-108 -88 -217 -188 -410 -376 -200 -195 -252 -233 -338 -245 -92 -12 -174 33 -265 147 -130 161 -314 542 -370 765 -42 164 -27 311 37 367 54 49 183 60 365 33z"/>
        </g>
      </svg>
    </div>
  </div>
  <div class="app-name">SvoyVPN Pro</div>
  <div class="app-tagline">Быстрый и надёжный, потому что Свой</div>
  <div class="card">
    <div class="tabs">
      <div class="tab active" onclick="switchTab('tg')">Telegram</div>
      <div class="tab" onclick="switchTab('email')">Email</div>
    </div>
    <!-- Telegram Panel -->
    <div id="panelTg" class="panel active">
      <button class="tg-btn" onclick="startTgLogin()">
        <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
          <path d="M12 0C5.373 0 0 5.373 0 12s5.373 12 12 12 12-5.373 12-12S18.627 0 12 0zm5.562 8.248l-2.02 9.52c-.15.658-.54.818-1.084.508l-3-2.21-1.447 1.394c-.16.16-.295.295-.605.295l.213-3.053 5.56-5.023c.242-.213-.054-.333-.373-.12l-6.871 4.326-2.962-.924c-.643-.204-.657-.643.136-.953l11.57-4.462c.537-.194 1.006.131.883.702z"/>
        </svg>
        Войти через Telegram
      </button>
      <div id="pollingBox" class="polling">
        <div class="polling-text">Ожидаем подтверждения от бота<span class="dots"></span></div>
      </div>
      <div id="tgError" class="error-msg"></div>
      <div class="tg-hint">Вы будете перенаправлены в Telegram. После нажатия «Старт» в боте вход произойдёт автоматически.</div>
    </div>
    <!-- Email Panel -->
    <div id="panelEmail" class="panel">
      <div class="sub-tabs">
        <div class="sub-tab active" onclick="switchEmailTab('login')">Вход</div>
        <div class="sub-tab" onclick="switchEmailTab('register')">Регистрация</div>
      </div>
      <!-- Login Form -->
      <div id="loginForm">
        <div id="loginError" class="error-msg"></div>
        <div class="input-group">
          <label class="input-label">Email</label>
          <input class="input-field" type="email" id="loginEmail" placeholder="you@example.com" autocomplete="email"/>
        </div>
        <div class="input-group">
          <label class="input-label">Пароль</label>
          <input class="input-field" type="password" id="loginPass" placeholder="••••••••" autocomplete="current-password"/>
        </div>
        <div style="text-align:right;margin-bottom:16px;">
          <a href="#" onclick="switchEmailTab('reset')" style="color:var(--muted);font-size:13px;text-decoration:none;">Забыли пароль?</a>
        </div>
        <button class="email-btn" onclick="doLogin()">Войти в аккаунт</button>
      </div>
      <!-- Reset Password Form -->
      <div id="resetForm" style="display:none;">
        <div id="resetError" class="error-msg"></div>
        <div id="resetSuccess" class="success-msg"></div>
        <div id="resetStep1">
          <div class="input-group">
            <label class="input-label">Email</label>
            <input class="input-field" type="email" id="resetEmail" placeholder="you@example.com"/>
          </div>
          <button class="email-btn" onclick="sendResetOtp()">Получить код</button>
        </div>
        <div id="resetStep2" style="display:none;">
          <p style="font-size:13px;color:var(--muted);margin-bottom:14px;text-align:center;">Введите код из письма и новый пароль</p>
          <div class="input-group">
            <label class="input-label">Код из письма</label>
            <input class="input-field" type="text" id="resetOtp" placeholder="123456" maxlength="6" inputmode="numeric"/>
          </div>
          <div class="input-group">
            <label class="input-label">Новый пароль</label>
            <input class="input-field" type="password" id="resetNewPass" placeholder="••••••••"/>
          </div>
          <button class="email-btn" onclick="doResetPassword()">Сменить пароль</button>
        </div>
        <button onclick="switchEmailTab('login')" style="width:100%;padding:12px;background:none;border:none;color:var(--muted);font-size:13px;cursor:pointer;margin-top:8px;">← Вернуться ко входу</button>
      </div>
      <!-- Register Form -->
      <div id="registerForm" style="display:none;">
        <div id="registerError" class="error-msg"></div>
        <div id="registerSuccess" class="success-msg"></div>
        <div id="regStep1">
          <div class="input-group">
            <label class="input-label">Email</label>
            <input class="input-field" type="email" id="regEmail" placeholder="you@example.com" autocomplete="email"/>
          </div>
          <div class="input-group">
            <label class="input-label">Пароль (минимум 6 символов)</label>
            <input class="input-field" type="password" id="regPass" placeholder="••••••••" autocomplete="new-password"/>
          </div>
          <div class="input-group">
            <label class="input-label">Повторите пароль</label>
            <input class="input-field" type="password" id="regPass2" placeholder="••••••••" autocomplete="new-password"/>
          </div>
          <button class="email-btn" onclick="sendOtp()">Получить код на email</button>
        </div>
        <div id="regStep2" style="display:none;">
          <p style="font-size:13px;color:var(--muted);margin-bottom:14px;text-align:center;">Введите 6-значный код,<br>отправленный на вашу почту</p>
          <div class="input-group">
            <label class="input-label">Код подтверждения</label>
            <input class="input-field" type="text" id="regOtp" placeholder="123456" maxlength="6" inputmode="numeric" autocomplete="one-time-code"/>
          </div>
          <button class="email-btn" onclick="doRegister()">Подтвердить и войти</button>
          <button onclick="showRegStep1()" style="width:100%;padding:12px;background:none;border:none;color:var(--muted);font-size:13px;cursor:pointer;margin-top:8px;">← Изменить email</button>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
  const API = '\(AppConfig.apiBaseURL)/api';
  const BOT = '\(botUsername)';
  let pollingInterval = null;
  let currentNonce = null;
  let _pendingRegEmail = '';

  function notifyNative(token, userId) {
    // iOS WKWebView bridge
    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.iOSAuth) {
      window.webkit.messageHandlers.iOSAuth.postMessage({ action: 'loginSuccess', token: token, userId: userId || '' });
    }
  }

  function switchTab(tab) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    if (tab === 'tg') {
      document.querySelectorAll('.tab')[0].classList.add('active');
      document.getElementById('panelTg').classList.add('active');
    } else {
      document.querySelectorAll('.tab')[1].classList.add('active');
      document.getElementById('panelEmail').classList.add('active');
    }
  }

  function switchEmailTab(t) {
    document.querySelectorAll('.sub-tabs .sub-tab').forEach(el => el.classList.remove('active'));
    document.getElementById('loginForm').style.display = 'none';
    document.getElementById('registerForm').style.display = 'none';
    document.getElementById('resetForm').style.display = 'none';
    if (t === 'login') {
      document.querySelectorAll('.sub-tabs .sub-tab')[0].classList.add('active');
      document.getElementById('loginForm').style.display = 'block';
    } else if (t === 'register') {
      document.querySelectorAll('.sub-tabs .sub-tab')[1].classList.add('active');
      document.getElementById('registerForm').style.display = 'block';
    } else if (t === 'reset') {
      document.getElementById('resetForm').style.display = 'block';
    }
  }

  async function startTgLogin() {
    hideError('tgError');
    try {
      const r = await fetch(API + '/auth/tg-init', { method: 'POST' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      currentNonce = data.nonce;
      window.location.href = data.botUrl;
      document.getElementById('pollingBox').classList.add('visible');
      startPolling(data.nonce);
    } catch(e) {
      showError('tgError', 'Ошибка подключения: ' + e.message);
    }
  }

  function startPolling(nonce) {
    if (pollingInterval) clearInterval(pollingInterval);
    pollingInterval = setInterval(async () => {
      try {
        const r = await fetch(API + '/auth/tg-poll?nonce=' + nonce);
        const data = await r.json();
        if (data.status === 'ok' && data.token) {
          clearInterval(pollingInterval);
          document.getElementById('pollingBox').classList.remove('visible');
          notifyNative(data.token, '');
        } else if (data.status === 'expired') {
          clearInterval(pollingInterval);
          document.getElementById('pollingBox').classList.remove('visible');
          showError('tgError', 'Время ожидания истекло. Попробуйте ещё раз.');
        }
      } catch(e) { /* retry */ }
    }, 2000);
  }

  async function doLogin() {
    hideError('loginError');
    const email = document.getElementById('loginEmail').value.trim();
    const pass = document.getElementById('loginPass').value;
    if (!email || !pass) { showError('loginError', 'Заполните все поля'); return; }
    try {
      const r = await fetch(API + '/auth/login', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({email, password: pass})
      });
      const data = await r.json();
      if (!r.ok) { showError('loginError', data.error || 'Ошибка входа'); return; }
      notifyNative(data.token, data.userId + '');
    } catch(e) {
      showError('loginError', 'Ошибка подключения');
    }
  }

  function showRegStep1() {
    document.getElementById('regStep1').style.display = 'block';
    document.getElementById('regStep2').style.display = 'none';
    hideError('registerError');
    document.getElementById('registerSuccess').classList.remove('visible');
  }

  async function sendOtp() {
    hideError('registerError');
    const email = document.getElementById('regEmail').value.trim();
    const pass = document.getElementById('regPass').value;
    const pass2 = document.getElementById('regPass2').value;
    if (!email || !pass) { showError('registerError', 'Заполните все поля'); return; }
    if (pass !== pass2) { showError('registerError', 'Пароли не совпадают'); return; }
    if (pass.length < 6) { showError('registerError', 'Пароль минимум 6 символов'); return; }
    const btn = event.target;
    btn.disabled = true; btn.textContent = 'Отправляем...';
    try {
      const r = await fetch(API + '/auth/email-otp', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({email, password: pass})
      });
      const data = await r.json();
      if (!r.ok) { showError('registerError', data.error || 'Ошибка'); btn.disabled=false; btn.textContent='Получить код на email'; return; }
      _pendingRegEmail = email;
      document.getElementById('regStep1').style.display = 'none';
      document.getElementById('regStep2').style.display = 'block';
      document.getElementById('registerSuccess').textContent = 'Код отправлен на ' + email;
      document.getElementById('registerSuccess').classList.add('visible');
    } catch(e) {
      showError('registerError', 'Ошибка подключения');
      btn.disabled=false; btn.textContent='Получить код на email';
    }
  }

  async function doRegister() {
    hideError('registerError');
    document.getElementById('registerSuccess').classList.remove('visible');
    const email = _pendingRegEmail || document.getElementById('regEmail').value.trim();
    const otp = document.getElementById('regOtp').value.trim();
    if (!otp) { showError('registerError', 'Введите код'); return; }
    try {
      const r = await fetch(API + '/auth/register', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({email, otp})
      });
      const data = await r.json();
      if (!r.ok) { showError('registerError', data.error || 'Ошибка регистрации'); return; }
      if (data.token) notifyNative(data.token, (data.userId || '') + '');
    } catch(e) {
      showError('registerError', 'Ошибка подключения');
    }
  }

  async function sendResetOtp() {
    hideError('resetError');
    const email = document.getElementById('resetEmail').value.trim();
    if (!email) { showError('resetError', 'Введите email'); return; }
    const btn = event.target;
    btn.disabled = true; btn.textContent = 'Отправляем...';
    try {
      const r = await fetch(API + '/auth/reset-otp', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({email})
      });
      const data = await r.json();
      if (!r.ok) { showError('resetError', data.error || 'Ошибка'); btn.disabled=false; btn.textContent='Получить код'; return; }
      document.getElementById('resetStep1').style.display = 'none';
      document.getElementById('resetStep2').style.display = 'block';
      document.getElementById('resetSuccess').textContent = 'Код сброса отправлен';
      document.getElementById('resetSuccess').classList.add('visible');
    } catch(e) {
      showError('resetError', 'Ошибка подключения');
      btn.disabled = false; btn.textContent = 'Получить код';
    }
  }

  async function doResetPassword() {
    hideError('resetError');
    const email = document.getElementById('resetEmail').value.trim();
    const otp = document.getElementById('resetOtp').value.trim();
    const pass = document.getElementById('resetNewPass').value;
    if (!otp || pass.length < 6) { showError('resetError', 'Заполните поля корректно'); return; }
    try {
      const r = await fetch(API + '/auth/reset-password', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({email, otp, password: pass})
      });
      const data = await r.json();
      if (!r.ok) { showError('resetError', data.error || 'Ошибка'); return; }
      document.getElementById('resetSuccess').textContent = 'Пароль изменён! Входим...';
      document.getElementById('resetSuccess').classList.add('visible');
      if (data.token) {
        setTimeout(() => notifyNative(data.token, (data.userId || '') + ''), 1500);
      } else {
        setTimeout(() => switchEmailTab('login'), 2000);
      }
    } catch(e) {
      showError('resetError', 'Ошибка подключения');
    }
  }

  function showError(id, msg) {
    const el = document.getElementById(id);
    el.textContent = msg;
    el.classList.add('visible');
  }
  function hideError(id) {
    document.getElementById(id).classList.remove('visible');
  }
</script>
</body>
</html>
"""
    }
}

// MARK: – WKNavigationDelegate

extension AuthViewController: WKNavigationDelegate {
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }
        let str = url.absoluteString
        // Allow Telegram deep-link URLs to open externally
        if str.hasPrefix("tg://") || str.hasPrefix("https://t.me/") {
            UIApplication.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }
}

// MARK: – JS → Native Bridge Handler

private final class AuthBridgeHandler: NSObject, WKScriptMessageHandler {
    weak var owner: AuthViewController?
    init(owner: AuthViewController) { self.owner = owner }

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.name == "iOSAuth",
              let body = message.body as? [String: Any],
              let action = body["action"] as? String,
              action == "loginSuccess",
              let token = body["token"] as? String, !token.isEmpty else { return }

        let userIdStr = body["userId"] as? String ?? ""
        TokenStorage.shared.saveToken(token)
        if let uid = Int64(userIdStr) {
            TokenStorage.shared.saveUserId(uid)
        }
        owner?.launchWebView()
    }
}

// MARK: – UIColor hex helper

extension UIColor {
    convenience init(hex: String) {
        var h = hex.trimmingCharacters(in: .whitespacesAndNewlines).uppercased()
        if h.hasPrefix("#") { h.removeFirst() }
        var rgb: UInt64 = 0
        Scanner(string: h).scanHexInt64(&rgb)
        let r = CGFloat((rgb & 0xFF0000) >> 16) / 255
        let g = CGFloat((rgb & 0x00FF00) >> 8)  / 255
        let b = CGFloat(rgb & 0x0000FF)          / 255
        self.init(red: r, green: g, blue: b, alpha: 1)
    }
}
