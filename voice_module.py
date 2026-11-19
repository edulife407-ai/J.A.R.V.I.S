# voice_module.py
"""
PLUG-AND-PLAY DETACHABLE VOICE MODULE FOR JARVIS (V6.2 ZERO-LAG SYNC + INTENT ABORT)
- Instant SSD Phrase Cache: Common confirmations ('Opening Notepad, sir.') play in 0.005s!
- Smart Delayed Loading Voice: Instant 0.0s actions speak their result immediately without filler!
- Mid-Flight Abort Hook: Saying 'Wait!' or barging in cancels any in-flight action immediately.
- Queue Flush Guard: Drains stale mic frames after every turn so commands NEVER pile up in bursts.
"""
import os
import re
import sys
import time
import json
import queue
import random
import hashlib
import urllib.request
import asyncio
import tempfile
import threading
from collections import deque
from typing import Callable
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
    "Jarvis, Brave, Chrome, Edge, Notepad, Calculator, VS Code, YouTube, Amazon, Flipkart, Myntra, "
    "kurta, Physics Wallah, PW, JEE, ESP32, AQI, air quality index, Delhi, New Delhi, Kolkata, Mumbai, "
    "Bengaluru, Chennai, Hyderabad, weather, temperature, time, date, squared, plus, minus, integral."
)

WAKE_WORD_PATTERNS = [
    r"\bjarvis\b", r"\bjaarvis\b", r"\bjavis\b", r"\bjervis\b",
    r"\bjarviz\b", r"\bjarves\b", r"\bcharvis\b", r"\bharvis\b",
]

WHISPER_HALLUCINATION_BLACKLIST = {
    "thank you", "thank you very much", "thanks for watching", "thanks for watching this video",
    "thank you for watching", "see you in the next video", "bye", "bye bye", "bye-bye",
    "you", "uh", "um", "ah", "oh", "huh", "ugh", "boop", "way", "alright", "good day",
    "have a wonderful day", "subtitles by", "i love you", "thank you thank you",
}

LOADING_PHRASES = {
    "normal": [
        "One moment, sir.",
        "Checking the screen now, sir.",
        "On it, sir.",
    ],
    "empathetic": [
        "I'm right here with you, sir.",
        "I hear you, sir.",
    ],
    "whisper": [
        "Right away, sir.",
        "Working quietly, sir.",
    ],
}

# Pre-cached instant reflex replies so 0.0s commands speak in 0.005s with zero internet delay!
INSTANT_REFLEX_PHRASES = [
    "DONE,",
    "Opening brave, sir.",
    "Opening notepad, sir.",
    "Opening calculator, sir.",
    "Opening youtube, sir.",
    "Opening amazon, sir.",
    "Closing notepad, sir.",
    "Closing brave, sir.",
    "Closing tab, sir.",
    "Clearing other tabs, sir.",
    "Previous tab, sir.",
    "Next tab, sir.",
    "Scrolling down.",
    "Scrolling up.",
    "Toggling media, sir.",
    "Aborted, sir. Standing by.",
]

EMOTION_PROFILES = {
    "normal":     {"rate_delta": 0,   "pitch_hz": 0,  "volume": 0.92, "dsp": "none"},
    "empathetic": {"rate_delta": -15, "pitch_hz": -5, "volume": 0.72, "dsp": "warm_comfort"},
    "whisper":    {"rate_delta": -18, "pitch_hz": -6, "volume": 0.30, "dsp": "acoustic_whisper"},
    "excited":    {"rate_delta": 12,  "pitch_hz": 4,  "volume": 1.00, "dsp": "none"},
    "calm":       {"rate_delta": -10, "pitch_hz": -3, "volume": 0.80, "dsp": "warm_comfort"},
}


def is_zero_latency_reflex_query(q: str) -> bool:
    """Returns True for 0.0s OS/Browser reflexes that should NOT play a loading phrase first."""
    ql = (q or "").strip().lower()
    if ql.startswith(("open ", "launch ", "start ", "close ", "scroll", "scrool", "glide", "go to ", "switch to ")):
        return True
    if any(p in ql for p in [
        "previous tab", "next tab", "close tab", "close this", "delete this", "clear all",
        "pause", "play", "go back", "refresh", "status report", "undo", "confirm", "cancel",
        "add to cart", "buy now", "what time", "time is it", "what is the time", "today's date"
    ]):
        return True
    return False


def humanize_text_for_speech(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(
        r"(?:\[|\(|\*)?\bemotion\s*[:=\-]?\s*(?:normal|empathetic|sad|whisper|excited|calm|happy)\b(?:\]|\)|\*)*[\s,.:;\-]*",
        "",
        t,
        flags=re.I,
    )
    t = re.sub(r"^\[LOADING\]\s*", "", t, flags=re.I)
    t = re.sub(r"^Jarvis says:\s*", "", t, flags=re.I)
    t = re.sub(r"[*_`#~>]+", "", t)
    t = re.sub(r"\.\s*,", ",", t)
    t = re.sub(r"\.{2,}", ".", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\bPWM\b", "P W M", t)
    t = re.sub(r"\bI2S\b", "I squared S", t, flags=re.I)
    t = re.sub(r"\bESP32\b", "E S P 32", t, flags=re.I)
    return t.strip()


def detect_user_emotion_and_style(user_text: str, current_style: str) -> str:
    tl = (user_text or "").lower()

    if any(w in tl for w in ["whisper", "quietly", "keep your voice down", "talk softly", "shh", "secret"]):
        return "whisper"
    if any(w in tl for w in ["normal voice", "speak normally", "stop whispering", "regular voice"]):
        return "normal"
    if any(w in tl for w in [
        "i am sad", "i'm sad", "feeling sad", "feeling low", "feeling down", "depressed",
        "crying", "i am stressed", "i'm stressed", "anxious", "overwhelmed", "bad day",
        "exhausted", "so tired", "feeling lonely", "upset", "disappointed"
    ]):
        return "empathetic"
    if any(w in tl for w in ["excited", "great news", "we did it", "awesome", "celebrate"]):
        return "excited"

    if current_style == "whisper":
        return "whisper"
    return "normal"


def apply_emotional_dsp_to_mp3(mp3_path: str, dsp_mode: str) -> str:
    if dsp_mode not in ["acoustic_whisper", "warm_comfort"] or not os.path.exists(mp3_path):
        return mp3_path

    try:
        snd = pygame.mixer.Sound(mp3_path)
        raw_arr = pygame.sndarray.array(snd)
        sr = 24000
        x = raw_arr.astype(np.float32) / 32768.0

        if dsp_mode == "warm_comfort":
            b, a = butter(2, 3200.0 / (sr / 2.0), btype="low")
            y = lfilter(b, a, x, axis=0) * 0.92

        elif dsp_mode == "acoustic_whisper":
            b_hp, a_hp = butter(3, 420.0 / (sr / 2.0), btype="high")
            voiceless = lfilter(b_hp, a_hp, x, axis=0)

            b_bp, a_bp = butter(2, [500.0 / (sr / 2.0), 3600.0 / (sr / 2.0)], btype="band")
            formants = lfilter(b_bp, a_bp, voiceless, axis=0)

            env = np.abs(formants)
            b_env, a_env = butter(1, 35.0 / (sr / 2.0), btype="low")
            smooth_env = lfilter(b_env, a_env, env, axis=0)

            white = np.random.normal(0.0, 1.0, size=x.shape).astype(np.float32)
            breath_noise = lfilter(b_bp, a_bp, white, axis=0) * smooth_env * 0.85

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


def trim_and_normalize_speech(audio: np.ndarray, noise_floor: float = 0.002) -> np.ndarray:
    if audio is None or len(audio) == 0:
        return audio

    centered = audio - np.mean(audio)
    win_size = 320
    if len(centered) >= win_size * 4:
        n_w = len(centered) // win_size
        wins = centered[: n_w * win_size].reshape(n_w, win_size)
        w_rms = np.sqrt(np.mean(wins ** 2, axis=1))
        thresh = max(noise_floor * 1.6, np.percentile(w_rms, 30))
        active_idx = np.where(w_rms > thresh)[0]
        if len(active_idx) >= 2:
            start_sample = max(0, (active_idx[0] - 1) * win_size)
            end_sample = min(len(centered), (active_idx[-1] + 2) * win_size)
            if (end_sample - start_sample) >= int(SAMPLE_RATE * 0.14):
                centered = centered[start_sample:end_sample]

    rms = float(np.sqrt(np.mean(centered ** 2)))
    if rms < 1e-6:
        return centered

    gain = min(8.0, max(1.0, 0.10 / rms))
    boosted = centered * gain
    peak = float(np.max(np.abs(boosted)))
    if peak > 0.90:
        boosted = 0.90 * np.tanh(boosted / 0.90)
    return boosted.astype(np.float32)


class IndustrialNeuralVoiceGuard:
    def __init__(self, wake_threshold: float = 0.32, live_threshold: float = 0.26):
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
        if num_frames < 4:
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
                    trigger = max(0.0035, self.noise_floor * 2.2)

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
                self.noise_floor = max(0.0012, float(data.get("noise_floor", 0.002)))
                if protos.ndim == 2 and protos.shape[1] == 256:
                    self.prototypes = protos
                    print(
                        f"🔒 [Neural Voice Lock] Loaded {len(self.prototypes)} Immutable Prototypes "
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
        self.noise_floor = max(0.0012, float(np.percentile(amb_rms, 80)))
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

    def check_barge_in_fast(self, audio_clip: np.ndarray) -> tuple[bool, float, float]:
        user_sim, echo_sim, _ = self.score_voice(audio_clip)
        if user_sim < 0.27:
            return False, user_sim, echo_sim
        if user_sim >= (echo_sim + 0.03):
            return True, user_sim, echo_sim
        return False, user_sim, echo_sim

    def verify_speaker(
        self,
        audio: np.ndarray,
        has_wake_word: bool = False,
        in_live_session: bool = False,
        is_post_barge_in: bool = False,
    ) -> tuple[bool, float, str]:
        user_sim, echo_sim, emb = self.score_voice(audio)
        if emb is None:
            return False, 0.0, "Clip too short"

        if is_post_barge_in:
            if user_sim >= 0.20:
                return True, user_sim, f"Verified Post-Barge-In (sim={user_sim:.2f})"
            return False, user_sim, f"Post-Barge-In Too Weak (sim={user_sim:.2f})"

        if self.tts_echo_embedding is not None and echo_sim > 0.52 and echo_sim > (user_sim + 0.06):
            return False, user_sim, f"Speaker Echo Ignored (echo={echo_sim:.2f})"

        req_thresh = self.live_threshold if (in_live_session or has_wake_word) else self.wake_threshold

        if user_sim >= req_thresh:
            return True, user_sim, f"Verified (sim={user_sim:.2f} >= {req_thresh:.2f})"
        return False, user_sim, f"Unauthorized Voice Rejected (sim={user_sim:.2f} < {req_thresh:.2f})"


class JarvisVoiceMouth:
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

        self.user_rate_offset = 0
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

    def _get_cached_phrase_path(self, sentence: str, rate_str: str, pitch_str: str, dsp_mode: str) -> str | None:
        """Checks if a short spoken confirmation (<70 chars) is cached on SSD for 0.005s playback."""
        if len(sentence) > 70:
            return None
        key = f"{sentence.lower().strip()}_{rate_str}_{pitch_str}_{dsp_mode}"
        h = hashlib.md5(key.encode("utf-8")).hexdigest()[:12]
        ext = ".wav" if dsp_mode != "none" else ".mp3"
        return os.path.join(LOADING_CACHE_DIR, f"reflex_{h}{ext}")

    def _prepare_loading_voice_cache(self):
        os.makedirs(LOADING_CACHE_DIR, exist_ok=True)
        print("⚡ [Loading Screen Audio] Verifying instant 'Thinking & Reflex Voice' cache...")
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

        # Pre-cache common 0.0s reflex responses in background so 'Opening Brave, sir' has 0.0s latency!
        def _warm_reflex_cache():
            bg_loop = asyncio.new_event_loop()
            r_str, p_str, _, d_mode = self._format_rate_and_pitch("normal")
            for p in INSTANT_REFLEX_PHRASES:
                c_path = self._get_cached_phrase_path(p, r_str, p_str, d_mode)
                if c_path and not os.path.exists(c_path):
                    try:
                        comm = edge_tts.Communicate(p, self.voice_name, rate=r_str, pitch=p_str)
                        bg_loop.run_until_complete(comm.save(c_path))
                    except Exception:
                        pass
            bg_loop.close()

        threading.Thread(target=_warm_reflex_cache, daemon=True).start()
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
                self.audio_file_queue.put((gen_id, cached_file, phrase_txt, vol, False))
        return gen_id

    def enqueue_sentence(self, sentence: str, gen_id: int, emotion: str | None = None):
        clean = humanize_text_for_speech(sentence)
        if not clean or gen_id != self.generation_id:
            return
        emo = emotion or self.current_emotion
        rate_str, pitch_str, vol, dsp_mode = self._format_rate_and_pitch(emo)

        # FAST-PATH SSD CACHE: If this short confirmation is already cached on disk, play in 0.005s!
        cached_path = self._get_cached_phrase_path(clean, rate_str, pitch_str, dsp_mode)
        if cached_path and os.path.exists(cached_path) and os.path.getsize(cached_path) > 256:
            if not self.is_speaking:
                self.is_speaking = True
                self.speech_start_time = time.time()
            self.is_waiting_for_brain = False
            self.audio_file_queue.put((gen_id, cached_path, clean, vol, False))
            return

        if not self.is_speaking:
            self.is_speaking = True
            self.speech_start_time = time.time()
        self.text_queue.put((gen_id, clean, emo))

    def speak_turn_answer(self, full_text: str, gen_id: int, emotion: str | None = None):
        if gen_id != self.generation_id:
            return
        emo = emotion or self.current_emotion
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", (full_text or "").strip()) if s.strip()]
        if not sentences:
            self.is_waiting_for_brain = False
            return
        for s in sentences:
            self.enqueue_sentence(s, gen_id, emotion=emo)

    def speak_non_blocking(self, full_text: str, emotion: str = "normal"):
        gen_id = self.start_new_turn(play_loading_phrase=False, emotion=emotion)
        self.speak_turn_answer(full_text, gen_id, emotion=emotion)

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

                # Save short confirmations into persistent SSD cache so next time takes 0.005s!
                cached_target = self._get_cached_phrase_path(sentence, rate_str, pitch_str, dsp_mode)
                is_temp_file = cached_target is None
                tmp_mp3 = cached_target if cached_target else os.path.join(
                    tempfile.gettempdir(), f"jarvis_s_{gen_id}_{seq}_{int(time.time()*1000)}.mp3"
                )
                if tmp_mp3.endswith(".wav"):
                    tmp_mp3 = tmp_mp3.replace(".wav", ".mp3")

                ok = loop.run_until_complete(self._synthesize_with_retry(sentence, tmp_mp3, rate_str, pitch_str))
                if ok and gen_id == self.generation_id:
                    final_audio_path = apply_emotional_dsp_to_mp3(tmp_mp3, dsp_mode)
                    self.is_waiting_for_brain = False
                    self.audio_file_queue.put((gen_id, final_audio_path, sentence, vol, is_temp_file))
                else:
                    if is_temp_file and os.path.exists(tmp_mp3):
                        try:
                            os.remove(tmp_mp3)
                        except Exception:
                            pass
                    if self.text_queue.empty() and self.audio_file_queue.empty():
                        self.is_speaking = False
            except Exception as e:
                print(f"\n[TTS Synth Warning] {e}")

    def _playback_worker_loop(self):
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

                wait_start = time.time()
                while (
                    gen_id == self.generation_id
                    and self.is_waiting_for_brain
                    and self.audio_file_queue.empty()
                    and (time.time() - wait_start) < 12.0
                ):
                    time.sleep(0.03)

                if gen_id == self.generation_id and not self.audio_file_queue.empty():
                    time.sleep(0.03)

                if (
                    gen_id == self.generation_id
                    and not self.is_waiting_for_brain
                    and self.text_queue.empty()
                    and self.audio_file_queue.empty()
                ):
                    self.is_speaking = False
                    self.last_speech_end_time = time.time()

            except Exception as e:
                print(f"\n[Playback Warning] {e}")
                self.is_speaking = False


# =====================================================================
# 5. PLUG-AND-PLAY VOICE PLUGIN CONTROLLER
# =====================================================================
class JarvisVoicePlugin:
    def __init__(
        self,
        external_command_handler: Callable[[str, int, str], None] | None = None,
        cancel_turn_callback: Callable[[], None] | None = None,
    ):
        self.bio_guard = IndustrialNeuralVoiceGuard(wake_threshold=0.32, live_threshold=0.26)
        self.bio_guard.load_or_enroll(force_enroll=False)
        self.mouth = JarvisVoiceMouth(bio_guard=self.bio_guard)
        self.external_command_handler = external_command_handler
        self.cancel_turn_callback = cancel_turn_callback

        self.audio_queue = queue.Queue()
        self.live_session_active = False
        self.paused_by_hotkey = False
        self.detached = False
        self.is_executing_command = False
        self.last_interaction_time = 0.0
        self.session_timeout_sec = 45.0
        self.ambient_rms = max(0.0020, self.bio_guard.noise_floor)
        self.conversation_history: deque = deque(maxlen=6)

        print("[STT Init] Loading faster-whisper ('base.en' int8) with Zero-Lag Sync...")
        self.whisper = WhisperModel("base.en", device="cpu", compute_type="int8", cpu_threads=4)
        print("[STT Ready] faster-whisper loaded.")

    def _flush_audio_queue(self):
        """Drains any stale queued audio frames so 3 repeated commands NEVER pile up and fire in a burst!"""
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break

    def _audio_callback(self, indata, frames, time_info, status):
        if not self.paused_by_hotkey and not self.detached:
            self.audio_queue.put(indata[:, 0].copy())

    def pause_for_hotkey(self):
        self.paused_by_hotkey = True
        if self.cancel_turn_callback:
            self.cancel_turn_callback()
        self.mouth.stop_immediately()
        self._flush_audio_queue()

    def resume_after_hotkey(self):
        self._flush_audio_queue()
        self.paused_by_hotkey = False
        self.last_interaction_time = time.time()

    def toggle_detach(self) -> bool:
        self.detached = not self.detached
        if self.detached:
            if self.cancel_turn_callback:
                self.cancel_turn_callback()
            self.mouth.stop_immediately()
            self._flush_audio_queue()
            print("\n🔌 [Voice Module DETACHED] Running in Hotkey-Only Mode (Press Ctrl+Alt+V to re-attach).")
        else:
            self._flush_audio_queue()
            print("\n🔌 [Voice Module RE-ATTACHED] Hands-Free Biometric Voice Active!")
        return not self.detached

    def _transcribe_clip(self, raw_audio_np: np.ndarray, is_barge_in: bool = False) -> str:
        boosted_audio = trim_and_normalize_speech(raw_audio_np, self.ambient_rms)
        use_vad = False if is_barge_in else True
        segments, _ = self.whisper.transcribe(
            boosted_audio,
            language="en",
            initial_prompt=INDIAN_ACCENT_PROMPT,
            beam_size=1,  # Greedy beam=1 cuts CPU transcription latency by 40%!
            vad_filter=use_vad,
            vad_parameters=dict(min_silence_duration_ms=220, speech_pad_ms=120),
            condition_on_previous_text=False,
            temperature=0.0,
        )
        valid_parts = []
        for seg in segments:
            if seg.no_speech_prob > 0.55:
                continue
            if seg.avg_logprob < -0.95:
                continue
            if seg.compression_ratio > 2.2:
                continue
            valid_parts.append(seg.text.strip())

        text = " ".join(valid_parts).strip()
        if not text:
            return ""

        for pat in WAKE_WORD_PATTERNS:
            text = re.sub(pat, "Jarvis", text, flags=re.I)

        clean_check = re.sub(r"[^\w\s]", "", text.lower()).strip()
        if clean_check in WHISPER_HALLUCINATION_BLACKLIST:
            return ""

        words = clean_check.split()
        if len(words) >= 6 and len(set(words)) <= 3:
            return ""

        return text

    def _stream_standalone_qwen(self, user_text: str, turn_emotion: str):
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
            "Do NOT include any bracketed tags, stage directions, or emotion labels—output ONLY the spoken words.\n\n"
            f"Recent Conversation:\n{hist_block}\n"
            f"User: \"{user_text}\"\nJarvis:"
        )

        def _stream_worker():
            buffer = ""
            full_reply = []
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
                            return
                        if not line:
                            continue
                        data = json.loads(line.decode("utf-8"))
                        token = data.get("response", "")
                        buffer += token

                        parts = re.split(r"(?<=[.!?])\s+", buffer)
                        if len(parts) > 1:
                            for complete_sent in parts[:-1]:
                                clean_s = humanize_text_for_speech(complete_sent)
                                if len(clean_s) >= 4:
                                    self.mouth.enqueue_sentence(clean_s, gen_id, emotion=turn_emotion)
                                    full_reply.append(clean_s)
                            buffer = parts[-1]

                if buffer.strip() and gen_id == self.mouth.generation_id:
                    clean_s = humanize_text_for_speech(buffer.strip())
                    if clean_s:
                        self.mouth.enqueue_sentence(clean_s, gen_id, emotion=turn_emotion)
                        full_reply.append(clean_s)

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
            self.mouth.speak_non_blocking("Systems online, sir. Voice lock and hotkey bridge calibrated.")
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

    def start_background_daemon(self):
        t = threading.Thread(target=self.run_forever, daemon=True)
        t.start()
        return t

    def run_forever(self):
        self.calibrate_speaker_echo()

        print("\n" + "=" * 76)
        print("🎙️ JARVIS PLUG-AND-PLAY VOICE MODULE ONLINE (V6.2 ZERO-LAG SYNC)")
        print("- Hands-Free Voice: Say 'Hey Jarvis...' in any mood or volume.")
        print("- Mid-Flight Abort: Say 'Wait!' or 'Open X instead' to cancel & pivot.")
        print("- Hotkeys: 'CTRL + ALT + J' (Type) | 'CTRL + ALT + V' (Detach Voice).")
        print("=" * 76 + "\n")

        speech_buffer = []
        silence_blocks = 0
        speaking_blocks = 0

        pre_roll = deque(maxlen=10)
        barge_active_blocks = 0
        barge_hangover = 0
        was_jarvis_speaking = False
        in_barge_in_capture = False
        barge_in_sim_score = 0.0
        announced_listening_on_hud = False

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=BLOCK_SIZE,
            callback=self._audio_callback,
        ):
            while True:
                if self.paused_by_hotkey or self.detached:
                    time.sleep(0.1)
                    continue

                try:
                    block = self.audio_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                rms = float(np.sqrt(np.mean(block ** 2)))
                pre_roll.append(block)

                # =========================================================
                # MODE A: JARVIS IS SPEAKING OR EXECUTING (BIOMETRIC BARGE-IN)
                # =========================================================
                if self.mouth.is_speaking or self.is_executing_command:
                    if self.mouth.is_speaking:
                        was_jarvis_speaking = True
                    in_barge_in_capture = False
                    if self.mouth.is_speaking and (time.time() - self.mouth.speech_start_time) < 0.22:
                        continue

                    barge_trigger = max(0.011, self.mouth.echo_rms_baseline * 1.32, self.ambient_rms * 2.5)

                    if rms > barge_trigger:
                        barge_active_blocks += 1
                        barge_hangover = 5
                    else:
                        if barge_hangover > 0:
                            barge_hangover -= 1
                        else:
                            barge_active_blocks = 0

                    if barge_active_blocks >= 3:
                        recent_user_frames = list(pre_roll)[-5:]
                        test_clip = np.concatenate(recent_user_frames)
                        should_stop, u_sim, e_sim = self.bio_guard.check_barge_in_fast(test_clip)

                        if should_stop:
                            # 1. Immediately abort any in-flight skill/brain turn in laptop_client.py!
                            if self.cancel_turn_callback:
                                self.cancel_turn_callback()
                            # 2. Stop audio playback immediately
                            self.mouth.stop_immediately()
                            self.is_executing_command = False
                            was_jarvis_speaking = False
                            in_barge_in_capture = True
                            barge_in_sim_score = u_sim

                            print(
                                f"\n⚡ [BARGE-IN INTERRUPT] Aborted active turn & listening! "
                                f"(NeuralSim={u_sim:.2f})"
                            )

                            speech_buffer = list(pre_roll)[-4:]
                            speaking_blocks = len(speech_buffer)
                            silence_blocks = 0
                            barge_active_blocks = 0
                            barge_hangover = 0
                            self.live_session_active = True
                            self.last_interaction_time = time.time()
                            continue
                        else:
                            barge_active_blocks = 0

                    continue

                # =========================================================
                # POST-SPEECH REVERB GUARD (SKIPPED DURING BARGE-IN CAPTURE)
                # =========================================================
                if not in_barge_in_capture:
                    if was_jarvis_speaking:
                        was_jarvis_speaking = False
                        speech_buffer.clear()
                        speaking_blocks = 0
                        silence_blocks = 0
                        barge_active_blocks = 0
                        barge_hangover = 0
                        announced_listening_on_hud = False
                        self._flush_audio_queue()
                        self.last_interaction_time = time.time()
                        continue

                    if (time.time() - self.mouth.last_speech_end_time) < 0.15:
                        continue

                # =========================================================
                # MODE B: LISTENING + INSTANT HUD FEEDBACK
                # =========================================================
                if (
                    self.live_session_active
                    and not in_barge_in_capture
                    and (time.time() - self.last_interaction_time) > self.session_timeout_sec
                ):
                    self.live_session_active = False
                    print("\n💤 [Live Session Timeout] Returning to Wake-Word Standby (Say 'Jarvis' or press Ctrl+Alt+J).")

                voice_threshold = (
                    max(0.0052, self.ambient_rms * 1.95)
                    if in_barge_in_capture
                    else max(0.0070, self.ambient_rms * 2.35)
                )

                if rms > voice_threshold:
                    if speaking_blocks == 0 and len(pre_roll) > 1:
                        speech_buffer.extend(list(pre_roll)[-2:])
                    speech_buffer.append(block)
                    speaking_blocks += 1
                    silence_blocks = 0

                    # Emit an instant HUD indicator at 150ms so you see "HEARING YOU..." while speaking!
                    if speaking_blocks == 3 and (self.live_session_active or in_barge_in_capture) and not announced_listening_on_hud:
                        announced_listening_on_hud = True
                        sys.stdout.write("\r[Jarvis] 🎙️ Hearing your voice — speak naturally...\r")
                        sys.stdout.flush()
                else:
                    if speaking_blocks > 0:
                        speech_buffer.append(block)
                        silence_blocks += 1

                        # 350ms (7 blocks) of silence after speech ends -> fast, snappy response!
                        if silence_blocks >= 7:
                            if 4 <= speaking_blocks <= 180:
                                full_audio = np.concatenate(speech_buffer)
                                t0 = time.perf_counter()

                                if in_barge_in_capture:
                                    is_authorized = True
                                    sim_score = barge_in_sim_score
                                else:
                                    is_authorized, sim_score, _ = self.bio_guard.verify_speaker(
                                        full_audio,
                                        has_wake_word=False,
                                        in_live_session=self.live_session_active,
                                        is_post_barge_in=False,
                                    )

                                if is_authorized:
                                    sys.stdout.write("\r[Jarvis] ⚡ Decoding your command...\r")
                                    sys.stdout.flush()
                                    transcript = self._transcribe_clip(full_audio, is_barge_in=in_barge_in_capture)
                                    stt_ms = (time.perf_counter() - t0) * 1000.0
                                    if not in_barge_in_capture:
                                        self._flush_audio_queue()

                                    if transcript and len(transcript) >= 2:
                                        self._handle_verified_speech(
                                            transcript,
                                            stt_ms,
                                            sim_score,
                                            was_barge_in=in_barge_in_capture,
                                        )
                                    elif in_barge_in_capture:
                                        print("🛑 [Barge-In Paused] Jarvis silenced — Standing by for your command, sir.")
                                        self.live_session_active = True
                                        self.last_interaction_time = time.time()

                            speech_buffer.clear()
                            speaking_blocks = 0
                            silence_blocks = 0
                            in_barge_in_capture = False
                            announced_listening_on_hud = False
                    else:
                        self.ambient_rms = min(0.015, 0.98 * self.ambient_rms + 0.02 * rms)

    def _handle_verified_speech(self, text: str, stt_ms: float, sim_score: float, was_barge_in: bool = False):
        tl = text.lower().strip()
        tl_clean = re.sub(r"[^\w\s]", "", tl).strip()
        words = set(tl_clean.split())

        has_wake_word = "jarvis" in words or any(p in tl_clean for p in ["hey guys", "hey javis", "hey service"])

        if not self.live_session_active and not has_wake_word and not was_barge_in:
            return

        print(f"🎙️ [You ({stt_ms:.0f}ms | Neural-Match: {sim_score:.2f})]: \"{text}\"")

        if any(p in tl_clean for p in ["go to sleep", "standby", "just stand by", "pause session", "stop listening"]):
            self.live_session_active = False
            self.mouth.speak_non_blocking("Going on standby, sir. Call my name or press Control Alt J anytime.")
            return

        if "enroll voice" in tl_clean or "recalibrate voice" in tl_clean:
            self.mouth.stop_immediately()
            self.bio_guard.load_or_enroll(force_enroll=True)
            return

        self.live_session_active = True
        self.last_interaction_time = time.time()

        # Strip wake prefix or leading interrupt words ("Wait, please open Brave", "Stop, Jarvis...")
        cleaned_query = re.sub(
            r"^(?:(?:stop|wait|wait\s+a\s+second|hold\s+on|listen|no)[\s,.:;!?]*)*(?:(?:hey\s+|okay\s+|ok\s+|yo\s+)?(?:jarvis|guys|javis)[\s,.:;!?]*)?",
            "",
            text,
            flags=re.I,
        ).strip()
        cleaned_query = cleaned_query.rstrip(".?!,;:")

        q_raw_clean = re.sub(r"[^\w\s]", "", tl).strip()
        if not cleaned_query or q_raw_clean in [
            "stop", "jarvis stop", "hey jarvis stop", "wait", "wait a second",
            "hold on", "quiet", "cancel", "enough", "shut up", "let it go", "nevermind"
        ]:
            if self.cancel_turn_callback:
                self.cancel_turn_callback()
            self.mouth.stop_immediately()
            print("🛑 [Interrupted & Paused] Standing by for your next command, sir.")
            return

        q_clean = re.sub(r"[^\w\s]", "", cleaned_query.lower()).strip()

        if any(p in q_clean for p in ["talk slower", "speak slower", "slow down", "speak slowly", "talk slowly", "too fast"]):
            self.mouth.user_rate_offset = max(-40, self.mouth.user_rate_offset - 22)
            self.mouth.speak_non_blocking(
                "Of course, sir. I've slowed my speaking cadence down for you.",
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

        turn_emotion = detect_user_emotion_and_style(cleaned_query, self.mouth.current_emotion)
        if turn_emotion == "whisper":
            self.mouth.current_emotion = "whisper"

        if turn_emotion == "empathetic" or self.external_command_handler is None:
            self._stream_standalone_qwen(cleaned_query, turn_emotion=turn_emotion)
            self.last_interaction_time = time.time()
            return

        # SMART LOADING VOICE GATE:
        # If this is an instant 0.0s reflex command ('open notepad', 'open brave', 'close tab'),
        # DO NOT play a 2-second loading phrase first! Speak the result immediately in sync with the action!
        use_loading_voice = not is_zero_latency_reflex_query(cleaned_query)
        gen_id = self.mouth.start_new_turn(play_loading_phrase=use_loading_voice, emotion=turn_emotion)
        self.is_executing_command = True

        def _dispatch_to_jarvis():
            try:
                self.external_command_handler(cleaned_query, gen_id, turn_emotion)
            finally:
                self.is_executing_command = False
                self._flush_audio_queue()
                self.last_interaction_time = time.time()

        threading.Thread(target=_dispatch_to_jarvis, daemon=True).start()


if __name__ == "__main__":
    print("=== RUNNING VOICE_MODULE.PY IN STANDALONE SANDBOX MODE ===")
    plugin = JarvisVoicePlugin(external_command_handler=None)
    try:
        plugin.run_forever()
    except KeyboardInterrupt:
        plugin.mouth.stop_immediately()
        print("\n[Standalone Voice Module Exited]")