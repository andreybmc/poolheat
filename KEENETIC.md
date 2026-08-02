# poolheat на Keenetic Peak (OPKG / Entware)

Да, размещать можно **пакетом Entware (`.ipk`)** — это как раз то, что Keenetic называет OPKG.

## Установка с GitHub (рекомендуется)

Репозиторий: `https://github.com/andreybmc/poolheat`  
(если URL другой — подставьте свой после `gh repo create`)

На роутере (Entware уже стоит):

```sh
opkg update
opkg install git git-http ca-certificates python3 python3-pip

cd /opt
# если каталог уже есть: cd /opt/poolheat && git pull
git clone https://github.com/andreybmc/poolheat.git
cd poolheat
sh packaging/entware/install-from-git.sh
```

Обновление:

```sh
cd /opt/poolheat
git pull
sh packaging/entware/install-from-git.sh
```

UI: `http://<ip-keenetic>:8787/`  
Конфиг: `/opt/etc/poolheat/config.json`

## Схема

```
Keenetic Peak (aarch64 + Entware)
  /opt/bin/poolheatd              → launcher
  /opt/lib/poolheat/serve.py      → сервис
  /opt/share/poolheat/www/        → UI
  /opt/etc/poolheat/config.json   → miner IP, port, password
  /opt/var/poolheat/              → history.db, logs
  :8787                           → Web UI (LAN)
         │
         └── TCP 4028 ──► Whatsminer M63 (192.168.1.10)
```

## 0. Что нужно на Peak

1. Компонент **OPKG** в прошивке Keenetic.
2. **Entware** на USB **или** во встроенную storage (KeeneticOS 3.7+).
3. Для Peak / aarch64 installer обычно:

```text
https://bin.entware.net/aarch64-k3.10/installer/aarch64-installer.tar.gz
```

(точный URL — из [документации Keenetic OPKG](https://support.keenetic.com/) под вашу модель)

4. После Entware:

```sh
opkg update
opkg install python3 python3-pip
```

## 1. Собрать `.ipk` (на Mac)

```bash
cd ~/Documents/poolheat/packaging/entware
chmod +x build-ipk.sh
./build-ipk.sh
```

Готовый файл:

```text
~/Documents/poolheat/dist/poolheat_0.1.0-1_aarch64-3.10.ipk
```

Если нет `ar`, скрипт положит `.tar.gz` — тогда ручная установка (ниже).

## 2. Установка на роутер

Скопировать ipk на роутер (USB / scp) и:

```sh
opkg install /path/to/poolheat_0.1.0-1_aarch64-3.10.ipk
```

Или:

```sh
opkg install python3 python3-pip
pip3 install pycryptodome passlib
opkg install ./poolheat_0.1.0-1_aarch64-3.10.ipk
```

`postinst` сам поставит crypto-зависимости и попробует стартовать сервис.

## 3. Конфиг

```sh
vi /opt/etc/poolheat/config.json
```

```json
{
  "bind": "0.0.0.0",
  "http_port": 8787,
  "miner_host": "192.168.1.10",
  "miner_port": 4028,
  "api_password": "admin"
}
```

Перезапуск:

```sh
/opt/etc/init.d/S99poolheat-standalone restart
# или
/opt/etc/init.d/S99poolheat restart
```

## 4. Открыть UI

С ПК в LAN:

```text
http://<ip-keenetic>:8787/
```

IP роутера — обычно `192.168.1.1`.

### Firewall

Если UI не открывается — в Keenetic может понадобиться разрешить входящий TCP **8787** на LAN (или OPKG «в интернет-центр» / firewall rules). Для LAN-only часто хватает bind `0.0.0.0`.

**Не** пробрасывайте 8787 в интернет без auth/VPN.

## 5. Управление сервисом

```sh
/opt/etc/init.d/S99poolheat-standalone status
/opt/etc/init.d/S99poolheat-standalone start
/opt/etc/init.d/S99poolheat-standalone stop
/opt/etc/init.d/S99poolheat-standalone restart

# лог
tail -f /opt/var/poolheat/poolheat.log
```

## 6. Ручная установка без `.ipk` (если ar/ipk не собрался)

На роутере:

```sh
mkdir -p /opt/lib/poolheat /opt/share/poolheat/www /opt/etc/poolheat /opt/var/poolheat /opt/bin
# скопировать файлы из packaging/entware/opt/ ...
chmod +x /opt/bin/poolheatd /opt/etc/init.d/S99poolheat-standalone
opkg install python3 python3-pip
pip3 install pycryptodome passlib
/opt/etc/init.d/S99poolheat-standalone start
```

## 7. Ограничения Peak

| Тема | Замечание |
|------|-----------|
| CPU/RAM | Один poller + лёгкий UI — ок; не 10 майнеров |
| USB | Если Entware на USB — SPOF; лучше internal storage |
| Python | Тяжелее Go-бинаря; для MVP нормально |
| Write API | Нужны `pycryptodome` + `passlib` |
| power_pct | Temporary / LOW_HASH — предпочтительнее mode |

## 8. Дальше (production)

1. Сменить `api_password` и UI token.
2. Только LAN / WireGuard / Keenetic Cloud.
3. 48h `dry_run` перед thrashing writes.
4. Опционально: переписать сервис на **static Go arm64** — один файл, без pip.

## Файлы в репо

```text
Documents/poolheat/
  ui-demo/                 # разработка на Mac
  packaging/entware/       # дерево OPKG
    CONTROL/
    opt/
    build-ipk.sh
  dist/                    # готовый .ipk после сборки
  KEENETIC.md              # этот runbook
```
