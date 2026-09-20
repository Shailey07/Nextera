const groqKey = document.getElementById('groq-key');
const groqModel = document.getElementById('groq-model');
const geminiKey = document.getElementById('gemini-key');
const geminiModel = document.getElementById('gemini-model');
const saveBtn = document.getElementById('save-btn');
const resetBtn = document.getElementById('reset-btn');
const statusEl = document.getElementById('status');

const cookieCauth = document.getElementById('cookie-cauth');
const cookieCsrf = document.getElementById('cookie-csrf');
const cookie204u = document.getElementById('cookie-204u');
const saveCookiesBtn = document.getElementById('save-cookies-btn');
const refreshCookiesBtn = document.getElementById('refresh-cookies-btn');

const DEFAULTS = {
  groqApiKey: '',
  groqModel: 'openai/gpt-oss-120b',
  geminiApiKey: '',
  geminiModel: 'gemini-2.5-flash'
};

async function loadSettings() {
  const settings = await chrome.storage.local.get([
    ...Object.keys(DEFAULTS),
    'cookieCauth', 'cookieCsrf', 'cookie204u'
  ]);

  groqKey.value = settings.groqApiKey || '';
  groqModel.value = settings.groqModel || DEFAULTS.groqModel;
  geminiKey.value = settings.geminiApiKey || '';
  geminiModel.value = settings.geminiModel || DEFAULTS.geminiModel;

  cookieCauth.value = settings.cookieCauth || '';
  cookieCsrf.value = settings.cookieCsrf || '';
  cookie204u.value = settings.cookie204u || '';
}

function showStatus(message, type = 'success') {
  statusEl.textContent = message;
  statusEl.className = `status ${type}`;
  statusEl.classList.remove('hidden');
  setTimeout(() => statusEl.classList.add('hidden'), 4000);
}

saveBtn.addEventListener('click', async () => {
  const settings = {
    groqApiKey: groqKey.value.trim(),
    groqModel: groqModel.value,
    geminiApiKey: geminiKey.value.trim(),
    geminiModel: geminiModel.value
  };

  await chrome.storage.local.set(settings);

  try {
    const response = await chrome.runtime.sendMessage({
      action: 'syncKeys',
      ...settings
    });

    if (response?.ok) {
      showStatus('Settings saved and synced to config.json', 'success');
    } else {
      showStatus('Saved locally, but sync failed: ' + (response?.error || 'unknown'), 'error');
    }
  } catch (err) {
    showStatus('Saved locally, but native host not reachable', 'error');
  }
});

saveCookiesBtn.addEventListener('click', async () => {
  const cauth = cookieCauth.value.trim();
  const csrf = cookieCsrf.value.trim();
  const u204 = cookie204u.value.trim();

  if (!cauth) {
    showStatus('CAUTH cookie required hai!', 'error');
    return;
  }

  const cookies = { CAUTH: cauth };
  if (csrf) cookies['CSRF3-Token'] = csrf;
  if (u204) cookies['__204u'] = u204;

  await chrome.storage.local.set({
    cookieCauth: cauth,
    cookieCsrf: csrf,
    cookie204u: u204
  });

  try {
    const response = await chrome.runtime.sendMessage({
      action: 'syncCookies',
      cookies
    });

    if (response?.ok) {
      showStatus('Cookies saved and synced to config.json!', 'success');
    } else {
      showStatus('Saved locally, but sync failed: ' + (response?.error || 'unknown'), 'error');
    }
  } catch (err) {
    showStatus('Saved locally, but native host not reachable', 'error');
  }
});

refreshCookiesBtn.addEventListener('click', async () => {
  if (!confirm('Browser se automatically cookies fetch karne ki koshish karein?\n\n(Chrome/Firefox band hona chahiye best results ke liye)')) return;

  showStatus('Fetching cookies from browser...', 'info');

  try {
    const response = await chrome.runtime.sendMessage({
      action: 'fetchCookies'
    });

    if (response?.cookies && response.cookies.CAUTH) {
      cookieCauth.value = response.cookies.CAUTH || '';
      cookieCsrf.value = response.cookies['CSRF3-Token'] || '';
      cookie204u.value = response.cookies['__204u'] || '';

      await chrome.storage.local.set({
        cookieCauth: cookieCauth.value,
        cookieCsrf: cookieCsrf.value,
        cookie204u: cookie204u.value
      });

      showStatus('Cookies fetched from browser and saved!', 'success');
    } else {
      showStatus('Cookies not found. Manually paste karo.', 'error');
    }
  } catch (err) {
    showStatus('Fetch failed: ' + err.message, 'error');
  }
});

resetBtn.addEventListener('click', async () => {
  if (!confirm('Reset all settings to default?')) return;
  await chrome.storage.local.set(DEFAULTS);
  await loadSettings();
  showStatus('Settings reset to default', 'info');
});

loadSettings();