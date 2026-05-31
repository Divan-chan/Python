#!/usr/bin/env python3
"""
sort_photos.py — Сортировка фото/видео с айфона по геолокации и году.

Логика:
  - Есть GPS + не Москва  → {Папка назначения}/{Город}/{Год}/файл
  - Есть GPS + Москва      → {Папка назначения}/{Год}/файл
  - Нет GPS / нет даты     → {Папка назначения}/_без_геолокации/файл

Поддерживаемые форматы: HEIC, JPG, JPEG, PNG, MOV

Установи зависимости перед запуском:
  pip install pillow piexif exifread pillow-heif geopy

Использование:
  python3 sort_photos.py --src /путь/к/папке/с/фото --dst /путь/к/папке/назначения

  # Пробный запуск (ничего не копирует, только показывает что будет):
  python3 sort_photos.py --src ./Фото --dst ./Сортировано --dry-run

  # Переместить вместо копирования:
  python3 sort_photos.py --src ./Фото --dst ./Сортировано --move
"""

import argparse
import shutil
import struct
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Зависимости ──────────────────────────────────────────────────────────────

try:
    import exifread
except ImportError:
    sys.exit("❌  Установи зависимости: pip install pillow piexif exifread pillow-heif geopy")

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except ImportError:
    HEIF_AVAILABLE = False
    print("⚠️  pillow-heif не найден — HEIC-файлы будут обрабатываться через exifread напрямую")

try:
    from geopy.geocoders import Nominatim
    from geopy.exc import GeocoderTimedOut, GeocoderServiceError
    GEOPY_AVAILABLE = True
except ImportError:
    sys.exit("❌  Установи geopy: pip install geopy")

# ── Настройки ────────────────────────────────────────────────────────────────

SUPPORTED_EXTENSIONS = {".heic", ".jpg", ".jpeg", ".png", ".mov"}

# Координаты центра Москвы и радиус (в градусах, ~50 км)
MOSCOW_LAT = 55.7558
MOSCOW_LON = 37.6173
MOSCOW_RADIUS_DEG = 0.45  # ~50 км

NOMINATIM_USER_AGENT = "photo_sorter_script/1.0"
GEOCODE_DELAY = 1.1  # секунды между запросами (лимит Nominatim: 1 req/sec)

# ── Геокодирование ────────────────────────────────────────────────────────────

geolocator = Nominatim(user_agent=NOMINATIM_USER_AGENT)
_geocode_cache: dict[tuple[float, float], str] = {}


def is_moscow(lat: float, lon: float) -> bool:
    """Проверяет, находится ли точка в пределах Московского региона."""
    return (
        abs(lat - MOSCOW_LAT) < MOSCOW_RADIUS_DEG
        and abs(lon - MOSCOW_LON) < MOSCOW_RADIUS_DEG
    )


def coords_to_city(lat: float, lon: float) -> str:
    """
    Возвращает название города/населённого пункта по координатам.
    Использует кэш и задержку для соблюдения лимитов Nominatim.
    """
    # Округляем до 2 знаков (~1 км) для кэша
    key = (round(lat, 2), round(lon, 2))
    if key in _geocode_cache:
        return _geocode_cache[key]

    time.sleep(GEOCODE_DELAY)
    try:
        location = geolocator.reverse(
            f"{lat}, {lon}",
            language="ru",
            timeout=10,
        )
        if location is None:
            city = "_неизвестное_место"
        else:
            addr = location.raw.get("address", {})
            # Приоритет: city > town > village > county > state
            city = (
                addr.get("city")
                or addr.get("town")
                or addr.get("village")
                or addr.get("county")
                or addr.get("state")
                or "_неизвестное_место"
            )
            # Убираем символы, небезопасные для имён папок
            city = "".join(c for c in city if c not in r'\/:*?"<>|').strip()
    except (GeocoderTimedOut, GeocoderServiceError) as e:
        print(f"    ⚠️  Геокодирование не удалось ({e}) — используется '_геолокация_недоступна'")
        city = "_геолокация_недоступна"

    _geocode_cache[key] = city
    return city


# ── Извлечение GPS из EXIF ────────────────────────────────────────────────────

def _dms_to_decimal(dms_values, ref: str) -> float | None:
    """Конвертирует градусы/минуты/секунды в десятичные градусы."""
    try:
        d = float(dms_values[0].num) / float(dms_values[0].den)
        m = float(dms_values[1].num) / float(dms_values[1].den)
        s = float(dms_values[2].num) / float(dms_values[2].den)
        decimal = d + m / 60 + s / 3600
        if ref in ("S", "W"):
            decimal = -decimal
        return decimal
    except (IndexError, AttributeError, ZeroDivisionError):
        return None


def extract_gps_exifread(path: Path) -> tuple[float, float] | None:
    """Извлекает GPS-координаты через exifread (работает с HEIC, JPG, JPEG, PNG)."""
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False, stop_tag="GPS GPSLongitude")

        lat_tag = tags.get("GPS GPSLatitude")
        lat_ref = tags.get("GPS GPSLatitudeRef")
        lon_tag = tags.get("GPS GPSLongitude")
        lon_ref = tags.get("GPS GPSLongitudeRef")

        if not (lat_tag and lat_ref and lon_tag and lon_ref):
            return None

        lat = _dms_to_decimal(lat_tag.values, str(lat_ref))
        lon = _dms_to_decimal(lon_tag.values, str(lon_ref))

        if lat is not None and lon is not None:
            return lat, lon
    except Exception:
        pass
    return None


def extract_gps_mov(path: Path) -> tuple[float, float] | None:
    """
    Извлекает GPS из MOV/MP4 через парсинг QuickTime atoms (udta/©xyz).
    Формат Apple: "+55.7558+037.6173/" (ISO 6709)
    """
    try:
        with open(path, "rb") as f:
            data = f.read()

        # Ищем atom ©xyz в бинарных данных
        marker = b"\xa9xyz"
        idx = data.find(marker)
        if idx == -1:
            return None

        # Структура: [2 bytes length][2 bytes lang][string]
        start = idx + 4
        str_len = struct.unpack(">H", data[start: start + 2])[0]
        # start+2 = lang code, start+4 = начало строки
        gps_str = data[start + 4: start + 4 + str_len - 4].decode("utf-8", errors="ignore")

        # Парсим ISO 6709: "+55.7558+037.6173/" или "+55.7558+037.6173+200/"
        import re
        match = re.match(r"([+-]\d+\.?\d*)([+-]\d+\.?\d*)", gps_str)
        if match:
            lat = float(match.group(1))
            lon = float(match.group(2))
            return lat, lon
    except Exception:
        pass
    return None


# ── Извлечение даты ───────────────────────────────────────────────────────────

def extract_date(path: Path) -> datetime | None:
    """Извлекает дату съёмки из EXIF или имени файла."""
    # 1. Через exifread (HEIC/JPG/PNG)
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False, stop_tag="EXIF DateTimeOriginal")
        for tag in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime"):
            if tag in tags:
                dt_str = str(tags[tag])
                return datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass

    # 2. Дата изменения файла как запасной вариант
    try:
        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime)
    except Exception:
        pass

    return None


# ── Основная логика ───────────────────────────────────────────────────────────

def determine_destination(path: Path, dst_root: Path) -> Path:
    """
    Определяет папку назначения для файла.
    Возвращает полный путь к папке (без имени файла).
    """
    ext = path.suffix.lower()

    # Получаем GPS
    gps = None
    if ext == ".mov":
        gps = extract_gps_mov(path)
    if gps is None:
        gps = extract_gps_exifread(path)

    # Получаем дату
    date = extract_date(path)
    year = str(date.year) if date else "_без_даты"

    if gps is None:
        return dst_root / "_без_геолокации"

    lat, lon = gps

    if is_moscow(lat, lon):
        return dst_root / year
    else:
        city = coords_to_city(lat, lon)
        return dst_root / city / year


def get_unique_path(dst_folder: Path, filename: str) -> Path:
    """Если файл уже существует, добавляет суффикс _1, _2, ..."""
    dst = dst_folder / filename
    if not dst.exists():
        return dst
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while True:
        new_name = f"{stem}_{counter}{suffix}"
        dst = dst_folder / new_name
        if not dst.exists():
            return dst
        counter += 1


def sort_folder(src: Path, dst: Path, move: bool = False, dry_run: bool = False):
    """Основная функция сортировки."""
    files = [
        f for f in src.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    ]

    if not files:
        print(f"⚠️  В папке {src} не найдено файлов с расширениями: {', '.join(SUPPORTED_EXTENSIONS)}")
        return

    total = len(files)
    print(f"\n📁 Найдено файлов: {total}")
    print(f"{'🔍 ПРОБНЫЙ ЗАПУСК — файлы не будут перемещены/скопированы' if dry_run else ''}\n")

    stats: dict[str, int] = {}
    action = "Перемещение" if move else "Копирование"

    for i, file in enumerate(sorted(files), 1):
        print(f"[{i:>{len(str(total))}}/{total}] {file.name}")

        dst_folder = determine_destination(file, dst)
        dst_path = get_unique_path(dst_folder, file.name)

        label = str(dst_path.relative_to(dst))
        print(f"         → {label}")

        folder_key = str(dst_folder.relative_to(dst))
        stats[folder_key] = stats.get(folder_key, 0) + 1

        if not dry_run:
            dst_folder.mkdir(parents=True, exist_ok=True)
            if move:
                shutil.move(str(file), dst_path)
            else:
                shutil.copy2(str(file), dst_path)

    print(f"\n{'=' * 50}")
    print(f"✅  {action} завершено!\n")
    print("📊  Итог по папкам:")
    for folder, count in sorted(stats.items()):
        print(f"   {folder:<40} {count} файл(ов)")
    print(f"\n   Итого: {total} файл(ов)")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Сортировка фото/видео с айфона по геолокации и году",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--src", required=True, help="Папка с исходными файлами")
    parser.add_argument("--dst", required=True, help="Папка назначения")
    parser.add_argument("--move", action="store_true", help="Переместить вместо копирования")
    parser.add_argument("--dry-run", action="store_true", help="Пробный запуск без изменений")
    args = parser.parse_args()

    src = Path(args.src).expanduser().resolve()
    dst = Path(args.dst).expanduser().resolve()

    if not src.exists() or not src.is_dir():
        sys.exit(f"❌  Папка источника не найдена: {src}")

    print(f"📂 Источник:    {src}")
    print(f"📂 Назначение:  {dst}")
    print(f"⚙️  Режим:       {'ПЕРЕМЕЩЕНИЕ' if args.move else 'КОПИРОВАНИЕ'}")

    sort_folder(src, dst, move=args.move, dry_run=args.dry_run)


if __name__ == "__main__":
    main()