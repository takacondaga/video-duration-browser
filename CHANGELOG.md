# Changelog

Todos los cambios relevantes de este proyecto se documentan aquí.
El formato sigue loosely [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/)
y el proyecto usa [Versionado Semántico](https://semver.org/lang/es/).

## [1.1.0] - 2026-11-19

### Agregado

- Vista de **duplicados** que agrupa videos por la huella de contenido ya
  calculada, con detalle de las copias presentes y no disponibles.
- **Renombrado y organización masiva** con vista previa de los cambios antes
  de aplicarlos. Nunca sobrescribe archivos existentes y revierte los
  renombrados en disco si falla la actualización de la base de datos.
- **Filtros guardados** con nombre, persistidos en la tabla `meta` de la base
  de datos local.
- **Tema oscuro** opcional, recordado entre sesiones.
- Columnas **Creado** y **Modificado**, ordenables en ambos sentidos, con
  migración de base v2→v3 y backfill sin recalcular huellas.
- Versión visible en el título de la ventana y en **Acerca de…**.
- `LICENSE` (MIT), este `CHANGELOG` y workflow de CI que corre los tests.
- Script `build_exe.py` para empaquetar la aplicación con PyInstaller,
  incluyendo `ffprobe.exe` cuando está disponible.

### Cambiado

- `find_ffprobe()` prioriza la carpeta de la aplicación antes que el `PATH`,
  para que el `.exe` funcione sin instalar FFmpeg.
- La barra de herramientas y los menúes **Ver** y **Herramientas** agrupan
  las nuevas funciones; el diálogo de ayuda documenta su uso.
- Suite ampliada de 101 a 131 tests, todos en verde.

## [1.0.0]

### Agregado

- Indexador de videos por disco (serial/UUID) con duración leída con
  `ffprobe`, tamaño, carpeta y fechas.
- Notas y etiquetas asociadas a la huella de contenido, exportables e
  importables en TSV.
- Estados `Presente`, `Falta`, `Disco desconectado` y `Sin verificar`, con
  filtro para ocultar no disponibles y poda segura de ubicaciones viejas.
- Escaneo por etapas con barra de progreso, tooltips y ayuda de primer uso.
- Código separado en el paquete `vdb/` y migraciones de base v1→v2.
