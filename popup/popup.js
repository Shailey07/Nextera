const logEl = document.getElementById('log');
const statusEl = document.getElementById('status-text');
const statusBox = document.getElementById('status-box');
const progressBar = document.getElementById('progress-bar');
const btnComplete = document.getElementById('btn-complete');
const btnLLM = document.getElementById('btn-llm');
const btnCurrentQuiz = document.getElementById('btn-current-quiz');
const btnStop = document.getElementById('btn-stop');
const currentItemEl = document.getElementById('current-item');
const currentItemText = document.getElementById('current-item-text');

const btnVideos = document.getElementById('btn-videos');
const btnReadings = document.getElementById('btn-readings');
const btnQuizzes = document.getElementById('btn-quizzes');
const btnGraded = document.getElementById('btn-graded');
const btnDiscussions = document.getElementById('btn-discussions');
const btnShareLink = document.getElementById('btn-share-link');

let pollInterval = null;
let shownLogCount = 0;
let isRunning = false;

function addLog(message, type = 'info') {
  const entry = document.createElement('div');
  entry.className = `log-entry ${type}`;
  entry.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
  logEl.appendChild(entry);
  logEl.scrollTop = logEl.scrollHeight;
}

function setStatus(text, state = 'ready') {
  statusEl.textContent = text;
  statusBox.className = 'status-box';
  if (state === 'error') statusBox.classList.add('error');
  if (state === 'running') statusBox.classList.add('running');
}

function setRunningUI(running) {
  isRunning = running;
  const allBtns = [btnComplete, btnLLM, btnCurrentQuiz, btnVideos, btnReadings,
                   btnQuizzes, btnGraded, btnDiscussions, btnShareLink];
  allBtns.forEach(btn => { if (btn) btn.disabled = running; });

  if (running) {
    btnStop.classList.remove('hidden');
    currentItemEl.classList.remove('hidden');
  } else {
    btnStop.classList.add('hidden');
    currentItemEl.classList.add('hidden');
    currentItemText.textContent = '—';
  }
}

async function getCourseSlug() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url?.includes('coursera.org')) return null;
  const match = tab.url.match(/\/learn\/([^\/]+)/);
  return match ? match[1] : null;
}

async function runNextera(mode) {
  if (isRunning) return;

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url?.includes('coursera.org')) {
    setStatus('Coursera course kholo', 'error');
    return;
  }
  const match = tab.url.match(/\/learn\/([^\/]+)/);
  const slug = match ? match[1] : null;
  if (!slug) {
    setStatus('Course slug not found', 'error');
    return;
  }

  setStatus('Starting...', 'running');
  setRunningUI(true);
  addLog(`Course: ${slug}`, 'info');
  addLog(`Mode: ${mode}`, 'info');
  progressBar.style.width = '5%';
  shownLogCount = 0;

  try {
    const response = await chrome.runtime.sendMessage({
      action: 'runSkipera',
      slug,
      mode,
      currentUrl: tab.url
    });

    if (response?.error) {
      setStatus('Error: ' + response.error, 'error');
      addLog(response.error, 'error');
      setRunningUI(false);
      return;
    }

    addLog('Python script running...', 'success');
    setStatus('Processing...', 'running');
    pollProgress();
  } catch (err) {
    setStatus('Failed: ' + err.message, 'error');
    addLog(err.message, 'error');
    setRunningUI(false);
  }
}

function pollProgress() {
  if (pollInterval) clearInterval(pollInterval);

  pollInterval = setInterval(async () => {
    const { progress, logs, done } = await chrome.storage.local.get(['progress', 'logs', 'done']);

    if (logs && logs.length > shownLogCount) {
      logs.slice(shownLogCount).forEach(log => {
        addLog(log.msg, log.type);
        if (log.msg && log.msg.includes('Processing')) {
          const match = log.msg.match(/Processing (.+)$/);
          if (match) currentItemText.textContent = match[1].substring(0, 50);
        }
      });
      shownLogCount = logs.length;
    }

    if (progress) progressBar.style.width = `${progress}%`;

    if (done) {
      clearInterval(pollInterval);
      pollInterval = null;
      setStatus('Complete!', 'ready');
      progressBar.style.width = '100%';
      setRunningUI(false);
      await chrome.storage.local.remove(['progress', 'logs', 'done']);
    }
  }, 1000);
}

btnStop.addEventListener('click', async () => {
  setStatus('Stopping...', 'error');
  addLog('Stopping script...', 'warning');
  try {
    await chrome.runtime.sendMessage({ action: 'stopSkipera' });
    if (pollInterval) clearInterval(pollInterval);
    pollInterval = null;
    setRunningUI(false);
    setStatus('Stopped', 'error');
    addLog('Script stopped by user.', 'error');
  } catch (err) {
    addLog('Stop failed: ' + err.message, 'error');
  }
});

btnComplete.addEventListener('click', () => runNextera('complete'));
btnLLM.addEventListener('click', () => runNextera('llm'));
btnCurrentQuiz.addEventListener('click', () => runNextera('current'));

btnVideos.addEventListener('click', () => runNextera('videos'));
btnReadings.addEventListener('click', () => runNextera('readings'));
btnQuizzes.addEventListener('click', () => runNextera('quizzes'));
btnGraded.addEventListener('click', () => runNextera('graded'));
btnDiscussions.addEventListener('click', () => runNextera('discussions'));
btnShareLink.addEventListener('click', () => runNextera('sharelink'));

document.getElementById('settings-btn').addEventListener('click', () => {
  chrome.runtime.openOptionsPage();
});

window.addEventListener('unload', () => {
  if (pollInterval) clearInterval(pollInterval);
});

getCourseSlug().then(slug => {
  if (!slug) setStatus('Coursera course kholo', 'error');
});