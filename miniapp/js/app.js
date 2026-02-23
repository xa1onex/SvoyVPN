// Telegram WebApp API
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

// Глобальные переменные
let currentUser = null;
let selectedTariff = null;
let selectedPaymentMethod = null;
let deviceCount = 1;
let tariffs = [];
let paymentMethods = [];

// Инициализация приложения
document.addEventListener('DOMContentLoaded', async () => {
    // Скрываем бонусный баннер на главном экране
    const bonusBanner = document.getElementById('bonusBanner');
    if (bonusBanner) {
        bonusBanner.style.display = 'none';
    }
    
    await initApp();
});

// Инициализация
async function initApp() {
    try {
        // Получаем данные пользователя из Telegram
        const initData = tg.initDataUnsafe;
        if (initData && initData.user) {
            currentUser = {
                id: initData.user.id,
                firstName: initData.user.first_name,
                lastName: initData.user.last_name,
                username: initData.user.username,
                photoUrl: initData.user.photo_url
            };
            updateProfileInfo();
        }

        // Загружаем данные пользователя с сервера
        await loadUserData();
        
        // Загружаем тарифы
        await loadTariffs();
        
        // Загружаем способы оплаты
        await loadPaymentMethods();
        
        // Устанавливаем тему
        tg.setHeaderColor('#1a1a2e');
        tg.setBackgroundColor('#1a1a2e');
        
    } catch (error) {
        console.error('Ошибка инициализации:', error);
        showError('Ошибка загрузки данных');
    }
}

// Загрузка данных пользователя
async function loadUserData() {
    try {
        const response = await fetch('/api/user', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                initData: tg.initData
            })
        });
        
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.error || 'Ошибка загрузки данных пользователя');
        }
        
        const data = await response.json();
        
        // Обновляем информацию о подписке
        if (data.subscription) {
            updateSubscriptionInfo(data.subscription);
        }
        
        // Обновляем ссылку на подписку
        if (data.subscription && data.subscription.subscriptionUrl) {
            const url = data.subscription.subscriptionUrl;
            const urlInput = document.getElementById('subscriptionUrl');
            const setupUrlInput = document.getElementById('setupSubscriptionUrl');
            const profileUrlInput = document.getElementById('profileUrl');
            
            if (urlInput) urlInput.value = url;
            if (setupUrlInput) setupUrlInput.value = url;
            if (profileUrlInput) profileUrlInput.value = url;
        }
        
    } catch (error) {
        console.error('Ошибка загрузки данных пользователя:', error);
        // Не показываем ошибку пользователю при первой загрузке
    }
}

// Загрузка тарифов
async function loadTariffs() {
    try {
        const response = await fetch('/api/tariffs');
        if (!response.ok) throw new Error('Ошибка загрузки тарифов');
        
        tariffs = await response.json();
        renderTariffs();
        
    } catch (error) {
        console.error('Ошибка загрузки тарифов:', error);
    }
}

// Загрузка способов оплаты
async function loadPaymentMethods() {
    try {
        const response = await fetch('/api/payment-methods');
        if (!response.ok) throw new Error('Ошибка загрузки способов оплаты');
        
        paymentMethods = await response.json();
        renderPaymentMethods();
        
    } catch (error) {
        console.error('Ошибка загрузки способов оплаты:', error);
    }
}

// Обновление информации о подписке
function updateSubscriptionInfo(subscription) {
    if (!subscription) return;
    
    const dateElement = document.getElementById('subscriptionDate');
    const statusElement = document.getElementById('subscriptionStatus');
    
    if (subscription.endDate) {
        dateElement.textContent = `До ${formatDate(subscription.endDate)}`;
    }
    
    if (subscription.isActive) {
        statusElement.textContent = 'подписка активна';
        statusElement.className = 'info-status active';
    } else {
        statusElement.textContent = 'подписка истекла';
        statusElement.className = 'info-status expired';
    }
}

// Обновление информации профиля
function updateProfileInfo() {
    if (!currentUser) return;
    
    const nameElement = document.getElementById('profileName');
    const avatarElement = document.getElementById('profileAvatar');
    
    if (nameElement) {
        nameElement.textContent = currentUser.firstName || 'Пользователь';
    }
    
    if (avatarElement && currentUser.photoUrl) {
        avatarElement.style.backgroundImage = `url(${currentUser.photoUrl})`;
        avatarElement.style.backgroundSize = 'cover';
        avatarElement.textContent = '';
    }
}

// Рендеринг тарифов
function renderTariffs() {
    const container = document.getElementById('tariffsGrid');
    if (!container) return;
    
    container.innerHTML = '';
    
    tariffs.forEach(tariff => {
        const card = document.createElement('div');
        card.className = `tariff-card ${tariff.popular ? 'popular' : ''}`;
        card.onclick = () => selectTariff(tariff);
        
        const benefit = tariff.oldPrice ? 
            `выгода ${Math.round((1 - tariff.price / tariff.oldPrice) * 100)}%` : '';
        
        card.innerHTML = `
            <div class="tariff-number">${tariff.months}</div>
            <div class="tariff-period">${getMonthsText(tariff.months)}</div>
            ${benefit ? `<div class="tariff-benefit">${benefit}</div>` : ''}
            ${tariff.oldPrice ? `<div class="tariff-price-old">$${tariff.oldPrice.toFixed(2)}</div>` : ''}
            <div class="tariff-price">$${tariff.price.toFixed(2)}</div>
            <div class="tariff-price-month">$${(tariff.price / tariff.months).toFixed(2)} / в месяц</div>
        `;
        
        card.onclick = () => selectTariff(tariff);
        container.appendChild(card);
    });
}

// Рендеринг способов оплаты
function renderPaymentMethods() {
    const container = document.getElementById('paymentMethods');
    if (!container) return;
    
    container.innerHTML = '';
    
    paymentMethods.forEach(method => {
        const item = document.createElement('div');
        item.className = 'payment-method';
        item.onclick = () => selectPaymentMethod(method);
        
        item.innerHTML = `
            <div class="payment-method-left">
                <div class="payment-method-icon">${method.icon}</div>
                <div class="payment-method-info">
                    <div class="payment-method-name">${method.name}</div>
                    ${method.description ? `<div class="payment-method-desc">${method.description}</div>` : ''}
                    ${method.badge ? `<div class="payment-method-badge">${method.badge}</div>` : ''}
                </div>
            </div>
            <div class="payment-method-check" style="display: none;">✓</div>
        `;
        
        item.onclick = () => selectPaymentMethod(method);
        container.appendChild(item);
    });
}

// Выбор тарифа
function selectTariff(tariff) {
    selectedTariff = tariff;
    
    // Обновляем визуальное выделение
    document.querySelectorAll('.tariff-card').forEach(card => {
        card.classList.remove('selected');
    });
    
    // Находим карточку выбранного тарифа
    const cards = document.querySelectorAll('.tariff-card');
    tariffs.forEach((t, index) => {
        if (t.id === tariff.id && cards[index]) {
            cards[index].classList.add('selected');
        }
    });
    
    // Активируем кнопку продолжения
    const continueBtn = document.getElementById('continueBtn');
    if (continueBtn) {
        continueBtn.disabled = false;
    }
}

// Выбор способа оплаты
function selectPaymentMethod(method) {
    selectedPaymentMethod = method;
    
    // Обновляем визуальное выделение
    document.querySelectorAll('.payment-method').forEach((item, index) => {
        item.classList.remove('selected');
        const check = item.querySelector('.payment-method-check');
        if (check) check.style.display = 'none';
        
        // Проверяем, является ли этот элемент выбранным
        if (paymentMethods[index] && paymentMethods[index].id === method.id) {
            item.classList.add('selected');
            const checkEl = item.querySelector('.payment-method-check');
            if (checkEl) checkEl.style.display = 'block';
        }
    });
    
    // Обновляем цену
    updateTotalPrice();
}

// Обновление количества устройств
function updateDeviceCount(count) {
    deviceCount = parseInt(count);
    document.getElementById('deviceCount').textContent = deviceCount;
    updateTotalPrice();
}

// Обновление общей цены
function updateTotalPrice() {
    if (!selectedTariff) return;
    
    const totalPrice = selectedTariff.price * deviceCount;
    const priceElement = document.getElementById('totalPrice');
    if (priceElement) {
        // Конвертируем в рубли (если цена в долларах, умножаем на курс)
        // Для простоты считаем, что цена уже в рублях
        priceElement.textContent = totalPrice.toFixed(2);
    }
}

// Навигация между экранами
function showScreen(screenId) {
    document.querySelectorAll('.screen').forEach(screen => {
        screen.classList.remove('active');
    });
    
    const screen = document.getElementById(screenId);
    if (screen) {
        screen.classList.add('active');
    }
    
    // Показываем/скрываем бонусный баннер только на экране тарифов
    const bonusBanner = document.getElementById('bonusBanner');
    if (bonusBanner) {
        if (screenId === 'tariffsScreen') {
            bonusBanner.style.display = 'block';
        } else {
            bonusBanner.style.display = 'none';
        }
    }
}

function showHome() {
    showScreen('homeScreen');
}

function showTariffs() {
    showScreen('tariffsScreen');
}

function showPayment() {
    if (!selectedTariff) {
        tg.showAlert('Пожалуйста, выберите тариф');
        return;
    }
    showScreen('paymentScreen');
    updateTotalPrice();
}

function showSetup() {
    showScreen('setupScreen');
}

function showProfile() {
    showScreen('profileScreen');
}

// Копирование ссылки на подписку
async function copySubscriptionUrl() {
    const url = document.getElementById('subscriptionUrl').value || 
                document.getElementById('setupSubscriptionUrl').value;
    
    if (url) {
        await navigator.clipboard.writeText(url);
        tg.showAlert('Ссылка скопирована!');
    }
}

// Копирование ссылки профиля
async function copyProfileUrl() {
    const url = document.getElementById('profileUrl').value;
    if (url) {
        await navigator.clipboard.writeText(url);
        tg.showAlert('Ссылка скопирована!');
    }
}

// Обновление подписки
async function refreshSubscription() {
    tg.showPopup({
        title: 'Обновление',
        message: 'Обновление данных подписки...',
        buttons: [{ type: 'ok' }]
    });
    
    await loadUserData();
    tg.showAlert('Подписка обновлена!');
}

// Обработка оплаты
async function processPayment() {
    if (!selectedTariff || !selectedPaymentMethod) {
        tg.showAlert('Пожалуйста, выберите тариф и способ оплаты');
        return;
    }
    
    try {
        const response = await fetch('/api/payment/create', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                initData: tg.initData,
                tariffId: selectedTariff.id,
                paymentMethod: selectedPaymentMethod.id,
                deviceCount: deviceCount
            })
        });
        
        if (!response.ok) {
            throw new Error('Ошибка создания платежа');
        }
        
        const data = await response.json();
        
            // Обработка в зависимости от способа оплаты
            if (selectedPaymentMethod.id === 'stars') {
                // Оплата через Telegram Stars
                // Создаем invoice через бота
                tg.showAlert('Оплата через Telegram Stars будет обработана ботом');
                // Закрываем miniapp и открываем бота для оплаты
                tg.openTelegramLink(data.invoiceUrl || `https://t.me/${tg.initDataUnsafe.user?.username || 'bot'}?start=payment_${selectedTariff.id}`);
            } else if (selectedPaymentMethod.id === 'yookassa') {
                // Оплата через ЮKassa
                if (data.paymentUrl) {
                    tg.openLink(data.paymentUrl);
                } else {
                    tg.showAlert('Ошибка создания платежа');
                }
            } else {
                // Другие способы оплаты
                tg.showAlert('Оплата обрабатывается...');
            }
        
    } catch (error) {
        console.error('Ошибка оплаты:', error);
        tg.showAlert('Ошибка при создании платежа');
    }
}

// Добавление подписки в приложение
function addSubscription() {
    const url = document.getElementById('setupSubscriptionUrl').value;
    if (url) {
        // Открываем URL в приложении VPN
        window.open(url, '_blank');
    }
}

// Копирование конфига
async function copyConfig() {
    const url = document.getElementById('setupSubscriptionUrl').value;
    if (url) {
        await navigator.clipboard.writeText(url);
        tg.showAlert('Конфигурация скопирована!');
    }
}

// Выбор приложения
function selectApp(button, appId) {
    document.querySelectorAll('.app-btn').forEach(btn => {
        btn.classList.remove('active');
    });
    button.classList.add('active');
    updateInstructions();
}

// Обновление инструкций в зависимости от ОС
function updateInstructions() {
    const os = document.getElementById('osSelector').value;
    const storeButtons = document.getElementById('storeButtons');
    
    if (os === 'ios') {
        storeButtons.innerHTML = `
            <button class="btn-store" onclick="openAppStore('ru')">App Store (RU)</button>
            <button class="btn-store" onclick="openAppStore('global')">App Store (Global)</button>
        `;
    } else if (os === 'android') {
        storeButtons.innerHTML = `
            <button class="btn-store" onclick="openPlayStore()">Google Play</button>
        `;
    } else {
        storeButtons.innerHTML = `
            <button class="btn-store" onclick="openWebsite()">Скачать</button>
        `;
    }
}

// Открытие App Store
function openAppStore(region) {
    // Здесь должна быть ссылка на приложение в App Store
    window.open('https://apps.apple.com/app/v2raytun', '_blank');
}

// Открытие Google Play
function openPlayStore() {
    window.open('https://play.google.com/store/apps/details?id=com.v2raytun', '_blank');
}

// Открытие сайта
function openWebsite() {
    window.open('https://v2raytun.com', '_blank');
}

// Показать реферальную программу
function showReferral() {
    tg.showAlert('Реферальная программа');
    // Здесь можно открыть отдельный экран с реферальной программой
}

// Показать транзакции
function showTransactions() {
    tg.showAlert('Транзакции');
    // Здесь можно открыть отдельный экран с транзакциями
}

// Открыть канал
function openChannel() {
    window.open('https://t.me/svoyvpn', '_blank');
}

// Связаться с поддержкой
function contactSupport() {
    window.open('https://t.me/svoyvpn_support', '_blank');
}

// Сбросить активные сессии
function resetSessions() {
    tg.showConfirm('Вы уверены, что хотите сбросить все активные сессии?', (confirmed) => {
        if (confirmed) {
            // Здесь должна быть логика сброса сессий
            tg.showAlert('Активные сессии сброшены');
        }
    });
}

// Вспомогательные функции
function formatDate(dateString) {
    const date = new Date(dateString);
    const day = String(date.getDate()).padStart(2, '0');
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const year = date.getFullYear();
    return `${day}.${month}.${year}`;
}

function getMonthsText(months) {
    if (months === 1) return 'Месяц';
    if (months >= 2 && months <= 4) return 'Месяца';
    return 'Месяцев';
}

function showError(message) {
    tg.showAlert(message);
}
