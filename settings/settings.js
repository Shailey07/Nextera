const groqKey = document.getElementById('groq-key');
const groqModel = document.getElementById('groq-model');
const geminiKey = document.getElementById('gemini-key');
const geminiModel = document.getElementById('gemini-model');
const saveBtn = document.getElementById('save-btn');
const resetBtn = document.getElementById('reset-btn');
const statusEl = document.getElementById('status');

const DEFAULTS = {
  groqApiKey: '',
  groqModel: 'openai/gpt-oss-20b',
  geminiApiKey: '',
  geminiModel: 'gemini-2.5-flash'
};

async function loadSettings() {
  const settings = await chrome.storage.local.get(Object.keys(DEFAULTS));
  groqKey.value = settings.groqApiKey || '';
  groqModel.value = settings.groqModel || DEFAULTS.groqModel;
  geminiKey.value = settings.geminiApiKey || '';
  geminiModel.value = settings.geminiModel || DEFAULTS.geminiModel;
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

resetBtn.addEventListener('click', async () => {
  if (!confirm('Reset all settings to default?')) return;
  await chrome.storage.local.set(DEFAULTS);
  await loadSettings();
  showStatus('Settings reset to default', 'info');
});

loadSettings();