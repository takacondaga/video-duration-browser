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

Flujo básico:

1. **Escanear carpeta/disco** para agregar un origen al índice.
2. **Discos** para ver los orígenes conocidos, escanear o actualizar.
3. **Actualizar índice** para releer los discos conectados.
4. Seleccionar videos y usar **Anotar (F2)** para agregar notas o etiquetas.
5. Usar los filtros y hacer clic en los encabezados para ordenar.

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

Si movés el script a otra carpeta, se crea una base nueva y vacía en la nueva
ubicación. Para conservar el índice, mové también `videos_index.db` o usá
Exportar/Importar notas para trasladar las notas.

## Notas y privacidad

- Las notas nunca se borran automáticamente.
- Dos copias idénticas del mismo video comparten la nota.
- Exportar/Importar notas genera o lee un `.txt` (TSV) legible fuera del
  programa.
- La base de datos y sus respaldos quedan fuera del control de versiones.

## Contenido del repositorio

```text
Nuevo Archivo Total/
  video_duration_browser 1.0.py   # aplicación completa
  README.md                       # este archivo
  .gitignore                      # excluye base de datos, respaldos y temporales
```

No se incluyen bases de datos, capturas, videos ni binarios de FFmpeg.
