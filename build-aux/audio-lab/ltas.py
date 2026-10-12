"""Long-term spectrum (all frames, energy weighted) of an output relative to the mic, both normalised to 300-3000 Hz."""
import sys, subprocess, numpy as np
R = 48000
def read(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-ar", str(R), "-af", "pan=mono|c0=c0", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).astype(float)
def ltas(x):
    N, H = 4096, 2048; win = np.hanning(N); n = (len(x)-N)//H
    P = (np.abs(np.fft.rfft(np.stack([x[i*H:i*H+N]*win for i in range(n)]), axis=1))**2).mean(axis=0)
    return P, np.fft.rfftfreq(N, 1/R)
P1, f = ltas(read(sys.argv[1]))
print(f"{'band Hz':>8}" + "".join(f"{p.split('/')[-1][:18]:>20}" for p in sys.argv[2:]))
outs = [ltas(read(p))[0] for p in sys.argv[2:]]
ref = lambda P: 10*np.log10(P[(f>=300)&(f<3000)].sum())
for lo, hi in ((100, 250), (2000, 3000), (3000, 5000), (5000, 8000), (8000, 16000)):
    m = (f>=lo)&(f<hi); base = 10*np.log10(P1[m].sum()) - ref(P1)
    print(f"{lo:>8}" + "".join(f"{10*np.log10(P[m].sum()) - ref(P) - base:20.1f}" for P in outs))
