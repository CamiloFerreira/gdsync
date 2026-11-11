# Seguridad

## Reportar una vulnerabilidad

No abras un issue público. Usa **Security → Report a vulnerability** en este repositorio (GitHub Private Vulnerability Reporting).

## Versiones soportadas

Solo la última versión recibe parches. Las versiones por debajo de `min_supported` (en `release.json`) se consideran vulnerables y **se bloquean**.

## Cómo funcionan las actualizaciones obligatorias

Cada release publica `update.json`:

```json
{
  "version": "1.2.0",
  "min_supported": "1.2.0",
  "advisory": "Descripción breve del problema corregido",
  "assets": { "deb": {"url": "…", "sha256": "…"}, "appimage": {"url": "…", "sha256": "…"} }
}
```

- **App**: si `version actual < min_supported`, muestra un diálogo que no se puede cerrar, deja de lanzar sincronizaciones y ofrece *Actualizar ahora*.
- **CLI / timer systemd**: `gdsync sync` consulta `update.json` (como mucho cada 6 h, con caché offline). Si la versión es vulnerable, no sincroniza, escribe el error en el log y envía una notificación crítica.
- **Integridad**: la app solo descarga desde `github.com/CamiloFerreira/gdsync/releases/download/` y verifica el SHA-256 antes de instalar.

## Publicar un parche de seguridad (mantenedor)

1. Corrige el problema.
2. Sube `VERSION`, por ejemplo a `1.2.1`.
3. En `release.json`, pon `"min_supported": "1.2.1"` y una descripción en `"advisory"`.
4. `git push` a `main`. El workflow publica el release, y todas las instalaciones anteriores quedan obligadas a actualizar.
