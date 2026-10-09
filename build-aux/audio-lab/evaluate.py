"""Evaluate a processed two-track recording against the quality expectations (dossier §17, §19).

    evaluate.py ORIGINAL.mp4 OUTPUT.mp4 [VOICE.wav] [timeline.txt]

VOICE.wav is the echo-cancelled microphone (made with echo.cancel when not given).

Since the system track is laid over the voice untouched, OUTPUT - SYSTEM is the
processed voice (plus whatever echo residue and limiter action remain).
"""
import sys, subprocess
import numpy as np
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rec0 import echo

R = 48000

def read(path, track=0):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", f"0:a:{track}", "-ar", str(R),
                          "-af", "pan=mono|c0=c0", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).astype(float)

def db(x):
    return 20 * np.log10(np.sqrt(np.mean(np.square(x))) + 1e-9)

def per_second(x):
    n = len(x) // R
    return np.array([db(x[i * R:(i + 1) * R]) for i in range(n)])

def lag(a, b, max_ms=100):
    n = min(len(a), len(b)); nf = 1 << int(np.ceil(np.log2(2 * n)))
    c = np.fft.irfft(np.fft.rfft(a[:n], nf) * np.conj(np.fft.rfft(b[:n], nf)), nf)
    m = int(max_ms * R / 1000)
    c = np.concatenate([c[-m:], c[:m + 1]])
    k = int(np.argmax(np.abs(c)))
    return (k - m) / R * 1000, c[k] / (np.linalg.norm(a[:n]) * np.linalg.norm(b[:n]) + 1e-12)

def main(orig, out, voice_path=None, timeline=None):
    mic, sysd, outp = read(orig, 0), read(orig, 1), read(out, 0)
    n = min(len(mic), len(sysd), len(outp))
    mic, sysd, outp = mic[:n], sysd[:n], outp[:n]
    print(f"duration {n / R:.2f} s")
    active = per_second(sysd) > -60
    # 1. System fidelity: the system track must be in the output at gain 1, lag 0.
    if active.any():
        i0 = int(np.argmax(active)) * R
        i1 = i0 + min(10 * R, n - i0)
        ms, corr = lag(outp[i0:i1], sysd[i0:i1])
        g = np.dot(outp[i0:i1], sysd[i0:i1]) / (np.dot(sysd[i0:i1], sysd[i0:i1]) + 1e-12)
        print(f"system in output: lag {ms:+.3f} ms, projection gain {g:.3f} ({20*np.log10(abs(g)+1e-9):+.2f} dB), corr {corr:.3f}")
    resid = outp - sysd                     # the processed voice (+ echo residue, limiter)
    # 2. Echo residue: what part of the processed voice is still coherent with the system track.
    if active.any():
        clean, coherent = echo._cancel_span(resid, sysd)
        pe, pc, ps = per_second(resid), per_second(coherent), per_second(sysd)
        mi_c = echo._cancel_span(mic, sysd)[1]
        pm, pmc = per_second(mic), per_second(mi_c)
    # 3. Voice gain stability: processed voice vs echo-cancelled voice, per second.
    voice = read(voice_path) if voice_path else None
    if voice is not None:
        voice = voice[:n]
        pv = per_second(voice)
        gain = per_second(resid) - pv
    # timeline labels
    labels = {}
    if timeline:
        for line in open(timeline):
            k, t = line.split(); labels.setdefault(int(float(t)), []).append(k)
    print()
    print(" s   mic   sys  | out-sys  coh.echo(resid)  coh.echo(mic) | voice  gain | notes")
    for s in range(n // R):
        row = f"{s:3d} {per_second(mic)[s] if False else 0:0.0f}"
        m = db(mic[s * R:(s + 1) * R]); sy = db(sysd[s * R:(s + 1) * R]); r = db(resid[s * R:(s + 1) * R])
        line = f"{s:3d} {m:6.1f} {sy:6.1f} | {r:6.1f}"
        if active.any():
            line += f"   {pc[s] - pe[s]:6.1f} dB        {pmc[s] - pm[s]:6.1f} dB   "
        else:
            line += " " * 36
        if voice is not None:
            line += f"| {pv[s]:6.1f} {gain[s]:+6.1f}"
        else:
            line += "|             "
        line += " | " + ", ".join(labels.get(s, []))
        print(line)
    if voice is not None:
        ok = pv > -55
        g = gain[ok]
        print(f"\nvoice gain over the file (processed voice vs AEC voice, per second): median {np.median(g):+.1f} dB, "
              f"p10 {np.percentile(g, 10):+.1f}, p90 {np.percentile(g, 90):+.1f}, min {g.min():+.1f}, max {g.max():+.1f}, stdev {g.std():.2f}")
    # 4. Clicks in the processed voice that are not in the AEC voice nor in the system track.
    d = np.abs(np.diff(resid)); env = np.sqrt(np.convolve(resid ** 2, np.ones(480) / 480, "same"))[:-1]
    ratio = d / (env + 1e-4)
    ds = np.abs(np.diff(sysd))
    cand = [(ratio[i], i) for i in np.argsort(ratio)[::-1][:300] if ds[max(0, i - 48):i + 48].max() < 0.02]
    print("\nlargest discontinuities in the processed voice (not in the system track):")
    seen = []
    for r_, i in cand:
        t = i / R
        if any(abs(t - s_) < 0.05 for s_ in seen):
            continue
        seen.append(t)
        print(f"   t={t:8.3f}s  diff={d[i]:.4f}  env={env[i]:.4f}  ratio={r_:.1f}")
        if len(seen) >= 8:
            break
    # 5. loudness of the output
    err = subprocess.run(["ffmpeg", "-v", "info", "-i", out, "-af", "ebur128=peak=true", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    tail = [l.strip() for l in err.splitlines() if l.strip().startswith(("I:", "LRA:", "Peak:"))]
    print("\noutput loudness:", " ".join(tail))

if __name__ == "__main__":
    import tempfile
    args = sys.argv[1:]
    if len(args) < 3 or not args[2].endswith(".wav"):
        tmp = tempfile.mkdtemp(prefix="rec0-lab-")
        voice = f"{tmp}/voice.wav"
        echo.cancel(args[0], voice)
        args = args[:2] + [voice] + args[2:]
    main(*args)
    print()
    stability(args[0], args[1], args[2])


def stability(orig, out, voice_path, win_ms=100):
    """Gain of the processed voice vs the AEC voice per `win_ms` window: how often it is gated."""
    mic, sysd, outp, voice = read(orig, 0), read(orig, 1), read(out, 0), read(voice_path)
    n = min(len(mic), len(sysd), len(outp), len(voice))
    resid, voice = outp[:n] - sysd[:n], voice[:n]
    w = int(R * win_ms / 1000); m = n // w
    lv = 20 * np.log10(np.sqrt((voice[:m * w].reshape(m, w) ** 2).mean(1)) + 1e-9)
    lr = 20 * np.log10(np.sqrt((resid[:m * w].reshape(m, w) ** 2).mean(1)) + 1e-9)
    sysl = 20 * np.log10(np.sqrt((sysd[:m * w].reshape(m, w) ** 2).mean(1)) + 1e-9)
    ok = lv > -50
    g = lr - lv
    med = np.median(g[ok])
    for name, sel in (("system silent", ok & (sysl < -70)), ("system playing", ok & (sysl >= -70))):
        gs = g[sel]
        gated = (gs < med - 6).mean()
        deep = (gs < med - 12).mean()
        # transitions: crossings of the median-6 line
        below = gs < med - 6
        trans = int(np.sum(below[1:] != below[:-1]))
        print(f"{name:15s}: windows {sel.sum():4d}  gain median {np.median(gs):+5.1f} p10 {np.percentile(gs, 10):+5.1f} "
              f"p90 {np.percentile(gs, 90):+5.1f} min {gs.min():+5.1f} | gated(>6 dB under) {gated:5.1%}  (>12 dB) {deep:5.1%}  "
              f"gate transitions {trans}")
    return g
