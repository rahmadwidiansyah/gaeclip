"""Numpy-only MFCC matching the upstream TalkNet training features.

Replicates ``python_speech_features.mfcc`` call convention used by
TalkNet-ASD dataLoader (rectangular window, preemph 0.97, 26 mel bands,
NFFT 512, lifter 22, energy-appended c0) so the TalkSet checkpoint sees
the feature distribution it was trained on. int16 audio is consumed
AS-IS like the upstream loader (scale only shifts c0, which would break
the appended-energy coefficient the model learned).

No scipy dependency: DCT-II-ortho is a precomputed 26x26 matrix (trivial
cost), everything else is strided numpy.
"""

from __future__ import annotations

import numpy as np


def hz2mel(hertz: float) -> float:
    return 2595.0 * np.log10(1.0 + hertz / 700.0)


def mel2hz(mel: float) -> float:
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


def mel_filterbank(nfilt: int = 26, nfft: int = 512,
                   samplerate: int = 16000,
                   lowfreq: float = 0.0, highfreq: float | None = None,
                   ) -> np.ndarray:
    """(nfilt, nfft//2+1) triangular mel filterbank (Slaney-free HTK style)."""
    highfreq = highfreq if highfreq is not None else samplerate / 2.0
    lowmel = hz2mel(lowfreq)
    highmel = hz2mel(highfreq)
    melpoints = np.linspace(lowmel, highmel, nfilt + 2)
    hzpoints = np.array([mel2hz(m) for m in melpoints])
    bins = np.floor((nfft + 1) * hzpoints / samplerate).astype(int)
    fbank = np.zeros((nfilt, nfft // 2 + 1))
    for m in range(1, nfilt + 1):
        f_m_minus, f_m, f_m_plus = bins[m - 1], bins[m], bins[m + 1]
        for k in range(f_m_minus, f_m):
            fbank[m - 1, k] = (k - f_m_minus) / max(1, (f_m - f_m_minus))
        for k in range(f_m, f_m_plus):
            fbank[m - 1, k] = (f_m_plus - k) / max(1, (f_m_plus - f_m))
    return fbank


def dct_ortho_matrix(n: int) -> np.ndarray:
    """Type-II DCT with ortho normalization (matches scipy.fftpack)."""
    mat = np.cos(np.pi / n * np.outer(np.arange(n), np.arange(n) + 0.5))
    mat[0, :] *= 1.0 / np.sqrt(2.0)
    return mat * np.sqrt(2.0 / n)


def lifter_matrix(numcep: int, lift: int = 22) -> np.ndarray:
    n = np.arange(numcep)
    return 1.0 + (lift / 2.0) * np.sin(np.pi * n / lift)


_DCT_CACHE: dict[int, np.ndarray] = {}
_LIFTER_CACHE: dict[tuple[int, int], np.ndarray] = {}


def mfcc_features(signal: np.ndarray,
                  samplerate: int = 16000,
                  winlen: float = 0.025,
                  winstep: float = 0.010,
                  numcep: int = 13,
                  nfilt: int = 26,
                  nfft: int = 512,
                  preemph: float = 0.97,
                  ceplifter: int = 22) -> np.ndarray:
    """(n_frames, numcep) MFCC. Empty/silent-safe (eps guards, no NaN)."""
    signal = np.asarray(signal).astype(np.float64)
    if signal.size == 0:
        return np.zeros((0, numcep))
    # Pre-emphasis (first sample preserved, like the reference).
    emphasized = np.append(signal[0], signal[1:] - preemph * signal[:-1])
    frame_len = int(round(winlen * samplerate))
    frame_step = int(round(winstep * samplerate))
    signal_len = len(emphasized)
    numframes = 1 + int(np.ceil((signal_len - frame_len) / frame_step)) \
        if signal_len > frame_len else 1
    padlen = (numframes - 1) * frame_step + frame_len
    padding = np.zeros(max(0, padlen - signal_len))
    padded = np.concatenate((emphasized, padding))
    indices = (np.tile(np.arange(frame_len), (numframes, 1))
               + np.tile(np.arange(0, numframes * frame_step, frame_step),
                         (frame_len, 1)).T)
    frames = padded[indices.astype(np.int32, copy=False)]
    # Rectangular window (upstream default winfunc=ones): no-op here.
    magspec = np.abs(np.fft.rfft(frames, nfft))
    powspec = (1.0 / nfft) * (magspec ** 2)
    fbank = mel_filterbank(nfilt, nfft, samplerate)
    feat = np.dot(powspec, fbank.T)
    feat = np.where(feat <= 0, np.finfo(float).eps, feat)
    feat = np.log(feat)
    if nfft not in _DCT_CACHE:
        _DCT_CACHE[nfft] = dct_ortho_matrix(nfilt)
    cepstra = np.dot(feat, _DCT_CACHE[nfft].T)[:, :numcep]
    key = (numcep, ceplifter)
    if key not in _LIFTER_CACHE:
        _LIFTER_CACHE[key] = lifter_matrix(numcep, ceplifter)
    cepstra = cepstra * _LIFTER_CACHE[key]
    # Appended energy replaces c0 (upstream convention).
    energy = np.log(np.sum(powspec, axis=1) + 1e-10)
    cepstra[:, 0] = energy
    return cepstra.astype(np.float32)


def mfcc_for_window(audio_16k: np.ndarray,
                    num_frames: int,
                    fps: float = 25.0) -> np.ndarray:
    """MFCC rows aligned to TalkNet visual frames (4 audio rows / frame).

    Mirrors dataLoader.load_audio (fps-adaptive winlen/winstep, wrap-pad to
    numFrames*4 rows). No augmentation here (inference only).
    """
    cepstra = mfcc_features(
        np.asarray(audio_16k),
        samplerate=16000,
        winlen=0.025 * 25.0 / fps,
        winstep=0.010 * 25.0 / fps)
    need = int(num_frames * 4)
    if cepstra.shape[0] < need:
        shortage = need - cepstra.shape[0]
        cepstra = np.pad(cepstra, ((0, shortage), (0, 0)), mode="wrap")
    return cepstra[:need]
