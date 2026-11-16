'use client';

import { useEffect, useRef, useState } from 'react';

export default function Home() {
  const [supported, setSupported] = useState(null);
  const [active, setActive] = useState(false);
  const [status, setStatus] = useState('Ready when you are');
  const [messages, setMessages] = useState([]);
  const [interim, setInterim] = useState('');
  const [error, setError] = useState('');
  const [remaining, setRemaining] = useState(120);
  const live = useRef({ active: false, history: [], generation: 0, speaking: false });
  const transcript = useRef(null);

  useEffect(() => {
    setSupported(Boolean((window.SpeechRecognition || window.webkitSpeechRecognition) && window.speechSynthesis));
    return () => stop();
  }, []);
  useEffect(() => { transcript.current?.scrollTo({ top: transcript.current.scrollHeight, behavior: 'smooth' }); }, [messages, interim]);

  function stop(label = 'Conversation ended') {
    const s = live.current;
    s.active = false;
    s.generation++;
    clearInterval(s.clock);
    clearTimeout(s.restart);
    clearTimeout(s.idle);
    s.controller?.abort();
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
    s.idle = setTimeout(() => stop('Paused after inactivity'), 30_000);
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
    setStatus('Speaking');
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'en-US';
    utterance.rate = 1.03;
    const voice = speechSynthesis.getVoices().find(v => v.lang === 'en-US' && v.localService);
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
    setStatus('Listening');
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
    setStatus('Thinking');
    const generation = ++s.generation;
    s.controller = new AbortController();
    let answer = '';
    let spoken = 0;
    try {
      const response = await fetch('/api/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${s.token}` },
        body: JSON.stringify({ messages: s.history }), signal: s.controller.signal,
      });
      if (!response.ok) throw new Error((await response.json()).error || 'Unable to get a reply.');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        if (!s.active || generation !== s.generation) { await reader.cancel(); return; }
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data: ') || line.slice(6).trim() === '[DONE]') continue;
          let event;
          try { event = JSON.parse(line.slice(6)); } catch { continue; }
          if (event.error) throw new Error('The free model could not finish its response. Please try again.');
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
      }
      if (!answer.trim()) throw new Error('The free model returned an empty reply. Please try again.');
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
    setStatus('Connecting');
    const s = live.current;
    try {
      // Unlock synthesis during the initial user gesture for browsers that require it.
      speechSynthesis.cancel();
      const unlock = new SpeechSynthesisUtterance('');
      speechSynthesis.speak(unlock);
      const response = await fetch('/api/session', { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      const recognition = new Recognition();
      recognition.lang = 'en-US';
      recognition.continuous = true;
      recognition.interimResults = true;
      Object.assign(s, { active: true, token: data.token, history: [], recognition, fetching: false, speaking: false, pending: 0 });
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
          setError('Microphone access is unavailable. Allow microphone access in your browser settings and try again.');
          stop();
        } else if (event.error === 'network') {
          setError('Browser speech recognition could not connect. Check your connection or try desktop Chrome.');
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
      setStatus('Listening');
      s.expires = Date.now() + data.duration * 1000;
      s.clock = setInterval(() => {
        const left = Math.max(0, Math.ceil((s.expires - Date.now()) / 1000));
        setRemaining(left);
        if (!left) stop('Your demo is complete');
      }, 250);
      resetIdle();
      recognition.start();
    } catch (e) {
      stop('Ready when you are');
      setError(e.message || 'Unable to start the demo.');
    }
  }

  function interrupt() {
    const s = live.current;
    s.generation++;
    s.controller?.abort();
    s.fetching = false;
    s.pending = 0;
    speechSynthesis.cancel();
    resumeListening();
  }

  return <main>
    <nav><a className="brand" href="/">just<span>talk</span><span className="brand-dot">●</span></a><span className="nav-label">VOICE LAB / 001</span><span className="badge"><i /> PUBLIC DEMO</span></nav>
    <section className="hero">
      <div className="intro"><p className="eyebrow">LESS TYPING. MORE HUMAN.</p><h1>A conversation.<br />Not a <em>chatbox.</em></h1><p className="description">A little curiosity goes a long way. Talk about your day, explore an idea, or ask whatever’s on your mind.</p><div className="details"><span>No account needed</span><span>2-minute experience</span></div></div>
      <div className={`voice-card ${active ? 'is-active' : ''}`}>
        <div className="card-top"><span>YOUR AI CHAT COMPANION</span><span className="timer">{active ? `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, '0')}` : '02:00'}</span></div>
        <div className={`orb-wrap ${active ? 'moving' : ''}`} aria-hidden="true"><div className="orbit orbit-one" /><div className="orbit orbit-two" /><div className="orb"><div className="wave">{Array.from({ length: 9 }, (_, i) => <span key={i} style={{ '--i': i }} />)}</div></div></div>
        <div className="voice-status" role="status"><span className={active ? 'live-dot' : 'quiet-dot'} />{status}</div>
        <p className="card-hint">{active ? status === 'Speaking' ? 'Listen to the reply, or tap interrupt below.' : 'Speak naturally. We’ll take it from here.' : 'One click to connect. Then just talk.'}</p>
        <button className={`primary ${active ? 'end' : ''}`} disabled={supported === null || supported === false || status === 'Connecting'} onClick={() => active ? stop() : start()}>{active ? 'End conversation' : status === 'Connecting' ? 'Connecting…' : 'Start conversation'}<span aria-hidden="true">{active ? '↗' : '↗'}</span></button>
        {active && <button className="interrupt" onClick={interrupt} disabled={status !== 'Speaking' && status !== 'Thinking'}>Interrupt & listen</button>}
        {supported === false && <p className="notice">Speech recognition isn’t available in this browser. Try desktop Chrome or Edge.</p>}
        {error && <p className="notice" role="alert">{error}</p>}
        <div className="card-footer"><span className="small-dot" /> BROWSER-POWERED AUDIO <span>ENGLISH / EN-US</span></div>
      </div>
    </section>
    <section className="bottom-grid">
      <div className="prompts"><p className="eyebrow">A LITTLE INSPIRATION</p><h2>Start anywhere.</h2><p>“Tell me something surprising.”</p><p>“Let’s brainstorm a weekend adventure.”</p><p>“I have an idea I want to talk through.”</p></div>
      <div className="transcript"><div className="transcript-heading"><p className="eyebrow">THE CONVERSATION</p><span>LIVE TRANSCRIPT</span></div><div className="transcript-body" ref={transcript} role="log" aria-label="Conversation transcript">{messages.length === 0 && !interim ? <p className="empty">Your words, and ours, will appear here.<br /><span>Nothing to type. Nothing to sign up for.</span></p> : <>{messages.map((m, i) => <div className={`message ${m.role}`} key={i}><span>{m.role === 'user' ? 'YOU' : 'AI'}</span><p>{m.content}</p></div>)}{interim && <div className="message partial"><span>YOU</span><p>{interim}…</p></div>}</>}</div></div>
    </section>
    <footer><p>An experiment in more natural interfaces.</p><p>Speech may be processed by your browser’s provider. Text goes to OpenRouter.<br />No audio is sent to our server. Avoid sharing personal information.</p><p>You’re chatting with AI. It can make mistakes.<br />Free-model availability and browser support vary.</p></footer>
  </main>;
}
