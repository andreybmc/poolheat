# poolheat_WM на Keenetic — полная установка

Репозиторий: **https://github.com/andreybmc/poolheat**  
Версия: см. [`VERSION`](./VERSION) в корне.

Контроллер Whatsminer (pool heat) ставится **в Entware** (`/opt`) на Keenetic  
(Peak и другие модели с OPKG). UI: `http://<ip-роутера>:8787/`

Инструкция рассчитана на человека, у которого **ещё нет** Entware  
(или он не уверен, что он настроен). Если Entware уже работает —  
переходите к [§4. Установка poolheat](#4-установка-poolheat-с-github).

---

## 0. Что получится в итоге

| Что | Где |
|-----|-----|
| Web UI | `http://<keenetic-lan-ip>:8787/` |
| Backend | `/opt/lib/poolheat/serve.py` (Python 3) |
| Конфиг | `/opt/etc/poolheat/config.json` |
| Данные / логи | `/opt/var/poolheat/` |
| Автозапуск | `/opt/etc/init.d/S99poolheat*` |
| Опрос майнера | LAN TCP **4028** (Whatsminer API) |

**Не** пробрасывайте порт `8787` в интернет. Remote — через  
Keenetic Cloud / VPN, не raw port-forward.

---

## 1. Требования

| Требование | Комментарий |
|------------|-------------|
| Keenetic с OPKG | Peak, Hero, Giga и др. с компонентом **Open Package support** |
| Место для Entware | USB **ext4** (рекомендуется) **или** встроенное хранилище (UBIFS, если модель поддерживает) |
| Интернет с роутера | для `opkg`, `git clone`, обновлений с GitHub |
| LAN до майнера | TCP **4028**, по умолчанию `192.168.1.10` |
| Свободное место | желательно ≥ 100–150 MB на `/opt` (Python + deps + UI) |

### Архитектура Entware

Нужен installer **под CPU вашей модели**, не «любой»:

| Платформа (примеры) | Installer |
|---------------------|-----------|
| **aarch64** — Peak, Hero (KN-101x), Titan и др. | `https://bin.entware.net/aarch64-k3.10/installer/aarch64-installer.tar.gz` |
| **mipsel** — многие Giga / Ultra / DSL | `https://bin.entware.net/mipselsf-k3.4/installer/mipsel-installer.tar.gz` |

Список пакетов aarch64: https://bin.entware.net/aarch64-k3.10/Packages.html  

Точная модель и поддержка OPKG — в [документации Keenetic](https://support.keenetic.com/)  
(раздел OPKG / «Установка Entware»).

> **poolheat** — чистый Python-скрипт. Он ставится **на любую**  
> архитектуру Entware, где есть `python3` + `pip`.  
> Готовый `.ipk` в `dist/` собран под **aarch64-3.10** (Peak-класс).

---

## 2. Подготовка Keenetic: компоненты OPKG

В веб-интерфейсе роутера:

1. **Общие настройки** → **Параметры компонентов**  
   (General System Settings → Component options).
2. Установите:
   - **Open Package support** (обязательно)
   - **Поддержка файловой системы Ext** (если USB)
   - **Сервер SMB / доступ к файлам** (удобно для заливки installer на USB)
3. Дождитесь перезагрузки / обновления компонентов.

Официально:

- [OPKG](https://support.keenetic.com/) — компонент Open Package support  
- [Entware на USB](https://support.keenetic.com/)  
- [Entware во встроенную память](https://support.keenetic.com/) (KeeneticOS ≥ 3.7, не все модели)

---

## 3. Установка Entware

Выберите **один** вариант: USB **или** встроенная память.

### 3A. USB ext4 (рекомендуется)

1. На ПК отформатируйте флешку в **ext4**  
   (см. статью Keenetic «Using the ext4 file system on USB drives»).  
   NTFS/exFAT/FAT для OPKG **не** подходят.
2. Вставьте USB в роутер; диск должен появиться в  
   **Приложения → USB-устройства**.
3. Скачайте installer для **вашей** архитектуры (см. §1),  
   например для Peak/aarch64:
   ```text
   https://bin.entware.net/aarch64-k3.10/installer/aarch64-installer.tar.gz
   ```
4. Через SMB/FTP создайте на **корне раздела** каталог `install`  
   и положите туда архив **без переименования** (или с именем,  
   которое ожидает installer вашей платформы — см. docs Keenetic).
5. **Приложения → Менеджер пакетов OPKG**:
   - Накопитель: ваш USB (ext4)
   - Включите **Доступ** для учётки, которой разрешён OPKG
   - **Сохранить**
6. В **Диагностика → Системный журнал** должны появиться строки  
   `Starting "Entware" deployment...` → `"Entware" installed!`

После успеха `/opt` смонтирован с USB.

### 3B. Встроенное хранилище (UBIFS), если доступно

1. Компоненты OPKG + поддержка internal storage (см. docs модели).
2. **Менеджер пакетов OPKG** → Накопитель: **Встроенное хранилище** → Сохранить.
3. Онлайн-установка из CLI Keenetic (подставьте URL **вашей** arch):

```text
opkg disk storage:/ https://bin.entware.net/aarch64-k3.10/installer/aarch64-installer.tar.gz
```

Либо вручную: `storage:/install/` + installer + `opkg disk storage:/`.

Подробности: документация Keenetic «Installing OPKG Entware in the router's internal memory».

### 3C. SSH в Entware (обязательно для установки poolheat)

После установки Entware поднимается **dropbear**:

| Параметр | Значение по умолчанию |
|----------|------------------------|
| Login | `root` |
| Password | `keenetic` |
| Port | **22**, если компонент «SSH-сервер» Keenetic **не** стоит;  
|  | **222**, если системный SSH Keenetic установлен (Entware часто на 22,  
|  | системный — на 222 — смотрите журнал / docs) |

Подключение:

```sh
ssh -p 22 root@192.168.1.1
# или
ssh -p 222 root@192.168.1.1
```

**Сразу** смените пароль:

```sh
passwd
```

Проверка, что Entware жив:

```sh
opkg update
which opkg python3 || true
ls -la /opt
```

Альтернатива без внешнего SSH: в CLI Keenetic  
`(config)> exec sh` — получите BusyBox shell на роутере  
(удобно для диагностики, для git-clone удобнее SSH).

---

## 4. Установка poolheat с GitHub

На **роутере** (shell Entware, `root`):

```sh
opkg update
opkg install git git-http ca-certificates python3 python3-pip

cd /opt
# если каталог уже есть — см. «Обновление»
git clone https://github.com/andreybmc/poolheat.git
cd poolheat
sh packaging/entware/install-from-git.sh
```

Скрипт:

- `ui-demo/serve.py` → `/opt/lib/poolheat/serve.py`
- `ui-demo/index.html` → `/opt/share/poolheat/www/index.html`
- ставит `VERSION`, launcher `/opt/bin/poolheatd`, init `S99poolheat*`
- **не** перезаписывает существующий `/opt/etc/poolheat/config.json`
- ставит Python-зависимости (`pycryptodome`, `passlib`) и перезапускает сервис

### Конфиг майнера

```sh
vi /opt/etc/poolheat/config.json
```

Минимум:

```json
{
  "bind": "0.0.0.0",
  "http_port": 8787,
  "miner_host": "192.168.1.10",
  "miner_port": 4028,
  "api_password": "admin"
}
```

Подставьте IP и пароль API Whatsminer (не дефолт `admin` в бою).

Перезапуск:

```sh
/opt/etc/init.d/S99poolheat-standalone restart
# если есть rc.func Entware:
/opt/etc/init.d/S99poolheat restart
```

Открыть UI с ПК в LAN:

```text
http://192.168.1.1:8787/
```

(подставьте LAN IP Keenetic)

---

## 5. Firewall / сеть

| Правило | Действие |
|---------|----------|
| TCP **8787** из Home / LAN | разрешить (если firewall режет локальные сервисы) |
| TCP **8787** с WAN / Internet | **запретить**, port-forward **не** создавать |
| TCP **4028** роутер → майнер | должен ходить в LAN (обычно без доп. правил) |
| Remote UI | **Keenetic Cloud / Remote Access** или VPN + token, не raw 8787 |

Проверка с роутера:

```sh
wget -qO- http://127.0.0.1:8787/api/version
# или
curl -s http://127.0.0.1:8787/api/version
```

Проверка до майнера:

```sh
# если есть nc / busybox
echo '{"cmd":"summary"}' | nc 192.168.1.10 4028
```

---

## 6. Управление сервисом

```sh
/opt/etc/init.d/S99poolheat-standalone status
/opt/etc/init.d/S99poolheat-standalone start
/opt/etc/init.d/S99poolheat-standalone stop
/opt/etc/init.d/S99poolheat-standalone restart

tail -f /opt/var/poolheat/poolheat.log
cat /opt/lib/poolheat/VERSION
```

Автозапуск: init-скрипты в `/opt/etc/init.d/` поднимаются  
при монтировании Entware (`rc.unslung`). Если USB отвалился —  
`/opt` пропадает, сервис не стартует (SPOF USB — см. §9).

---

## 7. Обновление

### A) SSH + git

```sh
cd /opt/poolheat
git pull
sh packaging/entware/install-from-git.sh
```

### B) Web UI (без SSH)

1. Вкладка **Инфо**
2. **Проверить обновления** — local `VERSION` vs GitHub  
   (Releases / tags / `VERSION` на `main`)
3. **Установить** — tarball с GitHub, перезапуск сервиса

Конфиг `/opt/etc/poolheat/` и данные `/opt/var/poolheat/` **сохраняются**.

Если «Установить» неактивна:

- на GitHub нет версии выше локальной;
- роутер не достучался до `api.github.com` / `codeload.github.com`.

### C) Конкретный tag

```sh
cd /opt/poolheat
git fetch --tags
git checkout v0.3.22   # пример
sh packaging/entware/install-from-git.sh
```

---

## 8. Альтернатива: `.ipk` / tarball (без git)

### 8A. Готовый `.ipk` (aarch64)

На Mac/PC из клона:

```bash
cd packaging/entware
chmod +x build-ipk.sh
./build-ipk.sh
```

В `dist/`:

```text
poolheat_<ver>-1_aarch64-3.10.ipk
poolheat-<ver>-opt.tar.gz
```

На роутере (aarch64 + Entware):

```sh
opkg install python3 python3-pip
# скопируйте .ipk на роутер (scp / SMB), затем:
opkg install /tmp/poolheat_*_aarch64-3.10.ipk
```

`postinst` ставит pip-зависимости и стартует сервис.

### 8B. Ручная распаковка tarball

```sh
cd /
tar -xzf /tmp/poolheat-*-opt.tar.gz
# затем deps + init:
/opt/bin/pip3 install pycryptodome passlib
/opt/etc/init.d/S99poolheat-standalone restart
```

---

## 9. Ограничения и типичные проблемы

| Симптом | Что проверить |
|---------|----------------|
| `opkg: not found` | Entware не установлен / `/opt` не смонтирован |
| SSH connection refused | порт 22 vs 222; OPKG Access включён; dropbear в журнале |
| `git clone` SSL error | `opkg install ca-certificates git-http` |
| UI не открывается | `status` / `poolheat.log`; `python3` есть; bind/port в config |
| Майнер offline | `miner_host`, API 4028, пароль, один сегмент LAN |
| Write API / power fail | `pip3 install pycryptodome passlib` |
| После reboot сервиса нет | USB не смонтировался; OPKG disk не выбран |
| Мало места на UBIFS | используйте USB ext4 |

| Тема Peak / Entware | Замечание |
|---------------------|-----------|
| CPU/RAM | один poller + UI — обычно нормально |
| USB Entware | SPOF при отвале диска; надёжная флешка / internal |
| GitHub update | нужен исходящий HTTPS |
| Keenetic support | third-party OPKG **не** поддерживается поддержкой Keenetic |

---

## 10. Схема файлов

```text
Keenetic (Entware)
  /opt/bin/poolheatd                 launcher
  /opt/lib/poolheat/serve.py         backend
  /opt/lib/poolheat/VERSION          installed version
  /opt/share/poolheat/www/index.html UI
  /opt/etc/poolheat/config.json      miner IP, password, bind
  /opt/var/poolheat/                 history.db, logs, zone maps
  :8787  → Web UI (LAN)
         └── TCP 4028 → Whatsminer

/opt/poolheat/                       git clone (для git-обновлений)
  ui-demo/
  packaging/entware/install-from-git.sh
  VERSION
  KEENETIC.md
```

---

## 11. Быстрый чеклист «с нуля»

```text
□ Компонент Open Package support (+ Ext + SMB при USB)
□ Entware (USB ext4 или internal) — installer под вашу arch
□ SSH: root / сменённый пароль, opkg update работает
□ opkg install git git-http ca-certificates python3 python3-pip
□ git clone + install-from-git.sh
□ config.json: miner_host + api_password
□ UI http://<router>:8787/ открывается из LAN
□ 8787 не проброшен в WAN
□ 24–48h Dry Run перед live writes
```

Команды одной пачкой (когда Entware уже есть):

```sh
opkg update
opkg install git git-http ca-certificates python3 python3-pip
cd /opt && git clone https://github.com/andreybmc/poolheat.git
cd poolheat && sh packaging/entware/install-from-git.sh
vi /opt/etc/poolheat/config.json
/opt/etc/init.d/S99poolheat-standalone restart
# браузер: http://<keenetic-ip>:8787/
```

Обновление позже:

```sh
cd /opt/poolheat && git pull && sh packaging/entware/install-from-git.sh
```

или **Инфо → Проверить обновления → Установить** в UI.
