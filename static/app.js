/* ── State ── */
let currentJobId = null;
let selectedProvider = 'ollama';

/* ── DOM helpers ── */
const $ = id => document.getElementById(id);
const show = id => $(`${id}`).classList.remove('hidden');
const hide = id => $(`${id}`).classList.add('hidden');

/* ── Provider toggle ── */
async function setProvider(p) {
  selectedProvider = p;
  $('btn-anthropic').classList.toggle('active', p === 'anthropic');
  $('btn-ollama').classList.toggle('active', p === 'ollama');

  if (p === 'ollama') {
    show('ollama-model-wrap');
    await loadOllamaModels();
  } else {
    hide('ollama-model-wrap');
  }
}

async function loadOllamaModels() {
  const select = $('ollama-model');
  const status = $('ollama-status');
  select.innerHTML = '<option value="">Checking Ollama…</option>';
  status.textContent = '';
  status.className = 'ollama-status';

  try {
    const res = await fetch('/api/ollama/models');
    const data = await res.json();
    if (!data.available) {
      select.innerHTML = '<option value="">Ollama not running</option>';
      status.textContent = '— start Ollama first';
      status.className = 'ollama-status error';
      return;
    }
    if (!data.models.length) {
      select.innerHTML = '<option value="">No models installed</option>';
      status.textContent = '— run: ollama pull llama3.2';
      status.className = 'ollama-status error';
      return;
    }
    select.innerHTML = data.models.map(m => `<option value="${m}">${m}</option>`).join('');
    status.textContent = `${data.models.length} model${data.models.length > 1 ? 's' : ''} available`;
    status.className = 'ollama-status ok';
  } catch {
    select.innerHTML = '<option value="">Error</option>';
    status.textContent = '— could not reach server';
    status.className = 'ollama-status error';
  }
}

/* ── On load ── */
window.addEventListener('DOMContentLoaded', () => {
  setProvider('ollama');
  loadRecentReports();

  $('url-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') startCrawl();
  });
});

/* ── Recent reports ── */
async function loadRecentReports() {
  try {
    const res = await fetch('/api/reports');
    const files = await res.json();
    if (!files.length) return;

    const container = $('recent-reports');
    container.innerHTML = '<span>Recent:</span>';
    files.slice(0, 5).forEach(f => {
      const a = document.createElement('a');
      // derive a readable label from filename: scraped_example_com_20260404_120000.md
      const parts = f.replace('scraped_', '').replace('.md', '').split('_');
      // last two parts are date & time, rest is domain
      const label = parts.slice(0, -2).join('.').replace(/_/g, '.');
      a.textContent = label;
      a.onclick = () => loadExistingReport(f);
      container.appendChild(a);
    });
    show('recent-reports');
  } catch (_) {}
}

async function loadExistingReport(filename) {
  resetUI();
  setButton(true);
  showProgress('Analyzing ' + filename + '…');
  appendLog(`Loading existing report: ${filename}`);
  await runAnalysis(filename);
  setButton(false);
}

/* ── Crawl ── */
async function startCrawl() {
  const url = $('url-input').value.trim();
  if (!url) { showError('Please enter a URL.'); return; }

  resetUI();
  setButton(true);
  showProgress('Starting crawl…');
  hideError();

  let res;
  try {
    res = await fetch('/api/crawl', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
  } catch (e) {
    showError('Could not reach server. Is app.py running?');
    setButton(false);
    return;
  }

  const { job_id, error } = await res.json();
  if (error) { showError(error); setButton(false); return; }

  currentJobId = job_id;
  streamProgress(job_id);
}

function streamProgress(jobId) {
  const es = new EventSource(`/api/crawl/${jobId}/stream`);

  es.onmessage = async e => {
    const msg = JSON.parse(e.data);

    if (msg.type === 'progress') {
      appendLog(msg.message);
    }

    if (msg.type === 'done') {
      es.close();
      appendLog('\nCrawl complete. Generating analysis…');
      $('progress-status').textContent = 'Analyzing...';
      await runAnalysis(msg.file);
      setButton(false);
    }

    if (msg.type === 'error') {
      es.close();
      showError(msg.message);
      setButton(false);
    }
  };

  es.onerror = () => {
    es.close();
    showError('Lost connection to server.');
    setButton(false);
  };
}

/* ── Analysis ── */
async function runAnalysis(filename) {
  const ollamaModel = $('ollama-model')?.value || 'llama3.2';
  let res;
  try {
    res = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        file: filename,
        provider: selectedProvider,
        ollama_model: ollamaModel,
      }),
    });
  } catch (e) {
    showError('Analysis request failed. Is app.py running?');
    return;
  }

  const data = await res.json();
  if (data.error) {
    showError(data.error);
    return;
  }

  hide('progress-section');
  renderReport(data);
  loadRecentReports();
}

/* ── Render ── */
function renderReport(data) {
  // Company header
  $('company-name').textContent = data.company_name || data.domain || 'Unknown';
  const domainLink = $('company-domain');
  const domain = data.domain || '';
  domainLink.textContent = domain;
  domainLink.href = domain.startsWith('http') ? domain : `https://${domain}`;

  // Overview
  $('ov-what').textContent        = data.overview?.what_they_do         || '—';
  $('ov-audience').textContent    = data.overview?.target_audience       || '—';
  $('ov-biz').textContent         = data.overview?.business_model        || '—';
  $('ov-competitive').textContent = data.overview?.competitive_positioning || '—';

  // Features
  const grid = $('features-grid');
  grid.innerHTML = '';
  (data.features || []).forEach(f => {
    const card = document.createElement('div');
    card.className = 'feature-card';

    const name = document.createElement('div');
    name.className = 'feature-name';
    name.textContent = f.name;
    card.appendChild(name);

    const desc = document.createElement('div');
    desc.className = 'feature-desc';
    desc.textContent = f.description;
    card.appendChild(desc);

    if (f.source_url) {
      const link = document.createElement('a');
      link.className = 'feature-link';
      link.href = f.source_url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = 'Learn more →';
      card.appendChild(link);
    }

    grid.appendChild(card);
  });

  // Pages reviewed
  const pages = data.pages_reviewed || [];
  $('pages-count-label').textContent = `(${pages.length})`;
  const list = $('pages-list');
  list.innerHTML = '';
  pages.forEach(p => {
    const item = document.createElement('div');
    item.className = 'page-item';
    const a = document.createElement('a');
    a.href = p.url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    const titleSpan = document.createElement('span');
    titleSpan.className = 'page-title';
    titleSpan.textContent = p.title || p.url;
    const urlSpan = document.createElement('span');
    urlSpan.className = 'page-url';
    urlSpan.textContent = p.title ? p.url : '';
    a.appendChild(titleSpan);
    if (p.title) a.appendChild(urlSpan);
    item.appendChild(a);
    list.appendChild(item);
  });

  show('report-section');
}

/* ── Pages toggle ── */
function togglePages() {
  const list = $('pages-list');
  const chevron = $('pages-chevron');
  const isHidden = list.classList.contains('hidden');
  if (isHidden) {
    show('pages-list');
    chevron.classList.add('open');
  } else {
    hide('pages-list');
    chevron.classList.remove('open');
  }
}

/* ── Helpers ── */
function resetUI() {
  hide('report-section');
  hide('progress-section');
  hide('error-banner');
  $('progress-log').textContent = '';

  $('pages-chevron').classList.remove('open');
  hide('pages-list');
}

function showProgress(status) {
  $('progress-status').textContent = status;
  show('progress-section');
}

function appendLog(line) {
  const log = $('progress-log');
  log.textContent += line + '\n';
  log.scrollTop = log.scrollHeight;
}

function showError(msg) {
  const el = $('error-banner');
  el.textContent = msg;
  show('error-banner');
}

function hideError() {
  hide('error-banner');
}

function setButton(loading) {
  const btn = $('crawl-btn');
  if (loading) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>Running…';
  } else {
    btn.disabled = false;
    btn.textContent = 'Crawl';
  }
}
