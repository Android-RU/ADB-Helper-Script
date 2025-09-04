#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
adbhelper.py — удобный CLI-хелпер для ADB.

Требования:
- Python 3.9+
- Установленный ADB (Android SDK Platform Tools) в PATH или через --adb <path>

Основные возможности:
- devices, install, uninstall, screenshot, record, logcat, analyze-logs,
  app (start/stop/clear/grant-perms/info), input (tap/text/key/swipe),
  shell, pull, push, device-info, tcpip, screen (size/density/rotate)

Коды выхода:
  0 — успех
  1 — общая ошибка
  2 — не найден adb
  3 — нет устройств или несколько без --serial
  4 — неправильные аргументы
  5 — таймаут операции
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

# Пытаемся подключить tomllib (есть в Python 3.11+). В 3.9–3.10 тихо пропустим.
try:
    import tomllib  # type: ignore
except Exception:
    tomllib = None  # type: ignore

APP_NAME = "adbhelper"
VERSION = "1.0.0"
DEFAULT_TIMEOUT = 30  # секунд
LOG_FILE = "adbhelper.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 5

# Регулярки для парсинга вывода
RE_DEVICES_LINE = re.compile(
    r"^(?P<serial>\S+)\s+(?P<state>device|offline|unauthorized)\s*(.*model:(?P<model>\S+))?.*?(transport_id:(?P<transport>\d+))?\s*$"
)
RE_PROP = re.compile(r"^\[(.*?)\]: \[(.*?)\]$")
RE_LOGCAT_LEVEL = re.compile(r"^\S+ +(?P<level>[VDIWEF])/(?P<tag>[^() ]+)\s*\(")
RE_FATAL = re.compile(r"FATAL EXCEPTION|ANR in|java\.lang\.", re.IGNORECASE)


# ---------------------------
# Утилиты форматирования
# ---------------------------

def human_ts(ts: Optional[dt.datetime] = None) -> str:
    """Возвращает ISO8601-время без таймзоны (локальное)."""
    if ts is None:
        ts = dt.datetime.now()
    return ts.replace(microsecond=0).isoformat()


def ensure_dir(path: Union[str, Path]) -> None:
    """Создать директорию, если не существует."""
    Path(path).mkdir(parents=True, exist_ok=True)


def print_table(rows: List[Dict[str, Any]]) -> None:
    """Простой табличный вывод по ключам первого словаря."""
    if not rows:
        print("(пусто)")
        return
    headers = list(rows[0].keys())
    widths = {h: max(len(h), *(len(str(r.get(h, ""))) for r in rows)) for h in headers}
    # Шапка
    print(" | ".join(h.ljust(widths[h]) for h in headers))
    print("-+-".join("-" * widths[h] for h in headers))
    # Строки
    for r in rows:
        print(" | ".join(str(r.get(h, "")).ljust(widths[h]) for h in headers))


def json_or_table(rows: List[Dict[str, Any]], as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print_table(rows)


def die(code: int, msg: str) -> None:
    logging.error(msg)
    print(f"Ошибка: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------
# Конфигурация
# ---------------------------

@dataclass
class Config:
    adb_path: Optional[str] = None
    default_serial: Optional[str] = None
    default_timeout: int = DEFAULT_TIMEOUT
    output_dir_logs: str = "logs"
    output_dir_screens: str = "screenshots"

    @staticmethod
    def load() -> "Config":
        """Загрузка конфигурации из env / toml / значений по умолчанию."""
        cfg = Config()
        # 1) env-переменные
        cfg.adb_path = os.getenv("ADBHELPER_ADB_PATH") or None
        cfg.default_serial = os.getenv("ADBHELPER_DEFAULT_SERIAL") or None
        cfg.output_dir_logs = os.getenv("ADBHELPER_OUTPUT_LOGS") or cfg.output_dir_logs
        cfg.output_dir_screens = os.getenv("ADBHELPER_OUTPUT_SCREENS") or cfg.output_dir_screens
        try:
            to = int(os.getenv("ADBHELPER_DEFAULT_TIMEOUT", ""))
            if to > 0:
                cfg.default_timeout = to
        except Exception:
            pass

        # 2) ~/.adbhelper.toml (если есть и доступен tomllib)
        conf_path = Path.home() / ".adbhelper.toml"
        if tomllib and conf_path.exists():
            try:
                with open(conf_path, "rb") as f:
                    data = tomllib.load(f)  # type: ignore
                cfg.adb_path = data.get("adb_path") or cfg.adb_path
                cfg.default_serial = data.get("default_serial") or cfg.default_serial
                cfg.output_dir_logs = data.get("output_dir_logs") or cfg.output_dir_logs
                cfg.output_dir_screens = data.get("output_dir_screens") or cfg.output_dir_screens
                if isinstance(data.get("default_timeout"), int) and data["default_timeout"] > 0:
                    cfg.default_timeout = int(data["default_timeout"])
            except Exception as e:
                logging.warning("Не удалось прочитать конфиг %s: %s", conf_path, e)
        return cfg


# ---------------------------
# Логирование
# ---------------------------

def setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.INFO
    if verbose:
        level = logging.DEBUG
    if quiet:
        level = logging.WARNING

    root = logging.getLogger()
    root.setLevel(level)

    # Ротация файлового лога
    fh = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(fh)

    # Консоль
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(ch)


# ---------------------------
# Исполнитель ADB
# ---------------------------

class AdbRunner:
    """Отвечает за поиск adb, запуск команд, dry-run и таймауты."""

    def __init__(self, adb_path: Optional[str], dry_run: bool = False, timeout: int = DEFAULT_TIMEOUT):
        self._adb_path = self._discover_adb(adb_path)
        self.dry_run = dry_run
        self.timeout = timeout

    @staticmethod
    def _discover_adb(adb_path: Optional[str]) -> str:
        if adb_path:
            p = Path(adb_path)
            if p.is_dir():
                # Если передали путь к каталогу platform-tools
                candidate = p / ("adb.exe" if platform.system() == "Windows" else "adb")
                if candidate.exists():
                    return str(candidate)
            if p.exists():
                return str(p)
            die(2, f"Не найден adb по указанному пути: {adb_path}")
        # Ищем в PATH
        found = shutil.which("adb.exe" if platform.system() == "Windows" else "adb")
        if not found:
            die(2, "adb не найден в PATH. Установите Android SDK Platform Tools или укажите --adb.")
        return found

    def _build(self, args: List[str], serial: Optional[str] = None) -> List[str]:
        cmd = [self._adb_path]
        if serial:
            cmd += ["-s", serial]
        cmd += args
        return cmd

    def run(
        self,
        args: List[str],
        serial: Optional[str] = None,
        capture: bool = True,
        text: bool = True,
        timeout: Optional[int] = None,
        check: bool = False,
    ) -> Tuple[int, str, str]:
        """Запуск adb-команды. Возвращает (rc, stdout, stderr)."""
        cmd = self._build(args, serial)
        logging.debug("ADB CMD: %s", " ".join(cmd))
        if self.dry_run:
            print("[DRY-RUN]", " ".join(cmd))
            return 0, "", ""
        try:
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                timeout=timeout or self.timeout,
                check=False,
            )
            out = completed.stdout.decode("utf-8", errors="ignore") if (capture and completed.stdout) else ""
            err = completed.stderr.decode("utf-8", errors="ignore") if (capture and completed.stderr) else ""
            if check and completed.returncode != 0:
                raise subprocess.CalledProcessError(completed.returncode, cmd, output=out, stderr=err)
            return completed.returncode, out, err
        except subprocess.TimeoutExpired:
            die(5, f"Команда превысила таймаут {timeout or self.timeout} с: {' '.join(cmd)}")
        except FileNotFoundError:
            die(2, "adb не найден (FileNotFoundError). Проверьте установку и PATH.")
        except Exception as e:
            die(1, f"Не удалось выполнить команду: {' '.join(cmd)}\n{e}")

    def popen(
        self,
        args: List[str],
        serial: Optional[str] = None,
        stdout=None,
        stderr=None,
        text: bool = False,
    ) -> subprocess.Popen:
        """Открыть длительный процесс (например, logcat)."""
        cmd = self._build(args, serial)
        logging.debug("ADB POPEN: %s", " ".join(cmd))
        if self.dry_run:
            print("[DRY-RUN]", " ".join(cmd))
            # Эмуляция пустого процесса: завершим сразу
            proc = subprocess.Popen(["python", "-c", "print('dry-run')"], stdout=stdout, stderr=stderr)
            return proc
        try:
            return subprocess.Popen(cmd, stdout=stdout, stderr=stderr, text=text)
        except Exception as e:
            die(1, f"Не удалось запустить процесс: {' '.join(cmd)}\n{e}")


# ---------------------------
# Работа с устройствами
# ---------------------------

@dataclass
class DeviceInfo:
    serial: str
    state: str
    model: str = ""
    transport: Optional[str] = None
    android: Optional[str] = None
    sdk: Optional[str] = None


class DeviceSelector:
    def __init__(self, adb: AdbRunner):
        self.adb = adb

    def list_devices(self) -> List[DeviceInfo]:
        rc, out, err = self.adb.run(["devices", "-l"])
        if rc != 0:
            die(1, f"adb devices вернул код {rc}: {err.strip()}")
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        # Первая строка — "List of devices attached"
        lines = [l for l in lines if not l.lower().startswith("list of devices")]
        devs: List[DeviceInfo] = []
        for l in lines:
            m = RE_DEVICES_LINE.match(l)
            if not m:
                logging.debug("Пропускаю строку devices: %r", l)
                continue
            d = DeviceInfo(
                serial=m.group("serial"),
                state=m.group("state"),
                model=m.group("model") or "",
                transport=m.group("transport"),
            )
            devs.append(d)

        # Дополняем версией Android и SDK
        for d in devs:
            if d.state != "device":
                continue
            try:
                _, rel, _ = self.adb.run(["shell", "getprop", "ro.build.version.release"], d.serial)
                d.android = rel.strip() or None
                _, sdk, _ = self.adb.run(["shell", "getprop", "ro.build.version.sdk"], d.serial)
                d.sdk = sdk.strip() or None
            except Exception as e:
                logging.debug("Не удалось получить props для %s: %s", d.serial, e)
        return devs

    def pick(self, preferred_serial: Optional[str]) -> str:
        devs = self.list_devices()
        online = [d for d in devs if d.state == "device"]
        if preferred_serial:
            for d in online:
                if d.serial == preferred_serial:
                    return d.serial
            die(3, f"Устройство с serial {preferred_serial} не найдено или не в состоянии 'device'.")
        if not online:
            die(3, "Нет подключённых устройств (state 'device').")
        if len(online) > 1:
            msg = "Несколько подключённых устройств. Укажите --serial. Список:\n" + \
                  "\n".join(f"- {d.serial} ({d.model or 'n/a'})" for d in online)
            die(3, msg)
        return online[0].serial


# ---------------------------
# Парсинг значений и вспомогалки
# ---------------------------

def parse_since(s: Optional[str]) -> Optional[str]:
    """
    Преобразует --since в формат, понятный logcat через -T "YYYY-MM-DD HH:MM:SS.mmm".
    Поддерживает '5m', '2h', абсолютное ISO-8601 '2025-09-04T12:00:00'.
    """
    if not s:
        return None
    s = s.strip()
    now = dt.datetime.now()
    try:
        if re.fullmatch(r"\d+[smhd]", s):
            n = int(s[:-1])
            unit = s[-1]
            delta = {"s": dt.timedelta(seconds=n), "m": dt.timedelta(minutes=n),
                     "h": dt.timedelta(hours=n), "d": dt.timedelta(days=n)}[unit]
            t = now - delta
        else:
            # Пробуем ISO-8601
            t = dt.datetime.fromisoformat(s)
        return t.strftime("%Y-%m-%d %H:%M:%S.000")
    except Exception:
        return None


def sanitize_input_text(text: str) -> str:
    """
    Экранирование текста для `adb shell input text`.
    Правило: пробел -> %s; некоторые спецсимволы в проценты.
    (Не идеально для всех случаев, но покрывает типовые.)
    """
    mapping = {
        " ": "%s",
        "&": "\\&",
        "<": "\\<",
        ">": "\\>",
        "(": "\\(",
        ")": "\\)",
        ";": "\\;",
        "|": "\\|",
        "*": "\\*",
        "~": "\\~",
        "'": "\\'",
        '"': '\\"',
        "#": "\\#",
        "%": "\\%",
        "!": "\\!",
        "?": "\\?",
        ":": "\\:",
        "/": "\\/",
        "\\": "\\\\",
    }
    return "".join(mapping.get(ch, ch) for ch in text)


# ---------------------------
# Команды
# ---------------------------

def cmd_devices(adb: AdbRunner, args: argparse.Namespace) -> int:
    ds = DeviceSelector(adb)
    devs = ds.list_devices()
    rows = []
    for d in devs:
        rows.append({
            "serial": d.serial,
            "state": d.state,
            "model": d.model or "",
            "android": d.android or "",
            "sdk": d.sdk or "",
            "transport": d.transport or "",
        })
    json_or_table(rows, args.json)
    return 0


def cmd_install(adb: AdbRunner, args: argparse.Namespace) -> int:
    apk = Path(args.apk)
    if not apk.exists():
        die(4, f"APK не найден: {apk}")
    flags: List[str] = ["install"]
    if args.replace:
        flags.append("-r")
    if args.downgrade:
        flags.append("-d")
    if args.grant_all:
        flags.append("-g")
    serial = DeviceSelector(adb).pick(args.serial)
    rc, out, err = adb.run(flags + [str(apk)], serial)
    if rc == 0:
        print("Установка завершена.")
        print(out.strip())
    else:
        print(out.strip())
        print(err.strip(), file=sys.stderr)
    return rc


def cmd_uninstall(adb: AdbRunner, args: argparse.Namespace) -> int:
    if not args.package:
        die(4, "--package обязателен.")
    serial = DeviceSelector(adb).pick(args.serial)
    flags = ["uninstall"]
    if args.keep_data:
        flags.append("-k")
    flags.append(args.package)
    rc, out, err = adb.run(flags, serial)
    print(out.strip() or err.strip())
    return rc


def cmd_screenshot(adb: AdbRunner, args: argparse.Namespace, cfg: Config) -> int:
    serial = DeviceSelector(adb).pick(args.serial)
    out_path = Path(args.out) if args.out else Path(cfg.output_dir_screens) / f"{serial}_{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    ensure_dir(out_path.parent)
    # Используем exec-out для бинарного вывода
    if adb.dry_run:
        print(f"[DRY-RUN] Сохраню скриншот в {out_path}")
        return 0
    try:
        cmd = [adb._adb_path, "-s", serial, "exec-out", "screencap", "-p"]
        logging.debug("ADB SCREENSHOT: %s", " ".join(cmd))
        with open(out_path, "wb") as f:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = proc.communicate(timeout=args.timeout or adb.timeout)
            if proc.returncode != 0:
                die(1, f"screencap вернул {proc.returncode}: {err.decode('utf-8', 'ignore')}")
            f.write(out)
        if out_path.stat().st_size <= 0:
            die(1, "Получен пустой скриншот.")
        print(f"Скриншот сохранён: {out_path}")
        return 0
    except subprocess.TimeoutExpired:
        die(5, "Таймаут получения скриншота.")
    except Exception as e:
        die(1, f"Не удалось снять скриншот: {e}")
    return 1


def cmd_record(adb: AdbRunner, args: argparse.Namespace, cfg: Config) -> int:
    serial = DeviceSelector(adb).pick(args.serial)
    duration = min(max(args.duration or 30, 1), 180)
    bitrate = str(int((args.bitrate or 4) * 1_000_000))  # в бит/с
    remote_tmp = f"/sdcard/adbhelper_record_{int(time.time())}.mp4"
    out_path = Path(args.out) if args.out else Path(cfg.output_dir_screens) / f"{serial}_{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.mp4"
    ensure_dir(out_path.parent)

    # Запускаем запись на устройстве
    try:
        print(f"Запись экрана {duration} c...")
        rc, out, err = adb.run(
            ["shell", "screenrecord", f"--time-limit={duration}", f"--bit-rate={bitrate}", remote_tmp],
            serial,
            timeout=duration + 5,
        )
        if rc != 0:
            die(1, f"screenrecord вернул {rc}: {err.strip() or out.strip()}")
        # Перетягиваем файл
        rc, out, err = adb.run(["pull", remote_tmp, str(out_path)], serial)
        if rc != 0:
            die(1, f"Не удалось скачать видео: {err.strip() or out.strip()}")
        # Чистим временный файл
        adb.run(["shell", "rm", "-f", remote_tmp], serial)
        print(f"Видео сохранено: {out_path}")
        return 0
    except Exception as e:
        die(1, f"Ошибка записи экрана: {e}")
    return 1


def cmd_logcat(adb: AdbRunner, args: argparse.Namespace, cfg: Config) -> int:
    serial = DeviceSelector(adb).pick(args.serial)
    if args.clear:
        adb.run(["logcat", "-c"], serial)

    out_path = Path(args.out) if args.out else Path(cfg.output_dir_logs) / f"{serial}_{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    ensure_dir(out_path.parent)
    since = parse_since(args.since)

    cmd = ["logcat"]
    if since:
        cmd += ["-T", since]
    if args.filter:
        # Несколько фильтров tag:level — добавляем в конец команды
        cmd += args.filter

    if adb.dry_run:
        print("[DRY-RUN]", " ".join([adb._adb_path, "-s", serial] + cmd))
        print(f"[DRY-RUN] Логи писались бы в {out_path}")
        return 0

    duration = args.duration or 0
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            proc = adb.popen(cmd, serial, stdout=f, stderr=subprocess.PIPE, text=True)
            if duration > 0:
                # Останавливаем через duration секунд
                def _stop():
                    try:
                        time.sleep(duration)
                        # Посылаем SIGINT/SIGTERM для аккуратного завершения
                        if platform.system() != "Windows":
                            proc.send_signal(signal.SIGINT)
                        else:
                            proc.terminate()
                    except Exception:
                        pass
                t = threading.Thread(target=_stop, daemon=True)
                t.start()
            rc = proc.wait()
            if rc != 0 and rc is not None:
                err = proc.stderr.read() if proc.stderr else ""
                logging.warning("logcat завершился с кодом %s: %s", rc, (err or "").strip())
        print(f"Логи сохранены: {out_path}")
        return 0
    except Exception as e:
        die(1, f"Ошибка logcat: {e}")
    return 1


def cmd_analyze_logs(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        die(4, f"Файл логов не найден: {path}")
    levels = {"V": 0, "D": 0, "I": 0, "W": 0, "E": 0, "F": 0}
    tags: Dict[str, int] = {}
    fatals = 0
    lines_total = 0
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                lines_total += 1
                m = RE_LOGCAT_LEVEL.match(line)
                if m:
                    lvl = m.group("level")
                    tag = m.group("tag")
                    levels[lvl] = levels.get(lvl, 0) + 1
                    tags[tag] = tags.get(tag, 0) + 1
                if RE_FATAL.search(line):
                    fatals += 1
    except Exception as e:
        die(1, f"Не удалось прочитать файл: {e}")

    top_tags = sorted(tags.items(), key=lambda kv: kv[1], reverse=True)[:10]
    report = {
        "file": str(path),
        "analyzed_at": human_ts(),
        "lines": lines_total,
        "levels": levels,
        "fatals_or_anrs": fatals,
        "top10_tags": [{"tag": t, "count": c} for t, c in top_tags],
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("Анализ логов:")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_app(adb: AdbRunner, args: argparse.Namespace) -> int:
    serial = DeviceSelector(adb).pick(args.serial)
    sub = args.app_command

    if sub == "start":
        if not args.package and not args.activity and not args.action:
            die(4, "Нужно указать хотя бы --package и --activity, либо --action.")
        am = ["shell", "am", "start", "-W"]
        if args.action:
            am += ["-a", args.action]
        if args.data:
            am += ["-d", args.data]
        for ex in (args.extra or []):
            # extra в формате key=value
            if "=" in ex:
                k, v = ex.split("=", 1)
                am += ["--es", k, v]
        if args.package:
            comp = args.package
            if args.activity:
                # Если активити начинается с точki — дополним пакетом
                if args.activity.startswith("."):
                    comp = f"{args.package}/{args.activity}"
                elif "/" not in args.activity:
                    comp = f"{args.package}/{args.activity}"
                else:
                    comp = args.activity
            am += ["-n", comp]
        rc, out, err = adb.run(am, serial)
        print(out.strip() or err.strip())
        return rc

    if sub == "stop":
        if not args.package:
            die(4, "--package обязателен.")
        rc, out, err = adb.run(["shell", "am", "force-stop", args.package], serial)
        print("OK" if rc == 0 else (err.strip() or out.strip()))
        return rc

    if sub == "clear":
        if not args.package:
            die(4, "--package обязателен.")
        rc, out, err = adb.run(["shell", "pm", "clear", args.package], serial)
        print(out.strip() or err.strip())
        return rc

    if sub == "grant-perms":
        if not args.package or not args.perms:
            die(4, "--package и --perms обязательны.")
        rc_total = 0
        for p in args.perms:
            rc, out, err = adb.run(["shell", "pm", "grant", args.package, p], serial)
            if rc != 0:
                rc_total = rc
                print(f"{p}: FAIL - {(err.strip() or out.strip())}")
            else:
                print(f"{p}: OK")
        return rc_total

    if sub == "info":
        if not args.package:
            die(4, "--package обязателен.")
        rc, out, err = adb.run(["shell", "dumpsys", "package", args.package], serial)
        if rc != 0:
            print(err.strip() or out.strip(), file=sys.stderr)
            return rc
        info = {
            "package": args.package,
            "versionName": "",
            "versionCode": "",
            "uid": "",
            "grantedPermissions": [],
            "path": "",
            "mainActivity": "",
        }
        # Путь до apk
        rc2, out2, _ = adb.run(["shell", "pm", "path", args.package], serial)
        if rc2 == 0:
            info["path"] = out2.strip().replace("package:", "")
        # Разбираем dumpsys
        for line in out.splitlines():
            if "versionName=" in line:
                info["versionName"] = line.split("versionName=")[-1].strip()
            if "versionCode=" in line:
                # Может быть вида versionCode=12345 minSdk=...
                info["versionCode"] = line.split("versionCode=")[-1].split()[0]
            if "userId=" in line:
                info["uid"] = line.split("userId=")[-1].split()[0]
            if "granted=true" in line and "android.permission" in line:
                perm = line.strip().split()[0]
                info["grantedPermissions"].append(perm)
            if "android.intent.action.MAIN" in line and "LAUNCHER" in line:
                # Пытаемся найти main-activity — в блоке есть cmp=pack/.MainActivity
                m = re.search(r"cmp=(\S+)", line)
                if m:
                    info["mainActivity"] = m.group(1)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    die(4, f"Неизвестная подкоманда app: {sub}")
    return 4


def cmd_input(adb: AdbRunner, args: argparse.Namespace) -> int:
    serial = DeviceSelector(adb).pick(args.serial)
    sub = args.input_command
    if sub == "tap":
        rc, out, err = adb.run(["shell", "input", "tap", str(args.x), str(args.y)], serial)
        if rc != 0:
            print(err.strip() or out.strip(), file=sys.stderr)
        return rc
    if sub == "text":
        payload = sanitize_input_text(args.text)
        rc, out, err = adb.run(["shell", "input", "text", payload], serial)
        if rc != 0:
            print(err.strip() or out.strip(), file=sys.stderr)
        return rc
    if sub == "key":
        rc, out, err = adb.run(["shell", "input", "keyevent", args.key], serial)
        if rc != 0:
            print(err.strip() or out.strip(), file=sys.stderr)
        return rc
    if sub == "swipe":
        cmd = ["shell", "input", "swipe", str(args.x1), str(args.y1), str(args.x2), str(args.y2)]
        if args.duration:
            cmd.append(str(args.duration))
        rc, out, err = adb.run(cmd, serial)
        if rc != 0:
            print(err.strip() or out.strip(), file=sys.stderr)
        return rc
    die(4, f"Неизвестная подкоманда input: {sub}")
    return 4


def cmd_shell(adb: AdbRunner, args: argparse.Namespace) -> int:
    serial = DeviceSelector(adb).pick(args.serial)
    if not args.command:
        die(4, "Нужно передать команду для shell (используйте -- для окончания парсинга).")
    full_cmd = ["shell"]
    if args.root:
        full_cmd += ["su", "-c"]
        # Собираем строку для su -c '...'
        full_cmd += [" ".join(args.command)]
    else:
        full_cmd += args.command
    rc, out, err = adb.run(full_cmd, serial)
    if out:
        print(out, end="")
    if err and rc != 0:
        print(err, file=sys.stderr, end="")
    return rc


def cmd_pull(adb: AdbRunner, args: argparse.Namespace) -> int:
    serial = DeviceSelector(adb).pick(args.serial)
    if not args.remote:
        die(4, "--remote обязателен.")
    local = args.out or "."
    rc, out, err = adb.run(["pull", args.remote, local], serial)
    print(out.strip() or err.strip())
    return rc


def cmd_push(adb: AdbRunner, args: argparse.Namespace) -> int:
    serial = DeviceSelector(adb).pick(args.serial)
    if not args.src or not args.remote:
        die(4, "--src и --remote обязательны.")
    rc, out, err = adb.run(["push", args.src, args.remote], serial)
    print(out.strip() or err.strip())
    return rc


def cmd_device_info(adb: AdbRunner, args: argparse.Namespace) -> int:
    serial = DeviceSelector(adb).pick(args.serial)
    def sh(*cmd) -> str:
        _, out, _ = adb.run(["shell", *cmd], serial)
        return out.strip()

    try:
        info = {
            "serial": serial,
            "model": sh("getprop", "ro.product.model"),
            "brand": sh("getprop", "ro.product.brand"),
            "android": sh("getprop", "ro.build.version.release"),
            "sdk": sh("getprop", "ro.build.version.sdk"),
            "abi": sh("getprop", "ro.product.cpu.abi"),
            "root": "yes" if "uid=0" in sh("id") else "no",
            "battery": "",
            "storage": "",
            "mem": "",
        }
        # Батарея
        batt = sh("dumpsys", "battery")
        m_level = re.search(r"level: (\d+)", batt)
        m_status = re.search(r"status: (\d+)", batt)
        info["battery"] = f"{m_level.group(1)}% (status={m_status.group(1)})" if (m_level and m_status) else batt[:60] + "..."

        # Хранилище (df /data)
        storage = sh("df", "-h", "/data")
        info["storage"] = " ".join(storage.split())
        # Память
        mem = sh("dumpsys", "meminfo", "-c")
        info["mem"] = mem.splitlines()[0] if mem else ""

        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
        else:
            print_table([info])
        return 0
    except Exception as e:
        die(1, f"Не удалось собрать информацию об устройстве: {e}")
    return 1


def cmd_tcpip(adb: AdbRunner, args: argparse.Namespace) -> int:
    serial = DeviceSelector(adb).pick(args.serial) if args.tcpip_command != "disable" else None
    sub = args.tcpip_command
    if sub == "enable":
        port = str(args.port or 5555)
        rc, out, err = adb.run(["tcpip", port], serial)
        print(out.strip() or err.strip())
        return rc
    if sub == "connect":
        host = args.host
        port = args.port or 5555
        rc, out, err = adb.run(["connect", f"{host}:{port}"], serial=None)
        print(out.strip() or err.strip())
        return rc
    if sub == "disable":
        rc, out, err = adb.run(["usb"], serial=None)
        print(out.strip() or err.strip())
        return rc
    die(4, f"Неизвестная подкоманда tcpip: {sub}")
    return 4


def cmd_screen(adb: AdbRunner, args: argparse.Namespace) -> int:
    serial = DeviceSelector(adb).pick(args.serial)
    sub = args.screen_command
    if sub == "size":
        if args.set:
            rc, out, err = adb.run(["shell", "wm", "size", args.set], serial)
            print(out.strip() or err.strip())
            return rc
        else:
            rc, out, err = adb.run(["shell", "wm", "size"], serial)
            print(out.strip() or err.strip())
            return rc
    if sub == "density":
        if args.set:
            rc, out, err = adb.run(["shell", "wm", "density", str(args.set)], serial)
            print(out.strip() or err.strip())
            return rc
        else:
            rc, out, err = adb.run(["shell", "wm", "density"], serial)
            print(out.strip() or err.strip())
            return rc
    if sub == "rotate":
        # Отключаем авто-поворот и задаём user_rotation
        if args.landscape:
            adb.run(["shell", "settings", "put", "system", "accelerometer_rotation", "0"], serial)
            rc, out, err = adb.run(["shell", "settings", "put", "system", "user_rotation", "1"], serial)
            print(out.strip() or err.strip())
            return rc
        if args.portrait:
            adb.run(["shell", "settings", "put", "system", "accelerometer_rotation", "0"], serial)
            rc, out, err = adb.run(["shell", "settings", "put", "system", "user_rotation", "0"], serial)
            print(out.strip() or err.strip())
            return rc
        if args.unlock:
            rc1, o1, e1 = adb.run(["shell", "settings", "put", "system", "accelerometer_rotation", "1"], serial)
            print(o1.strip() or e1.strip() or "OK")
            return rc1
    die(4, f"Неизвестная подкоманда screen: {sub}")
    return 4


# ---------------------------
# Argparse
# ---------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="adbhelper.py",
        description="Адб-хелпер для повседневных задач Android-разработчика/QA.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Примеры:
              python adbhelper.py devices
              python adbhelper.py install app-release.apk --grant-all --replace
              python adbhelper.py screenshot --out out/screen.png
              python adbhelper.py logcat --duration 60 --filter ActivityManager:I
              python adbhelper.py app start --package com.example --activity .MainActivity
              python adbhelper.py input text "hello world"
              python adbhelper.py device-info --json
        """),
    )
    # Глобальные опции
    p.add_argument("--adb", help="Путь к бинарнику adb или каталогу platform-tools")
    p.add_argument("--serial", help="Serial устройства (adb -s)")
    p.add_argument("--timeout", type=int, help="Глобальный таймаут в секундах (по умолчанию 30)")
    p.add_argument("--dry-run", action="store_true", help="Показывать adb-команды, не выполняя их")
    p.add_argument("--verbose", action="store_true", help="Подробный лог")
    p.add_argument("--quiet", action="store_true", help="Минимум вывода")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")

    sub = p.add_subparsers(dest="command", required=True)

    # devices
    sp = sub.add_parser("devices", help="Список подключённых устройств")
    sp.add_argument("--json", action="store_true", help="Вывод в JSON")
    sp.set_defaults(func=cmd_devices)

    # install
    sp = sub.add_parser("install", help="Установка APK")
    sp.add_argument("apk", help="Путь к .apk")
    sp.add_argument("--replace", action="store_true", help="Перестановка (adb install -r)")
    sp.add_argument("--downgrade", action="store_true", help="Даунгрейд (adb install -d)")
    sp.add_argument("--grant-all", action="store_true", help="Авто-выдача runtime-разрешений (adb install -g)")
    sp.set_defaults(func=cmd_install)

    # uninstall
    sp = sub.add_parser("uninstall", help="Удаление пакета")
    sp.add_argument("--package", required=True, help="Package name")
    sp.add_argument("--keep-data", action="store_true", help="Сохранить данные (-k)")
    sp.set_defaults(func=cmd_uninstall)

    # screenshot
    sp = sub.add_parser("screenshot", help="Скриншот экрана")
    sp.add_argument("--out", help="Файл вывода .png (по умолчанию в screenshots/)")
    sp.set_defaults(func=cmd_screenshot)

    # record
    sp = sub.add_parser("record", help="Запись экрана")
    sp.add_argument("--duration", type=int, help="Длительность, сек (1-180, по умолчанию 30)")
    sp.add_argument("--bitrate", type=float, help="Битрейт Мбит/с (по умолчанию 4)")
    sp.add_argument("--out", help="Файл вывода .mp4 (по умолчанию в screenshots/)")
    sp.set_defaults(func=cmd_record)

    # logcat
    sp = sub.add_parser("logcat", help="Сбор логов logcat")
    sp.add_argument("--out", help="Файл вывода (по умолчанию в logs/)")
    sp.add_argument("--since", help='С какого момента: "5m", "2h" или ISO "2025-09-04T12:00:00"')
    sp.add_argument("--filter", action="append", help="Фильтр tag:level (можно несколько)")
    sp.add_argument("--clear", action="store_true", help="Очистить буфер перед сбором")
    sp.add_argument("--duration", type=int, help="Сколько секунд писать поток (по умолчанию до прерывания)")
    sp.set_defaults(func=cmd_logcat)

    # analyze-logs
    sp = sub.add_parser("analyze-logs", help="Офлайн-анализ сохранённого файла логов")
    sp.add_argument("--file", required=True, help="Путь к файлу логов")
    sp.add_argument("--json", action="store_true", help="Вывод отчёта в JSON")
    sp.set_defaults(func=cmd_analyze_logs)

    # app
    sp = sub.add_parser("app", help="Операции с приложением (start/stop/clear/grant-perms/info)")
    ap = sp.add_subparsers(dest="app_command", required=True)

    ap_start = ap.add_parser("start", help="Старт активити/интента")
    ap_start.add_argument("--package", help="Package name (для -n <pkg>/<act>)")
    ap_start.add_argument("--activity", help="Имя активити (.MainActivity или pkg/.Act)")
    ap_start.add_argument("--action", help="Интент-экшен, например android.intent.action.VIEW")
    ap_start.add_argument("--data", help="URI-данные для интента")
    ap_start.add_argument("--extra", nargs="*", help="Пары key=value для --es")
    ap_start.set_defaults(func=cmd_app)

    ap_stop = ap.add_parser("stop", help="Force stop приложения")
    ap_stop.add_argument("--package", required=True, help="Package name")
    ap_stop.set_defaults(func=cmd_app)

    ap_clear = ap.add_parser("clear", help="Сброс данных приложения")
    ap_clear.add_argument("--package", required=True, help="Package name")
    ap_clear.set_defaults(func=cmd_app)

    ap_grant = ap.add_parser("grant-perms", help="Выдать runtime-разрешения")
    ap_grant.add_argument("--package", required=True, help="Package name")
    ap_grant.add_argument("--perms", nargs="+", required=True, help="Список разрешений")
    ap_grant.set_defaults(func=cmd_app)

    ap_info = ap.add_parser("info", help="Информация о пакете")
    ap_info.add_argument("--package", required=True, help="Package name")
    ap_info.set_defaults(func=cmd_app)

    # input
    sp = sub.add_parser("input", help="Ввод событий (tap/text/key/swipe)")
    ip = sp.add_subparsers(dest="input_command", required=True)

    ip_tap = ip.add_parser("tap", help="Клик по координатам")
    ip_tap.add_argument("x", type=int)
    ip_tap.add_argument("y", type=int)
    ip_tap.set_defaults(func=cmd_input)

    ip_text = ip.add_parser("text", help="Ввод текста")
    ip_text.add_argument("text")
    ip_text.set_defaults(func=cmd_input)

    ip_key = ip.add_parser("key", help="Нажатие ключа KEYCODE_*")
    ip_key.add_argument("key", help="Например KEYCODE_BACK, KEYCODE_HOME, KEYCODE_ENTER")
    ip_key.set_defaults(func=cmd_input)

    ip_swipe = ip.add_parser("swipe", help="Свайп")
    ip_swipe.add_argument("x1", type=int)
    ip_swipe.add_argument("y1", type=int)
    ip_swipe.add_argument("x2", type=int)
    ip_swipe.add_argument("y2", type=int)
    ip_swipe.add_argument("--duration", type=int, help="Длительность, мс")
    ip_swipe.set_defaults(func=cmd_input)

    # shell
    sp = sub.add_parser("shell", help="Выполнить команду в shell")
    sp.add_argument("--root", action="store_true", help="Выполнить через su -c (если возможно)")
    sp.add_argument("command", nargs=argparse.REMAINDER, help="Команда (используйте -- для окончания парсинга)")
    sp.set_defaults(func=cmd_shell)

    # pull / push
    sp = sub.add_parser("pull", help="Скачать файл/папку с устройства")
    sp.add_argument("--remote", required=True, help="Путь на устройстве")
    sp.add_argument("--out", help="Локальный путь (по умолчанию текущая папка)")
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser("push", help="Залить файл/папку на устройство")
    sp.add_argument("--src", required=True, help="Локальный файл/папка")
    sp.add_argument("--remote", required=True, help="Путь на устройстве")
    sp.set_defaults(func=cmd_push)

    # device-info
    sp = sub.add_parser("device-info", help="Сводка по устройству")
    sp.add_argument("--json", action="store_true", help="Вывод в JSON")
    sp.set_defaults(func=cmd_device_info)

    # tcpip
    sp = sub.add_parser("tcpip", help="Подключение по сети (enable/connect/disable)")
    tp = sp.add_subparsers(dest="tcpip_command", required=True)

    tp_enable = tp.add_parser("enable", help="Перевести устройство в TCPIP режим")
    tp_enable.add_argument("--port", type=int, help="Порт (по умолчанию 5555)")
    tp_enable.set_defaults(func=cmd_tcpip)

    tp_connect = tp.add_parser("connect", help="Подключиться к устройству по IP")
    tp_connect.add_argument("--host", required=True, help="IP адрес устройства")
    tp_connect.add_argument("--port", type=int, help="Порт (по умолчанию 5555)")
    tp_connect.set_defaults(func=cmd_tcpip)

    tp_disable = tp.add_parser("disable", help="Вернуться в USB режим (adb usb)")
    tp_disable.set_defaults(func=cmd_tcpip)

    # screen
    sp = sub.add_parser("screen", help="Параметры экрана (size/density/rotate)")
    sc = sp.add_subparsers(dest="screen_command", required=True)

    sc_size = sc.add_parser("size", help="Размер экрана (получить/установить)")
    sc_size.add_argument("--set", help="Установить WxH, например 1080x1920")
    sc_size.set_defaults(func=cmd_screen)

    sc_density = sc.add_parser("density", help="Плотность DPI (получить/установить)")
    sc_density.add_argument("--set", type=int, help="Установить плотность dpi")
    sc_density.set_defaults(func=cmd_screen)

    sc_rotate = sc.add_parser("rotate", help="Ориентация экрана")
    g = sc_rotate.add_mutually_exclusive_group(required=True)
    g.add_argument("--landscape", action="store_true", help="Альбомная")
    g.add_argument("--portrait", action="store_true", help="Портретная")
    g.add_argument("--unlock", action="store_true", help="Вернуть авто-поворот")
    sc_rotate.set_defaults(func=cmd_screen)

    return p


# ---------------------------
# main
# ---------------------------

def main(argv: Optional[List[str]] = None) -> int:
    cfg = Config.load()
    parser = build_parser()
    args = parser.parse_args(argv)

    # Настраиваем логирование
    setup_logging(verbose=args.verbose, quiet=args.quiet)

    # Вычисляем итоговый таймаут
    timeout = args.timeout or cfg.default_timeout or DEFAULT_TIMEOUT

    # Создаём раннер
    adb = AdbRunner(adb_path=args.adb or cfg.adb_path, dry_run=args.dry_run, timeout=timeout)

    # Выполняем команду
    try:
        if args.command in {"screenshot", "record", "logcat"}:
            return args.func(adb, args, cfg)  # type: ignore
        else:
            return args.func(adb, args) if "adb" in args.func.__code__.co_varnames else args.func(args)  # type: ignore
    except KeyboardInterrupt:
        print("\nПрервано пользователем (Ctrl+C).", file=sys.stderr)
        return 1
    except SystemExit as e:
        # die() вызывает sys.exit — пробросим код
        return int(e.code) if isinstance(e.code, int) else 1
    except Exception as e:
        logging.exception("Необработанная ошибка")
        print(f"Необработанная ошибка: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())