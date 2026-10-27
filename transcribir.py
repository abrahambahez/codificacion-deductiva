#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=1.40"]
# ///
"""Transcribe los audios de la carpeta compartida de Drive con el Whisper local de ZimaOS.

Lista la carpeta «Entrevistas» con rclone, descarga a `~/.cache/codificacion-deductiva/` los audios que
falten y deja una transcripción cruda por audio en `01 Transcripciones/<entrevista>/<audio>.txt`.
Un audio está pendiente si ningún txt de `01 Transcripciones` lleva su `audio_id` en el frontmatter.
El script nunca borra audios ni transcripciones.

Requiere un remoto de rclone llamado `drive` configurado con `--drive-shared-with-me`.

Uso:
    uv run Scripts/transcribir.py                  # todos los audios sin transcripción
    uv run Scripts/transcribir.py "Entrevista A" "A 3.opus"   # los que contengan ese texto en su ruta
    uv run Scripts/transcribir.py --force ...      # vuelve a transcribir y sobrescribe
    uv run Scripts/transcribir.py --dry-run        # lista qué haría sin descargar, transcribir ni escribir
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

VAULT = Path(__file__).resolve().parent.parent
TRANSCRIPCIONES = VAULT / "01 Transcripciones"
CACHE = Path.home() / ".cache" / "codificacion-deductiva"
REMOTO = "drive:Entrevistas "

AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".ogg", ".wav", ".flac", ".webm", ".mp4", ".mpeg", ".mpga", ".aac"}
BASE_URL = "http://zimaos.local:9100/v1"
MODEL = "whisper-1"
LANGUAGE = "es"
PROMPT = "Entrevista en español, con preguntas y respuestas."
CHUNK_SECONDS = 600


def leer_frontmatter(path: Path) -> dict[str, str]:
    """Pares `clave: valor` del frontmatter, sin interpretar YAML."""
    lineas = path.read_text(encoding="utf-8").splitlines()
    if not lineas or lineas[0] != "---":
        return {}
    datos = {}
    for linea in lineas[1:]:
        if linea == "---":
            break
        clave, _, valor = linea.partition(":")
        datos[clave.strip()] = valor.strip().strip('"')
    return datos


def nombre_salida(nombre: str) -> str:
    """'Nota A.mp3.m4a' → 'Nota A'; 'A 2 .mp3' → 'A 2'."""
    while (suffix := Path(nombre).suffix.lower()) in AUDIO_EXTS:
        nombre = nombre[: -len(suffix)]
    return nombre.strip()


def timestamp(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def duracion(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(out.strip())


def fragmentar(audio: Path, tmp: Path) -> list[Path]:
    """Convierte a mp3 mono 16 kHz y lo parte en bloques de CHUNK_SECONDS."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(audio), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
         "-f", "segment", "-segment_time", str(CHUNK_SECONDS), "-reset_timestamps", "1",
         str(tmp / "frag_%03d.mp3")],
        check=True,
    )
    return sorted(tmp.glob("frag_*.mp3"))


def transcribir(client, audio: Path, model: str, language: str, prompt: str) -> list[str]:
    lineas: list[str] = []
    offset = 0.0
    with tempfile.TemporaryDirectory() as tmp:
        fragmentos = fragmentar(audio, Path(tmp))
        for i, frag in enumerate(fragmentos, 1):
            print(f"    fragmento {i}/{len(fragmentos)}", flush=True)
            with frag.open("rb") as fh:
                res = client.audio.transcriptions.create(
                    model=model, file=fh, language=language, prompt=prompt,
                    response_format="verbose_json", timestamp_granularities=["segment"],
                )
            for seg in res.segments or []:
                texto = seg.text.strip()
                if texto:
                    lineas.append(f"[{timestamp(offset + seg.start)}] {texto}")
            offset += duracion(frag)
    return lineas


def listar_drive() -> list[dict]:
    """Audios de la carpeta de Drive, cada uno con su `Path` relativo a la carpeta y su `ID`."""
    out = subprocess.run(
        ["rclone", "lsjson", "-R", "--files-only", REMOTO], capture_output=True, text=True, check=True,
    ).stdout
    audios = []
    for a in json.loads(out):
        if Path(a["Name"]).suffix.lower() not in AUDIO_EXTS:
            continue
        if "/" not in a["Path"]:
            print(f"aviso: {a['Path']} no está en la subcarpeta de una entrevista, se omite", file=sys.stderr)
            continue
        audios.append(a)
    return sorted(audios, key=lambda a: a["Path"])


def entrevista(audio: dict) -> str:
    """Nombre de la subcarpeta del audio en Drive, sin los espacios sobrantes que dejan los nombres de Drive."""
    return Path(audio["Path"]).parent.as_posix().strip()


def descargar(audio: dict) -> Path:
    local = CACHE / audio["Path"]
    if not local.exists():
        subprocess.run(["rclone", "copy", f"{REMOTO}/{audio['Path']}", str(local.parent)], check=True)
    return local


def frontmatter(audio: dict) -> str:
    return (
        "---\n"
        f"audio_id: {audio['ID']}\n"
        f"audio: https://drive.google.com/file/d/{audio['ID']}/view\n"
        f"entrevista: {entrevista(audio)}\n"
        "---\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audios", nargs="*", help="texto que debe aparecer en la ruta del audio en Drive (por defecto, todos)")
    ap.add_argument("--force", action="store_true", help="vuelve a transcribir y sobrescribe")
    ap.add_argument("--dry-run", action="store_true", help="lista qué haría sin descargar, transcribir ni escribir")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--language", default=LANGUAGE)
    ap.add_argument("--prompt", default=PROMPT, help="contexto para guiar vocabulario y estilo")
    args = ap.parse_args()

    transcritos = {leer_frontmatter(p).get("audio_id"): p for p in TRANSCRIPCIONES.rglob("*.txt")}
    audios = [a for a in listar_drive() if not args.audios or any(s in a["Path"] for s in args.audios)]

    destinos: dict[Path, str] = {}
    pendientes: list[tuple[dict, Path]] = []
    for audio in audios:
        salida = transcritos.get(audio["ID"])
        if salida and not args.force:
            print(f"omitido (ya transcrito): {audio['Path']}")
            continue
        if not salida:
            salida = TRANSCRIPCIONES / entrevista(audio) / f"{nombre_salida(audio['Name'])}.txt"
            if salida in destinos:
                sys.exit(f"Colisión de nombres: '{audio['Path']}' y '{destinos[salida]}' → '{salida.relative_to(VAULT)}'")
            destinos[salida] = audio["Path"]
        pendientes.append((audio, salida))

    if args.dry_run or not pendientes:
        for audio, salida in pendientes:
            print(f"pendiente: {audio['Path']} → {salida.relative_to(VAULT)}")
        return

    from openai import OpenAI

    client = OpenAI(base_url=BASE_URL, api_key="local")
    for audio, salida in pendientes:
        print(f"transcribiendo: {audio['Path']}", flush=True)
        try:
            lineas = transcribir(client, descargar(audio), args.model, args.language, args.prompt)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            continue
        salida.parent.mkdir(parents=True, exist_ok=True)
        salida.write_text(frontmatter(audio) + "\n" + "\n".join(lineas) + "\n", encoding="utf-8")
        print(f"  → {salida.relative_to(VAULT)} ({len(lineas)} segmentos)")


if __name__ == "__main__":
    main()
