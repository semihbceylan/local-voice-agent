import { EMA } from './tts.js';

const BASE = 'https://huggingface.co/ozcancelik/ema-lightning-onnx/resolve/13c431db0356b2f7fafb1247cd823ec0d777c820/';
let model;
let cancelledThrough = 0;
let queue = Promise.resolve();

self.onmessage = ({ data }) => {
  if (data.type === 'cancel') {
    cancelledThrough = Math.max(cancelledThrough, data.id);
    return;
  }
  // Inference sessions must not overlap, even when an older answer is cancelled.
  queue = queue.then(async () => {
    const { id } = data;
    try {
      if (data.type === 'load') {
        let backend = data.preferCPU ? 'wasm' : 'webgpu';
        if (backend === 'webgpu' && !await navigator.gpu?.requestAdapter()) backend = 'wasm';
        const progress = (stage, fraction) => self.postMessage({ type: 'progress', id, stage, fraction });
        try {
          model = await EMA.load(backend, BASE, progress);
        } catch (error) {
          if (backend === 'wasm') throw error;
          backend = 'wasm';
          self.postMessage({ type: 'progress', id, stage: 'fallback', fraction: 0 });
          model = await EMA.load(backend, BASE, progress);
        }
        self.postMessage({ type: 'ready', id, backend });
      } else if (data.type === 'speak' && id > cancelledThrough) {
        if (!model) throw new Error('Model not loaded');
        let chunks = 0;
        for await (const samples of model.stream(data.text, { speed: 1, seed: 42 })) {
          if (id <= cancelledThrough) return;
          self.postMessage({ type: 'audio', id, samples }, [samples.buffer]);
          chunks++;
        }
        if (id > cancelledThrough) self.postMessage({ type: 'done', id, chunks });
      }
    } catch {
      self.postMessage({ type: 'error', id, message: data.type === 'load'
        ? 'EMA yüklenemedi. Bağlantınızı ve tarayıcı desteğini kontrol edip tekrar deneyin.'
        : 'Bu cihazda ses üretilemedi. Lütfen yeniden deneyin.' });
    }
  });
};
