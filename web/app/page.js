'use client';

import { useEffect, useRef, useState } from 'react';
import { readChatEvents } from '../lib/stream.mjs';
import { loadBrowserEMA } from '../lib/browser-ema.mjs';

const READY = 'Hazır olduğunda buradayım';

// What the person sees under the orb in each phase of the conversation.
const HINTS = {
  idle: 'Bir kez başlat. Sonra sadece konuş.',
  loading: 'Ses modeli cihazına indiriliyor. Sonraki ziyarette hazır olacak.',
  connecting: 'Oturum açılıyor…',
  listening: 'Doğal konuş. Sustuğunda sıra bende.',
  thinking: 'Yanıt hazırlanıyor. İstersen sözü alabilirsin.',
  preparing: 'Ses hazırlanıyor. İstersen sözü alabilirsin.',
  speaking: 'Yanıtı dinle ya da sözü al.',
};

const PROMPTS = [
  'Bana şaşırtıcı bir şey anlat.',
  'Hafta sonu için birlikte plan yapalım.',
  'Aklımda bir fikir var, konuşalım mı?',
];

function clock(seconds) {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

export default function Home() {
  const [supported, setSupported] = useState(null);
  const [active, setActive] = useState(false);
  // idle | loading | connecting | listening | thinking | preparing | speaking
  const [phase, setPhase] = useState('idle');
  const [status, setStatus] = useState(READY);
  const [progress, setProgress] = useState(0);
  const [messages, setMessages] = useState([]);
  const [interim, setInterim] = useState('');
  const [error, setError] = useState('');
  const [duration, setDuration] = useState(120);
  const [remaining, setRemaining] = useState(120);
  const [backend, setBackend] = useState('');
  const live = useRef({ active: false, history: [], generation: 0, speaking: false });
  const transcript = useRef(null);

  function go(nextPhase, label) {
    setPhase(nextPhase);
    setStatus(label);
  }

  useEffect(() => {
    setSupported(Boolean((window.SpeechRecognition || window.webkitSpeechRecognition) && window.AudioContext && window.Worker));
    // Only release resources on unmount; never touch the visible state here
    // (React runs this cleanup once in development, which used to show
    // "Sohbet sona erdi" before any conversation had started).
    return () => teardown();
  }, []);
  useEffect(() => { transcript.current?.scrollTo({ top: transcript.current.scrollHeight, behavior: 'smooth' }); }, [messages, interim]);

  function teardown() {
    const s = live.current;
    s.active = false;
    s.generation++;
    clearInterval(s.clock);
    clearTimeout(s.restart);
    clearTimeout(s.idle);
    s.controller?.abort();
    s.audioController?.abort();
    s.audio?.pause();
    s.ema?.cancel();
    s.recognition?.abort();
    window.speechSynthesis?.cancel();
    s.speaking = false;
    s.pending = 0;
  }

  function stop(label = 'Sohbet sona erdi') {
    teardown();
    setActive(false);
    setInterim('');
    go('idle', label);
  }

  function resetIdle() {
    const s = live.current;
    clearTimeout(s.idle);
    s.idle = setTimeout(() => stop('Sessizlik nedeniyle duraklatıldı'), 30_000);
  }

  function listen() {
    const s = live.current;
    if (!s.active || s.speaking) return;
    try { s.recognition.start(); } catch { /* Already running. */ }
  }

  function speak(text, generation) {
    const s = live.current;
    if (!text.trim() || !s.active || generation !== s.generation) return;
    if (!s.speaking) {
      s.speaking = true;
      s.recognition.abort(); // Avoid transcribing the browser's own voice.
    }
    s.pending = (s.pending || 0) + 1;
    if (s.ema) {
      s.audioQueue = (s.audioQueue || Promise.resolve()).then(async () => {
        if (!s.active || generation !== s.generation) return;
        try {
          go('preparing', 'Ses hazırlanıyor');
          await s.ema.speak(text, () => {
            if (s.active && generation === s.generation) go('speaking', 'Konuşuyor');
          });
        } catch (error) {
          if (error.name !== 'AbortError' && s.active && generation === s.generation) setError(error.message);
        } finally {
          if (generation === s.generation) {
            s.pending = Math.max(0, s.pending - 1);
            if (!s.pending && !s.fetching) resumeListening();
          }
        }
      });
      return;
    }
    go('speaking', 'Konuşuyor');
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'tr-TR';
    utterance.rate = 1.03;
    const voice = speechSynthesis.getVoices().find(v => v.lang.startsWith('tr'));
    if (voice) utterance.voice = voice;
    const finish = () => {
      if (generation !== s.generation) return;
      s.pending = Math.max(0, s.pending - 1);
      if (!s.pending && !s.fetching) resumeListening();
    };
    utterance.onend = finish;
    utterance.onerror = finish;
    speechSynthesis.speak(utterance);
  }

  function resumeListening() {
    const s = live.current;
    if (!s.active) return;
    s.speaking = false;
    go('listening', 'Dinliyor');
    resetIdle();
    clearTimeout(s.restart);
    s.restart = setTimeout(listen, 350);
  }

  async function reply(text) {
    const s = live.current;
    if (!s.active || s.fetching || s.speaking) return;
    clearTimeout(s.idle);
    s.recognition.abort();
    s.fetching = true;
    s.history = [...s.history, { role: 'user', content: text }].slice(-19);
    setMessages([...s.history]);
    setInterim('');
    go('thinking', 'Düşünüyor');
    const generation = ++s.generation;
    s.controller = new AbortController();
    let answer = '';
    let spoken = 0;
    try {
      const response = await fetch('/api/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${s.token}` },
        body: JSON.stringify({ messages: s.history }), signal: s.controller.signal,
      });
      if (!response.ok) throw new Error((await response.json()).error || 'Yanıt alınamadı.');
      let finishReason;
      for await (const event of readChatEvents(response.body)) {
          if (!s.active || generation !== s.generation) return;
          finishReason = event.choices?.[0]?.finish_reason || finishReason;
          if (event.error) throw new Error('Model yanıtını tamamlayamadı. Lütfen tekrar deneyin.');
          answer += event.choices?.[0]?.delta?.content || '';
          setMessages([...s.history, { role: 'assistant', content: answer }]);
          const unsaid = answer.slice(spoken);
          const boundary = /[.!?](?:\s|$)/.exec(unsaid);
          if (boundary) {
            const end = boundary.index + boundary[0].length;
            speak(unsaid.slice(0, end), generation);
            spoken += end;
          }
      }
      if (!answer.trim()) throw new Error(finishReason === 'length'
        ? 'Model yanıt sınırına ulaştı. Lütfen farklı bir ücretsiz model deneyin.'
        : 'Model boş yanıt döndürdü. Lütfen tekrar deneyin.');
      speak(answer.slice(spoken), generation);
      s.history = [...s.history, { role: 'assistant', content: answer }];
    } catch (e) {
      if (e.name !== 'AbortError' && s.active) setError(e.message);
    } finally {
      if (generation === s.generation) {
        s.fetching = false;
        if (!s.pending) resumeListening();
      }
    }
  }

  async function start() {
    setError('');
    setProgress(0);
    const s = live.current;
    try {
      go('loading', 'Ses motoru yükleniyor');
      s.ema = await loadBrowserEMA(({ stage, fraction }) => {
        const stages = { normalizer: 'Türkçe metin işleyici', text: 'Metin modeli', sound: 'Ses modeli', decoder: 'Ses çözücü', fallback: 'CPU moduna geçiliyor' };
        setProgress(fraction || 0);
        go('loading', stages[stage] || 'Ses motoru yükleniyor');
      });
      setBackend(s.ema?.backend === 'webgpu' ? 'WebGPU' : 'WASM / CPU');
      go('connecting', 'Bağlanıyor');
      const response = await fetch('/api/session', { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      const recognition = new Recognition();
      recognition.lang = 'tr-TR';
      recognition.continuous = true;
      recognition.interimResults = true;
      Object.assign(s, { active: true, token: data.token, speech: data.speech, audioQueue: Promise.resolve(), history: [], recognition, fetching: false, speaking: false, pending: 0 });
      recognition.onresult = event => {
        if (!s.active || s.fetching || s.speaking) return;
        let final = '';
        let partial = '';
        for (let i = event.resultIndex; i < event.results.length; i++) {
          if (event.results[i].isFinal) final += event.results[i][0].transcript;
          else partial += event.results[i][0].transcript;
        }
        setInterim(partial);
        resetIdle();
        if (final.trim()) void reply(final.trim());
      };
      recognition.onerror = event => {
        if (['not-allowed', 'service-not-allowed', 'audio-capture'].includes(event.error)) {
          setError('Mikrofona erişilemiyor. Tarayıcı ayarlarından mikrofon izni verip tekrar deneyin.');
          stop();
        } else if (event.error === 'network') {
          setError('Konuşma tanıma servisine bağlanılamadı. Bağlantınızı kontrol edin veya masaüstü Chrome kullanın.');
          stop();
        }
      };
      recognition.onend = () => {
        if (s.active && !s.speaking && !s.fetching) {
          clearTimeout(s.restart);
          s.restart = setTimeout(listen, 400);
        }
      };
      setMessages([]);
      setDuration(data.duration);
      setRemaining(data.duration);
      setActive(true);
      go('listening', 'Dinliyor');
      s.expires = Date.now() + data.duration * 1000;
      s.clock = setInterval(() => {
        const left = Math.max(0, Math.ceil((s.expires - Date.now()) / 1000));
        setRemaining(left);
        if (!left) stop('Deneme süresi sona erdi');
      }, 250);
      resetIdle();
      recognition.start();
    } catch (e) {
      stop(READY);
      setError(e.message || 'Sohbet başlatılamadı.');
    }
  }

  function interrupt() {
    const s = live.current;
    s.generation++;
    s.controller?.abort();
    s.audioController?.abort();
    s.audio?.pause();
    s.ema?.cancel();
    s.audioQueue = Promise.resolve();
    s.fetching = false;
    s.pending = 0;
    speechSynthesis.cancel();
    resumeListening();
  }

  const busy = phase === 'loading' || phase === 'connecting';
  const interruptible = ['thinking', 'preparing', 'speaking'].includes(phase);
  const last = messages[messages.length - 1];
  const streaming = phase === 'thinking' && last?.role === 'assistant';
  const timeShare = active ? remaining / Math.max(1, duration) : 1;

  return <main>
    <nav>
      <a className="brand" href="/">sadece<b>sohbet</b><span className="brand-dot" aria-hidden="true" /></a>
      <span className="badge"><i aria-hidden="true" /> Herkese açık demo</span>
    </nav>

    <section className="layout">
      <div className="intro">
        <p className="eyebrow">Yazma. İçinden geldiği gibi konuş.</p>
        <h1>Bir mesaj kutusu değil, <em>sohbet.</em></h1>
        <p className="description">Gününü anlat, bir fikri birlikte keşfedelim ya da aklına takılanı sor. Türkçe konuş, sohbet kendiliğinden aksın.</p>
        <ul className="details">
          <li>Hesap gerekmez</li>
          <li>{Math.round(duration / 60)} dakikalık deneyim</li>
          <li>Ses cihazında üretilir</li>
        </ul>
      </div>

      <div className={`call ${active ? 'is-active' : ''}`}>
        <div className="call-top">
          <span className="call-title">Yapay zekâ sohbet arkadaşın</span>
          <span className={`timer ${active && remaining <= 20 ? 'low' : ''}`} aria-label="Kalan süre">{clock(active ? remaining : duration)}</span>
          <span className="time-bar" aria-hidden="true"><span style={{ transform: `scaleX(${timeShare})` }} /></span>
        </div>

        <div className="stage-area">
          <div className={`stage phase-${phase}`} style={{ '--progress': progress }} aria-hidden="true">
            <div className="ripple" /><div className="ripple late" />
            <div className="ring" />
            <div className="orb">
              <div className="bars">{Array.from({ length: 7 }, (_, i) => <span key={i} style={{ '--i': i }} />)}</div>
              <div className="dots"><i /><i /><i /></div>
            </div>
          </div>
          <div className="status" role="status" aria-live="polite">
            <span className="status-dot" />
            {status}{phase === 'loading' && progress > 0 ? ` · %${Math.round(progress * 100)}` : ''}
          </div>
          <p className="hint">{HINTS[phase]}</p>

          <div className="controls">
            <button
              className={`primary ${active ? 'end' : ''}`}
              disabled={!supported || busy}
              onClick={() => active ? stop() : start()}
            >
              {active ? <>
                <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>
                Sohbeti bitir
              </> : busy ? <>
                <span className="spinner" aria-hidden="true" />
                Ses motoru hazırlanıyor…
              </> : <>
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.9V21h2v-2.1a7 7 0 0 0 6-6.9h-2Z" /></svg>
                Sohbeti başlat
              </>}
            </button>
            {active && <button className="secondary" onClick={interrupt} disabled={!interruptible}>
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.9V21h2v-2.1a7 7 0 0 0 6-6.9h-2Z" /></svg>
              Sözü al
            </button>}
          </div>

          {supported === false && <p className="notice">Bu tarayıcı konuşma tanımayı desteklemiyor. Masaüstü Chrome veya Edge deneyin.</p>}
          {error && <p className="notice" role="alert">{error}</p>}
        </div>

        <div className="transcript">
          <div className="transcript-head">
            <span>Sohbetimiz</span>
            {active && <span className="live"><i aria-hidden="true" /> Canlı</span>}
          </div>
          <div className="transcript-body" ref={transcript} role="log" aria-label="Sohbet metni">
            {messages.length === 0 && !interim ? <div className="empty">
              <p>Bir yerden başla. Örneğin:</p>
              <ul>{PROMPTS.map(p => <li key={p}>“{p}”</li>)}</ul>
            </div> : <>
              {messages.map((m, i) => <div className={`message ${m.role}`} key={i}>
                <span className="who">{m.role === 'user' ? 'Sen' : 'Yapay zekâ'}</span>
                <p className={streaming && i === messages.length - 1 ? 'streaming' : ''}>{m.content}</p>
              </div>)}
              {interim && <div className="message user partial">
                <span className="who">Sen</span>
                <p>{interim}…</p>
              </div>}
            </>}
          </div>
        </div>

        <div className="call-footer">
          <span><i aria-hidden="true" /> EMA · {backend || 'cihazında çalışır'}</span>
          <span>Türkçe · tr-TR</span>
        </div>
      </div>
    </section>

    {!active && <p className="download-note">İlk kullanımda yaklaşık 36 MB model ve çalışma dosyası indirilir; sonraki ziyaretler için önbelleğe alınır.</p>}

    <footer>
      <p>Daha doğal iletişim için küçük bir deney. <a href="/ema/NOTICE">EMA ve açık kaynak lisansları</a></p>
      <p>Konuşma tanıma tarayıcı sağlayıcısı tarafından işlenebilir. Metin OpenRouter’a gönderilir. EMA sesi tarayıcında üretir. Modeller Hugging Face’ten, çalışma motoru jsDelivr’dan indirilir. Kişisel bilgi paylaşma.</p>
      <p>Yapay zekâyla sohbet ediyorsun; hata yapabilir. Ücretsiz model ve tarayıcı desteği değişebilir.</p>
    </footer>
  </main>;
}
