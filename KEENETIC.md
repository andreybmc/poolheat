# poolheat_WM на Keenetic — полная установка

Репозиторий: **https://github.com/andreybmc/poolheat**  
Версия: см. [`VERSION`](./VERSION) в корне.

Контроллер Whatsminer (pool heat) ставится **в Entware** (`/opt`) на Keenetic  
(Peak и другие модели с OPKG). UI: `http://<ip-роутера>:8787/`

Инструкция рассчитана на человека, у которого **ещё нет** Entware  
(или он не уверен, что он настроен). Если Entware уже работает —  
переходите к [§4. Установка poolheat](#4-установка-poolheat-с-github).

**Консоль Peak (SSH / веб-CLI / Entware):** [§3C](#3c-консоль-peak--entware--как-попасть-ssh-и-cli) —  
как войти, порты **22 vs 222**, `exec sh`, рестарт poolheat.

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

### 3C. Консоль Peak / Entware — как попасть (SSH и CLI)

На Keenetic есть **два разных** «шелла». Их легко перепутать:

| Среда | Что это | Куда вы попадаете | Типичный доступ |
|-------|---------|-------------------|-----------------|
| **CLI KeeneticOS** | командная строка прошивки | `(config)>` ndm, сеть, компоненты | веб «Командная строка» / SSH **учётки admin** |
| **Shell Entware** | Linux-окружение OPKG | BusyBox, `/opt`, `opkg`, poolheat | SSH **`root`** (dropbear Entware) |

**poolheat** живёт в Entware (`/opt/...`). Для `git`, `opkg`, рестарта сервиса нужен **shell Entware**  
(или `exec sh` из CLI — см. ниже).

Официально:

- [SSH remote access (Keenetic CLI)](https://support.keenetic.com/carrier/kn-1713/en/22340-ssh-remote-access-to-the-router-command-line.html)
- [Entware на USB](https://support.keenetic.com/hero/kn-1012/en/20980-installing-the-entware-repository-on-a-usb-drive.html)  
  (логин/пароль Entware, порты **22 / 222**)

---

#### 3C.1. Через веб Keenetic (без SSH с Mac)

1. Откройте веб-интерфейс Peak: `http://192.168.1.1/`  
   (LAN IP может быть другим — смотрите на наклейке / DHCP).
2. Войдите учёткой **администратора** (та же, что для настроек роутера).
3. Найдите **командную строку / CLI**:
   - **Управление** → **Диагностика** → **Командная строка**  
     (или **Diagnostics → Command line** / «CLI» — зависит от темы KeeneticOS),  
   - либо иконка терминала в интерфейсе (на новых прошивках).
4. В CLI вы обычно в режиме прошивки, приглашение вида `(config)>`.

Перейти в **shell Entware / BusyBox** с диска OPKG:

```text
(config)> exec sh
```

или (если доступно):

```text
(config)> system shell
```

После успеха prompt станет похож на `/ #` или `root@...`.  
Проверка Entware:

```sh
ls /opt
opkg --version
```

Выход из `exec sh` обратно в CLI: `exit`.

> **Зачем это нужно:** если с Mac SSH не пускает (нет ключа/пароля),  
> рестарт poolheat и просмотр логов можно сделать **прямо из веб-CLI**  
> после `exec sh` (см. §6).

---

#### 3C.2. SSH в Entware (рекомендуется для установки и отладки)

После установки Entware поднимается **dropbear** (свой SSH, не путать с «SSH-сервер» KeeneticOS).

| Параметр | Значение по умолчанию (Entware) |
|----------|----------------------------------|
| Login | **`root`** |
| Password | **`keenetic`** (смените сразу!) |
| Host | LAN IP Peak, часто `192.168.1.1` |

**Порт — важно:**

| Ситуация | Порт SSH **Entware** |
|----------|----------------------|
| Компонент **«SSH-сервер»** KeeneticOS **установлен** | обычно **222** |
| Компонента SSH KeeneticOS **нет** | обычно **22** |

Так указано в документации Keenetic (Entware on USB).  
На Peak с установленным SSH-сервером прошивки чаще всего:

```sh
ssh -p 222 root@192.168.1.1
```

Если не коннектится — попробуйте:

```sh
ssh -p 22 root@192.168.1.1
```

Клиенты:

- **macOS / Linux:** Terminal → команда `ssh` выше  
- **Windows:** [PuTTY](https://www.putty.org/) → Connection type **SSH**, Host `192.168.1.1`, Port **222** (или 22) → Open  
  Login: `root`, password: `keenetic` (или ваш)

При первом входе примите host key (`yes`).

**Сразу** смените пароль root Entware:

```sh
passwd
```

Пароль **root Entware** ≠ пароль **admin** веб-интерфейса (это разные учётки).

Проверка, что это именно Entware:

```sh
opkg update
which opkg python3 || true
ls -la /opt
df -h /opt
```

Типичные команды poolheat (из этого shell):

```sh
/opt/etc/init.d/S99poolheat-standalone status
/opt/etc/init.d/S99poolheat-standalone restart
curl -s http://127.0.0.1:8787/api/version
tail -50 /opt/var/poolheat/poolheat.log
```

---

#### 3C.3. SSH в CLI KeeneticOS (admin, не Entware)

Если установлен компонент **SSH-сервер** KeeneticOS:

1. **Общие настройки** → **Параметры компонентов** → включить **SSH-сервер**.
2. Подключение **учёткой администратора** роутера (как в веб), порт часто **22** или отдельный (см. настройки безопасности / private segment).

```sh
ssh admin@192.168.1.1
# затем в CLI:
exec sh
```

`exec sh` снова даёт путь к BusyBox/Entware, если OPKG смонтирован.

Без `exec sh` вы **не** увидите `opkg` / `/opt/lib/poolheat` как в Linux-shell.

---

#### 3C.4. Как понять, какой порт открыт

С ПК в LAN:

```sh
# macOS / Linux
nc -z -v 192.168.1.1 22
nc -z -v 192.168.1.1 222
```

Или в журнале Keenetic после установки Entware ищите строки вида  
`Log on to start an SSH session using login - root, password - keenetic`  
и указание порта.

| Симптом | Что проверить |
|---------|----------------|
| `Connection refused` на 22 | попробуйте **222** (и наоборот) |
| `Permission denied (publickey,password)` | неверный пароль **root** Entware; с Mac нет ключа — нужен пароль; не логиньтесь как `admin` на порт Entware |
| `Connection timed out` | не тот IP / не LAN / firewall |
| SSH ok, но `opkg: not found` | вы в CLI прошивки, а не в Entware → `exec sh` или SSH root:222 |
| `/opt` пустой | OPKG/USB не смонтирован — **Менеджер пакетов OPKG** → Access, USB |

---

#### 3C.5. Безопасность

- Смените пароль `root` Entware с `keenetic`.
- **Не** пробрасывайте SSH (22/222) и **8787** в интернет.
- Remote: Keenetic Cloud / VPN, не raw port-forward.
- Для автоматизации с Mac удобнее **SSH-ключ** на Entware:

```sh
# на Mac
ssh-copy-id -p 222 root@192.168.1.1
# или вручную: ~/.ssh/id_ed25519.pub → /opt/etc/dropbear/authorized_keys
# (путь dropbear зависит от сборки Entware — смотрите docs dropbear)
```

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

- `ui/serve.py` → `/opt/lib/poolheat/serve.py`
- `ui/index.html` → `/opt/share/poolheat/www/index.html`
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

Команды ниже — из **shell Entware** (как войти: [§3C](#3c-консоль-peak--entware--как-попасть-ssh-и-cli)).

```sh
/opt/etc/init.d/S99poolheat-standalone status
/opt/etc/init.d/S99poolheat-standalone start
/opt/etc/init.d/S99poolheat-standalone stop
/opt/etc/init.d/S99poolheat-standalone restart

tail -f /opt/var/poolheat/poolheat.log
cat /opt/lib/poolheat/VERSION
curl -s http://127.0.0.1:8787/api/version
```

Если UI/бот «мертвы» (`http://192.168.1.1:8787` не открывается),  
чаще всего упал процесс — зайдите в консоль (§3C) и сделайте `restart`  
и `tail` лога.

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
  ui/
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
