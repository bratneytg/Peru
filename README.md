# peruanos

Herramientas para probar cuánto aguanta tu red WiFi/local antes de saturarse.

## Archivos

- `wifi_stress.py` — servidor + interfaz web + cliente de consola (solo Python 3, sin dependencias). Mide la red local (LAN), sin pasar por internet.
  - Servidor: `python3 wifi_stress.py`
  - Cliente CLI: `python3 wifi_stress.py client http://IP:8765 -n 24 -t 30 -m both`
  - Varias copias en paralelo: `python3 wifi_stress.py multiclient http://IP:8765 -n 24 -c 3`
  - Ver por qué algunos dispositivos (tele, altavoces...) no notan la saturación: `python3 wifi_stress.py --ayuda-bandas`

- `wifi_web_stress.html` — versión 100% navegador, sin instalar nada. Genera tráfico contra los servidores públicos de prueba de velocidad de Cloudflare. Satura el WiFi **y** la línea de internet (el tráfico sale y vuelve por tu conexión).

Solo para usar en tu propia red.
