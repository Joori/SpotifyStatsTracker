// Now playing card. Polls /api/now-playing only while this page is open and visible,
// and moves the progress bar locally between polls.
(function () {
  const card = document.getElementById('now-playing');
  if (!card) return;

  const POLL_MS = 20000;
  const MIN_POLL_MS = 5000;
  const els = {
    label: card.querySelector('[data-np="label"]'),
    cover: card.querySelector('[data-np="cover"]'),
    title: card.querySelector('[data-np="title"]'),
    subtitle: card.querySelector('[data-np="subtitle"]'),
    progress: card.querySelector('[data-np="progress"]'),
    bar: card.querySelector('[data-np="bar"]'),
    elapsed: card.querySelector('[data-np="elapsed"]'),
    total: card.querySelector('[data-np="total"]'),
  };
  let state = null;
  let receivedAt = 0;
  let pollTimer = null;
  let tickTimer = null;

  function fmt(ms) {
    if (ms == null || ms < 0) return '';
    const s = Math.floor(ms / 1000);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = String(s % 60).padStart(2, '0');
    return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`;
  }

  function currentProgress() {
    if (!state || state.progressMs == null) return null;
    let p = state.progressMs + (state.isPlaying ? Date.now() - receivedAt : 0);
    if (state.durationMs) p = Math.min(p, state.durationMs);
    return p;
  }

  function renderProgress() {
    const p = currentProgress();
    const hasBar = p != null && state && state.durationMs;
    els.progress.hidden = !hasBar;
    if (!hasBar) return;
    const pct = Math.max(0, Math.min(100, (p / state.durationMs) * 100));
    els.bar.style.width = pct + '%';
    els.progress.setAttribute('aria-valuenow', String(Math.round(pct)));
    els.elapsed.textContent = fmt(p);
    els.total.textContent = fmt(state.durationMs);
  }

  function setCover(url) {
    if (url && /^https:\/\//.test(url)) {
      if (els.cover.getAttribute('src') !== url) els.cover.src = url;
      els.cover.hidden = false;
    } else {
      els.cover.hidden = true;
      els.cover.removeAttribute('src');
    }
  }

  function setTitle(text, url) {
    els.title.textContent = text || '';
    if (url && /^https:\/\/open\.spotify\.com\//.test(url)) {
      els.title.href = url;
    } else {
      els.title.removeAttribute('href');
    }
  }

  function render() {
    if (!state || !state.available) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    card.classList.toggle('is-idle', !state.active);
    if (!state.active) {
      els.label.textContent = 'Now playing';
      setTitle('Nothing playing on Spotify right now', null);
      els.subtitle.textContent = '';
      setCover(null);
      els.progress.hidden = true;
      return;
    }
    const paused = !state.isPlaying;
    if (state.type === 'ad') {
      els.label.textContent = paused ? 'Paused' : 'Now playing';
      setTitle('Advertisement', null);
      els.subtitle.textContent = 'Your music will be back shortly';
      setCover(null);
    } else if (state.type === 'unknown' || !state.title) {
      els.label.textContent = paused ? 'Paused' : 'Now playing';
      setTitle('Something is playing on Spotify', null);
      els.subtitle.textContent = '';
      setCover(null);
    } else {
      els.label.textContent = (paused ? 'Paused' : 'Now playing') + (state.type === 'episode' ? ' · Podcast' : '');
      setTitle(state.title, state.url);
      els.subtitle.textContent = state.subtitle || '';
      setCover(state.imageUrl);
    }
    renderProgress();
  }

  function schedule() {
    clearTimeout(pollTimer);
    if (document.hidden) return;
    let delay = POLL_MS;
    const p = currentProgress();
    if (state && state.isPlaying && p != null && state.durationMs) {
      const left = state.durationMs - p + 1500;   // refresh soon after the track ends
      delay = Math.max(MIN_POLL_MS, Math.min(POLL_MS, left));
    }
    pollTimer = setTimeout(poll, delay);
  }

  function poll() {
    if (document.hidden) return;
    fetch('/api/now-playing', { cache: 'no-store' })
      .then((r) => r.json())
      .then((data) => {
        if (data && data.reason === 'rateLimited' && state && state.available) {
          return;   // keep showing what we had
        }
        state = data;
        receivedAt = Date.now();
        render();
      })
      .catch(() => {})
      .finally(schedule);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearTimeout(pollTimer);
      clearInterval(tickTimer);
      tickTimer = null;
    } else {
      poll();
      if (!tickTimer) tickTimer = setInterval(renderProgress, 1000);
    }
  });

  poll();
  tickTimer = setInterval(renderProgress, 1000);
})();
