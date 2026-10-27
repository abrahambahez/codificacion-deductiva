#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6"]
# ///
"""Genera en `02 Anonimizadas` una copia anonimizada de cada transcripción cruda de `01 Transcripciones`.

Las transcripciones crudas no se modifican. La copia sustituye nombres y alias por su seudónimo en el
contenido, en el nombre del archivo y en el nombre de la carpeta de la entrevista, y añade
`anonimizada: true` al frontmatter original. Es la única entrada que sale hacia Claude.

El mapa de personas es un YAML con una lista de entradas:

    - name: Nombre Completo
      aliases: [Apodo, Variante mal transcrita]
      nickname: Seudónimo

El mapa es la llave de reidentificación: vive en `.personas.yaml` en la raíz del vault (Obsidian Sync
no sincroniza archivos ocultos fuera de .obsidian), o donde indique --mapa.

La búsqueda ignora mayúsculas y acentos ("Maria" encuentra "maría") y solo reemplaza palabras
completas. Si dos términos se traslapan, gana el más largo ("Ana Pérez" antes que "Ana").

Uso:
    uv run Scripts/anonimizar.py                  # copia las transcripciones que aún no tienen copia
    uv run Scripts/anonimizar.py --force          # regenera todas las copias tras actualizar el mapa
    uv run Scripts/anonimizar.py --dry-run        # valida el mapa y cuenta sustituciones sin escribir
    uv run Scripts/anonimizar.py --whois Seudónimo  # imprime el nombre original
    uv run Scripts/anonimizar.py --desanonimizar "Entrevista B"   # vuelve a los nombres reales
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml

VAULT = Path(__file__).resolve().parent.parent
TRANSCRIPCIONES = VAULT / "01 Transcripciones"
ANONIMIZADAS = VAULT / "02 Anonimizadas"
REVISION = VAULT / "04 Codificación"
DATOS = VAULT / "05 Datos"
MAPA = VAULT / ".personas.yaml"

VARIANTES = {
    "a": "aáàäâ", "e": "eéèëê", "i": "iíìïî", "o": "oóòöô", "u": "uúùüû", "n": "nñ", "c": "cç",
}
ANTES = r"(?<![^\W_])"
DESPUES = r"(?![^\W_])"


@dataclass
class Persona:
    name: str
    aliases: list[str]
    nickname: str

    @property
    def terminos(self) -> list[str]:
        return [self.name, *self.aliases]


def normalizar(texto: str) -> str:
    """Clave de comparación: sin acentos, sin mayúsculas y con espacios colapsados."""
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFD", texto) if not unicodedata.combining(c)
    )
    return " ".join(sin_acentos.casefold().split())


def patron_termino(termino: str) -> str:
    partes = []
    for c in normalizar(termino):
        if c == " ":
            partes.append(r"\s+")
        elif c in VARIANTES:
            partes.append(f"[{VARIANTES[c]}]")
        else:
            partes.append(re.escape(c))
    return "".join(partes)


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


def cargar_mapa(ruta: Path) -> list[Persona]:
    if not ruta.is_file():
        sys.exit(f"No existe el mapa de personas: {ruta}")
    datos = yaml.safe_load(ruta.read_text(encoding="utf-8")) or []
    if not isinstance(datos, list):
        sys.exit(f"{ruta}: se esperaba una lista de personas en la raíz del YAML")

    personas, errores = [], []
    for i, entrada in enumerate(datos, 1):
        if not isinstance(entrada, dict):
            errores.append(f"entrada {i}: no es un objeto")
            continue
        name, nickname = entrada.get("name"), entrada.get("nickname")
        aliases = entrada.get("aliases") or []
        if not isinstance(name, str) or not name.strip():
            errores.append(f"entrada {i}: falta 'name'")
        if not isinstance(nickname, str) or not nickname.strip():
            errores.append(f"entrada {i}: falta 'nickname'")
        if not isinstance(aliases, list) or not all(isinstance(a, str) and a.strip() for a in aliases):
            errores.append(f"entrada {i}: 'aliases' debe ser una lista de textos no vacíos")
        if not errores:
            personas.append(Persona(name.strip(), [a.strip() for a in aliases], nickname.strip()))
    if errores:
        sys.exit("Mapa inválido:\n  " + "\n  ".join(errores))
    return personas


def validar(personas: list[Persona], regex: re.Pattern[str]) -> None:
    """Aborta si un término o seudónimo es ambiguo o si un seudónimo contiene un término protegido."""
    errores = []
    duenos: dict[str, int] = {}
    for i, p in enumerate(personas):
        for t in p.terminos:
            clave = normalizar(t)
            j = duenos.setdefault(clave, i)
            if j != i:
                errores.append(
                    f"'{t}' aparece en dos personas distintas "
                    f"(seudónimos '{personas[j].nickname}' y '{p.nickname}')"
                )

    seudonimos: dict[str, str] = {}
    for p in personas:
        clave = normalizar(p.nickname)
        if clave in seudonimos:
            errores.append(f"el seudónimo '{p.nickname}' está repetido")
        seudonimos[clave] = p.nickname
        if m := regex.search(p.nickname):
            errores.append(f"el seudónimo '{p.nickname}' contiene el término protegido '{m.group()}'")

    if errores:
        sys.exit("Mapa inválido:\n  " + "\n  ".join(dict.fromkeys(errores)))

    for p in personas:
        for t in p.terminos:
            if len(normalizar(t)) < 3:
                print(f"aviso: el término '{t}' es muy corto y puede reemplazar palabras comunes",
                      file=sys.stderr)


def compilar(reemplazo: dict[str, str]) -> tuple[re.Pattern[str], dict[str, str]]:
    """Una sola regex para todos los términos de la tabla `término normalizado → sustituto`, el más largo primero."""
    alternativas = sorted(reemplazo, key=len, reverse=True)
    cuerpo = "|".join(patron_termino(t) for t in alternativas)
    return re.compile(f"{ANTES}(?:{cuerpo}){DESPUES}", re.IGNORECASE), reemplazo


def whois(personas: list[Persona], nickname: str) -> None:
    clave = normalizar(nickname)
    for p in personas:
        if normalizar(p.nickname) == clave:
            print(p.name)
            return
    sys.exit(f"No hay nadie con el seudónimo '{nickname}'")


def sustituir(regex: re.Pattern[str], reemplazo: dict[str, str], texto: str) -> tuple[str, Counter[str]]:
    conteo: Counter[str] = Counter()

    def sustituto(m: re.Match[str]) -> str:
        nuevo = reemplazo[normalizar(m.group())]
        conteo[nuevo] += 1
        return nuevo

    return regex.sub(sustituto, unicodedata.normalize("NFC", texto)), conteo


def anonimizar(regex: re.Pattern[str], reemplazo: dict[str, str], force: bool, dry_run: bool) -> None:
    copias = {leer_frontmatter(p).get("audio_id"): p for p in ANONIMIZADAS.rglob("*.txt")}
    destinos: dict[Path, Path] = {}
    total = generadas = 0
    for cruda in sorted(TRANSCRIPCIONES.rglob("*.txt")):
        audio_id = leer_frontmatter(cruda).get("audio_id")
        previa = copias.get(audio_id)
        if previa and not force:
            continue
        if previa and leer_frontmatter(previa).get("anonimizada") == "false":
            print(f"omitido (desanonimizada): {previa.relative_to(VAULT)}")
            continue

        relativa = cruda.relative_to(TRANSCRIPCIONES).as_posix()
        destino = ANONIMIZADAS / sustituir(regex, reemplazo, relativa)[0]
        if destino in destinos:
            sys.exit(f"Colisión: '{cruda.relative_to(VAULT)}' y '{destinos[destino].relative_to(VAULT)}' "
                     f"→ '{destino.relative_to(VAULT)}'")
        destinos[destino] = cruda

        texto, conteo = sustituir(regex, reemplazo, cruda.read_text(encoding="utf-8"))
        texto = texto.replace("\n---\n", "\nanonimizada: true\n---\n", 1)
        total += conteo.total()
        generadas += 1
        detalle = ", ".join(f"{nick} ×{n}" for nick, n in conteo.most_common()) or "sin sustituciones"
        print(f"{'se generaría' if dry_run else 'generada'}: {destino.relative_to(VAULT)} ({detalle})")
        if dry_run:
            continue
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(texto, encoding="utf-8")
        if previa and previa != destino:
            previa.unlink()

    print(f"{generadas} copias, {total} sustituciones" + (" (dry-run, nada escrito)" if dry_run else ""))


def marcar_desanonimizada(texto: str) -> str:
    return texto.replace("\nanonimizada: true\n", "\nanonimizada: false\n", 1)


def desanonimizar(personas: list[Persona], seleccion: list[str], dry_run: bool) -> None:
    """Devuelve los nombres canónicos a los derivados de las entrevistas de `02 Anonimizadas`.

    Renombra los txt anonimizados (y su carpeta), las revisiones y las copias de trabajo; sustituye
    los seudónimos dentro de las copias de trabajo y marca txt y copias con `anonimizada: false`.
    Los archivos se emparejan por `audio_id`.
    """
    regex, reemplazo = compilar({normalizar(p.nickname): p.name for p in personas})
    copias = {leer_frontmatter(p).get("audio_id"): p for p in DATOS.glob("*.md")}
    revisiones = {leer_frontmatter(p).get("audio_id"): p for p in REVISION.glob("*.md")}

    cambios: list[tuple[Path, Path, str | None]] = []
    carpetas = []
    for carpeta in sorted(d for d in ANONIMIZADAS.iterdir() if d.is_dir()):
        if seleccion and not any(s in carpeta.name for s in seleccion):
            continue
        carpetas.append(carpeta)
        for txt in sorted(carpeta.glob("*.txt")):
            relativa = sustituir(regex, reemplazo, txt.relative_to(ANONIMIZADAS).as_posix())[0]
            cambios.append((txt, ANONIMIZADAS / relativa, marcar_desanonimizada(txt.read_text(encoding="utf-8"))))
            audio_id = leer_frontmatter(txt).get("audio_id")
            if copia := copias.get(audio_id):
                texto, conteo = sustituir(regex, reemplazo, copia.read_text(encoding="utf-8"))
                detalle = ", ".join(f"{nombre} ×{n}" for nombre, n in conteo.most_common()) or "sin sustituciones"
                print(f"{'se sustituiría' if dry_run else 'sustituido'}: {copia.relative_to(VAULT)} ({detalle})")
                cambios.append((copia, copia.with_name(sustituir(regex, reemplazo, copia.name)[0]), marcar_desanonimizada(texto)))
            if revision := revisiones.get(audio_id):
                cambios.append((revision, revision.with_name(sustituir(regex, reemplazo, revision.name)[0]), None))

    choques = [d for o, d, _ in cambios if d != o and d.exists()]
    if choques:
        sys.exit("No se puede renombrar, ya existen:\n  " + "\n  ".join(str(d.relative_to(VAULT)) for d in choques))

    for origen, destino, texto in cambios:
        if destino != origen:
            print(f"{'se renombraría' if dry_run else 'renombrado'}: {origen.relative_to(VAULT)} → {destino.relative_to(VAULT)}")
        if dry_run:
            continue
        if texto is not None:
            origen.write_text(texto, encoding="utf-8")
        destino.parent.mkdir(parents=True, exist_ok=True)
        origen.rename(destino)
    if not dry_run:
        for carpeta in carpetas:
            if not any(carpeta.iterdir()):
                carpeta.rmdir()
    print(f"{len(carpetas)} entrevistas" + (" (dry-run, nada escrito)" if dry_run else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mapa", type=Path, default=MAPA, help=f"YAML de personas (por defecto {MAPA})")
    ap.add_argument("--whois", metavar="SEUDÓNIMO", help="imprime el nombre original de un seudónimo")
    ap.add_argument("--force", action="store_true", help="regenera las copias que ya existen")
    ap.add_argument("--desanonimizar", nargs="*", metavar="ENTREVISTA",
                    help="devuelve los nombres canónicos a las entrevistas indicadas (todas si no se indica ninguna)")
    ap.add_argument("--dry-run", action="store_true", help="solo valida y cuenta, sin escribir")
    args = ap.parse_args()

    personas = cargar_mapa(args.mapa.expanduser())
    regex, reemplazo = compilar({normalizar(t): p.nickname for p in personas for t in p.terminos})
    validar(personas, regex)

    if args.whois:
        whois(personas, args.whois)
    elif args.desanonimizar is not None:
        desanonimizar(personas, args.desanonimizar, args.dry_run)
    else:
        anonimizar(regex, reemplazo, args.force, args.dry_run)


if __name__ == "__main__":
    main()
