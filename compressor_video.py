#!/usr/bin/env python3
"""
Compressor de Vídeo - interface simples sobre o FFmpeg.

Requisitos: Python 3.8+ (com tkinter) e FFmpeg instalado (ffmpeg + ffprobe no PATH,
ou colocados na mesma pasta deste arquivo).

Os vídeos originais nunca são alterados: os comprimidos são gravados como novos
arquivos (nome_comprimido.mp4), por padrão numa subpasta "comprimidos".
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

RESOLUTIONS = {"Original": None, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
FPS_OPTIONS = ["Original", "30", "24", "15", "12", "10"]
SPEEDS = ["veryfast", "faster", "fast", "medium", "slow", "slower"]
NVENC_PRESET = {"veryfast": "p2", "faster": "p3", "fast": "p4", "medium": "p5", "slow": "p6", "slower": "p7"}
AUDIO_BITRATES = ["32k", "48k", "64k", "96k", "128k", "192k"]
AUDIO_MODES = ["Manter original", "Reduzir (AAC)", "Remover áudio"]

PROFILES = {
    "Reunião - recomendado": dict(codec="H.264 (libx264)", crf=28, speed="medium", res="720p", fps="15",
                                  amode="Reduzir (AAC)", abr="64k", mono=True),
    "Reunião - máxima compressão": dict(codec="H.265 (libx265)", crf=30, speed="medium", res="720p", fps="10",
                                        amode="Reduzir (AAC)", abr="48k", mono=True),
    "Alta qualidade": dict(codec="H.264 (libx264)", crf=22, speed="medium", res="Original", fps="Original",
                           amode="Reduzir (AAC)", abr="128k", mono=False),
}
CODEC_MAP = {
    "H.264 (libx264)": "libx264",
    "H.265 (libx265)": "libx265",
    "H.264 NVIDIA (nvenc)": "h264_nvenc",
    "H.265 NVIDIA (nvenc)": "hevc_nvenc",
}


# ----------------------------------------------------------------------------
# Núcleo (sem interface): localizar ffmpeg, ler informações e montar o comando
# ----------------------------------------------------------------------------
def find_tool(name):
    exe = name + (".exe" if os.name == "nt" else "")
    local = Path(__file__).resolve().parent / exe
    if local.exists():
        return str(local)
    return shutil.which(name)


def fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_time(sec):
    sec = int(max(0, sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def available_codecs(ffmpeg):
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout
    except Exception:
        out = ""
    names = []
    for label, enc in CODEC_MAP.items():
        if enc in ("libx264", "libx265") or f" {enc} " in out:
            names.append(label)
    return names or ["H.264 (libx264)"]


def probe(ffprobe, path):
    """Devolve duração, altura, fps, codec de áudio (ou None se não houver áudio)."""
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
    if res.returncode != 0:
        raise RuntimeError("Não foi possível ler o arquivo (não parece ser um vídeo válido).")
    data = json.loads(res.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video is None:
        raise RuntimeError("O arquivo não contém faixa de vídeo.")

    def rate(txt):
        try:
            a, b = txt.split("/")
            return float(a) / float(b) if float(b) else 0.0
        except Exception:
            return 0.0

    fps = rate(video.get("avg_frame_rate", "0/0")) or rate(video.get("r_frame_rate", "0/0"))
    duration = 0.0
    for src in (data.get("format", {}), video):
        try:
            duration = float(src.get("duration"))
            break
        except (TypeError, ValueError):
            pass
    return dict(duration=duration, height=int(video.get("height") or 0), fps=fps,
                acodec=audio.get("codec_name") if audio else None)


def build_command(ffmpeg, src, dst_tmp, opts, info):
    enc = CODEC_MAP[opts["codec"]]
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-progress", "pipe:1", "-nostats",
           "-i", str(src), "-map", "0:v:0"]
    if info["acodec"] and opts["amode"] != "Remover áudio":
        cmd += ["-map", "0:a:0"]

    filters = []
    target_h = RESOLUTIONS.get(opts["res"])
    if target_h and info["height"] > target_h:
        filters.append(f"scale=-2:{target_h}")
    if opts["fps"] != "Original":
        target_fps = float(opts["fps"])
        if info["fps"] == 0 or info["fps"] > target_fps + 0.5:
            filters.append(f"fps={opts['fps']}")
    if filters:
        cmd += ["-vf", ",".join(filters)]

    crf, speed = str(opts["crf"]), opts["speed"]
    if enc in ("libx264", "libx265"):
        cmd += ["-c:v", enc, "-crf", crf, "-preset", speed, "-pix_fmt", "yuv420p"]
    else:  # nvenc
        cmd += ["-c:v", enc, "-rc", "vbr", "-cq", crf, "-b:v", "0", "-preset", NVENC_PRESET[speed],
                "-pix_fmt", "yuv420p"]
    if enc in ("libx265", "hevc_nvenc"):
        cmd += ["-tag:v", "hvc1"]

    if not info["acodec"] or opts["amode"] == "Remover áudio":
        cmd += ["-an"]
    elif opts["amode"] == "Manter original":
        if info["acodec"] in ("aac", "mp3", "ac3", "eac3", "opus"):
            cmd += ["-c:a", "copy"]
        else:  # codec que o MP4 não aceita bem: recodifica
            cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-c:a", "aac", "-b:a", opts["abr"]]
        if opts["mono"]:
            cmd += ["-ac", "1"]

    cmd += ["-movflags", "+faststart", "-f", "mp4", str(dst_tmp)]
    return cmd


def unique_output(src, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"{src.stem}_comprimido.mp4"
    n, dst = 1, base
    while dst.exists():
        n += 1
        dst = out_dir / f"{src.stem}_comprimido_{n}.mp4"
    return dst


class Cancelled(Exception):
    pass


def compress_file(ffmpeg, ffprobe, src, out_dir, opts, on_progress, cancel_event, proc_holder):
    """Comprime um arquivo. Devolve (caminho_final, tamanho_final). on_progress(frac, texto)."""
    src = Path(src)
    info = probe(ffprobe, src)
    dst = unique_output(src, out_dir)
    tmp = dst.with_name(dst.stem + ".part.mp4")
    cmd = build_command(ffmpeg, src, tmp, opts, info)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    proc_holder["proc"] = proc
    errors = []
    started = time.time()
    try:
        for line in proc.stdout:
            line = line.strip()
            if cancel_event.is_set():
                proc.kill()
                raise Cancelled()
            key, _, val = line.partition("=")
            if key in ("out_time_us", "out_time_ms") and val.lstrip("-").isdigit() and info["duration"] > 0:
                done = max(0, int(val)) / 1_000_000
                frac = min(done / info["duration"], 1.0)
                el = time.time() - started
                speed = done / el if el > 0 else 0
                eta = (info["duration"] - done) / speed if speed > 0 else 0
                on_progress(frac, f"{fmt_time(done)} / {fmt_time(info['duration'])}  •  {speed:.1f}x  •  faltam ~{fmt_time(eta)}")
            elif "=" not in line and line:
                errors.append(line)
        proc.wait()
        if cancel_event.is_set():
            raise Cancelled()
        if proc.returncode != 0:
            raise RuntimeError("FFmpeg falhou: " + " | ".join(errors[-3:]) if errors else "FFmpeg falhou.")
        os.replace(tmp, dst)
        return dst, dst.stat().st_size
    except BaseException:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    finally:
        proc_holder["proc"] = None


# ----------------------------------------------------------------------------
# Interface
# ----------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    root = tk.Tk()
    root.title("Compressor de Vídeo")
    root.geometry("940x720")
    root.minsize(860, 640)
    if not ffmpeg or not ffprobe:
        messagebox.showerror(
            "FFmpeg não encontrado",
            "Instale o FFmpeg e tente de novo.\n\n"
            "Windows: abra o Prompt e rode  winget install Gyan.FFmpeg\n"
            "Mac: brew install ffmpeg\nLinux: sudo apt install ffmpeg\n\n"
            "Depois, feche e abra este programa novamente.")
        root.destroy()
        return

    codecs = available_codecs(ffmpeg)
    q = queue.Queue()
    cancel_event = threading.Event()
    proc_holder = {"proc": None}
    state = {"running": False, "paths": {}, "applying": False}  # iid -> Path

    pad = dict(padx=10, pady=4)

    # --- tabela de arquivos / relatório
    files_box = ttk.LabelFrame(root, text="Vídeos")
    files_box.pack(fill="both", expand=True, **pad)
    cols = ("orig", "final", "red", "status")
    tree = ttk.Treeview(files_box, columns=cols, height=7, selectmode="extended")
    tree.heading("#0", text="Arquivo")
    tree.heading("orig", text="Original")
    tree.heading("final", text="Comprimido")
    tree.heading("red", text="Redução")
    tree.heading("status", text="Situação")
    tree.column("#0", width=300)
    for c, w in (("orig", 90), ("final", 100), ("red", 80), ("status", 180)):
        tree.column(c, width=w, anchor="center")
    sb = ttk.Scrollbar(files_box, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    btns = ttk.Frame(files_box)
    btns.pack(side="right", fill="y", padx=6, pady=6)
    tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
    sb.pack(side="left", fill="y", pady=6)

    def add_paths(paths):
        for p in paths:
            p = Path(p)
            if p.suffix.lower() not in VIDEO_EXTS or p in state["paths"].values():
                continue
            iid = tree.insert("", "end", text=p.name, values=(fmt_size(p.stat().st_size), "", "", "Na fila"))
            state["paths"][iid] = p

    def add_files():
        add_paths(filedialog.askopenfilenames(title="Escolha os vídeos",
                                              filetypes=[("Vídeos", " ".join("*" + e for e in sorted(VIDEO_EXTS))), ("Todos", "*.*")]))

    def add_folder():
        d = filedialog.askdirectory(title="Escolha uma pasta com vídeos")
        if d:
            add_paths(sorted(Path(d).iterdir()))

    def remove_sel():
        if state["running"]:
            return
        for iid in tree.selection():
            state["paths"].pop(iid, None)
            tree.delete(iid)

    def clear_all():
        if state["running"]:
            return
        state["paths"].clear()
        tree.delete(*tree.get_children())

    for text, cmd in (("Adicionar vídeos…", add_files), ("Adicionar pasta…", add_folder),
                      ("Remover selecionados", remove_sel), ("Limpar lista", clear_all)):
        ttk.Button(btns, text=text, command=cmd, width=20).pack(fill="x", pady=2)

    # --- configurações
    cfg = ttk.LabelFrame(root, text="Configurações")
    cfg.pack(fill="x", **pad)
    v_profile = tk.StringVar(value="Reunião - recomendado")
    v_codec = tk.StringVar()
    v_crf = tk.IntVar(value=28)
    v_speed = tk.StringVar()
    v_res = tk.StringVar()
    v_fps = tk.StringVar()
    v_amode = tk.StringVar()
    v_abr = tk.StringVar()
    v_mono = tk.BooleanVar()

    def row(r, c, label, widget):
        ttk.Label(cfg, text=label).grid(row=r, column=c * 2, sticky="e", padx=(10, 4), pady=4)
        widget.grid(row=r, column=c * 2 + 1, sticky="w", padx=(0, 10), pady=4)

    row(0, 0, "Perfil rápido:", ttk.Combobox(cfg, textvariable=v_profile, values=list(PROFILES) + ["Personalizado"],
                                              state="readonly", width=28))
    row(0, 1, "Codec:", ttk.Combobox(cfg, textvariable=v_codec, values=codecs, state="readonly", width=22))
    crf_frame = ttk.Frame(cfg)
    crf_label = ttk.Label(crf_frame, width=3)
    ttk.Scale(crf_frame, from_=18, to=35, variable=v_crf, length=170,
              command=lambda v: (v_crf.set(round(float(v))), crf_label.config(text=str(v_crf.get())),
                                 v_profile.set("Personalizado"))).pack(side="left")
    crf_label.pack(side="left", padx=4)
    ttk.Label(crf_frame, text="(menor = melhor qualidade)").pack(side="left")
    row(1, 0, "Qualidade (CRF):", crf_frame)
    row(1, 1, "Velocidade:", ttk.Combobox(cfg, textvariable=v_speed, values=SPEEDS, state="readonly", width=12))
    row(2, 0, "Resolução máx.:", ttk.Combobox(cfg, textvariable=v_res, values=list(RESOLUTIONS), state="readonly", width=12))
    row(2, 1, "FPS máx.:", ttk.Combobox(cfg, textvariable=v_fps, values=FPS_OPTIONS, state="readonly", width=12))
    row(3, 0, "Áudio:", ttk.Combobox(cfg, textvariable=v_amode, values=AUDIO_MODES, state="readonly", width=18))
    aframe = ttk.Frame(cfg)
    abr_cb = ttk.Combobox(aframe, textvariable=v_abr, values=AUDIO_BITRATES, state="readonly", width=6)
    abr_cb.pack(side="left")
    mono_cb = ttk.Checkbutton(aframe, text="Mono (ideal para voz)", variable=v_mono)
    mono_cb.pack(side="left", padx=8)
    row(3, 1, "Bitrate do áudio:", aframe)
    ttk.Label(cfg, text="Dica: resolução e FPS só reduzem; vídeos já menores que o limite não são aumentados.",
              foreground="#555").grid(row=4, column=0, columnspan=4, sticky="w", padx=10, pady=(0, 6))

    def apply_profile(*_):
        p = PROFILES.get(v_profile.get())
        if not p:
            return
        state["applying"] = True
        v_codec.set(p["codec"] if p["codec"] in codecs else codecs[0])
        v_crf.set(p["crf"]); crf_label.config(text=str(p["crf"]))
        v_speed.set(p["speed"]); v_res.set(p["res"]); v_fps.set(p["fps"])
        v_amode.set(p["amode"]); v_abr.set(p["abr"]); v_mono.set(p["mono"])
        state["applying"] = False
        refresh_audio()

    def refresh_audio(*_):
        st = "readonly" if v_amode.get() == "Reduzir (AAC)" else "disabled"
        abr_cb.config(state=st)
        mono_cb.config(state="normal" if st == "readonly" else "disabled")

    def mark_custom(*_):
        if state["applying"]:
            return
        if v_profile.get() != "Personalizado":
            p = PROFILES.get(v_profile.get(), {})
            cur = dict(codec=v_codec.get(), speed=v_speed.get(), res=v_res.get(), fps=v_fps.get(),
                       amode=v_amode.get(), abr=v_abr.get(), mono=v_mono.get())
            if any(p.get(k) != v for k, v in cur.items() if k != "codec") and p:
                v_profile.set("Personalizado")

    v_profile.trace_add("write", apply_profile)
    v_amode.trace_add("write", refresh_audio)
    for var in (v_speed, v_res, v_fps, v_amode, v_abr, v_mono):
        var.trace_add("write", mark_custom)

    # --- saída
    out = ttk.LabelFrame(root, text="Onde salvar")
    out.pack(fill="x", **pad)
    v_outmode = tk.StringVar(value="side")
    v_outdir = tk.StringVar()
    ttk.Radiobutton(out, text='Numa pasta "comprimidos" ao lado de cada vídeo original', variable=v_outmode,
                    value="side").grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=2)
    ttk.Radiobutton(out, text="Nesta pasta:", variable=v_outmode, value="custom").grid(row=1, column=0, sticky="w", padx=10, pady=2)
    ttk.Entry(out, textvariable=v_outdir, width=60).grid(row=1, column=1, sticky="we", padx=4)

    def choose_out():
        d = filedialog.askdirectory(title="Pasta de destino")
        if d:
            v_outdir.set(d)
            v_outmode.set("custom")

    ttk.Button(out, text="Escolher…", command=choose_out).grid(row=1, column=2, padx=10, pady=2)
    out.columnconfigure(1, weight=1)

    # --- execução
    run = ttk.Frame(root)
    run.pack(fill="x", **pad)
    start_btn = ttk.Button(run, text="Comprimir")
    cancel_btn = ttk.Button(run, text="Cancelar", state="disabled")
    start_btn.pack(side="left")
    cancel_btn.pack(side="left", padx=8)
    summary = ttk.Label(run, text="", font=("TkDefaultFont", 10, "bold"))
    summary.pack(side="right")

    prog_file = ttk.Progressbar(root, maximum=100)
    prog_file.pack(fill="x", padx=10, pady=(2, 0))
    file_lbl = ttk.Label(root, text="Pronto.")
    file_lbl.pack(anchor="w", padx=10)
    prog_total = ttk.Progressbar(root, maximum=100)
    prog_total.pack(fill="x", padx=10, pady=(4, 10))

    def current_opts():
        return dict(codec=v_codec.get(), crf=int(v_crf.get()), speed=v_speed.get(), res=v_res.get(),
                    fps=v_fps.get(), amode=v_amode.get(), abr=v_abr.get(), mono=v_mono.get())

    def worker(items, opts, outmode, outdir):
        total_orig = total_final = 0
        n = len(items)
        for i, (iid, src) in enumerate(items):
            if cancel_event.is_set():
                q.put(("status", iid, "Cancelado"))
                continue
            q.put(("start", iid, i, n))
            try:
                folder = Path(outdir) if outmode == "custom" else src.parent / "comprimidos"
                dst, size = compress_file(ffmpeg, ffprobe, src, folder, opts,
                                          lambda f, t, iid=iid: q.put(("prog", iid, f, t, i, n)),
                                          cancel_event, proc_holder)
                orig = src.stat().st_size
                total_orig += orig
                total_final += size
                red = (1 - size / orig) * 100 if orig else 0
                q.put(("done", iid, size, red))
            except Cancelled:
                q.put(("status", iid, "Cancelado"))
            except Exception as e:  # erro num arquivo não derruba a fila
                q.put(("error", iid, str(e)))
        q.put(("finish", total_orig, total_final))

    def start():
        if not state["paths"]:
            messagebox.showinfo("Compressor", "Adicione pelo menos um vídeo.")
            return
        outmode, outdir = v_outmode.get(), v_outdir.get().strip()
        if outmode == "custom" and not outdir:
            messagebox.showinfo("Compressor", "Escolha a pasta de destino.")
            return
        items = [(iid, p) for iid, p in state["paths"].items()
                 if tree.set(iid, "status") not in ("Concluído",)]
        if not items:
            messagebox.showinfo("Compressor", "Todos os vídeos da lista já foram comprimidos.")
            return
        cancel_event.clear()
        state["running"] = True
        start_btn.config(state="disabled")
        cancel_btn.config(state="normal")
        summary.config(text="")
        for iid, _ in items:
            tree.set(iid, "status", "Na fila")
            tree.set(iid, "final", "")
            tree.set(iid, "red", "")
        threading.Thread(target=worker, args=(items, current_opts(), outmode, outdir), daemon=True).start()

    def cancel():
        cancel_event.set()
        p = proc_holder.get("proc")
        if p and p.poll() is None:
            p.kill()
        file_lbl.config(text="Cancelando…")

    start_btn.config(command=start)
    cancel_btn.config(command=cancel)

    def poll():
        try:
            while True:
                msg = q.get_nowait()
                kind = msg[0]
                if kind == "start":
                    _, iid, i, n = msg
                    tree.set(iid, "status", "Comprimindo…")
                    tree.see(iid)
                    prog_file["value"] = 0
                    file_lbl.config(text=f"Arquivo {i + 1} de {n}: {tree.item(iid, 'text')}")
                elif kind == "prog":
                    _, iid, frac, text, i, n = msg
                    prog_file["value"] = frac * 100
                    prog_total["value"] = (i + frac) / n * 100
                    tree.set(iid, "status", f"{frac * 100:.0f}%")
                    file_lbl.config(text=f"Arquivo {i + 1} de {n}: {tree.item(iid, 'text')}   —   {text}")
                elif kind == "done":
                    _, iid, size, red = msg
                    tree.set(iid, "final", fmt_size(size))
                    tree.set(iid, "red", f"{red:.0f}%")
                    tree.set(iid, "status", "Concluído")
                elif kind == "status":
                    tree.set(msg[1], "status", msg[2])
                elif kind == "error":
                    tree.set(msg[1], "status", "Erro")
                    tree.set(msg[1], "red", "")
                    messagebox.showwarning("Erro ao comprimir", f"{tree.item(msg[1], 'text')}\n\n{msg[2]}")
                elif kind == "finish":
                    _, to, tf = msg
                    state["running"] = False
                    start_btn.config(state="normal")
                    cancel_btn.config(state="disabled")
                    prog_file["value"] = 0
                    if cancel_event.is_set():
                        file_lbl.config(text="Cancelado.")
                    else:
                        prog_total["value"] = 100
                        file_lbl.config(text="Concluído.")
                    if to:
                        summary.config(text=f"Total: {fmt_size(to)} → {fmt_size(tf)}  (−{(1 - tf / to) * 100:.0f}%)")
        except queue.Empty:
            pass
        root.after(150, poll)

    def on_close():
        if state["running"] and not messagebox.askyesno("Sair", "Há compressão em andamento. Cancelar e sair?"):
            return
        cancel()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    apply_profile()
    poll()
    root.mainloop()


if __name__ == "__main__":
    run_gui()
