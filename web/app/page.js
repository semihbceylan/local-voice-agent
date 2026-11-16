'use client';

import { useEffect, useRef, useState } from 'react';
import { readChatEvents } from '../lib/stream.mjs';
import { loadBrowserEMA } from '../lib/browser-ema.mjs';

export default function Home() {
  const [supported, setSupported] = useState(null);
  const [active, setActive] = useState(false);
  const [status, setStatus] = useState('Hazır olduğunda buradayım');
  const [messages, setMessages] = useState([]);
  const [interim, setInterim] = useState('');
  const [error, setError] = useState('');
  const [remaining, setRemaining] = useState(120);
  const live = useRef({ active: false, history: [], generation: 0, speaking: false });
  const transcript = useRef(null);

  useEffect(() => {
    setSupported(Boolean((window.SpeechRecognition || window.webkitSpeechRecognition) && window.AudioContext && window.Worker));
    return () => stop();
  }, []);
  useEffect(() => { transcript.current?.scrollTo({ top: transcript.current.scrollHeight, behavior: 'smooth' }); }, [messages, interim]);

  function stop(label = 'Sohbet sona erdi') {
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
    setActive(false);
    setInterim('');
    setStatus(label);
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
          setStatus('Ses hazırlanıyor');
          await s.ema.speak(text, () => {
            if (s.active && generation === s.generation) setStatus('Konuşuyor');
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
    setStatus('Konuşuyor');
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
    setStatus('Dinliyor');
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
    setStatus('Düşünüyor');
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
    setStatus('Bağlanıyor');
    const s = live.current;
    try {
      setStatus('EMA yükleniyor');
      s.ema = await loadBrowserEMA(({ stage, fraction }) => {
        const stages = { normalizer: 'Türkçe metin işleyici', text: 'Metin modeli', sound: 'Ses modeli', decoder: 'Ses çözücü', fallback: 'CPU moduna geçiliyor' };
        setStatus(`${stages[stage] || 'EMA yükleniyor'} · %${Math.round(fraction * 100)}`);
      });
      setStatus('Bağlanıyor');
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
      setRemaining(data.duration);
      setActive(true);
      setStatus('Dinliyor');
      s.expires = Date.now() + data.duration * 1000;
      s.clock = setInterval(() => {
        const left = Math.max(0, Math.ceil((s.expires - Date.now()) / 1000));
        setRemaining(left);
        if (!left) stop('Deneme süresi sona erdi');
      }, 250);
      resetIdle();
      recognition.start();
    } catch (e) {
      stop('Hazır olduğunda buradayım');
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

  return <main>
    <nav><a className="brand" href="/">sadece<span>sohbet</span><span className="brand-dot">●</span></a><span className="nav-label">SES LAB / 001</span><span className="badge"><i /> HERKESE AÇIK DEMO</span></nav>
    <section className="hero">
      <div className="intro"><p className="eyebrow">YAZMA. İÇİNDEN GELDİĞİ GİBİ KONUŞ.</p><h1>Bir mesaj kutusu<br />değil, <em>sohbet.</em></h1><p className="description">Gününü anlat, bir fikri birlikte keşfedelim ya da aklına takılanı sor. Türkçe konuş, sohbet kendiliğinden aksın.</p><div className="details"><span>Hesap gerekmez</span><span>2 dakikalık deneyim</span></div></div>
      <div className={`voice-card ${active ? 'is-active' : ''}`}>
        <div className="card-top"><span>YAPAY ZEKÂ SOHBET ARKADAŞIN</span><span className="timer">{active ? `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}` : '02:00'}</span></div>
        <div className={`orb-wrap ${active ? 'moving' : ''}`} aria-hidden="true"><div className="orbit orbit-one" /><div className="orbit orbit-two" /><div className="orb"><div className="wave">{Array.from({ length: 9 }, (_, i) => <span key={i} style={{ '--i': i }} />)}</div></div></div>
        <div className="voice-status" role="status"><span className={active ? 'live-dot' : 'quiet-dot'} />{status}</div>
        <p className="card-hint">{active ? status === 'Konuşuyor' ? 'Yanıtı dinle ya da aşağıdan sözü al.' : 'Doğal konuş. Gerisini bize bırak.' : 'Bir kez başlat. Sonra sadece konuş.'}</p>
        <button className={`primary ${active ? 'end' : ''}`} disabled={supported === null || supported === false || (!active && status !== 'Hazır olduğunda buradayım' && status !== 'Sohbet sona erdi' && status !== 'Deneme süresi sona erdi' && status !== 'Sessizlik nedeniyle duraklatıldı')} onClick={() => active ? stop() : start()}>{active ? 'Sohbeti bitir' : status === 'Bağlanıyor' || status.includes('yükleniyor') || status.includes(' · %') ? 'Ses motoru hazırlanıyor…' : 'Sohbeti başlat'}<span aria-hidden="true">↗</span></button>
        {active && <button className="interrupt" onClick={interrupt} disabled={!['Konuşuyor', 'Düşünüyor', 'Ses hazırlanıyor'].includes(status)}>Sözü al ve dinlemeye geç</button>}
        {supported === false && <p className="notice">Bu tarayıcı konuşma tanımayı desteklemiyor. Masaüstü Chrome veya Edge deneyin.</p>}
        {error && <p className="notice" role="alert">{error}</p>}
        <div className="card-footer"><span className="small-dot" /> EMA · {live.current.ema?.backend === 'webgpu' ? 'WEBGPU' : live.current.ema ? 'WASM / CPU' : 'CİHAZINDA ÇALIŞIR'} <span>TÜRKÇE / TR-TR</span></div>
        {!active && <p className="card-hint">İlk kullanımda yaklaşık 36 MB model ve ek çalışma dosyaları indirilir; sonraki ziyaretler için önbelleğe alınır.</p>}
      </div>
    </section>
    <section className="bottom-grid">
      <div className="prompts"><p className="eyebrow">BİRAZ İLHAM</p><h2>Bir yerden başla.</h2><p>“Bana şaşırtıcı bir şey anlat.”</p><p>“Hafta sonu için birlikte plan yapalım.”</p><p>“Aklımda bir fikir var, konuşalım mı?”</p></div>
      <div className="transcript"><div className="transcript-heading"><p className="eyebrow">SOHBETİMİZ</p><span>CANLI KONUŞMA METNİ</span></div><div className="transcript-body" ref={transcript} role="log" aria-label="Sohbet metni">{messages.length === 0 && !interim ? <p className="empty">Senin ve bizim sözlerimiz burada görünecek.<br /><span>Yazmak yok. Üye olmak yok.</span></p> : <>{messages.map((m, i) => <div className={`message ${m.role}`} key={i}><span>{m.role === 'user' ? 'SEN' : 'YAPAY ZEKÂ'}</span><p>{m.content}</p></div>)}{interim && <div className="message partial"><span>SEN</span><p>{interim}…</p></div>}</>}</div></div>
    </section>
    <footer><p>Daha doğal iletişim için küçük bir deney.<br /><a href="/ema/NOTICE">EMA ve açık kaynak lisansları</a></p><p>Konuşma tanıma tarayıcı sağlayıcısı tarafından işlenebilir. Metin OpenRouter’a gönderilir.<br />EMA sesi tarayıcında üretir. Modeller Hugging Face’ten, çalışma motoru jsDelivr’dan indirilir. Kişisel bilgi paylaşma.</p><p>Yapay zekâyla sohbet ediyorsun; hata yapabilir.<br />Ücretsiz model ve tarayıcı desteği değişebilir.</p></footer>
  </main>;
}
