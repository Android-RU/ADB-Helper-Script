# CLI-хелпер для управления устройствами Android

`adbhelper.py` — это Python-скрипт, который упрощает повседневные задачи Android-разработчика и тестировщика: установка/удаление APK, снятие скриншотов и видео с экрана, сбор и анализ `logcat`, запуск активити, ввод событий и многое другое. Скрипт использует установленный у вас `adb` и не требует сторонних Python-зависимостей.

---

## Требования

* **Python:** 3.9+
* **ADB:** Android SDK Platform Tools должны быть установлены и доступны в `PATH`

  * Либо передайте путь к `adb` через флаг `--adb` (можно указать файл или каталог `platform-tools`)
* **ОС:** Windows 10/11, macOS 12+, Linux (Ubuntu 20.04+)

---

## Установка

1. Склонируйте репозиторий **ADB-Helper-Script**:

```bash
git clone https://github.com/Android-RU/ADB-Helper-Script.git
cd ADB-Helper-Script
```

2. Убедитесь, что `adb` доступен:

```bash
adb version
```

> Если команда не найдена — добавьте `platform-tools` в `PATH` или используйте `--adb`.

3. Запустите помощь:

```bash
python adbhelper.py -h
```

---

## Быстрый старт

```bash
# Список устройств
python adbhelper.py devices

# Установка APK c разрешениями и заменой
python adbhelper.py install app-release.apk --grant-all --replace

# Скриншот в файл
python adbhelper.py screenshot --out out/screen.png

# Логи за 2 минуты с фильтром и записью в файл
python adbhelper.py logcat --duration 120 --filter ActivityManager:I --out logs/am.log

# Старт главной активити
python adbhelper.py app start --package com.example.app --activity .MainActivity

# Ввод текста в текущее поле
python adbhelper.py input text "hello world"

# Сводка по устройству в JSON
python adbhelper.py device-info --json
```

---

## Подробный справочник команд

> Общий формат:

```bash
python adbhelper.py <command> [subcommand] [options]
```

### `devices`

Список подключённых устройств с моделью и версией Android.

```bash
python adbhelper.py devices [--json]
```

### `install`

Установка APK.

```bash
python adbhelper.py install <path_to.apk> [--grant-all] [--replace] [--downgrade]
```

### `uninstall`

Удаление пакета.

```bash
python adbhelper.py uninstall --package <name> [--keep-data]
```

### `screenshot`

Снять скриншот экрана.

```bash
python adbhelper.py screenshot [--out <file.png>]
```

### `record`

Записать видео экрана (по умолчанию 30 с, максимум 180 с).

```bash
python adbhelper.py record [--duration <sec>] [--bitrate <Mbps>] [--out <file.mp4>]
```

### `logcat`

Сбор логов `logcat` в файл с фильтрами.

```bash
python adbhelper.py logcat [--out <file.log>] [--since <5m|2h|ISO>]
                         [--filter <tag:level>]... [--clear] [--duration <sec>]
```

### `analyze-logs`

Офлайн-анализ сохранённого файла логов.

```bash
python adbhelper.py analyze-logs --file <path> [--json]
```

### `app`

Операции с приложением.

```bash
# Старт активити/интента
python adbhelper.py app start --package <p> [--activity <A>] [--action <ACTION>] [--data <uri>] [--extra k=v]...

# Остановка приложения
python adbhelper.py app stop --package <p>

# Сброс данных
python adbhelper.py app clear --package <p>

# Выдать runtime-разрешения
python adbhelper.py app grant-perms --package <p> --perms CAMERA RECORD_AUDIO ...

# Информация о пакете
python adbhelper.py app info --package <p>
```

### `input`

Ввод событий.

```bash
python adbhelper.py input tap <x> <y>
python adbhelper.py input text "строка"
python adbhelper.py input key KEYCODE_BACK
python adbhelper.py input swipe <x1> <y1> <x2> <y2> [--duration <ms>]
```

### `shell`

Выполнить shell-команду (используйте `--` для окончания парсинга).

```bash
python adbhelper.py shell [--root] -- getprop ro.build.version.release
```

### `pull` / `push`

Обмен файлами.

```bash
python adbhelper.py pull --remote <path> [--out <local>]
python adbhelper.py push --src <local> --remote <path>
```

### `device-info`

Сводка по устройству.

```bash
python adbhelper.py device-info [--json]
```

### `tcpip`

Подключение по сети.

```bash
python adbhelper.py tcpip enable [--port <5555>]
python adbhelper.py tcpip connect --host <ip> [--port <p>]
python adbhelper.py tcpip disable
```

### `screen`

Параметры экрана.

```bash
python adbhelper.py screen size [--set WxH]
python adbhelper.py screen density [--set <dpi>]
python adbhelper.py screen rotate (--landscape | --portrait | --unlock)
```

---

## Глобальные опции

```text
--adb <path>     Путь к бинарнику adb или каталогу platform-tools
--serial <id>    Serial устройства (как в `adb -s`)
--timeout <sec>  Глобальный таймаут (по умолчанию 30 с)
--dry-run        Печать adb-команд без выполнения
--verbose        Подробный лог
--quiet          Минимум вывода
--version        Печать версии
-h, --help       Справка
```

---

## Коды возврата

* `0` — успех
* `1` — общая ошибка/исключение
* `2` — не найден `adb`
* `3` — нет устройств или несколько без `--serial`
* `4` — неверные аргументы
* `5` — таймаут операции

---

## Конфигурация

Скрипт поддерживает конфиг-файл (опционально) и переменные окружения.

### Файл `~/.adbhelper.toml`

```toml
adb_path = "/path/to/platform-tools/adb"   # или каталог platform-tools
default_serial = "emulator-5554"
default_timeout = 30
output_dir_logs = "logs"
output_dir_screens = "screenshots"
```

### Переменные окружения

```text
ADBHELPER_ADB_PATH=/path/to/adb
ADBHELPER_DEFAULT_SERIAL=emulator-5554
ADBHELPER_DEFAULT_TIMEOUT=30
ADBHELPER_OUTPUT_LOGS=logs
ADBHELPER_OUTPUT_SCREENS=screenshots
```

> Приоритет: **CLI → env → конфиг → значения по умолчанию**.

---

## Практические рецепты

* **Быстрый smoke-тест запуска:**

```bash
python adbhelper.py app start --package com.example --activity .MainActivity
python adbhelper.py logcat --duration 10 --filter ActivityManager:I
```

* **Сбор логов с сохранением и анализом:**

```bash
python adbhelper.py logcat --duration 120 --out logs/run.log
python adbhelper.py analyze-logs --file logs/run.log --json
```

* **Запись экрана в 60 FPS-подобной нагрузке (битрейт 6 Мбит/с, 45 с):**

```bash
python adbhelper.py record --duration 45 --bitrate 6
```

* **Работа с несколькими устройствами:**

```bash
python adbhelper.py --serial emulator-5554 screenshot
python adbhelper.py --serial R58M123ABC install app.apk
```

* **Безопасная проверка команд:**

```bash
python adbhelper.py --dry-run install app.apk --replace
```

---

## Отладка и частые проблемы

* **`adb не найден`**
  Установите Android SDK Platform Tools и добавьте `platform-tools` в `PATH`, либо укажите `--adb`.

* **`Нет подключённых устройств`**
  Проверьте кабель/драйверы (Windows), включите `Developer options` и `USB debugging`.

* **Несколько устройств подключено**
  Укажите конкретный девайс: `--serial <id>`.

* **Права доступа на macOS**
  Разрешите терминалу доступ к съёмке экрана/файлам при записи экрана/скриншотах, если требуется.

* **Видео пустое/повреждено**
  Уменьшите длительность/битрейт, проверьте свободное место на устройстве и ПК.

---

## Разработка и тесты

* Код — единый файл `adbhelper.py` с подробными комментариями.
* Логи сохраняются в `adbhelper.log` с ротацией (до 5 файлов по 1 МБ).
* Мини-идеи для тестов:

  * Юнит-тест парсинга аргументов и режима `--dry-run`.
  * Интеграционные сценарии: `devices`, `screenshot`, `logcat --duration 5`, `install/uninstall` на тестовом APK.
