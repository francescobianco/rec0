"""Sample-to-sample jumps: where the biggest discontinuities are in a file's audio."""
import subprocess, sys
import numpy as np

def read(path, track=0, mono=True):
    args = ["ffmpeg", "-v", "error", "-i", path, "-map", f"0:a:{track}", "-ar", "48000", "-f", "f32le"]
    args += ["-af", "pan=mono|c0=c0"] if mono else ["-ac", "2"]
    raw = subprocess.run(args + ["-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, np.float32).astype(float)
    return x if mono else x.reshape(-1, 2)[:, 0]

def jumps(x, frame=1024):
    d = np.abs(np.diff(x))
    n = len(d) // frame
    return d[:n * frame].reshape(n, frame).max(1), d

if __name__ == "__main__":
    for path in sys.argv[1:]:
        x = read(path)
        j, d = jumps(x)
        env = np.sqrt(np.convolve(x ** 2, np.ones(480) / 480, "same"))
        # A click is a jump much larger than the local signal envelope.
        ratio = d / (env[:-1] + 1e-4)
        idx = np.argsort(ratio)[::-1][:400]
        print(f"== {path}  samples {len(x)}  max|diff| {d.max():.4f}")
        seen = []
        for i in idx:
            t = i / 48000
            if any(abs(t - s) < 0.05 for s in seen):
                continue
            seen.append(t)
            print(f"   t={t:9.4f}s  diff={d[i]:.4f}  env={env[i]:.4f}  ratio={ratio[i]:.1f}")
            if len(seen) >= 12:
                break
        i = int(23.744 * 48000)
        print(f"   around 23.744 s: max diff {d[i-2400:i+2400].max():.4f}, env {env[i]:.4f}")
