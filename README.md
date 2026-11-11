# gdsync: Google Drive Sync para Linux

App de escritorio (GTK4 + libadwaita) con icono en la barra superior que mantiene **una carpeta** de Google Drive sincronizada en ambos sentidos con una carpeta de tu equipo. Por debajo usa [`rclone bisync`](https://rclone.org/bisync/) y un timer de `systemd --user`, así que el sync sigue funcionando aunque cierres la app.

## Instalación

Descarga el paquete desde [Releases](https://github.com/CamiloFerreira/gdsync/releases/latest):

**Ubuntu / Debian (.deb)**
```bash
sudo apt install ./gdsync_*_all.deb
```

**AppImage** (necesita GTK4 y libadwaita ≥ 1.5 en el sistema: Ubuntu 24.04+, Fedora 39+, Debian 13+)
```bash
chmod +x gdsync-*-x86_64.AppImage && ./gdsync-*-x86_64.AppImage
```

**Desde el código**
```bash
git clone https://github.com/CamiloFerreira/gdsync && cd gdsync
./gdsync desktop        # añade la app al menú de aplicaciones
```

En GNOME, el icono de la bandeja necesita la extensión *AppIndicator*. Ubuntu la trae activada.

## Uso

1. Abre **Google Drive Sync**. Si rclone no está instalado, la app lo instala en `~/.local/bin` sin pedir sudo.
2. Ve a **Ajustes → Cuentas → +** para conectar tu cuenta de Google (se abre el navegador).
3. En **Carpeta**, navega por tu Drive, pulsa *Usar esta carpeta*, elige la carpeta local y la frecuencia, y luego **Guardar**.
4. Listo: queda activada la sincronización automática y la app arranca con la sesión.

| Pestaña | Contenido |
|---|---|
| Estado | Estado general, Sincronizar ahora, Simular, sync automático, próxima y última ejecución, cuota de Drive |
| Carpeta | Navegador de Drive, carpeta local y frecuencia |
| Actividad | Log en vivo con colores, cancelar, forzar resync |
| Ajustes | Inicio con la sesión, notificaciones, cuentas, exclusiones, versión de rclone |

Si cierras la ventana, la app sigue en la bandeja. Para cerrarla del todo usa **Salir** en el menú del icono, o `Ctrl+Q`.

### CLI

El mismo motor se puede usar desde la terminal (`gdsync`, o `./gdsync.AppImage cli` en el AppImage):

```
gdsync setup | sync | dry-run | resync | status | logs | enable | disable | version
```

## Actualizaciones y seguridad

- La app revisa si hay versiones nuevas al arrancar y cada 6 horas. Si hay una, muestra un aviso con el botón **Actualizar**:
  - **AppImage**: se reemplaza a sí misma.
  - **.deb**: se instala con `pkexec apt`.
  - **Código fuente**: te lleva al release.
- La descarga se verifica con **SHA-256** y solo se aceptan URLs de los releases de este repo.
- **Actualizaciones obligatorias**: si tu versión es menor que `min_supported` (o sea, tiene una vulnerabilidad conocida), la app bloquea la sincronización con un diálogo que no se puede cerrar. El CLI y el timer también se niegan a sincronizar hasta que actualices. Más detalles en [SECURITY.md](SECURITY.md).

## Publicar versiones (mantenedor)

Los releases salen automáticamente de `main`:

1. Haz tus cambios y sube el número en `VERSION` (por ejemplo `1.0.0` → `1.1.0`).
2. `git push`. GitHub Actions valida el código, compila el **.deb** y el **AppImage**, genera `update.json` y `SHA256SUMS`, y crea el release `vX.Y.Z` con notas automáticas.

Si un push no cambia `VERSION`, solo se valida el código y no se publica nada.

## Archivos

- Config: `~/.config/gdsync/config`
- Exclusiones: `~/.config/gdsync/filters.txt`
- Log: `~/.local/state/gdsync/sync.log`
- Units: `~/.config/systemd/user/gdsync.{service,timer}`

## Notas

- **Conflictos**: gana el archivo más reciente; el otro se guarda como `archivo.conflict1`.
- Los Google Docs/Sheets/Slides nativos se omiten.
- **Protección**: bisync se detiene si un sync va a borrar más del 50% de los archivos.
- **Sync sin sesión iniciada**: `sudo loginctl enable-linger $USER`.
- El client_id compartido de rclone tiene límites de velocidad. Si sincronizas mucho, conviene [crear el tuyo](https://rclone.org/drive/#making-your-own-client-id).

Licencia MIT.
