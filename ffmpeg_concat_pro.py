#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FFmpeg Concat Pro v2
====================
Junta vídeos priorizando ZERO perda de qualidade.

Estratégia (do melhor para o pior caso):
  1. Arquivos compatíveis         -> cópia direta de stream (-c copy), bit a bit idêntico.
  2. Arquivo com defeito leve     -> remux de reparo (-c copy), sem recodificar.
  3. Só o áudio é diferente       -> áudio ajustado, vídeo continua intacto (-c:v copy).
  4. Vídeo realmente incompatível -> só ESSE arquivo é recodificado (CRF baixo) para
                                     casar com o padrão da maioria.
  5. Depois de unir, o resultado é VERIFICADO (duração, áudio, erros de leitura).
     Se reprovar, tenta outro método de união (via MPEG-TS) e, em último caso,
     recodifica tudo com qualidade visualmente sem perdas (opcional).

Requisitos: Python 3.8+, ffmpeg e ffprobe no PATH (ou na mesma pasta do script).
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# --------------------------------------------------------------------------- #
# Utilitários de ambiente
# --------------------------------------------------------------------------- #
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_tool(name: str):
    exe = name + (".exe" if os.name == "nt" else "")
    local = Path(__file__).resolve().parent / exe
    if local.exists():
        return str(local)
    return shutil.which(name)


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")


def run(cmd):
    return subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
    )


def ff_base():
    return [FFMPEG, "-y", "-nostdin", "-hide_banner", "-v", "error",
            "-progress", "pipe:1", "-nostats"]


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def tail(text: str, n: int = 6) -> str:
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return "\n".join(lines[-n:]) if lines else "(sem detalhes)"


def fmt_size(b: float) -> str:
    return f"{b / 1024 ** 3:.2f} GB" if b >= 1024 ** 3 else f"{b / 1024 ** 2:.1f} MB"


# --------------------------------------------------------------------------- #
# Modelo de mídia
# --------------------------------------------------------------------------- #
@dataclass
class MediaInfo:
    path: str
    size: int = 0
    duration: float = 0.0
    created: float = 0.0
    mtime: float = 0.0
    readable: bool = True
    has_video: bool = False
    vcodec: str = ""
    pix_fmt: str = ""
    width: int = 0
    height: int = 0
    rotation: int = 0
    fps: float = 0.0
    fps_str: str = "0/1"
    has_audio: bool = False
    acodec: str = ""
    sample_rate: int = 0
    channels: int = 0
    errors: list = field(default_factory=list)
    video_corrupt: bool = False
    audio_corrupt: bool = False

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def disp_dims(self):
        return (self.height, self.width) if self.rotation in (90, 270) else (self.width, self.height)

    def vsig(self):
        return (self.vcodec, self.pix_fmt, self.width, self.height, self.rotation, round(self.fps, 2))

    def asig(self):
        return (self.acodec, self.sample_rate, self.channels)


def _tag(tags: dict, key: str):
    for k, v in (tags or {}).items():
        if k.lower() == key:
            return v
    return None


def _parse_ctime(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt.timestamp() if dt.year >= 1990 else 0.0
    except Exception:
        return 0.0


def _parse_fps(s):
    try:
        f = Fraction(s)
        return float(f) if f > 0 else 0.0
    except (ValueError, ZeroDivisionError, TypeError):
        return 0.0


def _rotation(stream):
    rot = _tag(stream.get("tags", {}), "rotate")
    if rot is not None:
        try:
            return int(float(rot)) % 360
        except ValueError:
            pass
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            try:
                return int(round(float(sd["rotation"]))) % 360
            except ValueError:
                pass
    return 0


def probe(path: str) -> MediaInfo:
    info = MediaInfo(path=path)
    try:
        info.size = os.path.getsize(path)
        info.mtime = os.path.getmtime(path)
    except OSError as e:
        info.readable = False
        info.errors.append(f"Sem acesso ao arquivo: {e}")
        return info

    r = run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path])
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    streams = data.get("streams") or []
    if not streams:
        info.readable = False
        info.errors.append("Arquivo ilegível (cabeçalho/moov ausente ou corrompido)")
        return info

    fmt = data.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic", 0)), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    try:
        info.duration = float(fmt.get("duration") or 0)
    except ValueError:
        info.duration = 0.0
    if info.duration <= 0 and v:
        try:
            info.duration = float(v.get("duration") or 0)
        except ValueError:
            pass

    info.created = _parse_ctime(_tag(fmt.get("tags", {}), "creation_time")) or info.mtime

    if v:
        info.has_video = True
        info.vcodec = v.get("codec_name", "")
        info.pix_fmt = v.get("pix_fmt", "")
        info.width = int(v.get("width") or 0)
        info.height = int(v.get("height") or 0)
        info.rotation = _rotation(v)
        info.fps = _parse_fps(v.get("r_frame_rate")) or _parse_fps(v.get("avg_frame_rate"))
        info.fps_str = v.get("r_frame_rate") if _parse_fps(v.get("r_frame_rate")) else v.get("avg_frame_rate", "0/1")
    else:
        info.errors.append("Sem trilha de vídeo")

    if a:
        info.has_audio = True
        info.acodec = a.get("codec_name", "")
        info.sample_rate = int(a.get("sample_rate") or 0)
        info.channels = int(a.get("channels") or 0)

    if v and info.duration <= 0:
        info.errors.append("Duração inválida/ausente")
    return info


def scan_integrity(path: str, deep: bool = False):
    """Lê o arquivo inteiro e devolve as mensagens de erro do ffmpeg.
    deep=False: lê pacotes (rápido, pega container/timestamps quebrados).
    deep=True : decodifica tudo (lento, pega frames corrompidos)."""
    cmd = [FFMPEG, "-v", "error", "-nostdin", "-i", path, "-map", "0:v:0?", "-map", "0:a:0?"]
    if not deep:
        cmd += ["-c", "copy"]
    cmd += ["-f", "null", "-"]
    r = run(cmd)
    lines = [l.strip() for l in r.stderr.splitlines() if l.strip()]
    if r.returncode != 0 and not lines:
        lines.append(f"ffmpeg retornou código {r.returncode}")
    return lines


def scan_streams(path: str, deep: bool, has_audio: bool):
    """Devolve (erros_video, erros_audio, erros_container).
    Modo profundo separa vídeo e áudio para reparar só o que está ruim."""
    if not deep:
        return [], [], scan_integrity(path, False)

    def one(m):
        r = run([FFMPEG, "-v", "error", "-nostdin", "-i", path, "-map", m, "-f", "null", "-"])
        return [l.strip() for l in r.stderr.splitlines() if l.strip()]
    return one("0:v:0"), (one("0:a:0") if has_audio else []), []


# --------------------------------------------------------------------------- #
# Plano: o que fazer com cada arquivo
# --------------------------------------------------------------------------- #
@dataclass
class Target:
    vcodec: str
    pix_fmt: str
    width: int
    height: int
    rotation: int
    fps: float
    fps_str: str
    acodec: str = ""
    sample_rate: int = 0
    channels: int = 0

    def asig(self):
        return (self.acodec, self.sample_rate, self.channels)


@dataclass
class Action:
    kind: str      # copy | repair | audio | video | skip
    reason: str = ""


def _x264(crf):
    return ["-crf", str(crf), "-preset", "medium"]


def _x265(crf):
    return ["-crf", str(crf), "-preset", "medium", "-tag:v", "hvc1", "-x265-params", "log-level=error"]


VIDEO_ENC = {"h264": ("libx264", _x264), "hevc": ("libx265", _x265)}
AUDIO_ENC = {"aac": "aac", "mp3": "libmp3lame", "ac3": "ac3", "eac3": "eac3",
             "opus": "libopus", "flac": "flac", "alac": "alac", "pcm_s16le": "pcm_s16le"}
LOSSY_AUDIO = {"aac", "mp3", "ac3", "eac3", "opus"}
MP4_VIDEO = {"h264", "hevc", "av1", "mpeg4"}
MP4_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac", "opus"}


def classify(i: MediaInfo, tv: Target) -> Action:
    if not i.readable or not i.has_video:
        return Action("skip", i.errors[0] if i.errors else "sem trilha de vídeo")

    vd = []
    if i.vcodec != tv.vcodec:
        vd.append(f"codec {i.vcodec}")
    if i.pix_fmt != tv.pix_fmt:
        vd.append(f"pix_fmt {i.pix_fmt}")
    if (i.width, i.height) != (tv.width, tv.height) or i.rotation != tv.rotation:
        vd.append(f"resolução {i.width}x{i.height}" + (f" rot {i.rotation}°" if i.rotation else ""))
    if round(i.fps, 2) != round(tv.fps, 2):
        vd.append(f"{i.fps:.2f} fps")
    if vd:
        return Action("video", "recodificar: " + ", ".join(vd))

    a_ok = (i.has_audio and i.asig() == tv.asig()) if tv.acodec else not i.has_audio
    if i.audio_corrupt:
        a_ok = False
    if not a_ok:
        if i.audio_corrupt:
            why = "áudio corrompido, será regravado"
        elif not i.has_audio:
            why = "sem áudio → silêncio"
        else:
            why = f"{i.acodec} {i.sample_rate}Hz {i.channels}ch"
        return Action("audio", f"ajustar áudio ({why}); vídeo intacto")

    if i.errors:
        return Action("repair", "reparo sem perdas: " + i.errors[0][:70])
    return Action("copy", "cópia direta")


def make_plan(items):
    """Escolhe o padrão da MAIORIA como alvo (menos arquivos recodificados)."""
    ok = [i for i in items if i.readable and i.has_video]
    if not ok:
        return None, [classify(i, None) for i in items]

    vc = Counter(i.vsig() for i in ok)
    top = vc.most_common(1)[0][0]
    ref = next(i for i in ok if i.vsig() == top)
    tv = Target(ref.vcodec, ref.pix_fmt, ref.width, ref.height, ref.rotation, ref.fps, ref.fps_str)
    if len(vc) > 1 and ref.rotation:          # normaliza orientação quando precisa recodificar
        tv.width, tv.height = ref.disp_dims
        tv.rotation = 0

    with_audio = [i for i in ok if i.has_audio]
    if with_audio:
        ac = Counter(i.asig() for i in with_audio)
        atop = ac.most_common(1)[0][0]
        tv.acodec, tv.sample_rate, tv.channels = next(i for i in with_audio if i.asig() == atop).asig()

    acts = [classify(i, tv) for i in items]
    changed = False
    if any(a.kind == "video" for a in acts) and tv.vcodec not in VIDEO_ENC:
        tv.vcodec, changed = "h264", True
    if any(a.kind in ("video", "audio") for a in acts) and tv.acodec and tv.acodec not in AUDIO_ENC:
        tv.acodec, changed = "aac", True
    if changed:
        acts = [classify(i, tv) for i in items]
    return tv, acts


def suggest_ext(tv: Target) -> str:
    if tv and tv.vcodec in MP4_VIDEO and (not tv.acodec or tv.acodec in MP4_AUDIO):
        return ".mp4"
    return ".mkv"


def mov_flags(ext: str):
    return ["-movflags", "+faststart"] if ext in (".mp4", ".mov") else []


def write_list(path: Path, files):
    with open(path, "w", encoding="utf-8") as f:
        for p in files:
            q = Path(p).resolve().as_posix().replace("'", "'\\''")
            f.write(f"file '{q}'\n")


# --------------------------------------------------------------------------- #
# Pipeline (roda em thread; nunca mexe na interface diretamente)
# --------------------------------------------------------------------------- #
class Cancelled(Exception):
    pass


class PipelineError(Exception):
    pass


class Pipeline:
    def __init__(self, items, actions, tv, out_path, crf, deep, last_resort, ui, cancel):
        self.items, self.actions, self.tv = items, actions, tv
        self.out_path = out_path
        self.ext = Path(out_path).suffix.lower() or ".mp4"
        self.crf, self.deep, self.last_resort_ok = crf, deep, last_resort
        self.ui, self.cancel = ui, cancel
        self.total = 1.0
        self.total_dur = 0.0

    # ---- helpers ----
    def log(self, msg):
        self.ui(("log", msg))

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def set_progress(self, done):
        self.ui(("progress", max(0.0, min(100.0, done / self.total * 100 if self.total else 0))))

    def ffmpeg(self, cmd, duration, offset, scale=1.0):
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
        err_buf = []
        t = threading.Thread(target=lambda: err_buf.extend(proc.stderr), daemon=True)
        t.start()
        for line in proc.stdout:
            if self.cancel.is_set():
                proc.kill()
                break
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    sec = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                self.set_progress(offset + max(0.0, min(sec, duration)) * scale)
        proc.wait()
        t.join(timeout=3)
        self.check()
        return proc.returncode == 0, "".join(err_buf)

    # ---- construção dos comandos ----
    def audio_args(self):
        tv = self.tv
        args = ["-c:a", AUDIO_ENC[tv.acodec], "-ar", str(tv.sample_rate), "-ac", str(tv.channels)]
        if tv.acodec in LOSSY_AUDIO:
            args += ["-b:a", "192k" if tv.channels == 1 else ("320k" if tv.channels == 2 else "448k")]
        return args

    def make_cmd(self, it, kind, out, force=False):
        tv = self.tv
        cmd = ff_base() + ["-err_detect", "ignore_err"]
        if kind != "audio" or force:
            # genpts/discardcorrupt consertam timestamps quebrados, mas NÃO devem ser usados
            # quando só o áudio é refeito (bagunçam os timestamps do vídeo copiado)
            cmd += ["-fflags", "+genpts+discardcorrupt"]
        cmd += ["-i", it.path]
        want_audio = bool(tv.acodec)
        silence = want_audio and not it.has_audio
        if silence:
            layout = {1: "mono", 2: "stereo", 6: "5.1"}.get(tv.channels, "stereo")
            cmd += ["-f", "lavfi", "-i", f"anullsrc=r={tv.sample_rate}:cl={layout}"]
        cmd += ["-map", "0:v:0"]
        if want_audio:
            cmd += ["-map", "1:a:0" if silence else "0:a:0"]

        if kind == "video" or force:
            enc, opts = VIDEO_ENC[tv.vcodec]
            W, H = tv.width, tv.height
            vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={tv.fps_str}")
            cmd += ["-vf", vf, "-c:v", enc] + opts(self.crf)
            if tv.pix_fmt:
                cmd += ["-pix_fmt", tv.pix_fmt]
        else:
            cmd += ["-c:v", "copy"]

        if want_audio:
            a_match = it.has_audio and it.asig() == tv.asig() and not it.audio_corrupt
            if a_match and not force and kind in ("repair", "video"):
                cmd += ["-c:a", "copy"]
            else:
                cmd += self.audio_args()
                if not silence:
                    cmd += ["-af", "aresample=async=1:first_pts=0"]
        if silence or (kind == "audio" and not force):
            cmd += ["-shortest"]   # áudio novo não pode passar da duração do vídeo
        if kind == "repair" and not force:
            cmd += ["-avoid_negative_ts", "make_zero"]
        return cmd + ["-max_muxing_queue_size", "9999"] + mov_flags(self.ext) + [str(out)]

    # ---- etapas ----
    def prepare(self, n, it, act, tmp, offset):
        out = tmp / f"seg_{n:04d}{self.ext}"
        kind = act.kind
        self.log(f"[{n}] {it.name}: {act.reason}")
        if kind in ("repair", "audio"):
            ok, err = self.ffmpeg(self.make_cmd(it, kind, out), it.duration, offset)
            if ok and kind == "repair" and self.deep and scan_streams(str(out), True, False)[0]:
                self.log("    ainda há frames corrompidos após o remux → recodificando só este arquivo")
                ok = False
            elif not ok:
                self.log("    falhou, escalando para recodificação:\n" + tail(err, 3))
            if ok:
                return str(out)
        ok, err = self.ffmpeg(self.make_cmd(it, "video", out), it.duration, offset)
        if ok:
            return str(out)
        raise PipelineError(f"Falha ao processar {it.name}:\n{tail(err)}")

    def concat_demuxer(self, segs, out, tmp, offset):
        lst = tmp / "list.txt"
        write_list(lst, [p for p, _ in segs])
        cmd = ff_base() + ["-f", "concat", "-safe", "0", "-fflags", "+genpts", "-i", str(lst),
                           "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                           "-avoid_negative_ts", "make_zero"] + mov_flags(self.ext) + [str(out)]
        return self.ffmpeg(cmd, self.total_dur, offset)

    def concat_ts(self, segs, out, tmp, offset):
        """Passa por MPEG-TS (parâmetros do codec ficam dentro do stream): resolve
        glitches quando os arquivos têm SPS/PPS diferentes. Continua sem perdas."""
        bsf = {"h264": "h264_mp4toannexb", "hevc": "hevc_mp4toannexb"}.get(self.tv.vcodec)
        ts_files, done = [], offset
        try:
            for n, (p, dur) in enumerate(segs, 1):
                self.check()
                ts = tmp / f"ts_{n:04d}.ts"
                cmd = ff_base() + ["-i", p, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"]
                cmd += (["-bsf:v", bsf] if bsf else []) + ["-f", "mpegts", str(ts)]
                ok, err = self.ffmpeg(cmd, dur, done, 0.5)
                if not ok:
                    return False, err
                ts_files.append(str(ts))
                done += dur * 0.5
            lst = tmp / "list_ts.txt"
            write_list(lst, ts_files)
            cmd = ff_base() + ["-f", "concat", "-safe", "0", "-i", str(lst),
                               "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"]
            if self.tv.acodec == "aac":
                cmd += ["-bsf:a", "aac_adtstoasc"]
            cmd += mov_flags(self.ext) + [str(out)]
            return self.ffmpeg(cmd, self.total_dur, done, 0.5)
        finally:
            for f in ts_files:
                try:
                    os.remove(f)
                except OSError:
                    pass

    def verify(self, path, expected):
        problems = []
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return ["arquivo de saída vazio"]
        info = probe(str(path))
        if not info.readable or not info.has_video:
            return ["saída ilegível ou sem vídeo"]
        if self.tv.acodec and not info.has_audio:
            problems.append("faixa de áudio ausente")
        tol = max(1.5, expected * 0.01)
        if abs(info.duration - expected) > tol:
            problems.append(f"duração {info.duration:.1f}s difere do esperado {expected:.1f}s")
        errs = scan_integrity(str(path), deep=False)
        if errs:
            problems.append(f"erros de leitura: {errs[0][:100]}")
        return problems

    def finish(self, tmp_result):
        os.replace(tmp_result, self.out_path)
        return True, f"Vídeo gerado com sucesso:\n{self.out_path}\n\nTamanho: {fmt_size(os.path.getsize(self.out_path))}"

    def last_resort(self, jobs, tmp, final_tmp):
        self.log("⚠ ÚLTIMO RECURSO: recodificando tudo com qualidade visualmente sem perdas...")
        self.total = self.total_dur * 2
        segs, done = [], 0.0
        for n, (it, _a) in enumerate(jobs, 1):
            self.check()
            out = tmp / f"full_{n:04d}{self.ext}"
            ok, err = self.ffmpeg(self.make_cmd(it, "video", out, force=True), it.duration, done)
            if not ok:
                raise PipelineError(f"Falha ao recodificar {it.name}:\n{tail(err)}")
            segs.append((str(out), it.duration))
            done += it.duration
        ok, err = self.concat_demuxer(segs, final_tmp, tmp, done)
        if ok:
            problems = self.verify(final_tmp, self.total_dur)
            if not problems:
                return self.finish(final_tmp)
            err = "; ".join(problems)
        raise PipelineError("Mesmo recodificando tudo a união falhou:\n" + tail(err))

    def _run(self, tmp):
        jobs = [(it, a) for it, a in zip(self.items, self.actions) if a.kind != "skip"]
        self.total_dur = sum(it.duration for it, _ in jobs)
        prep_dur = sum(it.duration for it, a in jobs if a.kind != "copy")
        self.total = prep_dur + self.total_dur

        segments, done = [], 0.0
        for n, (it, a) in enumerate(jobs, 1):
            self.check()
            if a.kind == "copy":
                segments.append((it.path, it.duration))
                continue
            seg = self.prepare(n, it, a, tmp, done)
            real = probe(seg).duration   # cabeçalho de arquivo truncado mente; mede o trecho reparado
            segments.append((seg, real if real > 0 else it.duration))
            done += it.duration
        self.total_dur = sum(d for _, d in segments)
        self.total = prep_dur + self.total_dur

        final_tmp = tmp / f"result{self.ext}"
        methods = [("concat demuxer (cópia direta)", self.concat_demuxer),
                   ("remux via MPEG-TS (cópia direta)", self.concat_ts)]
        for label, fn in methods:
            self.check()
            self.log(f"Unindo com {label}...")
            ok, err = fn(segments, final_tmp, tmp, prep_dur)
            if not ok:
                self.log("    falhou:\n" + tail(err, 3))
                continue
            self.log("Verificando o resultado...")
            problems = self.verify(final_tmp, self.total_dur)
            if not problems:
                return self.finish(final_tmp)
            self.log("    verificação reprovou: " + "; ".join(problems))

        if not self.last_resort_ok:
            raise PipelineError("A união sem perdas falhou em todos os métodos.\n"
                                "Ative 'Permitir recodificação como último recurso' para forçar a entrega.")
        return self.last_resort(jobs, tmp, final_tmp)

    def run(self):
        out_dir = os.path.dirname(os.path.abspath(self.out_path))
        tmp = None
        try:
            tmp = Path(tempfile.mkdtemp(prefix=".concat_tmp_", dir=out_dir))
            ok, msg = self._run(tmp)
            self.ui(("done", ok, msg))
        except Cancelled:
            self.ui(("done", False, "Operação cancelada."))
        except PipelineError as e:
            self.ui(("done", False, str(e)))
        except Exception as e:  # noqa
            self.ui(("done", False, f"Erro inesperado: {e}"))
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #
SORTS = {
    "Data de gravação (metadados)": lambda i: i.created,
    "Data de modificação do arquivo": lambda i: i.mtime,
    "Nome (ordem natural)": lambda i: natural_key(i.name),
}
QUALITY = {
    "Visualmente sem perdas (CRF 12)": 12,
    "Alta qualidade (CRF 16)": 16,
    "Equilibrada (CRF 18)": 18,
}
ICONS = {"copy": "✅", "repair": "🔧", "audio": "🎚", "video": "🎞", "skip": "⛔"}


class App:
    def __init__(self, root):
        self.root = root
        root.title("FFmpeg Concat Pro v2 — sem perda de qualidade")
        root.geometry("1050x760")

        self.items: list[MediaInfo] = []
        self.actions: list[Action] = []
        self.target = None
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False

        self.build_ui()
        self.root.after(80, self.poll)

        if not FFMPEG or not FFPROBE:
            messagebox.showerror("FFmpeg não encontrado",
                                 "ffmpeg/ffprobe não foram encontrados.\n"
                                 "Instale e coloque no PATH, ou copie os executáveis para a pasta do script.")

    # ---- UI ----
    def build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill=tk.X)
        self.btn_add = ttk.Button(top, text="1. Adicionar vídeos", command=self.add_files)
        self.btn_add.pack(side=tk.LEFT, padx=3)
        ttk.Button(top, text="▲", width=3, command=lambda: self.move(-1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="▼", width=3, command=lambda: self.move(1)).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="Remover", command=self.remove_selected).pack(side=tk.LEFT, padx=3)
        ttk.Button(top, text="Limpar", command=self.clear_list).pack(side=tk.LEFT, padx=3)
        ttk.Label(top, text="Ordenar por:").pack(side=tk.LEFT, padx=(15, 3))
        self.sort_var = tk.StringVar(value=list(SORTS)[0])
        cb = ttk.Combobox(top, textvariable=self.sort_var, values=list(SORTS), state="readonly", width=30)
        cb.pack(side=tk.LEFT)
        cb.bind("<<ComboboxSelected>>", lambda e: (self.apply_sort(), self.refresh()))

        cols = ("ordem", "nome", "res", "fps", "codecs", "dur", "status")
        frame = ttk.Frame(self.root)
        frame.pack(fill=tk.BOTH, expand=True, padx=8)
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=12)
        for c, t, w, a in [("ordem", "#", 35, tk.CENTER), ("nome", "Arquivo", 230, tk.W),
                           ("res", "Resolução", 90, tk.CENTER), ("fps", "FPS", 55, tk.CENTER),
                           ("codecs", "Vídeo/Áudio", 110, tk.CENTER), ("dur", "Duração", 75, tk.CENTER),
                           ("status", "Plano para este arquivo", 400, tk.W)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a)
        sb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.tag_configure("copy", foreground="#1a7f37")
        self.tree.tag_configure("repair", foreground="#9a6700")
        self.tree.tag_configure("audio", foreground="#9a6700")
        self.tree.tag_configure("video", foreground="#bc4c00")
        self.tree.tag_configure("skip", foreground="#cf222e")

        opt = ttk.LabelFrame(self.root, text="Opções", padding=8)
        opt.pack(fill=tk.X, padx=8, pady=6)
        self.var_verify = tk.BooleanVar(value=True)
        self.var_deep = tk.BooleanVar(value=False)
        self.var_last = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Verificar integridade ao adicionar (lê os arquivos)",
                        variable=self.var_verify).grid(row=0, column=0, sticky=tk.W, padx=5)
        ttk.Checkbutton(opt, text="Decodificação completa (mais lenta, acha frames corrompidos)",
                        variable=self.var_deep).grid(row=0, column=1, sticky=tk.W, padx=5)
        ttk.Checkbutton(opt, text="Permitir recodificação como último recurso",
                        variable=self.var_last).grid(row=1, column=0, sticky=tk.W, padx=5)
        qf = ttk.Frame(opt)
        qf.grid(row=1, column=1, sticky=tk.W, padx=5)
        ttk.Label(qf, text="Qualidade se precisar recodificar:").pack(side=tk.LEFT)
        self.q_var = tk.StringVar(value=list(QUALITY)[0])
        ttk.Combobox(qf, textvariable=self.q_var, values=list(QUALITY), state="readonly",
                     width=30).pack(side=tk.LEFT, padx=5)

        dash = ttk.LabelFrame(self.root, text="Previsão & status", padding=8)
        dash.pack(fill=tk.X, padx=8, pady=4)
        self.lbl_size = ttk.Label(dash, text="Tamanho (soma das fontes): —", font=("Arial", 10, "bold"))
        self.lbl_dur = ttk.Label(dash, text="Duração: 00:00:00", font=("Arial", 10, "bold"))
        self.lbl_plan = ttk.Label(dash, text="Plano: —", font=("Arial", 10, "bold"))
        self.lbl_size.grid(row=0, column=0, sticky=tk.W, padx=8)
        self.lbl_dur.grid(row=0, column=1, sticky=tk.W, padx=8)
        self.lbl_plan.grid(row=1, column=0, columnspan=2, sticky=tk.W, padx=8, pady=(4, 0))
        self.btn_run = ttk.Button(dash, text="2. Iniciar concatenação", command=self.start, state=tk.DISABLED)
        self.btn_run.grid(row=0, column=2, rowspan=2, padx=10)
        self.btn_cancel = ttk.Button(dash, text="Cancelar", command=self.cancel.set, state=tk.DISABLED)
        self.btn_cancel.grid(row=0, column=3, rowspan=2, padx=4)
        dash.columnconfigure(1, weight=1)

        self.progress = ttk.Progressbar(self.root, maximum=100)
        self.progress.pack(fill=tk.X, padx=8, pady=(4, 0))
        self.lbl_status = ttk.Label(self.root, text="Pronto.")
        self.lbl_status.pack(anchor=tk.W, padx=10)

        lf = ttk.Frame(self.root)
        lf.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        self.log_box = tk.Text(lf, height=8, state=tk.DISABLED, wrap=tk.WORD)
        lsb = ttk.Scrollbar(lf, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=lsb.set)
        self.log_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lsb.pack(side=tk.RIGHT, fill=tk.Y)

    def log(self, msg):
        self.log_box.config(state=tk.NORMAL)
        self.log_box.insert(tk.END, msg + "\n")
        self.log_box.see(tk.END)
        self.log_box.config(state=tk.DISABLED)

    def set_busy(self, busy):
        self.busy = busy
        self.btn_add.config(state=tk.DISABLED if busy else tk.NORMAL)

    # ---- fila de eventos (threads -> UI) ----
    def poll(self):
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def handle(self, msg):
        kind = msg[0]
        if kind == "log":
            self.log(msg[1])
        elif kind == "status":
            self.lbl_status.config(text=msg[1])
        elif kind == "progress":
            self.progress["value"] = msg[1]
            self.lbl_status.config(text=f"Processando... {msg[1]:.0f}%")
        elif kind == "info":
            self.items.append(msg[1])
        elif kind == "analysis_done":
            self.set_busy(False)
            self.apply_sort()
            self.refresh()
            self.lbl_status.config(text="Análise concluída.")
        elif kind == "done":
            _, ok, text = msg
            self.set_busy(False)
            self.btn_cancel.config(state=tk.DISABLED)
            self.refresh()
            self.lbl_status.config(text="Concluído." if ok else "Finalizado com problemas.")
            if ok:
                self.progress["value"] = 100
                messagebox.showinfo("Sucesso", text)
            else:
                messagebox.showerror("Não foi possível concluir", text)

    # ---- lista ----
    def add_files(self):
        files = filedialog.askopenfilenames(
            title="Selecione os vídeos",
            filetypes=[("Vídeos", "*.mp4 *.mov *.mkv *.m4v *.avi *.ts *.mts *.m2ts"), ("Todos", "*.*")])
        known = {os.path.abspath(i.path) for i in self.items}
        new = [os.path.abspath(f) for f in files if os.path.abspath(f) not in known]
        if not new:
            return
        self.set_busy(True)
        self.btn_run.config(state=tk.DISABLED)
        threading.Thread(target=self.analyze, args=(new, self.var_verify.get(), self.var_deep.get()),
                         daemon=True).start()

    def analyze(self, paths, verify, deep):
        for n, p in enumerate(paths, 1):
            self.q.put(("status", f"Analisando {n}/{len(paths)}: {os.path.basename(p)}"))
            info = probe(p)
            if info.readable and info.has_video and verify:
                v_err, a_err, c_err = scan_streams(p, deep, info.has_audio)
                if v_err:
                    info.video_corrupt = True
                    info.errors.append(f"vídeo corrompido ({len(v_err)} erros): {v_err[0][:60]}")
                if a_err:
                    info.audio_corrupt = True
                    info.errors.append(f"áudio corrompido ({len(a_err)} erros): {a_err[0][:60]}")
                if c_err:
                    info.errors.append(f"{len(c_err)} erro(s) de leitura: {c_err[0][:70]}")
            self.q.put(("info", info))
        self.q.put(("analysis_done",))

    def apply_sort(self):
        self.items.sort(key=SORTS[self.sort_var.get()])

    def selected_index(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def move(self, d):
        i = self.selected_index()
        if i is None or self.busy:
            return
        j = i + d
        if 0 <= j < len(self.items):
            self.items[i], self.items[j] = self.items[j], self.items[i]
            self.refresh()
            self.tree.selection_set(str(j))

    def remove_selected(self):
        if self.busy:
            return
        for s in sorted((int(x) for x in self.tree.selection()), reverse=True):
            del self.items[s]
        self.refresh()

    def clear_list(self):
        if self.busy:
            return
        self.items.clear()
        self.refresh()
        self.progress["value"] = 0

    def refresh(self):
        self.target, self.actions = make_plan(self.items)
        self.tree.delete(*self.tree.get_children())
        counts = Counter()
        size = dur = 0
        for n, (it, a) in enumerate(zip(self.items, self.actions)):
            counts[a.kind] += 1
            if a.kind != "skip":
                size += it.size
                dur += it.duration
            res = f"{it.width}x{it.height}" if it.has_video else "-"
            self.tree.insert("", "end", iid=str(n), tags=(a.kind,), values=(
                n + 1, it.name, res, f"{it.fps:.2f}" if it.has_video else "-",
                f"{it.vcodec or '-'}/{it.acodec or '-'}", str(timedelta(seconds=int(it.duration))),
                f"{ICONS[a.kind]} {a.reason}"))
        self.lbl_size.config(text=f"Tamanho (soma das fontes): {fmt_size(size)}")
        self.lbl_dur.config(text=f"Duração: {timedelta(seconds=int(dur))}")
        if not self.items:
            self.lbl_plan.config(text="Plano: —")
        else:
            parts = [f"{counts['copy']} cópia direta", f"{counts['repair']} reparo",
                     f"{counts['audio']} ajuste de áudio", f"{counts['video']} recodificação"]
            if counts["skip"]:
                parts.append(f"{counts['skip']} ignorado(s)")
            lossless = counts["video"] == 0
            badge = "🟢 vídeo 100% preservado" if lossless else "🟡 alguns arquivos serão recodificados"
            self.lbl_plan.config(text=f"Plano: {' · '.join(parts)}   {badge}")
        runnable = len(self.items) - counts["skip"]
        self.btn_run.config(state=tk.NORMAL if runnable > 0 and not self.busy else tk.DISABLED)

    # ---- execução ----
    def start(self):
        if self.busy or not self.target:
            return
        jobs = [(i, a) for i, a in zip(self.items, self.actions) if a.kind != "skip"]
        skipped = [i for i, a in zip(self.items, self.actions) if a.kind == "skip"]
        if skipped and not messagebox.askyesno(
                "Arquivos ignorados",
                f"{len(skipped)} arquivo(s) ilegível(is) ou sem vídeo serão IGNORADOS:\n\n" +
                "\n".join(i.name for i in skipped[:8]) + "\n\nContinuar mesmo assim?"):
            return
        n_video = sum(1 for _, a in jobs if a.kind == "video")
        if n_video and not messagebox.askokcancel(
                "Recodificação necessária",
                f"{n_video} arquivo(s) têm vídeo incompatível e serão recodificados em qualidade "
                "visualmente sem perdas (os demais continuam bit a bit idênticos).\n\nContinuar?"):
            return

        ext = suggest_ext(self.target)
        first_dir = os.path.dirname(jobs[0][0].path)
        out = filedialog.asksaveasfilename(
            title="Salvar vídeo final", initialdir=first_dir, initialfile="video_final_unido" + ext,
            defaultextension=ext, filetypes=[("MP4", "*.mp4"), ("Matroska", "*.mkv"), ("MOV", "*.mov")])
        if not out:
            return
        out = os.path.abspath(out)
        if out in {os.path.abspath(i.path) for i, _ in jobs}:
            messagebox.showerror("Destino inválido", "O arquivo final não pode sobrescrever um dos vídeos de origem.")
            return
        if Path(out).suffix.lower() != ".mkv" and suggest_ext(self.target) == ".mkv":
            out = str(Path(out).with_suffix(".mkv"))
            self.log("Codecs não cabem em MP4/MOV com segurança → salvando como .mkv")

        need = sum(i.size for i, _ in jobs) * 1.1 + sum(i.size for i, a in jobs if a.kind != "copy") * 1.5
        free = shutil.disk_usage(os.path.dirname(out)).free
        if free < need and not messagebox.askyesno(
                "Pouco espaço em disco",
                f"Espaço livre: {fmt_size(free)}. Estimativa necessária: {fmt_size(need)}.\nContinuar?"):
            return

        self.cancel.clear()
        self.set_busy(True)
        self.btn_run.config(state=tk.DISABLED)
        self.btn_cancel.config(state=tk.NORMAL)
        self.progress["value"] = 0
        self.log(f"▶ Iniciando → {out}")
        pipe = Pipeline([i for i, _ in jobs] + skipped, [a for _, a in jobs] + [Action("skip")] * len(skipped),
                        self.target, out, QUALITY[self.q_var.get()], self.var_deep.get(),
                        self.var_last.get(), self.q.put, self.cancel)
        threading.Thread(target=pipe.run, daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
