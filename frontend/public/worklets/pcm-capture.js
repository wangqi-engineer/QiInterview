// AudioWorkletProcessor: 把 32-bit float PCM 重采样到 16000Hz 16-bit mono
// 浏览器本地 sampleRate 通常是 48000 或 44100，需要在主线程做线性重采样。

class PcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.targetRate = (options && options.processorOptions && options.processorOptions.targetRate) || 16000;
    this.inputRate = sampleRate; // 全局 sampleRate
    this._buffer = [];
    this._bufferLen = 0;
    this._frameSize = Math.floor(this.targetRate * 0.1); // 100ms
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channel = input[0];
    if (!channel) return true;
    // 单通道；若多通道下采样
    const ratio = this.inputRate / this.targetRate;
    const outLen = Math.floor(channel.length / ratio);
    const out = new Int16Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const idx = i * ratio;
      const i0 = Math.floor(idx);
      const i1 = Math.min(i0 + 1, channel.length - 1);
      const frac = idx - i0;
      const sample = channel[i0] * (1 - frac) + channel[i1] * frac;
      const s = Math.max(-1, Math.min(1, sample));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    this._buffer.push(out);
    this._bufferLen += out.length;
    while (this._bufferLen >= this._frameSize) {
      const merged = new Int16Array(this._frameSize);
      let pos = 0;
      while (pos < this._frameSize && this._buffer.length > 0) {
        const head = this._buffer[0];
        const need = this._frameSize - pos;
        if (head.length <= need) {
          merged.set(head, pos);
          pos += head.length;
          this._buffer.shift();
        } else {
          merged.set(head.subarray(0, need), pos);
          this._buffer[0] = head.subarray(need);
          pos += need;
        }
      }
      this._bufferLen -= this._frameSize;

      // 计算 RMS 用于本地 VAD
      let sumSq = 0;
      for (let i = 0; i < merged.length; i++) {
        const v = merged[i] / 32768;
        sumSq += v * v;
      }
      const rms = Math.sqrt(sumSq / merged.length);

      this.port.postMessage({
        type: "frame",
        pcm: merged.buffer,
        rms,
      }, [merged.buffer]);
    }
    return true;
  }
}

registerProcessor("pcm-capture", PcmCaptureProcessor);
