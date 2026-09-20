const NATIVE_HOST = 'com.nextera.fasttrack';

let nativePort = null;
let isRunning = false;
let currentRunId = 0;

function connectNative() {
  if (nativePort) return nativePort;
  try {
    nativePort = chrome.runtime.connectNative(NATIVE_HOST);
    nativePort.onDisconnect.addListener(() => {
      console.log('Native disconnected:', chrome.runtime.lastError?.message);
      nativePort = null;
      isRunning = false;
    });
    return nativePort;
  } catch (err) {
    console.error('Native connect failed:', err);
    return null;
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === 'runSkipera') {
    if (isRunning) {
      sendResponse({ error: 'Already running. Wait for it to finish.' });
      return true;
    }

    const port = connectNative();
    if (!port) {
      sendResponse({ error: 'Native host not installed.' });
      return true;
    }

    isRunning = true;
    currentRunId++;
    const runId = currentRunId;

    chrome.storage.local.set({ logs: [], progress: 0, done: false });

    port.postMessage({
      action: 'run',
      slug: msg.slug,
      mode: msg.mode,
      currentUrl: msg.currentUrl || ''
    });

    port.onMessage.addListener((response) => {
      if (runId !== currentRunId) return;

      if (response.type === 'log') {
        chrome.storage.local.get(['logs']).then(({ logs = [] }) => {
          logs.push({ msg: response.message, type: response.level });
          if (logs.length > 300) logs = logs.slice(-300);
          chrome.storage.local.set({ logs });
        });
      } else if (response.type === 'progress') {
        chrome.storage.local.set({ progress: response.value });
      } else if (response.type === 'done') {
        chrome.storage.local.set({ done: true });
        isRunning = false;
        port.disconnect();
        nativePort = null;
      }
    });

    sendResponse({ ok: true });
    return true;
  }

  if (msg.action === 'stopSkipera') {
    if (nativePort) {
      try { nativePort.disconnect(); } catch (e) {}
      nativePort = null;
    }
    isRunning = false;
    currentRunId++;
    chrome.storage.local.set({ done: true });
    sendResponse({ ok: true });
    return true;
  }

  if (msg.action === 'syncKeys') {
    const port = connectNative();
    if (!port) {
      sendResponse({ error: 'Native host not installed.' });
      return true;
    }

    port.postMessage({
      action: 'sync_keys',
      keys: {
        groq_api_key: msg.groqApiKey || '',
        groq_model: msg.groqModel || 'openai/gpt-oss-20b',
        gemini_api_key: msg.geminiApiKey || '',
        gemini_model: msg.geminiModel || 'gemini-2.5-flash'
      }
    });

    port.onMessage.addListener((response) => {
      if (response.type === 'keys_synced') {
        sendResponse({ ok: true });
        port.disconnect();
        nativePort = null;
      }
    });

    return true;
  }

  if (msg.action === 'syncCookies') {
    const port = connectNative();
    if (!port) {
      sendResponse({ error: 'Native host not installed.' });
      return true;
    }

    port.postMessage({
      action: 'sync_cookies',
      cookies: msg.cookies
    });

    port.onMessage.addListener((response) => {
      if (response.type === 'cookies_synced') {
        sendResponse({ ok: true });
        port.disconnect();
        nativePort = null;
      }
    });

    return true;
  }

  if (msg.action === 'fetchCookies') {
    const port = connectNative();
    if (!port) {
      sendResponse({ error: 'Native host not installed.' });
      return true;
    }

    port.postMessage({ action: 'fetch_cookies' });

    port.onMessage.addListener((response) => {
      if (response.type === 'cookies_fetched') {
        sendResponse({ cookies: response.cookies });
        port.disconnect();
        nativePort = null;
      }
    });

    return true;
  }

  if (msg.action === 'getCacheStats') {
    const port = connectNative();
    if (!port) {
      sendResponse({ count: 0 });
      return true;
    }

    port.postMessage({ action: 'get_cache_stats' });

    port.onMessage.addListener((response) => {
      if (response.type === 'cache_stats') {
        sendResponse({ count: response.count });
        port.disconnect();
        nativePort = null;
      }
    });

    return true;
  }
});

chrome.commands.onCommand.addListener(async (command) => {
  if (command === 'auto-complete') {
    chrome.action.openPopup();
  }
});