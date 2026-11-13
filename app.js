/* 单词小侦探 — 听音选图词汇量小测
 *
 * 纯静态网页：无框架、无构建步骤、无外部依赖。
 * 数据来自同目录 words.json（等级 + 单词 + 图片/emoji + 音频路径）。
 * 音频优先播放 audio 文件（Web Audio，播放稳定、可与语音解锁）；
 * 没有音频文件时自动降级为浏览器内置语音（SpeechSynthesis）念单词。
 */
(() => {
  'use strict';

  const IS_IOS =
    /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

  // 10ms 静音 WAV：首次手势时用它激活 HTMLAudioElement，iOS 后续才允许程序播放
  const SILENT_WAV =
    'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAESsAABErAAABAAgAZGF0YQAAAAA=';

  const STORAGE_KEY = 'word-detective:v1';
  const DEFAULT_QUESTIONS = 8;

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const screens = {
    home: $('#screen-home'),
    levels: $('#screen-levels'),
    test: $('#screen-test'),
    result: $('#screen-result'),
    placement: $('#screen-placement'),
  };

  const el = {
    levelGrid: $('#levelGrid'),
    startCard: $('#startCard'),
    btnStartPlacement: $('#btnStartPlacement'),
    placementBest: $('#placementBest'),
    homeError: $('#homeError'),
    btnLevelMode: $('#btnLevelMode'),
    btnLevelsBack: $('#btnLevelsBack'),
    levelsInfo: $('#levelsInfo'),
    placementEmoji: $('#placementEmoji'),
    placementTitle: $('#placementTitle'),
    placementLevel: $('#placementLevel'),
    placementVocab: $('#placementVocab'),
    placementNote: $('#placementNote'),
    ladder: $('#ladder'),
    placementMissedWrap: $('#placementMissedWrap'),
    placementMissed: $('#placementMissed'),
    btnPlacementAgain: $('#btnPlacementAgain'),
    btnPlacementLevel: $('#btnPlacementLevel'),
    btnPlacementHome: $('#btnPlacementHome'),
    btnParent: $('#btnParent'),
    parentModal: $('#parentModal'),
    btnCloseParent: $('#btnCloseParent'),
    selQuestions: $('#selQuestions'),
    btnSoundTest: $('#btnSoundTest'),
    soundState: $('#soundState'),
    historyList: $('#historyList'),
    historyCount: $('#historyCount'),
    btnClearHistory: $('#btnClearHistory'),
    btnQuit: $('#btnQuit'),
    testTitle: $('#testTitle'),
    progressText: $('#progressText'),
    progressFill: $('#progressFill'),
    feedback: $('#feedback'),
    btnReplay: $('#btnReplay'),
    cardGrid: $('#cardGrid'),
    resultEmoji: $('#resultEmoji'),
    resultTitle: $('#resultTitle'),
    scoreRing: $('#scoreRing'),
    scoreNum: $('#scoreNum'),
    scoreDetail: $('#scoreDetail'),
    stars: $('#stars'),
    missedWrap: $('#missedWrap'),
    missedList: $('#missedList'),
    btnAgain: $('#btnAgain'),
    btnNextLevel: $('#btnNextLevel'),
    btnHome: $('#btnHome'),
  };

  const state = {
    levels: [],
    words: [],
    wordsByLevel: new Map(),
    settings: { questions: DEFAULT_QUESTIONS },
    hasStoredQuestions: false,
    history: [],
    test: null,
    placement: null,
    totalUniqueWords: 0,
    timers: new Set(),
    audioCtx: null,
    audioPrimed: false,
    audioEl: null,
    audioElPrimed: false,          // iOS/Android：首次手势里已用静音 buffer 解锁
    currentAudioEl: null,        // 解码失败时的 <audio> 兜底
    buffers: new Map(),          // word id -> AudioBuffer
    loadingAudio: new Map(),     // word id -> Promise<AudioBuffer|null>
    currentSource: null,
    ttsTimer: null,
    wakeLock: null,
    cachedVoice: null,
  };

  /* ---------------- 基础工具 ---------------- */

  const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ESC_MAP[c]);

  function shuffle(arr) {
    const a = arr.slice();
    for (let i = a.length - 1; i > 0; i -= 1) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }

  function isImagePath(s) {
    if (typeof s !== 'string') return false;
    return (
      /^(https?:|data:|blob:|\.{0,2}\/|\/)/.test(s) ||
      /\.(png|jpe?g|svg|webp|gif|avif)(\?|#|$)/i.test(s)
    );
  }

  // 图片文件缺失/加载失败时，自动退回词条自带的 emoji（words.json 的 emoji 字段），
  // 这样词表里只有一部分词配了图也能正常测试。
  function attachImageFallback(root) {
    root.addEventListener(
      'error',
      (event) => {
        const img = event.target;
        if (!(img instanceof HTMLImageElement) || !img.dataset.emoji) return;
        const span = document.createElement('span');
        span.className = img.classList.contains('chip-img') ? 'chip-emoji' : 'pic-emoji';
        span.setAttribute('aria-hidden', 'true');
        span.textContent = img.dataset.emoji;
        img.replaceWith(span);
      },
      true,
    );
  }

  function formatDate(ts) {
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return '';
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function setScreen(name) {
    Object.entries(screens).forEach(([key, node]) => {
      node.classList.toggle('hidden', key !== name);
    });
  }

  function later(fn, ms) {
    const id = setTimeout(() => {
      state.timers.delete(id);
      fn();
    }, ms);
    state.timers.add(id);
    return id;
  }

  function clearTimers() {
    state.timers.forEach((id) => clearTimeout(id));
    state.timers.clear();
  }

  /* ---------------- 本地存储 ---------------- */

  function loadStored() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (!saved || typeof saved !== 'object') return;
      if (Number.isInteger(saved.questions) && saved.questions >= 0) {
        state.settings.questions = saved.questions;
        state.hasStoredQuestions = true;
      }
      if (Array.isArray(saved.history)) {
        state.history = saved.history
          .filter((row) => row && (Number.isInteger(row.levelId) || row.mode === 'placement'))
          .slice(-500);
      }
    } catch (err) {
      /* localStorage 不可用时静默忽略 */
    }
  }

  function saveStored() {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ questions: state.settings.questions, history: state.history.slice(-500) })
      );
    } catch (err) {
      /* 忽略：无痕模式等场景 */
    }
  }

  /* ---------------- 音频 ---------------- */

  // iOS/Android 浏览器要求「声音必须在用户手势里启动」。
  // 在页面任何第一次点击/触摸时就建立并唤醒 AudioContext，顺便播一个静音 buffer，
  // 之后自动出题时的声音才不会被浏览器拦截。
  function unlockAudio() {
    try {
      let ctx = state.audioCtx;
      if (!ctx) {
        const AudioCtor = window.AudioContext || window.webkitAudioContext;
        if (!AudioCtor) return null;
        ctx = new AudioCtor();
        state.audioCtx = ctx;
      }
      if (ctx.state === 'suspended') ctx.resume().catch(() => {});
      if (!state.audioPrimed) {
        state.audioPrimed = true;
        const rate = ctx.sampleRate || 22050;
        const frames = Math.max(1, Math.floor(rate * 0.01));
        const silent = ctx.createBuffer(1, frames, rate);
        const source = ctx.createBufferSource();
        source.buffer = silent;
        source.connect(ctx.destination);
        try {
          source.start(0);
        } catch (err) {
          /* ignore */
        }
      }
      primeAudioElement();
      return ctx;
    } catch (err) {
      return null;
    }
  }

  function getAudioEl() {
    if (state.audioEl) return state.audioEl;
    const el = document.createElement('audio');
    el.preload = 'auto';
    el.setAttribute('playsinline', '');
    el.setAttribute('webkit-playsinline', '');
    el.style.display = 'none';
    document.body.appendChild(el);
    state.audioEl = el;
    return el;
  }

  // 必须在用户手势里调用：用一小段静音激活 <audio>，之后才能程序化播放
  function primeAudioElement() {
    if (state.audioElPrimed) return;
    state.audioElPrimed = true;
    try {
      const el = getAudioEl();
      el.src = SILENT_WAV;
      const p = el.play();
      if (p && typeof p.then === 'function') {
        p.then(() => {
          try {
            el.pause();
            el.currentTime = 0;
          } catch (err) {
            /* ignore */
          }
        }).catch(() => {
          state.audioElPrimed = false;
        });
      }
    } catch (err) {
      state.audioElPrimed = false;
    }
  }

  async function ensureAudio() {
    const ctx = unlockAudio();
    if (!ctx) return null;
    if (ctx.state === 'suspended') {
      try {
        await ctx.resume();
      } catch (err) {
        /* ignore */
      }
    }
    return ctx;
  }

  function stopSource() {
    if (state.currentAudioEl) {
      try {
        state.currentAudioEl.pause();
        state.currentAudioEl.currentTime = 0;
      } catch (err) {
        /* ignore */
      }
      state.currentAudioEl = null;
    }
    if (state.currentSource) {
      try {
        state.currentSource.stop();
      } catch (err) {
        /* already ended */
      }
      state.currentSource = null;
    }
  }

  // 同 id 的音频候选地址：m4a 优先，其次 audio-wav/<id>.wav
  // （Safari/iOS 的 Web Audio 对 AAC 解码不稳定，WAV 作为兜底）
  function audioUrls(wordObj) {
    const urls = [];
    if (wordObj && wordObj.audio) {
      urls.push(wordObj.audio);
      if (/\.m4a$/i.test(wordObj.audio)) {
        urls.push(wordObj.audio.replace(/^audio\//, 'audio-wav/').replace(/\.m4a$/i, '.wav'));
      }
    }
    return urls;
  }

  // 兼容旧版 Safari：decodeAudioData 只支持回调写法
  function decodeAudioBuffer(ctx, raw) {
    return new Promise((resolve, reject) => {
      try {
        const maybe = ctx.decodeAudioData(raw, resolve, reject);
        if (maybe && typeof maybe.then === 'function') maybe.then(resolve, reject);
      } catch (err) {
        reject(err);
      }
    });
  }

  function loadBuffer(wordObj) {
    const ctx = state.audioCtx;
    if (!ctx || !wordObj) return Promise.resolve(null);
    if (state.buffers.has(wordObj.id)) return Promise.resolve(state.buffers.get(wordObj.id));
    if (state.loadingAudio.has(wordObj.id)) return state.loadingAudio.get(wordObj.id);

    const promise = (async () => {
      try {
        const urls = audioUrls(wordObj);
        for (const url of urls) {
          try {
            const res = await fetch(url, { cache: 'force-cache' });
            if (!res.ok) continue;
            const raw = await res.arrayBuffer();
            const buffer = await decodeAudioBuffer(ctx, raw);
            if (buffer) {
              state.buffers.set(wordObj.id, buffer);
              state.audioFormat = url;
              return buffer;
            }
          } catch (err) {
            /* 试下一个地址 */
          }
        }
        return null;
      } finally {
        state.loadingAudio.delete(wordObj.id);
      }
    })();

    state.loadingAudio.set(wordObj.id, promise);
    return promise;
  }

  function playWithAudioElement(wordObj) {
    if (!wordObj || !wordObj.audio) return false;
    const el = getAudioEl();
    const urls = audioUrls(wordObj);
    let idx = 0;
    try {
      el.pause();
    } catch (err) {
      /* ignore */
    }
    const tryNext = () => {
      const src = urls[idx];
      idx += 1;
      if (!src) return false;
      el.src = src;
      try {
        el.currentTime = 0;
      } catch (err) {
        /* ignore */
      }
      state.currentAudioEl = el;
      const played = el.play();
      if (played && typeof played.catch === 'function') {
        played.catch(() => {
          if (idx < urls.length) tryNext();
        });
      }
      return true;
    };
    return tryNext();
  }

  async function preloadLevelAudio(words) {
    if (!state.audioCtx) return;
    const targets = words.filter((w) => w.audio && !state.buffers.has(w.id));
    await Promise.all(targets.slice(0, 30).map((w) => loadBuffer(w)));
  }

  function flashSpeaker() {
    el.btnReplay.classList.remove('playing');
    void el.btnReplay.offsetWidth;
    el.btnReplay.classList.add('playing');
  }

  function pickVoice() {
    if (!('speechSynthesis' in window)) return null;
    if (state.cachedVoice) return state.cachedVoice;
    let voices = [];
    try {
      voices = window.speechSynthesis.getVoices() || [];
    } catch (err) {
      voices = [];
    }
    const en = voices.filter((v) => /^en/i.test(v.lang || ''));
    state.cachedVoice =
      en.find((v) => /Samantha|Ava|Allison|Karen|Google US English/i.test(v.name || '')) ||
      en.find((v) => v.lang === 'en-US') ||
      en[0] ||
      null;
    return state.cachedVoice;
  }

  function speak(text) {
    if (!('speechSynthesis' in window) || !text) return;
    const synth = window.speechSynthesis;
    const doSpeak = () => {
      try {
        synth.cancel();
        const utterance = new SpeechSynthesisUtterance(text);
        utterance.lang = 'en-US';
        utterance.rate = 0.85;
        utterance.pitch = 1;
        const voice = pickVoice();
        if (voice) utterance.voice = voice;
        synth.speak(utterance);
      } catch (err) {
        /* ignore */
      }
    };
    if (state.ttsTimer) {
      clearTimeout(state.ttsTimer);
      state.ttsTimer = null;
    }
    if (synth.speaking || synth.pending) {
      // 部分浏览器 cancel 后立刻 speak 会丢声音，稍等一拍
      state.ttsTimer = setTimeout(doSpeak, 60);
    } else {
      doSpeak();
    }
  }

  function stopSpeech() {
    if (state.ttsTimer) {
      clearTimeout(state.ttsTimer);
      state.ttsTimer = null;
    }
    if ('speechSynthesis' in window) {
      try {
        window.speechSynthesis.cancel();
      } catch (err) {
        /* ignore */
      }
    }
  }

  function playChime(kind) {
    const ctx = state.audioCtx;
    if (!ctx || ctx.state === 'closed') return;
    if (ctx.state === 'suspended') ctx.resume().catch(() => {});
    const now = ctx.currentTime + 0.02;
    const notes =
      kind === 'ok'
        ? [
            [783.99, 0, 0.16],
            [1046.5, 0.1, 0.2],
            [1318.51, 0.2, 0.28],
          ]
        : [
            [329.63, 0, 0.16],
            [261.63, 0.14, 0.24],
          ];
    notes.forEach(([freq, offset, dur]) => {
      try {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(freq, now + offset);
        gain.gain.setValueAtTime(0.0001, now + offset);
        gain.gain.exponentialRampToValueAtTime(0.07, now + offset + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + dur);
        osc.connect(gain).connect(ctx.destination);
        osc.start(now + offset);
        osc.stop(now + offset + dur + 0.05);
      } catch (err) {
        /* ignore */
      }
    });
  }

  function playTap() {
    const ctx = state.audioCtx;
    if (!ctx || ctx.state === 'closed') return;
    if (ctx.state === 'suspended') ctx.resume().catch(() => {});
    const now = ctx.currentTime + 0.01;
    try {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'triangle';
      osc.frequency.setValueAtTime(660, now);
      osc.frequency.exponentialRampToValueAtTime(990, now + 0.045);
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(0.12, now + 0.008);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.1);
      osc.connect(gain).connect(ctx.destination);
      osc.start(now);
      osc.stop(now + 0.12);
    } catch (err) {
      /* ignore */
    }
  }

  async function playWord(wordObj) {
    if (!wordObj) return;
    try {
      stopSpeech();
      const test = state.test;
      const guardIndex = test ? test.index : -1;

      // iOS / iPadOS 上 HTMLAudioElement 播放 AAC 最稳；先走这条路，避免 Web Audio 解码失败
      if (IS_IOS && playWithAudioElement(wordObj)) {
        flashSpeaker();
        return;
      }

      let ctx = state.audioCtx;
      if (!ctx) ctx = await ensureAudio();

      let buffer = null;
      if (ctx && wordObj.audio) {
        buffer = state.buffers.get(wordObj.id) || (await loadBuffer(wordObj));
      }

      // 加载期间已经换题了，别再播旧词
      if (test && state.test === test && test.index !== guardIndex) return;

      if (buffer && ctx && ctx.state !== 'closed') {
        if (ctx.state === 'suspended') await ctx.resume().catch(() => {});
        stopSource();
        const source = ctx.createBufferSource();
        source.buffer = buffer;
        source.connect(ctx.destination);
        source.onended = () => {
          if (state.currentSource === source) state.currentSource = null;
        };
        state.currentSource = source;
        source.start();
        flashSpeaker();
        return;
      }

      // 解码失败（例如 iOS 对 AAC 兼容差）时用原生 <audio> 播 m4a
      if (playWithAudioElement(wordObj)) {
        flashSpeaker();
        return;
      }

      speak(wordObj.tts || wordObj.word);
      flashSpeaker();
    } catch (err) {
      /* 任何音频异常都不影响答题 */
    }
  }

  async function requestWakeLock() {
    try {
      if (!('wakeLock' in navigator) || state.wakeLock) return;
      state.wakeLock = await navigator.wakeLock.request('screen');
      state.wakeLock.addEventListener('release', () => {
        state.wakeLock = null;
      });
    } catch (err) {
      state.wakeLock = null;
    }
  }

  function releaseWakeLock() {
    try {
      if (state.wakeLock) {
        const lock = state.wakeLock;
        state.wakeLock = null;
        lock.release();
      }
    } catch (err) {
      /* ignore */
    }
  }

  /* ---------------- 数据 ---------------- */

  async function loadData() {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const res = await fetch('words.json', { cache: 'no-store', signal: controller.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (!data || !Array.isArray(data.words) || data.words.length === 0) {
        throw new Error('words.json 里没有单词数据');
      }
      return data;
    } finally {
      clearTimeout(timeout);
    }
  }

  function setupData(data) {
    const rawWords = (Array.isArray(data.words) ? data.words : []).filter(
      (w) => w && typeof w.word === 'string' && w.word.trim() !== ''
    );
    const words = rawWords
      .map((w) => ({
        ...w,
        id: String(w.id || w.word),
        level: Number(w.level),
        image:
          w.photo !== undefined && w.photo !== ''
            ? w.photo
            : w.image !== undefined
            ? w.image
            : w.imageUrl !== undefined
            ? w.imageUrl
            : w.emoji,
      }))
      .filter((w) => Number.isInteger(w.level) && w.image !== undefined && w.image !== '');

    if (!words.length) throw new Error('词表为空：没有同时具备 level 和 image 的单词');

    let levels = Array.isArray(data.levels) ? data.levels : [];
    levels = levels
      .map((l) => ({
        id: Number(l.id),
        name: String(l.name || `等级 ${l.id}`),
        color: l.color || '#ff9f43',
        cumulativeWords: Number(l.cumulativeWords) || 0,
        newWords: Number(l.newWords) || 0,
        coverage: l.coverage && typeof l.coverage === 'object' ? l.coverage : null,
      }))
      .filter((l) => words.some((w) => w.level === l.id))
      .sort((a, b) => a.id - b.id);

    if (!levels.length) {
      const ids = [...new Set(words.map((w) => w.level))].sort((a, b) => a - b);
      levels = ids.map((id) => ({ id, name: `等级 ${id}`, color: '#ff9f43' }));
    }

    state.words = words;
    state.levels = levels;
    state.wordsByLevel = new Map();
    levels.forEach((level) => {
      state.wordsByLevel.set(
        level.id,
        words.filter((w) => w.level === level.id)
      );
    });

    if (data.meta && Number.isFinite(Number(data.meta.totalUniqueWords))) {
      state.totalUniqueWords = Number(data.meta.totalUniqueWords);
    }

    if (
      data.meta &&
      Number.isInteger(data.meta.defaultQuestions) &&
      data.meta.defaultQuestions >= 0 &&
      !state.hasStoredQuestions
    ) {
      state.settings.questions = data.meta.defaultQuestions;
    }
  }

  /* ---------------- 首页 ---------------- */

  function questionCountFor(available) {
    const want = state.settings.questions;
    if (!Number.isInteger(want) || want <= 0) return available;
    return Math.min(want, available);
  }

  function bestForLevel(levelId) {
    let best = null;
    state.history.forEach((row) => {
      if (row.levelId === levelId) best = best === null ? row.percent : Math.max(best, row.percent);
    });
    return best;
  }

  function renderLevels() {
    el.levelGrid.innerHTML = '';
    state.levels.forEach((level) => {
      const list = state.wordsByLevel.get(level.id) || [];
      const available = list.length;
      const canStart = available >= 3;
      const best = bestForLevel(level.id);
      const perRound = canStart ? questionCountFor(available) : 0;
      const roundText =
        state.settings.questions > 0 ? `每轮 ${perRound} 题` : `每轮全部 ${available} 题`;

      const coverage = level.coverage;
      const totalWords = coverage && coverage.total ? coverage.total : available;
      const coverText =
        coverage && coverage.percent != null
          ? `覆盖 ${coverage.percent}%`
          : '';

      const card = document.createElement('button');
      card.type = 'button';
      card.className = `level-card${state.levels.length > 8 ? ' level-card-compact' : ''}`;
      card.style.setProperty('--accent', level.color);
      card.disabled = !canStart;
      const title = /^P\d+$/i.test(level.name) ? `第 ${level.id} 级` : level.name;
      card.innerHTML = `
        <span class="level-badge">${escapeHtml(level.name)}</span>
        <span class="level-name">${escapeHtml(title)}</span>
        <span class="level-meta">${
          canStart
            ? `可测 ${available}/${totalWords} 词${coverText ? ` · ${coverText}` : ''} · ${roundText}`
            : `暂不可测 · 仅 ${available} 个带图词`
        }</span>
        <span class="level-best">${
          best === null ? (canStart ? '还没测过 · 来试试' : '') : `最好成绩 ${best}%`
        }</span>
      `;
      if (canStart) card.addEventListener('click', () => startTest(level.id));
      el.levelGrid.append(card);
    });
    if (el.levelsInfo && state.levels.length) {
      el.levelsInfo.textContent = `${state.levels[0].name}–${state.levels[state.levels.length - 1].name}`;
    }
  }

  function showLoadError(err) {
    console.error('[单词小侦探] 数据加载失败：', err);
    el.levelGrid.innerHTML = `
      <div class="load-error">
        <p class="load-error-title">🙈 没能读取 words.json</p>
        <p>这个页面需要通过本地服务器（或网站）打开，不能直接双击 index.html。</p>
        <p>在 <b>vocab-test</b> 文件夹里运行：</p>
        <pre>python3 -m http.server 8765</pre>
        <p>然后用浏览器打开 <b>http://localhost:8765</b></p>
        <p class="load-error-hint">macOS 也可以直接双击同目录的 start.command。</p>
      </div>
    `;
    el.btnParent.hidden = true;
    if (el.homeError) {
      el.homeError.hidden = false;
      el.homeError.innerHTML = el.levelGrid.innerHTML;
    }
    if (el.startCard) el.startCard.hidden = true;
  }

  /* ---------------- 测试流程 ---------------- */

  function startTest(levelId) {
    const level = state.levels.find((l) => l.id === levelId);
    const pool = state.wordsByLevel.get(levelId) || [];
    if (!level) return;
    if (pool.length < 3) {
      window.alert('这个等级的带图词不足 3 个，暂时没法开始测试。');
      return;
    }

    clearTimers();
    stopSpeech();
    stopSource();
    state.placement = null;

    const total = questionCountFor(pool.length);
    const queue = shuffle(pool).slice(0, total);
    state.test = {
      level,
      levelId,
      pool,
      queue,
      index: -1,
      current: null,
      cards: [],
      locked: false,
      correct: 0,
      wrong: [],
      percent: 0,
    };

    el.testTitle.textContent = /^P\d+$/i.test(level.name) ? `${level.name} · 第 ${level.id} 级` : `L${level.id} · ${level.name}`;
    el.progressText.textContent = `1 / ${queue.length}`;
    el.progressFill.style.width = '0%';
    setScreen('test');
    requestWakeLock();

    // 关键：在点击手势里创建/恢复音频上下文，iOS 才允许后面自动出声
    const audioPromise = ensureAudio();
    if (audioPromise) {
      audioPromise.then(() => preloadLevelAudio(queue.slice(0, 30))).catch(() => {});
    }

    advanceQuestion();
  }

  function pickDistractors(target, count) {
    const pool = state.wordsByLevel.get(target.level) || [];
    const used = new Set([target.image]);
    const others = pool.filter((w) => w.id !== target.id && w.image && !used.has(w.image));

    // 有 group 时优先避开同组词（避免 apple / banana 这类近义混淆项）
    const differentGroup = others.filter((w) => !target.group || w.group !== target.group);
    const picked = [];
    const take = (list) => {
      shuffle(list).forEach((w) => {
        if (picked.length < count && !used.has(w.image)) {
          used.add(w.image);
          picked.push(w);
        }
      });
    };
    take(differentGroup);
    if (picked.length < count) take(others);
    return picked;
  }

  function advanceQuestion() {
    const test = state.test;
    if (!test) return;

    clearTimers();
    stopSpeech();
    stopSource();

    test.index += 1;
    test.locked = false;
    el.cardGrid.classList.remove('locked');

    if (test.index >= test.queue.length) {
      if (test.mode === 'placement') {
        // 动态测级：记录这一题，决定下一题抽哪个等级（或结束）
        const extended = placementAfterQuestion();
        if (!extended || test.index >= test.queue.length) return;
      } else {
        finishTest();
        return;
      }
    }

    const target = test.queue[test.index];
    test.current = target;
    test.cards = shuffle([target, ...pickDistractors(target, 2)]);
    renderQuestion();
    void playWord(target);
  }

  function renderQuestion() {
    const test = state.test;
    if (test.mode === 'placement' && state.placement) {
      const p = state.placement;
      p.progress = (p.progress || 0) + 1;
      el.progressText.textContent = `第 ${p.progress} 题`;
      const overall = p.progress / PLACEMENT.maxQuestions;
      el.progressFill.style.width = `${Math.min(100, overall * 100)}%`;
    } else {
      el.progressText.textContent = `${test.index + 1} / ${test.queue.length}`;
      el.progressFill.style.width = `${((test.index + 1) / test.queue.length) * 100}%`;
    }
    el.feedback.className = 'feedback';
    el.feedback.textContent = '听一听，点一下图片吧';
    el.cardGrid.innerHTML = '';

    test.cards.forEach((wordObj) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'pic-card';
      btn.dataset.id = wordObj.id;
      btn.setAttribute('aria-label', `图片：${wordObj.word}`);
      const media = isImagePath(wordObj.image)
        ? `<img class="pic-img" src="${escapeHtml(wordObj.image)}" alt="" draggable="false" data-emoji="${escapeHtml(wordObj.emoji || '')}">`
        : `<span class="pic-emoji" aria-hidden="true">${escapeHtml(wordObj.image)}</span>`;
      btn.innerHTML = `${media}<span class="card-check" aria-hidden="true">✓</span>`;
      btn.addEventListener('click', () => pickCard(wordObj.id));
      el.cardGrid.append(btn);
    });
  }

  function praise() {
    const lines = ['太棒了！', '真厉害！', '答对啦！', '好耳朵！', '就是它！'];
    return lines[Math.floor(Math.random() * lines.length)];
  }

  function pickCard(id) {
    const test = state.test;
    if (!test || test.locked || !test.current) return;
    const target = test.current;
    const chosen = test.cards.find((w) => w.id === id);
    if (!chosen) return;

    playTap();

    const nodes = $$('.pic-card', el.cardGrid);
    const chosenNode = nodes.find((node) => node.dataset.id === id);
    const targetNode = nodes.find((node) => node.dataset.id === target.id);

    test.locked = true;
    el.cardGrid.classList.add('locked');

    if (test.mode === 'placement' && state.placement) {
      state.placement.lastAnswerCorrect = id === target.id;
    }

    if (id === target.id) {
      test.correct += 1;
      if (chosenNode) chosenNode.classList.add('correct');
      el.feedback.className = 'feedback ok';
      el.feedback.textContent = praise();
      playChime('ok');
      later(() => playWord(target), 320);
      later(() => advanceQuestion(), test.mode === 'placement' ? 950 : 1350);
    } else {
      test.wrong.push({ target, chosen });
      if (test.mode === 'placement' && state.placement) {
        state.placement.wrong.push({ target, chosen });
      }
      if (chosenNode) chosenNode.classList.add('wrong');
      if (targetNode) targetNode.classList.add('reveal');
      el.feedback.className = 'feedback bad';
      el.feedback.textContent = `这是 ${target.word} 哦～`;
      playChime('no');
      later(() => playWord(target), 420);
      later(() => advanceQuestion(), test.mode === 'placement' ? 1900 : 2250);
    }

    if (test.mode !== 'placement') {
      el.progressFill.style.width = `${((test.index + 1) / test.queue.length) * 100}%`;
    }
  }

  function finishTest() {
    const test = state.test;
    if (!test) return;
    clearTimers();
    stopSpeech();
    stopSource();
    releaseWakeLock();

    const total = test.queue.length;
    const percent = total ? Math.round((test.correct / total) * 100) : 0;
    test.percent = percent;

    state.history.push({
      ts: Date.now(),
      levelId: test.levelId,
      levelName: test.level.name,
      correct: test.correct,
      total,
      percent,
    });
    if (state.history.length > 500) state.history = state.history.slice(-500);
    saveStored();

    renderResult(test);
    setScreen('result');
  }

  /* ---------------- 自适应测级（动态穿插出题） ---------------- */

  const PLACEMENT = {
    maxQuestions: 12,   // 最多 12 题
    minQuestions: 8,    // 至少 8 题再考虑提前结束
    window: 2,          // 在当前水平前后 2 级范围内随机穿插
  };

  function placementLevelTitle(level) {
    return /^P\d+$/i.test(level.name)
      ? `${level.name} · 第 ${level.id} 级`
      : `L${level.id} · ${level.name}`;
  }

  function startPlacement() {
    clearTimers();
    stopSpeech();
    stopSource();
    state.test = null;
    state.placement = {
      cur: 0,                 // 当前估计的等级下标（从 P1=0 开始浮动）
      asked: 0,
      progress: 0,
      answers: [],            // { levelId, levelIndex, correct }
      wrong: [],
      askedIds: new Set(),
      lastAnswerCorrect: null,
      finished: false,
      estimate: 0,
      levelName: '',
      levelId: null,
      passedIndex: -1,
      boundary: null,
    };
    el.testTitle.textContent = '测一测水平';
    el.progressText.textContent = '准备中…';
    el.progressFill.style.width = '0%';
    el.feedback.className = 'feedback';
    el.feedback.textContent = '听一听，点一下图片吧';
    setScreen('test');
    requestWakeLock();

    const audioPromise = ensureAudio();
    if (audioPromise) {
      audioPromise.then(() => playTap()).catch(() => {});
    }
    if (placementAppendNext()) advanceQuestion();
    else finishPlacement();
  }

  function placementRandomLevelIndex() {
    const p = state.placement;
    const maxIdx = Math.max(0, state.levels.length - 1);
    const offsets = [-2, -1, 0, 1, 2];
    const weights = [0.07, 0.18, 0.5, 0.18, 0.07];
    const options = [];
    offsets.forEach((off, i) => {
      const idx = p.cur + off;
      if (idx < 0 || idx > maxIdx) return;
      const level = state.levels[idx];
      const words = state.wordsByLevel.get(level.id) || [];
      if (!words.some((w) => !p.askedIds.has(w.id) && w.image)) return;
      options.push({ idx, weight: weights[i] });
    });
    if (!options.length) return null;
    const total = options.reduce((sum, o) => sum + o.weight, 0);
    let r = Math.random() * total;
    for (const o of options) {
      r -= o.weight;
      if (r <= 0) return o.idx;
    }
    return options[options.length - 1].idx;
  }

  function placementPickWord(levelIndex) {
    const p = state.placement;
    const level = state.levels[levelIndex];
    if (!level) return null;
    const words = (state.wordsByLevel.get(level.id) || []).filter(
      (w) => !p.askedIds.has(w.id) && w.image
    );
    if (!words.length) return null;
    return words[Math.floor(Math.random() * words.length)];
  }

  function placementAppendNext() {
    const p = state.placement;
    if (!p || p.finished) return false;
    if (p.answers.length >= PLACEMENT.maxQuestions) return false;
    let levelIndex = placementRandomLevelIndex();
    if (levelIndex === null) return false;
    let level = state.levels[levelIndex];
    let word = placementPickWord(levelIndex);
    let guard = 0;
    while (!word && guard < 40) {
      levelIndex = placementRandomLevelIndex();
      if (levelIndex === null) return false;
      level = state.levels[levelIndex];
      word = placementPickWord(levelIndex);
      guard += 1;
    }
    if (!word) return false;
    p.askedIds.add(word.id);

    if (!state.test) {
      state.test = {
        mode: 'placement',
        level,
        levelId: level.id,
        pool: state.wordsByLevel.get(level.id) || [],
        queue: [],
        index: -1,
        current: null,
        cards: [],
        locked: false,
        correct: 0,
        wrong: [],
      };
    }
    state.test.level = level;
    state.test.levelId = level.id;
    state.test.pool = state.wordsByLevel.get(level.id) || [];
    state.test.queue.push(word);
    el.testTitle.textContent = placementLevelTitle(level);
    if (state.audioCtx) preloadLevelAudio([word]);
    return true;
  }

  function placementAfterQuestion() {
    const p = state.placement;
    const test = state.test;
    if (!p || !test || p.finished) return false;
    const answered = test.queue[test.index - 1];
    if (!answered) return false;

    const levelIndex = state.levels.findIndex((lv) => lv.id === answered.level);
    const correct = p.lastAnswerCorrect === true;
    p.answers.push({ levelId: answered.level, levelIndex, correct });
    p.asked = p.answers.length;

    if (correct) {
      p.cur = Math.min(state.levels.length - 1, Math.max(p.cur, levelIndex) + 1);
    } else {
      p.cur = Math.max(0, Math.min(p.cur, levelIndex) - 1);
    }

    const earlyStop =
      p.answers.length >= PLACEMENT.minQuestions &&
      p.cur <= 0 &&
      p.answers.slice(-3).every((a) => !a.correct);
    if (p.answers.length >= PLACEMENT.maxQuestions || earlyStop) {
      finishPlacement();
      return false;
    }
    if (!placementAppendNext()) {
      finishPlacement();
      return false;
    }
    return true;
  }

  function computePlacementEstimate() {
    const p = state.placement;
    const per = new Map();
    p.answers.forEach((a) => {
      const item = per.get(a.levelId) || { asked: 0, correct: 0 };
      item.asked += 1;
      if (a.correct) item.correct += 1;
      per.set(a.levelId, item);
    });
    const askedLevels = state.levels.filter((lv) => per.has(lv.id));
    let boundary = null;
    for (const lv of askedLevels) {
      const item = per.get(lv.id);
      if (item.correct / item.asked < 0.5) {
        boundary = { level: lv, asked: item.asked, correct: item.correct };
        break;
      }
    }
    let passedIndex = -1;
    if (boundary) {
      passedIndex = state.levels.findIndex((lv) => lv.id === boundary.level.id) - 1;
    } else if (askedLevels.length) {
      passedIndex = state.levels.findIndex(
        (lv) => lv.id === askedLevels[askedLevels.length - 1].id
      );
    }
    let estimate = 0;
    if (passedIndex >= 0) estimate = state.levels[passedIndex].cumulativeWords || 0;
    if (boundary) {
      const passedCum = estimate;
      const span = Math.max(0, (boundary.level.cumulativeWords || 0) - passedCum);
      const ratio = boundary.asked ? boundary.correct / boundary.asked : 0;
      estimate += Math.round((span * ratio) / 10) * 10;
    }
    return { estimate, passedIndex, boundary };
  }

  function finishPlacement() {
    const p = state.placement;
    if (!p || p.finished) return;
    p.finished = true;
    clearTimers();
    stopSpeech();
    stopSource();
    releaseWakeLock();

    const result = computePlacementEstimate();
    p.estimate = result.estimate;
    p.passedIndex = result.passedIndex;
    p.boundary = result.boundary;
    const passedLevel = p.passedIndex >= 0 ? state.levels[p.passedIndex] : null;
    p.levelName = passedLevel ? passedLevel.name : 'P1 以下';
    p.levelId = passedLevel
      ? passedLevel.id
      : p.boundary
        ? p.boundary.level.id
        : state.levels[0]
          ? state.levels[0].id
          : 1;

    state.history.push({
      ts: Date.now(),
      mode: 'placement',
      levelId: p.levelId,
      levelName: p.levelName,
      estimate: p.estimate,
      correct: p.answers.filter((a) => a.correct).length,
      total: p.answers.length,
      percent: p.answers.length
        ? Math.round((p.answers.filter((a) => a.correct).length / p.answers.length) * 100)
        : 0,
    });
    if (state.history.length > 500) state.history = state.history.slice(-500);
    saveStored();

    renderPlacementResult();
    setScreen('placement');
  }

  function renderPlacementResult() {
    const p = state.placement;
    if (!p) return;
    const estimate = p.estimate;
    const lo = Math.max(0, Math.round((estimate * 0.85) / 10) * 10);
    const hi = Math.round((estimate * 1.15) / 10) * 10;
    const passedLevel = p.passedIndex >= 0 ? state.levels[p.passedIndex] : null;

    if (passedLevel) {
      el.placementEmoji.textContent = '🎯';
      el.placementTitle.textContent = p.boundary ? '测出来啦！' : '答得真稳，测出来啦！';
      el.placementLevel.textContent = placementLevelTitle(passedLevel);
      el.placementVocab.textContent = `估计能听懂约 ${estimate} 个词（${lo}–${hi}）`;
      if (p.boundary) {
        const ratio = p.boundary.asked
          ? Math.round((p.boundary.correct / p.boundary.asked) * 100)
          : 0;
        el.placementNote.textContent = `这次是随机穿插出题：P1–${p.boundary.level.name} 之间来回抽题，答对升、答错退。${passedLevel.name} 答得稳，${p.boundary.level.name} 答对 ${p.boundary.correct}/${p.boundary.asked} 题（${ratio}%），所以按比例估算到这一级的一部分。`;
      } else {
        el.placementNote.textContent = `这次是随机穿插出题：在 P1–${passedLevel.name} 之间答得都很稳，没有遇到明显的“卡住”的等级；词汇量按词表累计词数估算。`;
      }
    } else {
      el.placementEmoji.textContent = '🌱';
      el.placementTitle.textContent = '先从最好玩的词开始吧';
      el.placementLevel.textContent = 'P1 以下';
      el.placementVocab.textContent =
        estimate > 0 ? `现在大约能听懂 ${estimate} 个词` : '刚开始积累，还测不出稳定的词汇量';
      el.placementNote.textContent =
        '这次遇到 P1 的词也还没答对。3 岁刚起步很正常，建议先用 P1 的词卡每天玩 5–10 分钟。';
    }

    const boundaryId = p.boundary ? p.boundary.level.id : null;
    el.ladder.innerHTML = state.levels
      .map((lv, idx) => {
        const isPassed = idx <= p.passedIndex;
        const isFailed = boundaryId !== null && lv.id === boundaryId;
        const cls = isPassed ? 'passed' : isFailed ? 'failed' : 'todo';
        return `<span class="ladder-dot ${cls}" title="${escapeHtml(lv.name)}">${
          isPassed ? '✓' : isFailed ? '✗' : ''
        }</span>`;
      })
      .join('');

    const seen = new Set();
    const missed = [];
    (p.wrong || []).forEach((item) => {
      const wordObj = item && item.target;
      if (wordObj && !seen.has(wordObj.id)) {
        seen.add(wordObj.id);
        missed.push(wordObj);
      }
    });
    el.placementMissedWrap.hidden = missed.length === 0;
    el.placementMissed.innerHTML = '';
    missed.slice(0, 12).forEach((wordObj) => el.placementMissed.append(missedChip(wordObj)));

    const practiceLevel = passedLevel || state.levels[0];
    el.btnPlacementLevel.dataset.levelId = practiceLevel ? String(practiceLevel.id) : '';
    el.btnPlacementLevel.textContent = practiceLevel ? `按 ${practiceLevel.name} 练习` : '按等级练习';
  }

  function renderPlacementBest() {
    if (!el.placementBest) return;
    const rows = state.history.filter(
      (r) => r && r.mode === 'placement' && Number.isFinite(r.estimate)
    );
    if (!rows.length) {
      el.placementBest.textContent = '第一次玩，直接点上面按钮开始';
      return;
    }
    const last = rows[rows.length - 1];
    el.placementBest.textContent = `上次测到 ${last.levelName || '—'} · 约 ${last.estimate} 个词`;
  }

  /* ---------------- 结果页 ---------------- */

  function renderResult(test) {
    const percent = test.percent;
    const starCount = percent >= 90 ? 3 : percent >= 70 ? 2 : percent >= 40 ? 1 : 0;
    const poolSize = test.pool.length;
    const level = state.levels.find((l) => l.id === test.levelId);
    const coverage = level && level.coverage;
    const totalWords = coverage && coverage.total ? coverage.total : poolSize;

    el.resultEmoji.textContent = percent >= 70 ? '🎉' : percent >= 40 ? '💪' : '🌱';
    el.resultTitle.textContent =
      percent >= 90
        ? '太厉害啦！'
        : percent >= 70
          ? '很不错哦！'
          : percent >= 40
            ? '继续加油～'
            : '慢慢来，会越来越多的';
    el.scoreNum.innerHTML = `${percent}<small>%</small>`;
    el.scoreRing.style.setProperty('--p', String(percent));
    el.scoreDetail.textContent =
      `这一轮 ${test.queue.length} 个词，认识 ${test.correct} 个。` +
      (totalWords > poolSize
        ? `该等级词表共 ${totalWords} 个词，其中 ${poolSize} 个有配图。`
        : `该等级共 ${poolSize} 个有配图的词。`) +
      (poolSize > test.queue.length ? '本次抽样测试。' : '');
    el.stars.innerHTML = [0, 1, 2]
      .map((i) => `<span class="${i < starCount ? 'on' : 'off'}">★</span>`)
      .join('');

    const seen = new Set();
    const missed = test.wrong
      .map((item) => item.target)
      .filter((wordObj) => {
        if (seen.has(wordObj.id)) return false;
        seen.add(wordObj.id);
        return true;
      });
    el.missedWrap.hidden = missed.length === 0;
    el.missedList.innerHTML = '';
    missed.forEach((wordObj) => el.missedList.append(missedChip(wordObj)));

    const idx = state.levels.findIndex((l) => l.id === test.levelId);
    const nextLevel = state.levels[idx + 1];
    const nextPool = nextLevel ? state.wordsByLevel.get(nextLevel.id) || [] : [];
    const canNext = Boolean(nextLevel && percent >= 80 && nextPool.length >= 3);
    el.btnNextLevel.hidden = !canNext;
    el.btnNextLevel.dataset.levelId = canNext ? String(nextLevel.id) : '';
    el.btnNextLevel.textContent = canNext ? `下一级 ${nextLevel.name} →` : '下一级 →';
  }

  function missedChip(wordObj) {
    const chip = document.createElement('span');
    chip.className = 'missed-chip';
    const media = isImagePath(wordObj.image)
      ? `<img class="chip-img" src="${escapeHtml(wordObj.image)}" alt="" data-emoji="${escapeHtml(wordObj.emoji || '')}">`
      : `<span class="chip-emoji" aria-hidden="true">${escapeHtml(wordObj.image)}</span>`;
    chip.innerHTML = `${media}<span>${escapeHtml(wordObj.word)}</span>`;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chip-audio';
    btn.textContent = '🔊';
    btn.setAttribute('aria-label', `再听一次 ${wordObj.word}`);
    btn.addEventListener('click', () => void playWord(wordObj));
    chip.append(btn);
    return chip;
  }

  /* ---------------- 家长设置 ---------------- */

  function renderHistory() {
    const rows = state.history.slice().reverse().slice(0, 30);
    el.historyCount.textContent = state.history.length ? `共 ${state.history.length} 次` : '';
    if (!rows.length) {
      el.historyList.innerHTML = '<div class="history-empty">还没有记录。测一轮就会出现在这里。</div>';
      return;
    }
    el.historyList.innerHTML = rows
      .map((row) => {
        const isPlacement = row.mode === 'placement';
        return `
        <div class="history-row">
          <div class="h-left">
            <span class="h-level">${escapeHtml(
              isPlacement ? `测级 · ${row.levelName || '—'}` : row.levelName || `L${row.levelId}`
            )}</span>
            <span class="h-date">${formatDate(row.ts)}</span>
          </div>
          <div class="h-right">
            <span class="h-percent">${
              isPlacement ? `约 ${row.estimate} 词` : `${row.percent}%`
            }</span>
            <span class="h-detail">${row.correct}/${row.total} ${isPlacement ? '题' : '词'}</span>
          </div>
        </div>`;
      })
      .join('');
  }

  function openParent() {
    el.selQuestions.value = String(state.settings.questions);
    renderHistory();
    el.parentModal.classList.remove('hidden');
  }

  function closeParent() {
    el.parentModal.classList.add('hidden');
  }

  /* ---------------- 导航 / 事件 ---------------- */

  function goHome() {
    clearTimers();
    stopSpeech();
    stopSource();
    releaseWakeLock();
    state.test = null;
    state.placement = null;
    setScreen('home');
    renderLevels();
    renderPlacementBest();
  }

  function onKeyDown(event) {
    if (!el.parentModal.classList.contains('hidden')) {
      if (event.key === 'Escape') closeParent();
      return;
    }
    if (screens.test.classList.contains('hidden')) return;
    const test = state.test;
    if (!test || test.locked || !test.cards.length) return;
    if (event.key === '1' || event.key === '2' || event.key === '3') {
      const wordObj = test.cards[Number(event.key) - 1];
      if (wordObj) pickCard(wordObj.id);
    } else if (event.code === 'Space') {
      event.preventDefault();
      if (test.current) void playWord(test.current);
    }
  }

  async function runSoundTest() {
    if (el.soundState) el.soundState.textContent = '正在试播…';
    unlockAudio();
    const ctx = await ensureAudio();
    playChime('ok');
    let decoded = false;
    const sample = state.words.find((w) => w.audio);

    // iOS / iPadOS：走已解锁的 HTMLAudioElement，最接近真机行为
    if (IS_IOS) {
      const ok = sample ? playWithAudioElement(sample) : false;
      await new Promise((resolve) => setTimeout(resolve, 900));
      const audioEl = getAudioEl();
      const playing = ok && (audioEl.currentTime > 0 || !audioEl.paused);
      if (el.soundState) {
        el.soundState.textContent = playing
          ? `已播放（手机音频元素 | AudioContext: ${ctx ? ctx.state : '-'}）。如没声音：检查 iPhone 侧边静音开关和音量。`
          : '播放指令被系统拦住了。请把 iPhone 侧边静音开关拨到响铃、调大音量，再点一次。';
      }
      return;
    }

    if (ctx && sample) {
      const buffer = await loadBuffer(sample);
      if (buffer && ctx.state !== 'closed') {
        try {
          stopSource();
          const source = ctx.createBufferSource();
          source.buffer = buffer;
          source.connect(ctx.destination);
          state.currentSource = source;
          source.start();
          decoded = true;
        } catch (err) {
          decoded = false;
        }
      }
    }
    if (el.soundState) {
      const stateText = ctx ? `音频状态：${ctx.state}` : '浏览器不支持 Web Audio';
      if (decoded) {
        el.soundState.textContent = `已播放（${stateText}）。听到「叮」和单词声就正常；如果手机静音请打开声音。`;
      } else if (ctx && sample) {
        el.soundState.textContent = `声音解码失败（${stateText}），已尝试原声兜底。请再点一次，或检查系统静音/音量。`;
      } else {
        el.soundState.textContent = `未能播放（${stateText}）。请再点一次，或换 Safari/Chrome 打开。`;
      }
    }
  }

  function bindEvents() {
    el.btnStartPlacement.addEventListener('click', () => startPlacement());
    el.btnLevelMode.addEventListener('click', () => {
      setScreen('levels');
      renderLevels();
    });
    el.btnLevelsBack.addEventListener('click', goHome);
    el.btnPlacementAgain.addEventListener('click', () => startPlacement());
    el.btnPlacementLevel.addEventListener('click', () => {
      const levelId = Number(el.btnPlacementLevel.dataset.levelId);
      if (levelId) startTest(levelId);
    });
    el.btnPlacementHome.addEventListener('click', goHome);

    el.btnParent.addEventListener('click', openParent);
    el.btnCloseParent.addEventListener('click', closeParent);
    el.parentModal.addEventListener('click', (event) => {
      if (event.target === el.parentModal) closeParent();
    });
    el.selQuestions.addEventListener('change', () => {
      if (el.selQuestions.value === '') return;
      state.settings.questions = Number(el.selQuestions.value) || 0;
      state.hasStoredQuestions = true;
      saveStored();
      renderLevels();
    });
    el.btnClearHistory.addEventListener('click', () => {
      if (!state.history.length) return;
      if (!window.confirm('确定清除所有测试记录吗？')) return;
      state.history = [];
      saveStored();
      renderHistory();
      renderLevels();
    });

    el.btnQuit.addEventListener('click', goHome);
    el.btnReplay.addEventListener('click', () => {
      if (state.test && state.test.current) void playWord(state.test.current);
    });
    el.btnAgain.addEventListener('click', () => {
      if (state.test) startTest(state.test.levelId);
    });
    el.btnNextLevel.addEventListener('click', () => {
      const levelId = Number(el.btnNextLevel.dataset.levelId);
      if (levelId) startTest(levelId);
    });
    el.btnHome.addEventListener('click', goHome);

    document.addEventListener('keydown', onKeyDown);

    // 任何一次触摸/点击都先解锁音频（iOS/Android 的自动播放限制）
    ['pointerdown', 'touchstart', 'touchend', 'click', 'keydown'].forEach((eventName) => {
      document.addEventListener(eventName, unlockAudio, { capture: true, passive: true });
    });

    if (el.btnSoundTest) {
      el.btnSoundTest.addEventListener('click', () => void runSoundTest());
    }

    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        stopSpeech();
        stopSource();
      } else if (state.audioCtx && state.audioCtx.state === 'suspended') {
        state.audioCtx.resume().catch(() => {});
      }
    });
    window.addEventListener('pagehide', () => {
      stopSpeech();
      stopSource();
    });

    if ('speechSynthesis' in window) {
      try {
        window.speechSynthesis.getVoices();
        window.speechSynthesis.onvoiceschanged = () => {
          state.cachedVoice = null;
          pickVoice();
        };
      } catch (err) {
        /* ignore */
      }
    }
  }

  async function boot() {
    loadStored();
    attachImageFallback(el.cardGrid);
    attachImageFallback(el.missedList);
    attachImageFallback(el.placementMissed);
    bindEvents();
    try {
      const data = await loadData();
      setupData(data);
      renderLevels();
      renderPlacementBest();
    } catch (err) {
      showLoadError(err);
    }
  }

  boot();
})();
