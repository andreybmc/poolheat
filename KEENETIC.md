# poolheat_WM на Keenetic (установка с GitHub)

Репозиторий: **https://github.com/andreybmc/poolheat**  
Версия: см. файл [`VERSION`](./VERSION) в корне (сейчас **0.3.0**).

Контроллер Whatsminer (pool heat) ставится в **Entware** на Peak / совместимый Keenetic.  
UI: `http://<ip-роутера>:8787/`

---

## 1. Что нужно

| Требование | Комментарий |
|------------|-------------|
| OPKG / Entware | Компонент OPKG в прошивке + Entware (USB или internal storage) |
| Сеть | `opkg` и `git`/`wget` до GitHub; LAN до майнера `:4028` |
| Python 3 | `opkg install python3 python3-pip` |
| Зависимости API | `pycryptodome`, `passlib` (ставит install-скрипт) |

Installer Entware для Peak (aarch64), ориентир:

```text
https://bin.entware.net/aarch64-k3.10/installer/aarch64-installer.tar.gz
```

(точный URL — [документация Keenetic OPKG](https://support.keenetic.com/) для вашей модели.)

---

## 2. Первая установка с GitHub (рекомендуется)

SSH на роутер (shell Entware):

```sh
opkg update
opkg install git git-http ca-certificates python3 python3-pip

cd /opt
# если каталог уже есть — см. «Обновление» ниже
git clone https://github.com/andreybmc/poolheat.git
cd poolheat
sh packaging/entware/install-from-git.sh
```

Скрипт:

- копирует `ui-demo/serve.py` → `/opt/lib/poolheat/serve.py`
- копирует `ui-demo/index.html` → `/opt/share/poolheat/www/index.html`
- ставит `VERSION`, launcher `poolheatd`, init `S99poolheat*`
- **не** перезаписывает существующий `/opt/etc/poolheat/config.json`
- ставит Python-зависимости и перезапускает сервис

Открыть UI:

```text
http://192.168.1.1:8787/
```

(подставьте IP вашего Keenetic)

### Конфиг майнера

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
# или, если есть rc.func:
/opt/etc/init.d/S99poolheat restart
```

---

## 3. Обновление

### A) Через SSH (git)

```sh
cd /opt/poolheat
git pull
sh packaging/entware/install-from-git.sh
```

### B) Через Web UI (без SSH)

Начиная с **0.2+** (и **0.3.0**):

1. Вкладка **Инфо**
2. **Проверить обновления** — сравнение локального `VERSION` с GitHub:
   - latest **Release**
   - **tags** (`v0.3.0` …)
   - файл **`VERSION`** на ветке `main`
3. **Установить** — скачивается tarball с GitHub (`codeload` / archive), обновляются `serve.py`, UI, `VERSION`, сервис перезапускается

Конфиг `/opt/etc/poolheat/` и данные `/opt/var/poolheat/` **сохраняются**.

Если кнопка «Установить» неактивна:

- на GitHub должен быть свежий **`VERSION`** / **tag** / **Release** выше локальной версии;
- роутер должен достучаться до `api.github.com` и `codeload.github.com` (или `github.com`).

### C) Конкретный tag

```sh
cd /opt/poolheat
git fetch --tags
git checkout v0.3.0
sh packaging/entware/install-from-git.sh
```

Или в UI после «Проверить» — установка выбранного ref (latest / tag / branch).

---

## 4. Схема файлов

```text
Keenetic (Entware)
  /opt/bin/poolheatd                 launcher
  /opt/lib/poolheat/serve.py         backend
  /opt/lib/poolheat/VERSION          installed version
  /opt/share/poolheat/www/index.html UI
  /opt/etc/poolheat/config.json      miner IP, password, bind
  /opt/var/poolheat/                 history.db, logs, zone_map_config.json
  :8787  → Web UI (LAN only recommended)
         └── TCP 4028 → Whatsminer
```

Клон репозитория (для git-обновлений):

```text
/opt/poolheat/                       git clone
  ui-demo/                           исходники
  packaging/entware/install-from-git.sh
  VERSION
  KEENETIC.md
```

---

## 5. Управление сервисом

```sh
/opt/etc/init.d/S99poolheat-standalone status
/opt/etc/init.d/S99poolheat-standalone start
/opt/etc/init.d/S99poolheat-standalone stop
/opt/etc/init.d/S99poolheat-standalone restart

tail -f /opt/var/poolheat/poolheat.log
```

Проверка версии:

```sh
cat /opt/lib/poolheat/VERSION
# или
wget -qO- http://127.0.0.1:8787/api/version
```

---

## 6. Альтернатива: `.ipk` пакет

На Mac/PC из клона репо:

```bash
cd packaging/entware
chmod +x build-ipk.sh
./build-ipk.sh
```

Артефакты в `dist/`:

```text
poolheat_0.3.0-1_aarch64-3.10.ipk
poolheat-0.3.0-opt.tar.gz
```

На роутере:

```sh
opkg install python3 python3-pip
opkg install /path/to/poolheat_0.3.0-1_aarch64-3.10.ipk
```

`postinst` поднимает зависимости и сервис.

---

## 7. Firewall / безопасность

- UI на **8787** — только LAN (или VPN / WireGuard).
- **Не** пробрасывайте 8787 в интернет без auth.
- Смените `api_password` майнера и не держите Dry Run выкл. без thr-настроек.
- Рекомендуется 24–48h **Dry Run** перед live writes.

---

## 8. Ограничения Peak

| Тема | Замечание |
|------|-----------|
| CPU/RAM | Один poller + UI — нормально |
| USB Entware | SPOF при отвале диска; лучше internal storage |
| Write API | Нужны `pycryptodome` + `passlib` |
| GitHub update | Нужен исходящий HTTPS с роутера |

---

## 9. Быстрый чеклист «с нуля»

```sh
# 1) Entware + пакеты
opkg update
opkg install git git-http ca-certificates python3 python3-pip

# 2) Клон и установка
cd /opt && git clone https://github.com/andreybmc/poolheat.git
cd poolheat && sh packaging/entware/install-from-git.sh

# 3) Майнер
vi /opt/etc/poolheat/config.json
/opt/etc/init.d/S99poolheat-standalone restart

# 4) Браузер
# http://<keenetic-ip>:8787/
```

Обновление позже:

```sh
cd /opt/poolheat && git pull && sh packaging/entware/install-from-git.sh
```

или **Инфо → Проверить обновления → Установить** в UI.
