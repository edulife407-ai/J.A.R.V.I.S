# voice_sandbox.py
import os
import re
import sys
import time
import json
import queue
import random
import hashlib
import itertools
import urllib.request
import asyncio
import tempfile
import threading
from collections import deque
import requests
import numpy as np
import sounddevice as sd
from scipy.signal import get_window, butter, lfilter
from scipy.io import wavfile

os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
import pygame
import edge_tts
import onnxruntime as ort
from faster_whisper import WhisperModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VOICEPRINT_FILE = os.path.join(BASE_DIR, "master_voiceprint_v4_neural.npz")
ONNX_MODEL_PATH = os.path.join(BASE_DIR, "wespeaker_en_voxceleb_resnet34.onnx")
LOADING_CACHE_DIR = os.path.join(BASE_DIR, "loading_voice_cache")
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"

WESPEAKER_URLS = [
    "https://huggingface.co/csukuangfj/speaker-embedding-models/resolve/main/wespeaker_en_voxceleb_resnet34.onnx",
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34.onnx",
]

SAMPLE_RATE = 16000
BLOCK_DURATION = 0.05  # 50ms frames
BLOCK_SIZE = int(SAMPLE_RATE * BLOCK_DURATION)

INDIAN_ACCENT_PROMPT = (
    "Jarvis, Hey Jarvis. Clear Indian English accent. "
    "Commands: talk slower, talk faster, whisper, normal voice, I am feeling sad, "
    "status report, daily briefing, pause the video, stop, wait, hold on, listen, "
    "open Brave, Physics Wallah, PW, YouTube, Amazon, Flipkart, men's kurta, add to cart, "
    "close all other tabs, JEE Advanced, Rotational Mechanics, ESP32 MP3 recorder, INMP441."
)

WAKE_WORD_PATTERNS = [
    r"\bjarvis\b", r"\bjaarvis\b", r"\bjavis\b", r"\bjervis\b",
    r"\bjarviz\b", r"\bjarves\b", r"\bcharvis\b", r"\bharvis\b",
]

# Pre-cached loading phrases grouped by emotional context
LOADING_PHRASES = {
    "normal": [
        "Coming right up, sir.",
        "Connecting with the neural core, give me a moment.",
        "Accessing the archives now, sir.",
        "Let me pull that up for you, sir.",
        "On it, sir. Processing your request.",
    ],
    "empathetic": [
        "I'm right here with you, sir. Give me just a moment.",
        "I hear you, sir. Let me think this through carefully.",
    ],
    "whisper": [
        "Right away, sir. Keeping my voice down.",
        "Connecting quietly now, sir.",
    ],
}

# Emotional & Prosody Profiles
EMOTION_PROFILES = {
    "normal":     {"rate_delta": 0,   "pitch_hz": 0,  "volume": 0.92, "dsp": "none"},
    "empathetic": {"rate_delta": -15, "pitch_hz": -5, "volume": 0.72, "dsp": "warm_comfort"},
    "whisper":    {"rate_delta": -18, "pitch_hz": -6, "volume": 0.30, "dsp": "acoustic_whisper"},
    "excited":    {"rate_delta": 12,  "pitch_hz": 4,  "volume": 1.00, "dsp": "none"},
    "calm":       {"rate_delta": -10, "pitch_hz": -3, "volume": 0.80, "dsp": "warm_comfort"},
}


def humanize_text_for_speech(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"\[EMOTION:\s*\w+\]", "", t, flags=re.I)
    t = re.sub(r"[*_`#~>]+", "", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\bPWM\b", "P W M", t)
    t = re.sub(r"\bI2S\b", "I squared S", t, flags=re.I)
    t = re.sub(r"\bESP32\b", "E S P 32", t, flags=re.I)
    return t.strip()


def detect_user_emotion_and_style(user_text: str, current_style: str) -> str:
    """Detects if the user is sad/stressed, wants a whisper, or wants an energetic tone."""
    tl = user_text.lower()

    if any(w in tl for w in ["whisper", "quietly", "keep your voice down", "talk softly", "shh", "secret"]):
        return "whisper"
    if any(w in tl for w in ["normal voice", "speak normally", "stop whispering", "regular voice"]):
        return "normal"
    if any(w in tl for w in [
        "i am sad", "i'm sad", "feeling sad", "feeling low", "feeling down", "depressed",
        "cry", "crying", "stressed", "anxious", "overwhelmed", "failed", "bad day",
        "exhausted", "so tired", "lonely", "upset", "hurt", "disappointed", "lost"
    ]):
        return "empathetic"
    if any(w in tl for w in ["excited", "great news", "we did it", "awesome", "celebrate", "hype"]):
        return "excited"

    # If currently in whisper mode, stay in whisper mode until told otherwise
    if current_style == "whisper":
        return "whisper"
    return "normal"


# =====================================================================
# 1. ACOUSTIC DSP ENGINE (REAL WHISPER VOCODER & EMPATHETIC WARMTH EQ)
# =====================================================================
def apply_emotional_dsp_to_mp3(mp3_path: str, dsp_mode: str) -> str:
    """
    Uses pygame.sndarray + scipy filters to transform standard TTS audio into:
    - 'acoustic_whisper': Strips vocal-cord fundamental buzz (<380 Hz), isolates vocal
      formants (450-3800 Hz), and blends soft breathy aspiration airflow.
    - 'warm_comfort': Applies a gentle 3.2 kHz Butterworth low-pass warmth filter
      so the voice sounds soft, close, and soothing when the user is sad.
    """
    if dsp_mode not in ["acoustic_whisper", "warm_comfort"] or not os.path.exists(mp3_path):
        return mp3_path

    try:
        snd = pygame.mixer.Sound(mp3_path)
        raw_arr = pygame.sndarray.array(snd)  # shape: (N, 2) int16 at 24000 Hz
        sr = 24000
        x = raw_arr.astype(np.float32) / 32768.0

        if dsp_mode == "warm_comfort":
            # Soft Butterworth low-pass at 3200 Hz removes harsh metallic upper-mids
            b, a = butter(2, 3200.0 / (sr / 2.0), btype="low")
            y = lfilter(b, a, x, axis=0)
            y = y * 0.92

        elif dsp_mode == "acoustic_whisper":
            # 1. High-pass above 420 Hz to remove voiced glottal cord buzz
            b_hp, a_hp = butter(3, 420.0 / (sr / 2.0), btype="high")
            voiceless = lfilter(b_hp, a_hp, x, axis=0)

            # 2. Band-pass formants (500 Hz - 3600 Hz)
            b_bp, a_bp = butter(2, [500.0 / (sr / 2.0), 3600.0 / (sr / 2.0)], btype="band")
            formants = lfilter(b_bp, a_bp, voiceless, axis=0)

            # 3. Generate soft breathy aspiration modulated by the speech syllable envelope
            env = np.abs(formants)
            b_env, a_env = butter(1, 35.0 / (sr / 2.0), btype="low")
            smooth_env = lfilter(b_env, a_env, env, axis=0)

            white = np.random.normal(0.0, 1.0, size=x.shape).astype(np.float32)
            breath_noise = lfilter(b_bp, a_bp, white, axis=0) * smooth_env * 0.85

            # Blend 72% voiceless formants + 28% breathy aspiration
            y = (0.72 * formants) + (0.28 * breath_noise)
            peak = float(np.max(np.abs(y))) + 1e-6
            y = (y / peak) * 0.45

        out_int16 = np.clip(y * 32767.0, -32767, 32767).astype(np.int16)
        wav_path = mp3_path.replace(".mp3", f"_{dsp_mode}.wav")
        wavfile.write(wav_path, sr, out_int16)
        try:
            os.remove(mp3_path)
        except Exception:
            pass
        return wav_path
    except Exception:
        return mp3_path


# =====================================================================
# 2. CONTIGUOUS SILENCE TRIMMER & DIGITAL AGC
# =====================================================================
def trim_and_normalize_speech(audio: np.ndarray, noise_floor: float = 0.002) -> np.ndarray:
    if audio is None or len(audio) == 0:
        return audio

    centered = audio - np.mean(audio)
    win_size = 320
    if len(centered) >= win_size * 4:
        n_w = len(centered) // win_size
        wins = centered[: n_w * win_size].reshape(n_w, win_size)
        w_rms = np.sqrt(np.mean(wins ** 2, axis=1))
        thresh = max(noise_floor * 1.4, np.percentile(w_rms, 30))
        active_idx = np.where(w_rms > thresh)[0]
        if len(active_idx) >= 2:
            start_sample = max(0, (active_idx[0] - 1) * win_size)
            end_sample = min(len(centered), (active_idx[-1] + 2) * win_size)
            if (end_sample - start_sample) >= int(SAMPLE_RATE * 0.15):
                centered = centered[start_sample:end_sample]

    rms = float(np.sqrt(np.mean(centered ** 2)))
    if rms < 1e-6:
        return centered

    gain = min(35.0, max(1.0, 0.12 / rms))
    boosted = centered * gain
    peak = float(np.max(np.abs(boosted)))
    if peak > 0.90:
        boosted = 0.90 * np.tanh(boosted / 0.90)
    return boosted.astype(np.float32)


# =====================================================================
# 3. CALIBRATED WESPEAKER RESNET-34 ENGINE (AAM-SOFTMAX SCALE)
# =====================================================================
class IndustrialNeuralVoiceGuard:
    def __init__(self, wake_threshold: float = 0.28, live_threshold: float = 0.22):
        self.wake_threshold = wake_threshold
        self.live_threshold = live_threshold
        self.prototypes: np.ndarray | None = None
        self.tts_echo_embedding: np.ndarray | None = None
        self.noise_floor: float = 0.002
        self._mel_80_bank = self._build_kaldi_80_fbank(sr=SAMPLE_RATE, n_fft=512, n_mels=80)
        self.ort_session = self._init_onnx_model()

    def _init_onnx_model(self) -> ort.InferenceSession:
        if not os.path.exists(ONNX_MODEL_PATH) or os.path.getsize(ONNX_MODEL_PATH) < 10_000_000:
            print("\n🌐 [One-Time Setup] Downloading Pretrained WeSpeaker ResNet-34 Neural Model (26.5 MB)...")
            for url in WESPEAKER_URLS:
                try:
                    urllib.request.urlretrieve(url, ONNX_MODEL_PATH)
                    print("✅ [Neural Model Downloaded] Saved to local disk.")
                    break
                except Exception as e:
                    print(f"   [Mirror fallback] {e}")

        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = 2
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session = ort.InferenceSession(ONNX_MODEL_PATH, sess_options=sess_opts, providers=["CPUExecutionProvider"])
        self.input_name = session.get_inputs()[0].name
        self.output_name = session.get_outputs()[0].name
        print("🧠 [Biometric Neural Engine] WeSpeaker ResNet-34 (256-D VoxCeleb) ONLINE")
        return session

    def _build_kaldi_80_fbank(self, sr: int = 16000, n_fft: int = 512, n_mels: int = 80) -> np.ndarray:
        low_mel = 1127.0 * np.log(1.0 + 20.0 / 700.0)
        high_mel = 1127.0 * np.log(1.0 + 7600.0 / 700.0)
        mel_pts = np.linspace(low_mel, high_mel, n_mels + 2)
        hz_pts = 700.0 * (np.exp(mel_pts / 1127.0) - 1.0)
        bins = np.floor((n_fft + 1) * hz_pts / sr).astype(int)

        fbank = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
        for m in range(1, n_mels + 1):
            l_b, c_b, r_b = bins[m - 1], bins[m], bins[m + 1]
            for k in range(l_b, c_b):
                fbank[m - 1, k] = (k - l_b) / max(1, (c_b - l_b))
            for k in range(c_b, r_b):
                fbank[m - 1, k] = (r_b - k) / max(1, (r_b - c_b))
        return fbank

    def extract_embedding(self, raw_audio: np.ndarray) -> np.ndarray | None:
        if raw_audio is None or len(raw_audio) < int(SAMPLE_RATE * 0.14):
            return None

        audio = trim_and_normalize_speech(raw_audio, self.noise_floor)
        sig = audio * 32768.0
        sig = np.append(sig[0], sig[1:] - 0.97 * sig[:-1])

        frame_len = 400
        frame_step = 160
        num_frames = 1 + (len(sig) - frame_len) // frame_step
        if num_frames < 5:
            return None

        indices = (
            np.tile(np.arange(0, frame_len), (num_frames, 1))
            + np.tile(np.arange(0, num_frames * frame_step, frame_step), (frame_len, 1)).T
        )
        frames = sig[indices.astype(np.int32, copy=False)]

        win = get_window("hamming", frame_len).astype(np.float32)
        mag = np.absolute(np.fft.rfft(frames * win, 512))
        power = mag ** 2

        mel_energy = np.dot(power, self._mel_80_bank.T)
        mel_energy = np.maximum(mel_energy, 1e-10)
        feats = np.log(mel_energy).astype(np.float32)
        feats -= np.mean(feats, axis=0, keepdims=True)

        if feats.shape[0] < 64:
            reps = int(np.ceil(64.0 / feats.shape[0]))
            feats = np.tile(feats, (reps, 1))[:64, :]

        input_tensor = np.expand_dims(feats, axis=0).astype(np.float32)
        raw_emb = self.ort_session.run([self.output_name], {self.input_name: input_tensor})[0]
        emb = raw_emb.flatten().astype(np.float32)
        norm = float(np.linalg.norm(emb))
        if norm < 1e-8:
            return None
        return emb / norm

    def _extract_multiscale_clean_windows(self, clean_audio: np.ndarray) -> list[np.ndarray]:
        trimmed = trim_and_normalize_speech(clean_audio, self.noise_floor)
        clips = [trimmed]
        win_samples = int(SAMPLE_RATE * 1.2)
        step_samples = int(SAMPLE_RATE * 0.6)
        if len(trimmed) > win_samples:
            for start in range(0, len(trimmed) - win_samples + 1, step_samples):
                clips.append(trimmed[start : start + win_samples])
        return clips

    def _record_smart_sample(self, prompt_idx: int, style_hint: str, sentence: str) -> np.ndarray:
        while True:
            input(f"\n[Sample {prompt_idx}/3 — {style_hint}]\nPress ENTER, then read:\n  \"{sentence}\"")
            print("  🎙️ Speak whenever you are ready...")

            recorded_chunks = []
            voiced_chunks = 0
            started = False
            silence_after = 0
            q_rec = queue.Queue()

            def _cb(indata, frames, time_info, status):
                q_rec.put(indata[:, 0].copy())

            t_start = time.time()
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK_SIZE, callback=_cb):
                while (time.time() - t_start) < 8.0:
                    block = q_rec.get()
                    rms = float(np.sqrt(np.mean(block ** 2)))
                    trigger = max(0.0025, self.noise_floor * 1.8)

                    bars = min(25, int((rms / max(0.02, trigger * 4)) * 20))
                    sys.stdout.write(f"\r   Mic Level: [{'█' * bars}{'.' * (25 - bars)}] (RMS: {rms:.4f}) ")
                    sys.stdout.flush()

                    if rms > trigger:
                        started = True
                        voiced_chunks += 1
                        silence_after = 0
                        recorded_chunks.append(block)
                    elif started:
                        recorded_chunks.append(block)
                        silence_after += 1
                        if voiced_chunks >= 16 and silence_after >= 12:
                            break

            print()
            if voiced_chunks >= 10:
                full_clip = np.concatenate(recorded_chunks)
                print(f"  ✅ Captured {len(full_clip)/SAMPLE_RATE:.1f}s of clean speech.")
                return full_clip
            else:
                print("  ⚠️ Barely caught any voice! Let's try that sample once more.")

    def load_or_enroll(self, force_enroll: bool = False):
        if not force_enroll and os.path.exists(VOICEPRINT_FILE):
            try:
                data = np.load(VOICEPRINT_FILE)
                protos = data["prototypes"]
                self.noise_floor = float(data.get("noise_floor", 0.002))
                is_repaired = bool(data.get("v4_1_repaired", False))

                if protos.ndim == 2 and protos.shape[1] == 256:
                    if not is_repaired and protos.shape[0] == 16:
                        clean_indices = [1, 5, 6, 10, 11, 15]
                        clean_rows = [protos[i] for i in clean_indices]
                        centroid = np.mean(clean_rows, axis=0)
                        centroid = (centroid / np.linalg.norm(centroid)).astype(np.float32)
                        clean_rows.insert(0, centroid)
                        self.prototypes = np.vstack(clean_rows).astype(np.float32)
                        np.savez(
                            VOICEPRINT_FILE,
                            prototypes=self.prototypes,
                            noise_floor=self.noise_floor,
                            v4_1_repaired=True,
                        )
                        print(f"🔧 [Auto-Repaired Voiceprint] Kept {len(self.prototypes)} pure 256-D neural prototypes!")
                        return

                    self.prototypes = protos
                    print(
                        f"🔒 [Neural Voice Lock] Loaded {len(self.prototypes)} Pure Prototypes "
                        f"(Wake Threshold: {self.wake_threshold} | Live Threshold: {self.live_threshold})"
                    )
                    return
            except Exception as e:
                print(f"[Voiceprint Load Warning] {e}")

        print("\n" + "=" * 72)
        print("🔒 CLEAN MULTI-WINDOW NEURAL VOICE ENROLLMENT (WeSpeaker ResNet-34)")
        print("Step 1: Measuring your microphone's silent noise floor for 1.5s...")
        print("=" * 72)
        time.sleep(0.3)
        amb_rec = sd.rec(int(1.5 * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="float32")
        sd.wait()
        amb_frames = amb_rec.flatten().reshape(-1, BLOCK_SIZE)
        amb_rms = [float(np.sqrt(np.mean(f ** 2))) for f in amb_frames]
        self.noise_floor = max(0.0008, float(np.percentile(amb_rms, 75)))
        print(f"✅ [Mic Calibrated] Ambient Noise Floor: {self.noise_floor:.4f}")

        prompts = [
            ("NORMAL CONVERSATIONAL TONE", "Hey Jarvis, give me a full system status report and daily briefing."),
            ("LOW-ENERGY / QUIET / CASUAL MOOD", "Jarvis, search Amazon for men's kurtas and close all other tabs."),
            ("FAST / COMMANDING INTERRUPT TONE", "Stop! Wait a second Jarvis, pause the video right now."),
        ]

        all_prototypes = []
        for idx, (style_label, sentence) in enumerate(prompts, 1):
            raw_clip = self._record_smart_sample(idx, style_label, sentence)
            for win_clip in self._extract_multiscale_clean_windows(raw_clip):
                emb = self.extract_embedding(win_clip)
                if emb is not None:
                    all_prototypes.append(emb)

        centroid = np.mean(all_prototypes, axis=0)
        centroid = (centroid / np.linalg.norm(centroid)).astype(np.float32)
        all_prototypes.insert(0, centroid)

        self.prototypes = np.vstack(all_prototypes).astype(np.float32)
        np.savez(
            VOICEPRINT_FILE,
            prototypes=self.prototypes,
            noise_floor=self.noise_floor,
            v4_1_repaired=True,
        )
        print(f"\n🔒 [Enrollment Complete] Saved {len(self.prototypes)} pure 256-D prototypes!\n")

    def score_voice(self, audio: np.ndarray) -> tuple[float, float, np.ndarray | None]:
        emb = self.extract_embedding(audio)
        if emb is None or self.prototypes is None:
            return 0.0, 0.0, None

        sims = np.dot(self.prototypes, emb)
        user_sim = float(np.max(sims))
        echo_sim = float(np.dot(self.tts_echo_embedding, emb)) if self.tts_echo_embedding is not None else 0.0
        return user_sim, echo_sim, emb

    def adapt_online(self, verified_emb: np.ndarray | None, sim_score: float):
        if verified_emb is not None and self.prototypes is not None and sim_score >= self.wake_threshold:
            updated = 0.85 * self.prototypes[0] + 0.15 * verified_emb
            self.prototypes[0] = (updated / np.linalg.norm(updated)).astype(np.float32)

    def check_barge_in_fast(self, audio_clip: np.ndarray, echo_rms_baseline: float) -> tuple[bool, float, float]:
        user_sim, echo_sim, _ = self.score_voice(audio_clip)
        if user_sim == 0.0:
            return False, 0.0, 0.0

        if echo_rms_baseline < (self.noise_floor * 2.5):
            return (user_sim >= 0.22), user_sim, echo_sim

        if user_sim >= 0.24 and (user_sim >= echo_sim):
            return True, user_sim, echo_sim
        return False, user_sim, echo_sim

    def verify_speaker(self, audio: np.ndarray, has_wake_word: bool = False, in_live_session: bool = False) -> tuple[bool, float, str]:
        user_sim, echo_sim, emb = self.score_voice(audio)
        if emb is None:
            return False, 0.0, "Clip too short"

        if self.tts_echo_embedding is not None and echo_sim > 0.55 and echo_sim > (user_sim + 0.10):
            return False, user_sim, f"Speaker Echo Ignored (echo={echo_sim:.2f})"

        req_thresh = self.live_threshold if (in_live_session or has_wake_word) else self.wake_threshold

        if user_sim >= req_thresh:
            self.adapt_online(emb, user_sim)
            return True, user_sim, f"Verified (sim={user_sim:.2f} >= {req_thresh:.2f})"
        return False, user_sim, f"Unauthorized Voice Rejected (sim={user_sim:.2f} < {req_thresh:.2f})"


# =====================================================================
# 4. EMOTIONAL + LOADING-SCREEN PIPELINED VOICE ENGINE (V5)
# =====================================================================
class JarvisVoiceMouth:
    """
    V5 Enhancements:
    1) Pre-Caches "Thinking / Loading" voice clips so Jarvis responds in 0.01s while Qwen thinks.
    2) Enqueues the Loading Phrase at Position #0 of the SAME `generation_id` turn queue so Qwen's
       answer NEVER interrupts the loading sentence—it finishes the loading phrase first, then speaks the answer!
    3) Supports Dynamic Speed Control (`user_rate_offset`) + Emotional DSP (`empathetic`, `whisper`, `excited`).
    """
    def __init__(self, bio_guard: IndustrialNeuralVoiceGuard):
        pygame.mixer.quit()
        pygame.mixer.init(frequency=24000, size=-16, channels=2, buffer=4096)
        self.bio_guard = bio_guard
        self.voice_name = "en-GB-RyanNeural"
        self.is_speaking = False
        self.is_waiting_for_brain = False
        self.speech_start_time = 0.0
        self.last_speech_end_time = 0.0
        self.echo_rms_baseline = 0.010
        self.echo_rms_max = 0.018

        # Persistent speed & emotion controls
        self.user_rate_offset = 0      # e.g. -25 for slower, +25 for faster
        self.current_emotion = "normal"

        self._lock = threading.Lock()
        self.generation_id = 0
        self.text_queue: queue.Queue = queue.Queue()
        self.audio_file_queue: queue.Queue = queue.Queue()
        self.loading_cache: dict[str, list[tuple[str, str]]] = {"normal": [], "empathetic": [], "whisper": []}

        self._prepare_loading_voice_cache()
        threading.Thread(target=self._synthesis_worker_loop, daemon=True).start()
        threading.Thread(target=self._playback_worker_loop, daemon=True).start()

    def _format_rate_and_pitch(self, emotion: str) -> tuple[str, str, float, str]:
        prof = EMOTION_PROFILES.get(emotion, EMOTION_PROFILES["normal"])
        total_rate = max(-50, min(60, self.user_rate_offset + prof["rate_delta"]))
        rate_str = f"{total_rate:+d}%"
        pitch_str = f"{prof['pitch_hz']:+d}Hz"
        return rate_str, pitch_str, prof["volume"], prof["dsp"]

    def _prepare_loading_voice_cache(self):
        """Synthesizes the spoken loading screen phrases once into disk cache so they trigger in 0.01s."""
        os.makedirs(LOADING_CACHE_DIR, exist_ok=True)
        print("⚡ [Loading Screen Audio] Preparing instant 'Thinking Voice' cache...")
        loop = asyncio.new_event_loop()
        for mood, phrases in LOADING_PHRASES.items():
            rate_str, pitch_str, _, dsp_mode = self._format_rate_and_pitch(mood)
            for phrase in phrases:
                h = hashlib.md5(f"{mood}_{phrase}".encode()).hexdigest()[:10]
                ext = ".wav" if dsp_mode != "none" else ".mp3"
                cached_path = os.path.join(LOADING_CACHE_DIR, f"load_{mood}_{h}{ext}")
                if not os.path.exists(cached_path):
                    raw_mp3 = os.path.join(LOADING_CACHE_DIR, f"load_{mood}_{h}.mp3")
                    try:
                        comm = edge_tts.Communicate(phrase, self.voice_name, rate=rate_str, pitch=pitch_str)
                        loop.run_until_complete(comm.save(raw_mp3))
                        cached_path = apply_emotional_dsp_to_mp3(raw_mp3, dsp_mode)
                    except Exception:
                        continue
                if os.path.exists(cached_path):
                    self.loading_cache[mood].append((cached_path, phrase))
        loop.close()

    async def _synthesize_with_retry(self, text: str, out_path: str, rate_str: str, pitch_str: str) -> bool:
        for _ in range(2):
            try:
                communicate = edge_tts.Communicate(text, self.voice_name, rate=rate_str, pitch=pitch_str)
                await asyncio.wait_for(communicate.save(out_path), timeout=6.0)
                if os.path.exists(out_path) and os.path.getsize(out_path) > 256:
                    return True
            except Exception:
                await asyncio.sleep(0.1)
        return False

    def stop_immediately(self):
        """Called ONLY when the user barges in or starts a brand-new turn."""
        with self._lock:
            self.generation_id += 1
            self.is_speaking = False
            self.is_waiting_for_brain = False
            self.last_speech_end_time = time.time()

            while not self.text_queue.empty():
                try:
                    self.text_queue.get_nowait()
                except queue.Empty:
                    break

            while not self.audio_file_queue.empty():
                try:
                    item = self.audio_file_queue.get_nowait()
                    old_file, is_temp = item[1], item[4]
                    if is_temp and old_file and os.path.exists(old_file):
                        os.remove(old_file)
                except Exception:
                    break

            try:
                if pygame.mixer.music.get_busy():
                    pygame.mixer.music.stop()
                pygame.mixer.music.unload()
            except Exception:
                pass

    def start_new_turn(self, play_loading_phrase: bool = False, emotion: str = "normal") -> int:
        """
        Starts a new turn. If `play_loading_phrase=True`, immediately queues a pre-cached
        thinking sentence at Position #0 of THIS turn's queue so the answer waits for it to finish!
        """
        self.stop_immediately()
        with self._lock:
            gen_id = self.generation_id
            self.current_emotion = emotion

        if play_loading_phrase:
            mood_key = emotion if emotion in self.loading_cache and self.loading_cache[emotion] else "normal"
            candidates = self.loading_cache.get(mood_key, [])
            if candidates:
                cached_file, phrase_txt = random.choice(candidates)
                _, _, vol, _ = self._format_rate_and_pitch(emotion)
                self.is_speaking = True
                self.is_waiting_for_brain = True
                self.speech_start_time = time.time()
                # Enqueue with is_temp=False so the cached loading file is kept on disk
                self.audio_file_queue.put((gen_id, cached_file, f"[LOADING] {phrase_txt}", vol, False))
        return gen_id

    def enqueue_sentence(self, sentence: str, gen_id: int, emotion: str | None = None):
        clean = humanize_text_for_speech(sentence)
        if not clean or gen_id != self.generation_id:
            return
        emo = emotion or self.current_emotion
        if not self.is_speaking:
            self.is_speaking = True
            self.speech_start_time = time.time()
        self.text_queue.put((gen_id, clean, emo))

    def speak_non_blocking(self, full_text: str, emotion: str = "normal"):
        gen_id = self.start_new_turn(play_loading_phrase=False, emotion=emotion)
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", (full_text or "").strip()) if s.strip()]
        for s in sentences:
            self.enqueue_sentence(s, gen_id, emotion=emotion)

    def _synthesis_worker_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        seq = 0
        while True:
            try:
                gen_id, sentence, emo = self.text_queue.get()
                if gen_id != self.generation_id:
                    continue
                seq += 1
                rate_str, pitch_str, vol, dsp_mode = self._format_rate_and_pitch(emo)
                tmp_mp3 = os.path.join(tempfile.gettempdir(), f"jarvis_s_{gen_id}_{seq}_{int(time.time()*1000)}.mp3")
                ok = loop.run_until_complete(self._synthesize_with_retry(sentence, tmp_mp3, rate_str, pitch_str))
                if ok and gen_id == self.generation_id:
                    final_audio_path = apply_emotional_dsp_to_mp3(tmp_mp3, dsp_mode)
                    self.is_waiting_for_brain = False
                    self.audio_file_queue.put((gen_id, final_audio_path, sentence, vol, True))
                else:
                    if os.path.exists(tmp_mp3):
                        try:
                            os.remove(tmp_mp3)
                        except Exception:
                            pass
                    if self.text_queue.empty() and self.audio_file_queue.empty():
                        self.is_speaking = False
            except Exception as e:
                print(f"\n[TTS Synth Warning] {e}")

    def _playback_worker_loop(self):
        """
        Plays items in `audio_file_queue` strictly in FIFO order:
        1) Finishes the Loading Phrase 100% first.
        2) Seamlessly plays Qwen's answer sentences right after.
        """
        spinner = itertools.cycle(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"])
        while True:
            try:
                gen_id, audio_file, sentence, vol, is_temp = self.audio_file_queue.get()
                if gen_id != self.generation_id:
                    if is_temp and os.path.exists(audio_file):
                        try:
                            os.remove(audio_file)
                        except Exception:
                            pass
                    continue

                with self._lock:
                    if gen_id != self.generation_id:
                        continue
                    self.is_speaking = True
                    if (time.time() - self.speech_start_time) > 12.0:
                        self.speech_start_time = time.time()
                    pygame.mixer.music.load(audio_file)
                    pygame.mixer.music.set_volume(vol)
                    pygame.mixer.music.play()

                sys.stdout.write("\r" + " " * 78 + "\r")
                emo_badge = self.current_emotion.upper()
                rate_badge = f"{self.user_rate_offset:+d}%" if self.user_rate_offset != 0 else "0%"
                print(f"🔊 [JARVIS | Style={emo_badge} | Speed={rate_badge}]: \"{sentence}\"")

                t_play_start = time.time()
                max_play_sec = max(4.0, len(sentence) * 0.16)
                while pygame.mixer.music.get_busy():
                    if gen_id != self.generation_id or (time.time() - t_play_start) > max_play_sec:
                        break
                    time.sleep(0.02)

                with self._lock:
                    try:
                        pygame.mixer.music.unload()
                    except Exception:
                        pass

                if is_temp and os.path.exists(audio_file):
                    try:
                        os.remove(audio_file)
                    except Exception:
                        pass

                # If the loading phrase finished and Qwen is still generating the first sentence,
                # keep `is_speaking` guarded or show a smooth terminal spinner until Sentence 1 arrives
                wait_start = time.time()
                while (
                    gen_id == self.generation_id
                    and self.is_waiting_for_brain
                    and self.audio_file_queue.empty()
                    and (time.time() - wait_start) < 6.0
                ):
                    sys.stdout.write(f"\r   {next(spinner)} [Neural Core Synthesizing Response...] ")
                    sys.stdout.flush()
                    time.sleep(0.06)

                if gen_id == self.generation_id and not self.audio_file_queue.empty():
                    time.sleep(0.06)  # Natural breath between loading phrase and answer

                if (
                    gen_id == self.generation_id
                    and not self.is_waiting_for_brain
                    and self.text_queue.empty()
                    and self.audio_file_queue.empty()
                ):
                    self.is_speaking = False
                    self.last_speech_end_time = time.time()
                    sys.stdout.write("\r" + " " * 78 + "\r")
                    sys.stdout.flush()

            except Exception as e:
                print(f"\n[Playback Warning] {e}")
                self.is_speaking = False


# =====================================================================
# 5. FULL-DUPLEX LISTENER (SPEED/EMOTION CONTROLS + LOADING SCREEN)
# =====================================================================
class FullDuplexConversationEngine:
    def __init__(self, bio_guard: IndustrialNeuralVoiceGuard, mouth: JarvisVoiceMouth):
        self.bio_guard = bio_guard
        self.mouth = mouth
        self.audio_queue = queue.Queue()
        self.live_session_active = False
        self.last_interaction_time = 0.0
        self.session_timeout_sec = 60.0
        self.ambient_rms = bio_guard.noise_floor
        self.conversation_history: deque = deque(maxlen=6)

        print("[STT Init] Loading faster-whisper ('base.en' int8) with Indian Accent conditioning...")
        self.whisper = WhisperModel("base.en", device="cpu", compute_type="int8", cpu_threads=4)
        print("[STT Ready] faster-whisper loaded.")

    def _audio_callback(self, indata, frames, time_info, status):
        self.audio_queue.put(indata[:, 0].copy())

    def _transcribe_clip(self, raw_audio_np: np.ndarray) -> str:
        boosted_audio = trim_and_normalize_speech(raw_audio_np, self.ambient_rms)
        segments, _ = self.whisper.transcribe(
            boosted_audio,
            language="en",
            initial_prompt=INDIAN_ACCENT_PROMPT,
            beam_size=2,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=250, speech_pad_ms=150),
            condition_on_previous_text=False,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        for pat in WAKE_WORD_PATTERNS:
            text = re.sub(pat, "Jarvis", text, flags=re.I)
        return text

    def _stream_qwen_to_mouth(self, user_text: str, turn_emotion: str):
        """
        1) Starts turn with `play_loading_phrase=True` so Jarvis immediately speaks a loading
           phrase ("Coming right up, sir...") while Qwen generates tokens in parallel.
        2) Streams Qwen's sentences into the SAME `gen_id` queue so the answer waits for the
           loading phrase to finish completely before speaking!
        """
        gen_id = self.mouth.start_new_turn(play_loading_phrase=True, emotion=turn_emotion)
        hist_block = "\n".join(self.conversation_history) if self.conversation_history else "None"

        if turn_emotion == "empathetic":
            persona_instruction = (
                "The user is feeling sad, stressed, or exhausted. Drop all sarcasm completely. "
                "Speak like a deeply caring, warm, gentle confidant and mentor. Validate their feelings softly, "
                "reassure them that they don't have to carry everything alone right now, and keep it to 2 warm sentences."
            )
        elif turn_emotion == "whisper":
            persona_instruction = (
                "You are whispering quietly in a hushed, low-key tone. Keep your sentences short, calm, and intimate."
            )
        else:
            persona_instruction = (
                "You are JARVIS, Tony Stark's dryly witty, warm, and hyper-competent British AI partner. "
                "Speak naturally with contractions (I'm, you're, we'll), keep it to 1 to 3 crisp sentences, and address the user as 'sir'."
            )

        prompt = (
            f"{persona_instruction}\n"
            "Start your response with one emotion tag: [EMOTION: NORMAL], [EMOTION: EMPATHETIC], [EMOTION: WHISPER], or [EMOTION: EXCITED].\n\n"
            f"Recent Conversation:\n{hist_block}\n"
            f"User: \"{user_text}\"\nJarvis:"
        )

        def _stream_worker():
            buffer = ""
            full_reply = []
            detected_tag_emotion = turn_emotion
            try:
                with requests.post(
                    OLLAMA_URL,
                    json={
                        "model": "qwen2.5:7b",
                        "prompt": prompt,
                        "stream": True,
                        "keep_alive": -1,
                        "options": {"temperature": 0.45, "num_predict": 115, "num_ctx": 1536},
                    },
                    stream=True,
                    timeout=25,
                ) as res:
                    for line in res.iter_lines():
                        if gen_id != self.mouth.generation_id:
                            return  # User barged in — abort immediately
                        if not line:
                            continue
                        data = json.loads(line.decode("utf-8"))
                        token = data.get("response", "")
                        buffer += token

                        # Check if Qwen emitted an [EMOTION: ...] tag
                        tag_m = re.search(r"\[EMOTION:\s*(\w+)\]", buffer, flags=re.I)
                        if tag_m:
                            tag_val = tag_m.group(1).strip().lower()
                            if tag_val in EMOTION_PROFILES and turn_emotion == "normal":
                                detected_tag_emotion = tag_val
                                self.mouth.current_emotion = tag_val
                            buffer = re.sub(r"\[EMOTION:\s*\w+\]", "", buffer, flags=re.I)

                        parts = re.split(r"(?<=[.!?])\s+", buffer)
                        if len(parts) > 1:
                            for complete_sent in parts[:-1]:
                                clean_s = complete_sent.strip()
                                if len(clean_s) >= 4:
                                    self.mouth.enqueue_sentence(clean_s, gen_id, emotion=detected_tag_emotion)
                                    full_reply.append(clean_s)
                            buffer = parts[-1]

                if buffer.strip() and gen_id == self.mouth.generation_id:
                    self.mouth.enqueue_sentence(buffer.strip(), gen_id, emotion=detected_tag_emotion)
                    full_reply.append(buffer.strip())

                self.mouth.is_waiting_for_brain = False

                if full_reply:
                    self.conversation_history.append(f"User: {user_text}")
                    self.conversation_history.append(f"Jarvis: {' '.join(full_reply)}")
            except Exception:
                self.mouth.is_waiting_for_brain = False
                if gen_id == self.mouth.generation_id:
                    self.mouth.enqueue_sentence(
                        f"I'm right here with you, sir. I heard: {user_text}.",
                        gen_id,
                        emotion=turn_emotion,
                    )

        threading.Thread(target=_stream_worker, daemon=True).start()

    def calibrate_speaker_echo(self):
        print("\n🔊 [Acoustic Calibration] Measuring speaker-to-mic bleed for 2.5 seconds (please stay quiet)...")
        cal_frames = []

        def _cal_cb(indata, frames, time_info, status):
            cal_frames.append(indata[:, 0].copy())

        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK_SIZE, callback=_cal_cb):
            self.mouth.speak_non_blocking("Calibrating neural voice lock and emotional resonance, sir.")
            time.sleep(0.45)
            while self.mouth.is_speaking:
                time.sleep(0.04)

        if cal_frames:
            echo_audio = np.concatenate(cal_frames)
            rms_vals = [float(np.sqrt(np.mean(f ** 2))) for f in cal_frames if np.sqrt(np.mean(f ** 2)) > self.ambient_rms * 1.2]
            if rms_vals:
                self.mouth.echo_rms_baseline = float(np.percentile(rms_vals, 85))
                self.mouth.echo_rms_max = float(np.percentile(rms_vals, 96))
            else:
                self.mouth.echo_rms_baseline = self.ambient_rms * 1.3
                self.mouth.echo_rms_max = self.ambient_rms * 1.8

            echo_emb = self.bio_guard.extract_embedding(echo_audio)
            if echo_emb is not None:
                self.bio_guard.tts_echo_embedding = echo_emb

            mode_str = "HEADPHONES / LOW-ECHO" if self.mouth.echo_rms_baseline < (self.ambient_rms * 2.5) else "LAPTOP SPEAKERS"
            print(
                f"✅ [Calibration Complete] Mode: {mode_str} | "
                f"Mic Floor: {self.ambient_rms:.4f} | Echo Baseline: {self.mouth.echo_rms_baseline:.4f}"
            )

    def run_forever(self):
        self.calibrate_speaker_echo()

        print("\n" + "=" * 76)
        print("🎙️ JARVIS V5 EMOTIONAL INTELLIGENCE + LOADING VOICE ENGINE ONLINE")
        print("- Spoken Loading Screen: Plays 'Coming right up...' and finishes before answering.")
        print("- Speed Control: Say 'talk slower', 'talk faster', or 'normal speed'.")
        print("- Emotional Modes: Say 'whisper to me', or tell him when you feel sad/stressed.")
        print("=" * 76 + "\n")

        speech_buffer = []
        silence_blocks = 0
        speaking_blocks = 0

        pre_roll = deque(maxlen=10)
        barge_active_blocks = 0
        barge_hangover = 0
        was_jarvis_speaking = False

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=BLOCK_SIZE,
            callback=self._audio_callback,
        ):
            while True:
                try:
                    block = self.audio_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                rms = float(np.sqrt(np.mean(block ** 2)))
                pre_roll.append(block)

                # =========================================================
                # MODE A: JARVIS IS SPEAKING (USER BARGE-IN MONITOR)
                # =========================================================
                if self.mouth.is_speaking:
                    was_jarvis_speaking = True
                    if (time.time() - self.mouth.speech_start_time) < 0.25:
                        continue

                    if self.mouth.echo_rms_baseline < (self.ambient_rms * 2.5):
                        barge_trigger = max(0.0035, self.ambient_rms * 2.0)
                    else:
                        barge_trigger = max(0.008, self.mouth.echo_rms_baseline * 1.22)

                    if rms > barge_trigger:
                        barge_active_blocks += 1
                        barge_hangover = 5
                    else:
                        if barge_hangover > 0:
                            barge_hangover -= 1
                        else:
                            barge_active_blocks = 0

                    sys.stdout.write(
                        f"\r   [Barge-In Meter] Mic RMS: {rms:.4f} / Trigger: {barge_trigger:.4f} "
                        f"| Active Frames: {barge_active_blocks}/3   "
                    )
                    sys.stdout.flush()

                    if barge_active_blocks >= 3:
                        test_clip = np.concatenate(list(pre_roll))
                        should_stop, u_sim, e_sim = self.bio_guard.check_barge_in_fast(
                            test_clip, self.mouth.echo_rms_baseline
                        )
                        loud_override = rms > max(0.025, self.mouth.echo_rms_max * 2.0)

                        if should_stop or loud_override:
                            self.mouth.stop_immediately()
                            was_jarvis_speaking = False
                            sys.stdout.write("\r" + " " * 78 + "\r")
                            print(
                                f"⚡ [BARGE-IN INTERRUPT] Cut off Jarvis! "
                                f"(NeuralSim={u_sim:.2f} vs EchoSim={e_sim:.2f})"
                            )

                            speech_buffer = list(pre_roll)
                            speaking_blocks = len(speech_buffer)
                            silence_blocks = 0
                            barge_active_blocks = 0
                            barge_hangover = 0
                            self.live_session_active = True
                            self.last_interaction_time = time.time()
                            continue

                    continue

                # =========================================================
                # POST-SPEECH REVERB GUARD
                # =========================================================
                if was_jarvis_speaking:
                    was_jarvis_speaking = False
                    speech_buffer.clear()
                    speaking_blocks = 0
                    silence_blocks = 0
                    barge_active_blocks = 0
                    barge_hangover = 0
                    self.last_interaction_time = time.time()
                    continue

                if (time.time() - self.mouth.last_speech_end_time) < 0.12:
                    continue

                # =========================================================
                # MODE B: VAD-FIRST + NEURAL SPEAKER VERIFICATION
                # =========================================================
                if self.live_session_active and (time.time() - self.last_interaction_time) > self.session_timeout_sec:
                    self.live_session_active = False
                    print("\n💤 [Live Session Timeout] Returning to Wake-Word Standby (Say 'Jarvis' to wake).")

                voice_threshold = max(0.0032, self.ambient_rms * 1.95)

                if rms > voice_threshold:
                    if speaking_blocks == 0 and len(pre_roll) > 1:
                        speech_buffer.extend(list(pre_roll)[:-1])
                    speech_buffer.append(block)
                    speaking_blocks += 1
                    silence_blocks = 0
                else:
                    if speaking_blocks > 0:
                        speech_buffer.append(block)
                        silence_blocks += 1

                        if silence_blocks >= 9:
                            if speaking_blocks >= 4:
                                full_audio = np.concatenate(speech_buffer)
                                t0 = time.perf_counter()

                                transcript = self._transcribe_clip(full_audio)
                                stt_ms = (time.perf_counter() - t0) * 1000.0

                                clean_words = re.sub(r"[^\w\s]", "", (transcript or "").lower()).strip()
                                if clean_words and len(clean_words) >= 2:
                                    has_wake = "jarvis" in clean_words
                                    is_authorized, sim_score, bio_msg = self.bio_guard.verify_speaker(
                                        full_audio,
                                        has_wake_word=has_wake,
                                        in_live_session=self.live_session_active,
                                    )
                                    if not is_authorized:
                                        print(f"🛡️ [Security Gate] Ignored '{transcript}' — {bio_msg}")
                                    else:
                                        print(f"🎙️ [You ({stt_ms:.0f}ms | Neural-Match: {sim_score:.2f})]: \"{transcript}\"")
                                        self._handle_verified_speech(transcript)

                            speech_buffer.clear()
                            speaking_blocks = 0
                            silence_blocks = 0
                    else:
                        self.ambient_rms = 0.97 * self.ambient_rms + 0.03 * rms

    def _handle_verified_speech(self, text: str):
        tl = text.lower().strip()
        tl_clean = re.sub(r"[^\w\s]", "", tl).strip()

        if any(p in tl_clean for p in ["go to sleep", "standby", "pause session", "stop listening"]):
            self.live_session_active = False
            self.mouth.speak_non_blocking("Going on standby, sir. I'm right here whenever you need me.")
            return

        if "enroll voice" in tl_clean or "recalibrate voice" in tl_clean:
            self.mouth.stop_immediately()
            self.bio_guard.load_or_enroll(force_enroll=True)
            return

        has_wake_word = "jarvis" in tl_clean
        if not self.live_session_active and not has_wake_word:
            print("   (Standby mode: Heard your verified voice, waiting for wake word 'Jarvis'.)")
            return

        self.live_session_active = True
        self.last_interaction_time = time.time()

        cleaned_query = re.sub(r"^(?:hey\s+|okay\s+|ok\s+|yo\s+)?jarvis[\s,.:;!?]*", "", text, flags=re.I).strip()
        if not cleaned_query:
            self.mouth.speak_non_blocking("Online and listening, sir. What's on your mind?", emotion=self.mouth.current_emotion)
            return

        q_clean = re.sub(r"[^\w\s]", "", cleaned_query.lower()).strip()

        if q_clean in [
            "stop", "wait", "wait a second", "hold on", "quiet", "cancel", "pause", "enough", "shut up", "let it go"
        ]:
            self.mouth.stop_immediately()
            print("🛑 [Interrupted & Paused] Standing by for your next command, sir.")
            return

        # =========================================================
        # DYNAMIC VOICE SPEED CONTROLS ("talk slower" / "talk faster")
        # =========================================================
        if any(p in q_clean for p in ["talk slower", "speak slower", "slow down", "speak slowly", "talk slowly", "too fast"]):
            self.mouth.user_rate_offset = max(-40, self.mouth.user_rate_offset - 22)
            self.mouth.speak_non_blocking(
                "Of course, sir. I've slowed my speaking cadence down for you. Tell me if this pace feels comfortable.",
                emotion=self.mouth.current_emotion,
            )
            return

        if any(p in q_clean for p in ["talk faster", "speak faster", "speed up", "faster", "too slow"]):
            self.mouth.user_rate_offset = min(45, self.mouth.user_rate_offset + 22)
            self.mouth.speak_non_blocking(
                "Right away, sir. Increasing vocal cadence now.",
                emotion=self.mouth.current_emotion,
            )
            return

        if any(p in q_clean for p in ["normal speed", "regular speed", "default speed", "reset speed"]):
            self.mouth.user_rate_offset = 0
            self.mouth.speak_non_blocking("Vocal speed reset to standard operating cadence, sir.")
            return

        # =========================================================
        # DIRECT WHISPER / NORMAL MODE TOGGLES
        # =========================================================
        if q_clean in ["whisper", "whisper mode", "start whispering", "whisper to me", "keep your voice down"]:
            self.mouth.current_emotion = "whisper"
            self.mouth.speak_non_blocking(
                "Whisper mode active, sir. I'll keep my voice hushed until you tell me otherwise.",
                emotion="whisper",
            )
            return

        if q_clean in ["normal voice", "stop whispering", "speak normally", "regular voice"]:
            self.mouth.current_emotion = "normal"
            self.mouth.speak_non_blocking("Restoring standard vocal projection, sir.", emotion="normal")
            return

        # Detect emotional state (Empathetic / Whisper / Excited / Normal)
        turn_emotion = detect_user_emotion_and_style(cleaned_query, self.mouth.current_emotion)
        if turn_emotion == "whisper":
            self.mouth.current_emotion = "whisper"

        # Stream Loading Phrase -> Qwen Response seamlessly!
        self._stream_qwen_to_mouth(cleaned_query, turn_emotion=turn_emotion)
        self.last_interaction_time = time.time()


if __name__ == "__main__":
    guard = IndustrialNeuralVoiceGuard(wake_threshold=0.28, live_threshold=0.22)
    guard.load_or_enroll(force_enroll=False)
    mouth = JarvisVoiceMouth(bio_guard=guard)
    engine = FullDuplexConversationEngine(bio_guard=guard, mouth=mouth)
    try:
        engine.run_forever()
    except KeyboardInterrupt:
        mouth.stop_immediately()
        print("\n[Voice Sandbox Exited]")