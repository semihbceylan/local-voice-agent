let engine;

class BrowserEMA {
  constructor() {
    this.worker = new Worker('/ema/worker.js', { type: 'module' });
    this.sequence = 0;
    this.jobs = new Map();
    this.sources = new Set();
    this.context = new AudioContext({ sampleRate: 48000 });
    this.worker.onmessage = ({ data }) => {
      const job = this.jobs.get(data.id);
      if (!job) return;
      if (data.type === 'progress') job.onProgress?.(data);
      else if (data.type === 'ready') {
        this.backend = data.backend;
        job.resolve(this);
      } else if (data.type === 'audio') {
        const buffer = this.context.createBuffer(1, data.samples.length, 48000);
        buffer.copyToChannel(data.samples, 0);
        const source = this.context.createBufferSource();
        source.buffer = buffer;
        source.connect(this.context.destination);
        this.sources.add(source);
        job.playing++;
        source.onended = () => {
          this.sources.delete(source);
          source.disconnect();
          job.playing--;
          if (job.done && !job.playing) job.resolve();
        };
        const start = Math.max(this.context.currentTime + 0.03, this.nextStart || 0);
        this.nextStart = start + buffer.duration;
        source.start(start);
        job.onAudio?.();
      } else if (data.type === 'done') {
        job.done = true;
        if (!data.chunks) job.reject(new Error('Metin seslendirilemedi.'));
        else if (!job.playing) job.resolve();
      } else if (data.type === 'error') job.reject(new Error(data.message));
    };
    this.worker.onerror = () => {
      for (const job of this.jobs.values()) job.reject(new Error('EMA ses motoru başlatılamadı.'));
    };
  }

  request(type, data, handlers = {}) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.cancel();
        reject(new Error('EMA işlemi zaman aşımına uğradı. Bağlantınızı kontrol edin.'));
      }, 180_000);
      const finish = (fn) => (value) => {
        clearTimeout(timer);
        this.jobs.delete(id);
        fn(value);
      };
      this.jobs.set(id, { type, playing: 0, ...handlers, resolve: finish(resolve), reject: finish(reject) });
      this.worker.postMessage({ type, id, ...data });
    });
  }

  async speak(text, onAudio) {
    await this.context.resume();
    return this.request('speak', { text }, { onAudio });
  }

  cancel() {
    this.worker.postMessage({ type: 'cancel', id: this.sequence });
    for (const [id, job] of this.jobs) if (job.type === 'speak') {
      job.reject(new DOMException('Cancelled', 'AbortError'));
    }
    for (const source of this.sources) { try { source.stop(); } catch {} }
    this.sources.clear();
    this.nextStart = 0;
  }
}

export async function loadBrowserEMA(onProgress) {
  if (engine) {
    await engine.context.resume();
    return engine.ready;
  }
  engine = new BrowserEMA();
  // Resume synchronously from the user's start click before any network requests.
  const current = engine;
  const resumed = current.context.resume();
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  current.ready = resumed.then(() => current.request('load', { preferCPU: ios }, { onProgress })).catch(error => {
    current.worker.terminate();
    void current.context.close();
    engine = undefined;
    throw error;
  });
  return current.ready;
}
