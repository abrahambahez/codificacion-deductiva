# Codificación deductiva

Scripts para llevar entrevistas de audio hasta una codificación cualitativa deductiva dentro de un vault de Obsidian. El flujo tiene tres etapas, cada una en su script: `transcribir.py` convierte los audios en transcripciones crudas con marcas de tiempo, `anonimizar.py` genera copias con seudónimos y `codificar.py` pide a Claude propuestas de codificación con un libro de códigos cerrado, que quien investiga revisa antes de aplicarlas.

## Principios de diseño

Quien investiga decide y la máquina propone. Claude nunca escribe directamente en los datos: deja una nota de revisión con una casilla por propuesta, y solo lo aprobado llega a la copia de trabajo. La codificación es deductiva porque el libro de códigos es cerrado, Claude elige indicadores de una lista fija definida por el esquema JSON de la respuesta y no puede inventar categorías.

La privacidad funciona como frontera. Solo salen hacia Claude las transcripciones de `02 Anonimizadas` marcadas con `anonimizada: true`; las crudas y el mapa de personas se quedan en local. La transcripción corre con un Whisper propio en la red local, de modo que los audios tampoco salen a un servicio externo. El mapa `.personas.yaml` es la llave de reidentificación y por eso vive fuera de este repositorio.

Los archivos se emparejan por identidad y no por nombre. Cada derivado de un audio lleva en su frontmatter el `audio_id` de Drive, lo que permite renombrar, anonimizar o desanonimizar sin perder el hilo entre transcripción, revisión y copia de trabajo.

Cada paso es idempotente y reversible. Los scripts omiten lo que ya está hecho salvo con `--force`, todos aceptan `--dry-run` para ver qué harían, y nada borra los insumos originales. Lo que inserta `codificar.py` queda marcado con un comentario `%%ia: …%%`, así `--revertir` puede quitarlo sin tocar los enlaces escritos a mano; `anonimizar.py --desanonimizar` devuelve los nombres canónicos cuando hace falta.

La codificación se integra con Obsidian en lugar de exportarse. Cada fragmento aprobado termina como párrafo propio con enlaces a su dimensión, categoría e indicador y una etiqueta `#eje/…`, de modo que los retroenlaces de cada nota del libro muestran sus fragmentos.

Cada script es autocontenido. Declara sus dependencias en el encabezado (PEP 723) y `uv` las resuelve al ejecutarlo, sin entorno virtual que mantener.

## Instalación

Los scripts esperan vivir en la carpeta `Scripts/` de un vault con esta estructura:

```
vault/
├── 01 Transcripciones/
├── 02 Anonimizadas/
├── 03 Codigos/        notas con frontmatter `tipo: codificación`
├── 04 Codificación/   notas de revisión generadas
├── 05 Datos/          copias de trabajo codificadas
├── CONTEXTO.md        objetivos de la investigación para orientar a Claude
├── .personas.yaml     mapa de personas, nunca en el repositorio
└── Scripts/           este repositorio
```

Clona el repositorio dentro del vault:

```sh
cd ruta/al/vault
git clone git@github.com:abrahambahez/codificacion-deductiva.git Scripts
```

Herramientas necesarias:

- [uv](https://docs.astral.sh/uv/) para ejecutar los scripts.
- `ffmpeg` y `ffprobe` para fragmentar los audios.
- [rclone](https://rclone.org/) con un remoto llamado `drive` configurado con `--drive-shared-with-me`, que tenga acceso a la carpeta «Entrevistas».
- Un servidor compatible con la API de transcripción de OpenAI en `http://zimaos.local:9100/v1` (la constante `BASE_URL` de `transcribir.py`).
- [Claude Code](https://claude.com/claude-code) con sesión iniciada, porque `codificar.py` lo invoca en modo headless.

## Uso

Todos los scripts aceptan textos posicionales para filtrar por ruta, `--dry-run` para simular y `--help` para ver sus opciones completas.

### Transcribir

```sh
uv run Scripts/transcribir.py                       # todos los audios sin transcripción
uv run Scripts/transcribir.py "Entrevista A"        # solo las rutas que contengan ese texto
uv run Scripts/transcribir.py --force ...           # vuelve a transcribir y sobrescribe
```

Cada audio de Drive debe estar en la subcarpeta de su entrevista. El resultado queda en `01 Transcripciones/<entrevista>/<audio>.txt`, una línea por segmento con su marca `[hh:mm:ss]`.

### Anonimizar

El mapa de personas es una lista YAML:

```yaml
- name: Nombre Completo
  aliases: [Apodo, Variante mal transcrita]
  nickname: Seudónimo
```

```sh
uv run Scripts/anonimizar.py                        # copia las transcripciones sin copia anonimizada
uv run Scripts/anonimizar.py --force                # regenera todo tras actualizar el mapa
uv run Scripts/anonimizar.py --whois Seudónimo      # imprime el nombre original
uv run Scripts/anonimizar.py --desanonimizar "Entrevista B"
```

La búsqueda ignora mayúsculas y acentos, reemplaza solo palabras completas y prefiere el término más largo cuando dos se traslapan. El script aborta si un término pertenece a dos personas o si un seudónimo contiene un nombre protegido.

### Codificar

```sh
uv run Scripts/codificar.py                         # propone para las entrevistas sin revisión
uv run Scripts/codificar.py --model opus "Entrevista B"
uv run Scripts/codificar.py --aplicar               # inserta las propuestas aprobadas
uv run Scripts/codificar.py --revertir              # quita lo que insertó el script
```

El ciclo es proponer, revisar y aplicar. Tras proponer, cada audio tiene una nota en `04 Codificación` con un callout por fragmento; marca `[x] Aprobar` en las que aceptas, y si quieres corrige el indicador del título o el eje. Al aplicar, la ruta Dimensión › Categoría se recalcula desde el libro y los fragmentos aprobados se insertan en la copia de trabajo de `05 Datos`. Si la copia cambió desde la propuesta y las marcas de tiempo ya no coinciden, el fragmento se omite con un aviso.

El libro de códigos se lee de las notas de `03 Codigos` con `tipo: codificación` en el frontmatter: cada nota es una dimensión, cada `## ` una categoría y cada `- [[Indicador]]: descripción` bajo `### Indicadores` un indicador.
