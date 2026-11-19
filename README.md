# Video Duration Browser

Indexador de videos de escritorio con interfaz Tkinter. Permite catalogar
videos de discos y carpetas, ordenarlos por duración, tamaño o nombre, buscar
por ruta o por notas, y asociar notas de texto plano a cada contenido.

## Características

- Índice de videos con duración, tamaño, nombre, carpeta y disco.
- Orden y filtros por duración, tamaño, nombre, disco, ruta y notas.
- Notas de texto plano y etiquetas asociadas al **contenido** del archivo
  (huella de tamaño + hash de fragmentos), no a su ruta. Si el video se mueve,
  se renombra o cambia la letra de la unidad, la nota lo sigue.
- Catálogo de discos identificados por número de serie de volumen (Windows) o
  UUID (Linux), no por letra de unidad.
- Los videos de discos desconectados siguen apareciendo en gris, indicando qué
  disco hay que conectar.
- Columna **Estado** que indica si cada archivo está `Presente`, `Falta`, en
  un `Disco desconectado` o `Sin verificar`, con un filtro para ocultar los no
  disponibles.
- Tabla principal con barras de desplazamiento vertical y horizontal, para ver
  todas las columnas aunque la ventana sea angosta. Los anchos de columna son
  fijos, así que el pulgar horizontal sigue visible al maximizar la ventana.
- Ruta completa de cada video al pasar el mouse por la columna **Carpeta**, o
  desde el menú contextual → **Ver ruta completa…** (texto seleccionable y
  botón **Copiar ruta**). El doble clic sigue abriendo el video.
- Tooltips en botones, filtros y campos; el botón **Ayuda / Primer uso…** (o el
  menú **Ayuda → Primer uso…**) muestra el orden de los botones para el primer
  uso y para los posteriores.
- Si un video se movió de ubicación, la copia `Presente` gana: la ruta vieja se
  quita del índice y la nota sigue unida al contenido (se actualiza solo la
  ruta guardada, nunca el texto ni la clave de la nota). Si ambas copias están
  presentes, se conservan las dos. La poda ocurre al terminar un escaneo o una
  verificación; una verificación cancelada no quita nada.
- Escaneo con leyenda de progreso permanente (`Etapa X de 7`), barra de avance y
  conteo de carpetas/archivos/videos mientras se recorre el disco.
- Exportación e importación de notas en un `.txt` (TSV) legible fuera del
  programa.
- Reutilización de duraciones ya leídas por huella de contenido, sin volver a
  ejecutar `ffprobe`.

## Requisitos

- Python 3.8 o superior.
- Tkinter. En Windows viene incluido con el instalador oficial de Python; en
  Linux suele instalarse con `sudo apt install python3-tk`.
- `ffprobe`, incluido en [FFmpeg](https://ffmpeg.org/). Debe estar en el `PATH`
  o copiado junto al script (`ffprobe.exe` en Windows).

## Uso

```powershell
python "video_duration_browser 1.0.py"
```

También se puede ejecutar como módulo desde la carpeta del proyecto:

```powershell
python -m vdb
```

Tests (sin dependencias externas):

```powershell
python -m unittest discover -s tests -t .
```

Flujo básico (también disponible en **Ayuda / Primer uso…**):

Primer uso:

1. **Escanear carpeta/disco…** para elegir la carpeta o el disco a catalogar
   (necesitás `ffprobe.exe` junto al script o en el `PATH`).
2. Seleccionar videos y usar **Anotar… (F2)** para agregar notas o etiquetas;
   la nota queda asociada al contenido, no a la ruta.
3. **Verificar ahora** para comprobar qué archivos siguen presentes.

Usos posteriores:

1. Conectar los discos que quieras consultar.
2. **Actualizar índice** para releer los discos ya configurados. Si cambiaste
   las carpetas, usá **Discos…** para elegir las raíces.
3. **Verificar ahora** después de mover o borrar videos; se puede cancelar y no
   modifica las notas.
4. Usar los filtros (**Buscar**, **Notas**, **Duración**, **Ocultar no
   disponibles**) y hacer clic en un encabezado para ordenar.
5. Doble clic abre el video; clic derecho → **Ver ruta completa…** muestra la
   ruta sin cortar.

Atajos principales:

- `F2`: anotar la selección.
- `Enter`: abrir el video seleccionado.
- `Supr`: enviar a la Papelera.
- `Mayús+Supr`: eliminar definitivamente.

## Base de datos local

Al ejecutarlo, el programa crea `videos_index.db` junto al script. Esa base
contiene el índice, las rutas locales, identificadores de discos, huellas y
notas privadas. **No se versiona en este repositorio**: está excluida por
`.gitignore`, igual que cualquier respaldo o archivo temporal de la base.

La base se crea junto al lanzador, no dentro de `vdb/`. Si movés el lanzador y
la carpeta `vdb/` a otra ubicación, se crea una base nueva y vacía en la nueva
ubicación. Para conservar el índice, mové también `videos_index.db` o usá
Exportar/Importar notas para trasladar las notas.

## Disponibilidad de los archivos

La columna **Estado** no modifica el índice: solo comprueba, sin borrar nada,
si cada archivo sigue en la ruta donde fue indexado.

| Estado | Significado |
|---|---|
| `Presente` | El disco está conectado y la ruta indexada existe. |
| `Falta` | El disco está conectado, pero la ruta ya no existe. |
| `Disco desconectado` | El volumen no está conectado; no se puede comprobar. |
| `Sin verificar` | Aún no se comprobó, es una unidad de red o no se pudo leer. |

- **Verificar ahora** recorre los archivos en segundo plano, muestra el progreso
  y se puede cancelar.
- El tick **Ocultar no disponibles** oculta `Falta` y `Disco desconectado`, y
  lanza la verificación automáticamente si hace falta.
- Las unidades de red no se verifican automáticamente para evitar bloqueos; si no
  figuran entre los discos conectados, aparecen como `Disco desconectado`.
- Un disco reconectado pasa a `Presente` o `Falta` después de verificar.
- Si el mismo contenido aparece en dos ubicaciones y una está `Presente`, se
  muestra solo esa: la copia no disponible se oculta en la vista y al terminar
  el escaneo o la verificación se quita del índice. La nota no se pierde: va con
  la huella, y solo se actualiza su ruta guardada. Si ambas copias están
  presentes, se conservan las dos. Si no hay ninguna `Presente`, no se quita
  ninguna fila.
- Una verificación cancelada no poda nada: el estado es parcial y no se puede
  decidir qué ubicación ganó.
- Escanear un disco sigue quitando del índice los archivos que ya no existen;
  las notas se conservan. Para revisar sin tocar el índice, usá esta columna.

### Etapas del escaneo

La barra de estado indica siempre la etapa actual, por ejemplo
`Etapa 2 de 7: Recorriendo carpetas — 412 carpetas, 3600 videos — F:/Series`.

| Etapa | Qué hace |
|---|---|
| 1 | Preparando disco (ruta y volumen) |
| 2 | Recorriendo carpetas y contando videos |
| 3 | Leyendo el índice anterior |
| 4 | Comparando con el índice |
| 5 | Leyendo duraciones y huellas (`i / total`) |
| 6 | Guardando índice y notas |
| 7 | Consolidando ubicaciones y mostrando el resumen |

Cuando se escanean varios discos, se agrega el prefijo `Disco i de N`.

## Notas y privacidad

- Las notas nunca se borran automáticamente.
- Dos copias idénticas del mismo video comparten la nota.
- Exportar/Importar notas genera o lee un `.txt` (TSV) legible fuera del
  programa.
- La base de datos y sus respaldos quedan fuera del control de versiones.

## Contenido del repositorio

```text
Nuevo Archivo Total/
  video_duration_browser 1.0.py   # lanzador de la aplicación
  vdb/                            # código de la aplicación
    config.py                     # constantes y rutas
    text.py                       # texto, formato y extensiones
    paths.py                      # rutas relativas al disco
    volumes.py                    # identificación de discos
    database.py                   # esquema, migraciones y notas
    presence.py                   # verificación de disponibilidad
    scan.py                       # escaneo, huellas y duraciones
    fileops.py                    # eliminación de archivos
    ui.py                         # interfaz Tkinter
  tests/                          # tests de la capa de datos y utilidades
  README.md                       # este archivo
  .gitignore                      # excluye base de datos, respaldos y temporales
```

No se incluyen bases de datos, capturas, videos ni binarios de FFmpeg.
