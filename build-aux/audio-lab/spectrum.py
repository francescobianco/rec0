"""Third-octave band levels of speech frames: mic original vs processed output, level-matched."""
import sys, subprocess, numpy as np
R = 48000
def read(path, track=0):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", f"0:a:{track}", "-ar", str(R),
                          "-af", "pan=mono|c0=c0", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).astype(float)
def ltas(x):
    N, H = 4096, 2048
    win = np.hanning(N)
    n = (len(x) - N) // H
    frames = np.stack([x[i*H:i*H+N] * win for i in range(n)])
    rms = 20*np.log10(np.sqrt(np.mean(frames**2, axis=1)) + 1e-9)
    thr = np.percentile(rms, 60)          # loud frames only: speech
    P = np.abs(np.fft.rfft(frames[rms > thr], axis=1))**2
    return P.mean(axis=0), np.fft.rfftfreq(N, 1/R)
a = ltas(read(sys.argv[1], int(sys.argv[3]) if len(sys.argv) > 3 else 0))
b = ltas(read(sys.argv[2]))
P1, f = a; P2, _ = b
edges = 63 * 2 ** (np.arange(0, 25) / 3)
print(f"{'band Hz':>9} {'mic dB':>7} {'out dB':>7} {'diff':>6}")
rows = []
for lo, hi in zip(edges[:-1], edges[1:]):
    m = (f >= lo) & (f < hi)
    l1, l2 = 10*np.log10(P1[m].sum()), 10*np.log10(P2[m].sum())
    rows.append((lo, l1, l2))
ref1 = 10*np.log10(P1[(f>=300)&(f<3000)].sum()); ref2 = 10*np.log10(P2[(f>=300)&(f<3000)].sum())
for lo, l1, l2 in rows:
    print(f"{lo:9.0f} {l1-ref1:7.1f} {l2-ref2:7.1f} {(l2-ref2)-(l1-ref1):6.1f}")
